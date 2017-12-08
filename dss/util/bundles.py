import json
import typing

import dss
from dss import Config, DSSException
from dss.hcablobstore import BundleFileMetadata, BundleMetadata
from dss.util import UrlBuilder
from cloud_blobstore import BlobNotFoundError

def get_bundle_from_bucket(
        uuid: str,
        replica: str,
        version: str,
        bucket: str,
        directurls: bool=False):

    uuid = uuid.lower()

    handle, hca_handle, default_bucket = Config.get_cloud_specific_handles(replica)

    # need the ability to use fixture bucket for testing
    bucket = default_bucket if bucket is None else bucket

    if version is None:
        # list the files and find the one that is the most recent.
        prefix = "bundles/{}.".format(uuid)
        for matching_file in handle.list(bucket, prefix):
            matching_file = matching_file[len(prefix):]
            if version is None or matching_file > version:
                version = matching_file

    if version is None:
        # no matches!
        raise DSSException(404, "not_found", "Cannot find file!")

    # retrieve the bundle metadata.
    try:
        bundle_metadata = json.loads(
            handle.get(
                bucket,
                "bundles/{}.{}".format(uuid, version)
            ).decode("utf-8"))
    except BlobNotFoundError:
        raise DSSException(404, "not_found", "Cannot find file!")

    filesresponse = []  # type: typing.List[dict]
    for file in bundle_metadata[BundleMetadata.FILES]:
        file_version = {
            'name': file[BundleFileMetadata.NAME],
            'content-type': file[BundleFileMetadata.CONTENT_TYPE],
            'size': file[BundleFileMetadata.SIZE],
            'uuid': file[BundleFileMetadata.UUID],
            'version': file[BundleFileMetadata.VERSION],
            'crc32c': file[BundleFileMetadata.CRC32C],
            's3_etag': file[BundleFileMetadata.S3_ETAG],
            'sha1': file[BundleFileMetadata.SHA1],
            'sha256': file[BundleFileMetadata.SHA256],
            'indexed': file[BundleFileMetadata.INDEXED],
        }
        if directurls:
            file_version['url'] = str(UrlBuilder().set(
                scheme=Config.get_storage_schema(replica),
                netloc=bucket,
                path=f"blobs/{file[BundleFileMetadata.SHA256]}.{file[BundleFileMetadata.SHA1]}.{file[BundleFileMetadata.S3_ETAG]}.{file[BundleFileMetadata.CRC32C]}"  # noqa
            ))
        filesresponse.append(file_version)

    return dict(
        bundle=dict(
            uuid=uuid,
            version=version,
            files=filesresponse,
            creator_uid=bundle_metadata[BundleMetadata.CREATOR_UID],
        )
    )

def get_bundle(
        uuid: str,
        replica: str,
        version: str=None,
        directurls: bool=False):
    return get_bundle_from_bucket(
        uuid,
        replica,
        version,
        None,
        directurls
    )