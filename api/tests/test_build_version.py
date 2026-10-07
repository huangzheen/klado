import unittest

from routers.settings import _version_from_image_ref


class BuildVersionTests(unittest.TestCase):
    def test_extracts_image_tag(self):
        self.assertEqual(
            _version_from_image_ref("registry.example.com/team/klado:485a30e"),
            "485a30e",
        )

    def test_extracts_tag_when_registry_has_a_port(self):
        self.assertEqual(
            _version_from_image_ref("registry.example.com:5000/team/klado:test-42"),
            "test-42",
        )

    def test_extracts_short_digest(self):
        self.assertEqual(
            _version_from_image_ref("registry.example.com/klado@sha256:abcdef0123456789"),
            "sha256:abcdef012345",
        )

    def test_ignores_unsubstituted_template(self):
        self.assertEqual(_version_from_image_ref("${image-backend}"), "")


if __name__ == "__main__":
    unittest.main()
