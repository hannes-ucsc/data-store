import json
import os
import sys
import unittest

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))  # noqa
sys.path.insert(0, pkg_root)  # noqa

from dss import Config, BucketConfig
from tests.json_gen.hca_generator import HCAJsonGenerator
from tests.infra import testmode

schema_urls = [
    "https://raw.githubusercontent.com/HumanCellAtlas/metadata-schema/4.6.0/json_schema/analysis_bundle.json",
    "https://raw.githubusercontent.com/HumanCellAtlas/metadata-schema/4.6.0/json_schema/assay_bundle.json",
    "https://raw.githubusercontent.com/HumanCellAtlas/metadata-schema/4.6.0/json_schema/project_bundle.json",
    "https://raw.githubusercontent.com/HumanCellAtlas/metadata-schema/4.6.0/json_schema/sample_bundle.json",
]


@testmode.standalone
class TestHCAGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Config.set_config(BucketConfig.TEST)

    def setUp(self):
        self.faker = HCAJsonGenerator(schema_urls)

    def test_locals(self):
        for url in schema_urls:
            name = url.split('/')[-1]
            self.assertEqual(self.faker.schemas[name], {'$ref': url, 'id': url})

    @unittest.skip("Test is inconsistant")  # TODO (tsmith) remove once tests run consistently
    def test_generation(self):
        for name in self.faker.schemas.keys():
            with self.subTest(name):
                fake_json = self.faker.generate(name)
                fake_json = json.loads(fake_json)
                self.assertIsInstance(fake_json, dict)
                self.assertEqual(fake_json[name]['describedBy'], self.faker.schemas[name]['id'])


if __name__ == "__main__":
    unittest.main()
