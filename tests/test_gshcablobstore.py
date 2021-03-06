#!/usr/bin/env python
# coding: utf-8

import os
import sys
import unittest

from cloud_blobstore.gs import GSBlobStore

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))  # noqa
sys.path.insert(0, pkg_root)  # noqa

from dss.storage.hcablobstore.gs import GSHCABlobStore
from tests import infra
from tests.hcablobstore_base import HCABlobStoreTests


class TestGSHCABlobStore(unittest.TestCase, HCABlobStoreTests):
    def setUp(self):
        self.credentials = infra.get_env("GOOGLE_APPLICATION_CREDENTIALS")
        self.test_bucket = infra.get_env("DSS_GS_BUCKET_TEST")
        self.test_fixtures_bucket = infra.get_env("DSS_GS_BUCKET_TEST_FIXTURES")
        self.blobhandle = GSBlobStore.from_auth_credentials(self.credentials)
        self.hcahandle = GSHCABlobStore(self.blobhandle)

    def tearDown(self):
        pass


if __name__ == '__main__':
    unittest.main()
