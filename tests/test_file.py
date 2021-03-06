#!/usr/bin/env python
# coding: utf-8

import datetime
import hashlib
import os
import requests
import sys
import tempfile
import typing
import unittest
import uuid


pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))  # noqa
sys.path.insert(0, pkg_root)  # noqa

import dss
from dss.config import BucketConfig, override_bucket_config, Replica
from dss.util import UrlBuilder
from dss.util.aws import AWS_MIN_CHUNK_SIZE
from dss.util.version import datetime_to_version_format
from tests.fixtures.cloud_uploader import GSUploader, S3Uploader, Uploader
from tests.infra import DSSAssertMixin, DSSUploadMixin, ExpectedErrorFields, get_env, generate_test_key, testmode
from tests.infra.server import ThreadedLocalServer
from dss.api.files import RETRY_AFTER_INTERVAL


# Max number of retries
FILE_GET_RETRY_COUNT = 10


class TestFileApi(unittest.TestCase, DSSAssertMixin, DSSUploadMixin):
    @classmethod
    def setUpClass(cls):
        cls.app = ThreadedLocalServer()
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()

    def setUp(self):
        dss.Config.set_config(dss.BucketConfig.TEST)
        self.s3_test_fixtures_bucket = get_env("DSS_S3_BUCKET_TEST_FIXTURES")
        self.gs_test_fixtures_bucket = get_env("DSS_GS_BUCKET_TEST_FIXTURES")
        self.s3_test_bucket = get_env("DSS_S3_BUCKET_TEST")
        self.gs_test_bucket = get_env("DSS_GS_BUCKET_TEST")

    @testmode.standalone
    def test_file_put(self):
        tempdir = tempfile.gettempdir()
        self._test_file_put(Replica.aws, "s3", self.s3_test_bucket, S3Uploader(tempdir, self.s3_test_bucket))
        self._test_file_put(Replica.gcp, "gs", self.gs_test_bucket, GSUploader(tempdir, self.gs_test_bucket))

    def _test_file_put(self, replica: Replica, scheme: str, test_bucket: str, uploader: Uploader):
        src_key = generate_test_key()
        src_data = os.urandom(1024)
        with tempfile.NamedTemporaryFile(delete=True) as fh:
            fh.write(src_data)
            fh.flush()

            uploader.checksum_and_upload_file(fh.name, src_key, "text/plain")

        source_url = f"{scheme}://{test_bucket}/{src_key}"

        file_uuid = str(uuid.uuid4())
        bundle_uuid = str(uuid.uuid4())
        version = datetime_to_version_format(datetime.datetime.utcnow())

        # should be able to do this twice (i.e., same payload, different UUIDs)
        self.upload_file(source_url, file_uuid, bundle_uuid=bundle_uuid, version=version)
        self.upload_file(source_url, str(uuid.uuid4()))

        # should be able to do this twice (i.e., same payload, same UUIDs)
        self.upload_file(source_url, file_uuid, bundle_uuid=bundle_uuid,
                         version=version, expected_code=requests.codes.ok)

        # should be able to do this twice (i.e., different payload, same UUIDs)
        self.upload_file(source_url, file_uuid, version=version, expected_code=requests.codes.ok)

    @testmode.integration
    def test_file_put_large(self):

        def upload_callable_creator(uploader_class: type) -> typing.Callable[[str, str], None]:
            def upload_callable(bucket: str, key: str) -> None:
                tempdir = tempfile.gettempdir()
                uploader = uploader_class(tempdir, bucket)

                src_data = os.urandom(AWS_MIN_CHUNK_SIZE + 1)
                with tempfile.NamedTemporaryFile(delete=True) as fh:
                    fh.write(src_data)
                    fh.flush()

                    uploader.checksum_and_upload_file(fh.name, key, "text/plain")
            return upload_callable

        self._test_file_put_large(Replica.aws, self.s3_test_bucket, upload_callable_creator(S3Uploader))
        self._test_file_put_large(Replica.gcp, self.gs_test_bucket, upload_callable_creator(GSUploader))

    def _test_file_put_large(self, replica: Replica, test_bucket: str, upload_func: typing.Callable[[str, str], None]):
        src_key = generate_test_key()
        upload_func(test_bucket, src_key)

        # We should be able to do this twice (i.e., same payload, different UUIDs).  First time should be asynchronous
        # since it's new data.  Second time should be synchronous since the data is present, but because S3 does not
        # make consistency guarantees, a second client might not see that the data is already there.  Therefore, we do
        # not mandate that it is done synchronously.
        for expect_async in [True, None]:
            resp_obj = self.upload_file_wait(
                f"{replica.storage_schema}://{test_bucket}/{src_key}",
                replica,
                expect_async=expect_async)
            self.assertHeaders(
                resp_obj.response,
                {
                    'content-type': "application/json",
                }
            )
            self.assertIn('version', resp_obj.json)

    # This is a test specific to AWS since it has separate notion of metadata and tags.
    @testmode.standalone
    def test_file_put_metadata_from_tags(self):
        resp_obj = self.upload_file_wait(
            f"s3://{self.s3_test_fixtures_bucket}/test_good_source_data/metadata_in_tags",
            Replica.aws,
        )
        self.assertHeaders(
            resp_obj.response,
            {
                'content-type': "application/json",
            }
        )
        self.assertIn('version', resp_obj.json)

    @testmode.standalone
    def test_file_put_upper_case_checksums(self):
        self._test_file_put_upper_case_checksums("s3", self.s3_test_fixtures_bucket)
        self._test_file_put_upper_case_checksums("gs", self.gs_test_fixtures_bucket)

    def _test_file_put_upper_case_checksums(self, scheme, fixtures_bucket):
        resp_obj = self.upload_file_wait(
            f"{scheme}://{fixtures_bucket}/test_good_source_data/incorrect_case_checksum",
            Replica.aws,
        )
        self.assertHeaders(
            resp_obj.response,
            {
                'content-type': "application/json",
            }
        )
        self.assertIn('version', resp_obj.json)

    @testmode.standalone
    def test_file_head(self):
        self._test_file_head(Replica.aws)
        self._test_file_head(Replica.gcp)

    def _test_file_head(self, replica: Replica):
        file_uuid = "ce55fd51-7833-469b-be0b-5da88ebebfcd"
        version = "2017-06-16T193604.240704Z"

        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid)
                  .add_query("replica", replica.name)
                  .add_query("version", version))

        with override_bucket_config(BucketConfig.TEST_FIXTURE):
            self.assertHeadResponse(
                url,
                [requests.codes.ok, requests.codes.moved]
            )

            # TODO: (ttung) verify headers

    @testmode.standalone
    def test_file_get_specific(self):
        self._test_file_get_specific(Replica.aws)
        self._test_file_get_specific(Replica.gcp)

    def _test_file_get_specific(self, replica: Replica):
        """
        Verify we can successfully fetch a specific file UUID+version.
        """
        file_uuid = "ce55fd51-7833-469b-be0b-5da88ebebfcd"
        version = "2017-06-16T193604.240704Z"

        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid)
                  .add_query("replica", replica.name)
                  .add_query("version", version))

        for i in range(FILE_GET_RETRY_COUNT):
            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                resp_obj = self.assertGetResponse(
                    url,
                    [requests.codes.found, requests.codes.moved]
                )
                if resp_obj.response.status_code == requests.codes.found:
                    url = resp_obj.response.headers['Location']
                    sha1 = resp_obj.response.headers['X-DSS-SHA1']
                    data = requests.get(url)
                    self.assertEqual(len(data.content), 11358)
                    self.assertEqual(resp_obj.response.headers['X-DSS-SIZE'], '11358')

                    # verify that the downloaded data matches the stated checksum
                    hasher = hashlib.sha1()
                    hasher.update(data.content)
                    self.assertEqual(hasher.hexdigest(), sha1)

                    # TODO: (ttung) verify more of the headers
                    return
                elif resp_obj.response.status_code == requests.codes.moved:
                    retryAfter = int(resp_obj.response.headers['Retry-After'])
                    self.assertEqual(retryAfter, RETRY_AFTER_INTERVAL)
                    self.assertIn(url, resp_obj.response.headers['Location'])
        self.fail(f"Failed after {FILE_GET_RETRY_COUNT} retries.")

    @testmode.standalone
    def test_file_get_latest(self):
        self._test_file_get_latest(Replica.aws)
        self._test_file_get_latest(Replica.gcp)

    def _test_file_get_latest(self, replica: Replica):
        """
        Verify we can successfully fetch the latest version of a file UUID.
        """
        file_uuid = "ce55fd51-7833-469b-be0b-5da88ebebfcd"

        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid)
                  .add_query("replica", replica.name))

        for i in range(FILE_GET_RETRY_COUNT):
            with override_bucket_config(BucketConfig.TEST_FIXTURE):
                resp_obj = self.assertGetResponse(
                    url,
                    [requests.codes.found, requests.codes.moved]
                )
                if resp_obj.response.status_code == requests.codes.found:
                    url = resp_obj.response.headers['Location']
                    sha1 = resp_obj.response.headers['X-DSS-SHA1']
                    data = requests.get(url)
                    self.assertEqual(len(data.content), 8685)
                    self.assertEqual(resp_obj.response.headers['X-DSS-SIZE'], '8685')

                    # verify that the downloaded data matches the stated checksum
                    hasher = hashlib.sha1()
                    hasher.update(data.content)
                    self.assertEqual(hasher.hexdigest(), sha1)

                    # TODO: (ttung) verify more of the headers
                    return
                elif resp_obj.response.status_code == requests.codes.moved:
                    retryAfter = int(resp_obj.response.headers['Retry-After'])
                    self.assertEqual(retryAfter, RETRY_AFTER_INTERVAL)
                    self.assertIn(url, resp_obj.response.headers['Location'])
        self.fail(f"Failed after {FILE_GET_RETRY_COUNT} retries.")

    @testmode.standalone
    def test_file_get_not_found(self):
        """
        Verify that we return the correct error message when the file cannot be found.
        """
        self._test_file_get_not_found(Replica.aws)
        self._test_file_get_not_found(Replica.gcp)

    def _test_file_get_not_found(self, replica: Replica):
        file_uuid = "ce55fd51-7833-469b-be0b-5da88ec0ffee"

        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid)
                  .add_query("replica", replica.name))

        with override_bucket_config(BucketConfig.TEST_FIXTURE):
            self.assertGetResponse(
                url,
                requests.codes.not_found,
                expected_error=ExpectedErrorFields(
                    code="not_found",
                    status=requests.codes.not_found,
                    expect_stacktrace=True)
            )

        version = "2017-06-16T193604.240704Z"
        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid)
                  .add_query("replica", replica.name)
                  .add_query("version", version))

        with override_bucket_config(BucketConfig.TEST_FIXTURE):
            self.assertGetResponse(
                url,
                requests.codes.not_found,
                expected_error=ExpectedErrorFields(
                    code="not_found",
                    status=requests.codes.not_found,
                    expect_stacktrace=True)
            )

    @testmode.standalone
    def test_file_get_no_replica(self):
        """
        Verify we raise the correct error code when we provide no replica.
        """
        file_uuid = "ce55fd51-7833-469b-be0b-5da88ec0ffee"

        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid))

        with override_bucket_config(BucketConfig.TEST_FIXTURE):
            self.assertGetResponse(
                url,
                requests.codes.bad_request,
                expected_error=ExpectedErrorFields(
                    code="illegal_arguments",
                    status=requests.codes.bad_request,
                    expect_stacktrace=True)
            )

    @testmode.standalone
    def test_file_size(self):
        """
        Verify size is correct after dss put and get
        """
        tempdir = tempfile.gettempdir()
        self._test_file_size(Replica.aws, "s3", self.s3_test_bucket, S3Uploader(tempdir, self.s3_test_bucket))
        self._test_file_size(Replica.gcp, "gs", self.gs_test_bucket, GSUploader(tempdir, self.gs_test_bucket))

    def _test_file_size(self, replica: Replica, scheme: str, test_bucket: str, uploader: Uploader):
        src_key = generate_test_key()
        src_size = 1024 + int.from_bytes(os.urandom(1), byteorder='little')
        src_data = os.urandom(src_size)
        with tempfile.NamedTemporaryFile(delete=True) as fh:
            fh.write(src_data)
            fh.flush()

            uploader.checksum_and_upload_file(fh.name, src_key, "text/plain")

        source_url = f"{scheme}://{test_bucket}/{src_key}"

        file_uuid = str(uuid.uuid4())
        bundle_uuid = str(uuid.uuid4())
        version = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H%M%S.%fZ")

        self.upload_file(source_url, file_uuid, bundle_uuid=bundle_uuid, version=version)

        url = str(UrlBuilder()
                  .set(path="/v1/files/" + file_uuid)
                  .add_query("replica", replica.name))

        for i in range(FILE_GET_RETRY_COUNT):
            with override_bucket_config(BucketConfig.TEST):
                resp_obj = self.assertGetResponse(
                    url,
                    [requests.codes.found, requests.codes.moved]
                )
                if resp_obj.response.status_code == requests.codes.found:
                    url = resp_obj.response.headers['Location']
                    data = requests.get(url)
                    self.assertEqual(len(data.content), src_size)
                    self.assertEqual(resp_obj.response.headers['X-DSS-SIZE'], str(src_size))
                    return
                elif resp_obj.response.status_code == requests.codes.moved:
                    retryAfter = int(resp_obj.response.headers['Retry-After'])
                    self.assertEqual(retryAfter, RETRY_AFTER_INTERVAL)
                    self.assertIn(url, resp_obj.response.headers['Location'])
        self.fail(f"Failed after {FILE_GET_RETRY_COUNT} retries.")

    def upload_file(
            self: typing.Any,
            source_url: str,
            file_uuid: str,
            bundle_uuid: str=None,
            version: str=None,
            expected_code: int=requests.codes.created,
    ):
        bundle_uuid = str(uuid.uuid4()) if bundle_uuid is None else bundle_uuid
        if version is None:
            timestamp = datetime.datetime.utcnow()
            version = timestamp.strftime("%Y-%m-%dT%H%M%S.%fZ")

        urlbuilder = UrlBuilder().set(path='/v1/files/' + file_uuid)
        urlbuilder.add_query("version", version)

        resp_obj = self.assertPutResponse(
            str(urlbuilder),
            expected_code,
            json_request_body=dict(
                bundle_uuid=bundle_uuid,
                creator_uid=0,
                source_url=source_url,
            ),
        )

        if resp_obj.response.status_code == requests.codes.created:
            self.assertHeaders(
                resp_obj.response,
                {
                    'content-type': "application/json",
                }
            )
            self.assertIn('version', resp_obj.json)


if __name__ == '__main__':
    unittest.main()
