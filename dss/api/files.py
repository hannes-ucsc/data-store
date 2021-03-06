import datetime
import io
import json
import os
import random
import re
import sys
import typing
from enum import Enum, auto
from uuid import uuid4

import iso8601
import requests
from cloud_blobstore import BlobAlreadyExistsError, BlobNotFoundError, BlobStore
from flask import jsonify, make_response, redirect, request
from dss.util.version import datetime_to_version_format

from dss import DSSException, dss_handler, stepfunctions
from dss.config import Config, Replica
from dss.storage.hcablobstore import FileMetadata, HCABlobStore
from dss.stepfunctions import gscopyclient, s3copyclient
from dss.util import tracing
from dss.util.aws import AWS_MIN_CHUNK_SIZE

ASYNC_COPY_THRESHOLD = AWS_MIN_CHUNK_SIZE
"""This is the maximum file size that we will copy synchronously."""

"""The retry-after interval in seconds. Sets up downstream libraries / users to
retry request after the specified interval."""
RETRY_AFTER_INTERVAL = 10

"""Probability of the 301 redirect with Retry-After header. This is a temporary measure, sets up downstream
libraries / users for success when we start integrating this with the checkout service. """
REDIRECT_PROBABILITY_PERCENTS = 5


@dss_handler
def head(uuid: str, replica: str, version: str=None):
    return get_helper(uuid, Replica[replica], version)


@dss_handler
def get(uuid: str, replica: str, version: str=None):
    return get_helper(uuid, Replica[replica], version)


def get_helper(uuid: str, replica: Replica, version: str=None):
    with tracing.Subsegment('parameterization'):
        handle = Config.get_blobstore_handle(replica)
        bucket = replica.bucket

    if version is None:
        with tracing.Subsegment('find_latest_version'):
            # list the files and find the one that is the most recent.
            prefix = "files/{}.".format(uuid)
            for matching_file in handle.list(bucket, prefix):
                matching_file = matching_file[len(prefix):]
                if version is None or matching_file > version:
                    version = matching_file

    if version is None:
        # no matches!
        raise DSSException(404, "not_found", "Cannot find file!")

    # retrieve the file metadata.
    try:
        with tracing.Subsegment('load_file'):
            file_metadata = json.loads(
                handle.get(
                    bucket,
                    "files/{}.{}".format(uuid, version)
                ).decode("utf-8"))
    except BlobNotFoundError as ex:
        raise DSSException(404, "not_found", "Cannot find file!")

    with tracing.Subsegment('make_path'):
        blob_path = "blobs/" + ".".join((
            file_metadata[FileMetadata.SHA256],
            file_metadata[FileMetadata.SHA1],
            file_metadata[FileMetadata.S3_ETAG],
            file_metadata[FileMetadata.CRC32C],
        ))

    if request.method == "GET":
        """
        Probabilistically return "Retry-After" header
        The retry-after interval can be relatively short now, but it sets up downstream
        libraries / users for success when we start integrating this with the checkout service.
        """
        if random.randint(0, 100) < REDIRECT_PROBABILITY_PERCENTS:
            with tracing.Subsegment('make_retry'):
                response = redirect(request.url, code=301)
                headers = response.headers
                headers['Retry-After'] = RETRY_AFTER_INTERVAL
                return response

        response = redirect(handle.generate_presigned_GET_url(
            bucket,
            blob_path))
    else:
        response = make_response('', 200)

    with tracing.Subsegment('set_headers'):
        headers = response.headers
        headers['X-DSS-CREATOR-UID'] = file_metadata[FileMetadata.CREATOR_UID]
        headers['X-DSS-VERSION'] = version
        headers['X-DSS-CONTENT-TYPE'] = file_metadata[FileMetadata.CONTENT_TYPE]
        headers['X-DSS-SIZE'] = file_metadata[FileMetadata.SIZE]
        headers['X-DSS-CRC32C'] = file_metadata[FileMetadata.CRC32C]
        headers['X-DSS-S3-ETAG'] = file_metadata[FileMetadata.S3_ETAG]
        headers['X-DSS-SHA1'] = file_metadata[FileMetadata.SHA1]
        headers['X-DSS-SHA256'] = file_metadata[FileMetadata.SHA256]

    return response


@dss_handler
def put(uuid: str, json_request_body: dict, version: str=None):
    class CopyMode(Enum):
        NO_COPY = auto()
        COPY_INLINE = auto()
        COPY_ASYNC = auto()

    uuid = uuid.lower()
    if version is not None:
        # convert it to date-time so we can format exactly as the system requires (with microsecond precision)
        timestamp = iso8601.parse_date(version)
    else:
        timestamp = datetime.datetime.utcnow()
    version = datetime_to_version_format(timestamp)

    source_url = json_request_body['source_url']
    cre = re.compile(
        "^"
        "(?P<schema>(?:s3|gs|wasb))"
        "://"
        "(?P<bucket>[^/]+)"
        "/"
        "(?P<key>.+)"
        "$")
    mobj = cre.match(source_url)
    if mobj and mobj.group('schema') == "s3":
        replica = Replica.aws
    elif mobj and mobj.group('schema') == "gs":
        replica = Replica.gcp
    else:
        schema = mobj.group('schema')
        raise DSSException(
            requests.codes.bad_request,
            "unknown_source_schema",
            f"source_url schema {schema} not supported")

    handle = Config.get_blobstore_handle(replica)
    hca_handle = Config.get_hcablobstore_handle(replica)
    dst_bucket = replica.bucket

    src_bucket = mobj.group('bucket')
    src_key = mobj.group('key')

    metadata = handle.get_user_metadata(src_bucket, src_key)
    size = handle.get_size(src_bucket, src_key)
    content_type = handle.get_content_type(src_bucket, src_key)

    # format all the checksums so they're lower-case.
    for metadata_spec in HCABlobStore.MANDATORY_METADATA.values():
        if metadata_spec['downcase']:
            keyname = typing.cast(str, metadata_spec['keyname'])
            metadata[keyname] = metadata[keyname].lower()

    # what's the target object name for the actual data?
    dst_key = ("blobs/" + ".".join(
        (
            metadata['hca-dss-sha256'],
            metadata['hca-dss-sha1'],
            metadata['hca-dss-s3_etag'],
            metadata['hca-dss-crc32c'],
        )
    )).lower()

    # does it exist? if so, we can skip the copy part.
    copy_mode = CopyMode.COPY_INLINE
    try:
        if hca_handle.verify_blob_checksum(dst_bucket, dst_key, metadata):
            copy_mode = CopyMode.NO_COPY
    except BlobNotFoundError:
        pass

    # build the json document for the file metadata.
    file_metadata = {
        FileMetadata.FORMAT: FileMetadata.FILE_FORMAT_VERSION,
        FileMetadata.CREATOR_UID: json_request_body['creator_uid'],
        FileMetadata.VERSION: version,
        FileMetadata.CONTENT_TYPE: content_type,
        FileMetadata.SIZE: size,
        FileMetadata.CRC32C: metadata['hca-dss-crc32c'],
        FileMetadata.S3_ETAG: metadata['hca-dss-s3_etag'],
        FileMetadata.SHA1: metadata['hca-dss-sha1'],
        FileMetadata.SHA256: metadata['hca-dss-sha256'],
    }
    file_metadata_json = json.dumps(file_metadata)

    if copy_mode != CopyMode.NO_COPY and size > ASYNC_COPY_THRESHOLD:
            copy_mode = CopyMode.COPY_ASYNC

    if copy_mode == CopyMode.COPY_ASYNC:
        if replica == Replica.aws:
            state = s3copyclient.copy_write_metadata_sfn_event(
                src_bucket, src_key,
                dst_bucket, dst_key,
                uuid, version,
                file_metadata_json,
            )
            state_machine_name_template = "dss-s3-copy-write-metadata-sfn-{stage}"
        elif replica == Replica.gcp:
            state = gscopyclient.copy_write_metadata_sfn_event(
                src_bucket, src_key,
                dst_bucket, dst_key,
                uuid, version,
                file_metadata_json,
            )
            state_machine_name_template = "dss-gs-copy-write-metadata-sfn-{stage}"
        else:
            raise ValueError("Unhandled replica")

        execution_id = str(uuid4())
        stepfunctions.step_functions_invoke(state_machine_name_template, execution_id, state)
        return jsonify(dict(task_id=execution_id, version=version)), requests.codes.accepted
    elif copy_mode == CopyMode.COPY_INLINE:
        handle.copy(src_bucket, src_key, dst_bucket, dst_key)

        # verify the copy was done correctly.
        assert hca_handle.verify_blob_checksum(dst_bucket, dst_key, metadata)

    try:
        write_file_metadata(handle, dst_bucket, uuid, version, file_metadata_json)
        status_code = requests.codes.created
    except BlobAlreadyExistsError:
        # fetch the file metadata, compare it to what we have.
        existing_file_metadata = json.loads(
            handle.get(
                dst_bucket,
                "files/{}.{}".format(uuid, version)
            ).decode("utf-8"))
        if existing_file_metadata != file_metadata:
            raise DSSException(
                requests.codes.conflict,
                "file_already_exists",
                f"file with UUID {uuid} and version {version} already exists")
        status_code = requests.codes.ok

    return jsonify(
        dict(version=version)), status_code


def write_file_metadata(
        handle: BlobStore,
        dst_bucket: str,
        file_uuid: str,
        file_version: str,
        document: str):
    # what's the target object name for the file metadata?
    metadata_key = f"files/{file_uuid}.{file_version}"

    # if it already exists, then it's a failure.
    try:
        handle.get_user_metadata(dst_bucket, metadata_key)
    except BlobNotFoundError:
        pass
    else:
        raise BlobAlreadyExistsError()

    handle.upload_file_handle(
        dst_bucket,
        metadata_key,
        io.BytesIO(document.encode("utf-8")))
