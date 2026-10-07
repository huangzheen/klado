"""A metadata classification cannot substitute for the delivered notice bytes."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('licence_materials', ROOT / 'scripts/verify_license_materials.py')
materials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(materials)
collector_spec = importlib.util.spec_from_file_location('licence_collector', ROOT / 'scripts/collect_runtime_licenses.py')
collector = importlib.util.module_from_spec(collector_spec)
collector_spec.loader.exec_module(collector)


class LicenceMaterialsTests(unittest.TestCase):
    def test_nested_notices_are_kept_but_python_modules_and_caches_are_excluded(self):
        self.assertTrue(collector.is_notice_file(Path('licenses/data/BUILD_LICENSES/abseil.txt')))
        self.assertTrue(collector.is_notice_file(Path('lib/utilsBundle.js.LICENSE')))
        for filename in ['licenses/__init__.py', 'licenses/_spdx.py',
                         'licenses/__pycache__/__init__.cpython-312.pyc']:
            self.assertFalse(collector.is_notice_file(Path(filename)))

    def test_distributed_sources_and_full_notice_texts_match_the_manifests(self):
        counts = materials.verify(ROOT)
        self.assertEqual(counts['sources'], 2)
        self.assertGreater(counts['frontend'], 100)
        self.assertGreater(counts['native'], 100)
        self.assertGreater(counts['runtime'], 90)

    def test_missing_or_modified_notice_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = {'file': 'LICENSE', 'sha256': hashlib.sha256(b'original').hexdigest()}
            with self.assertRaises(ValueError):
                materials.verify_entry(root, entry)
            (root / 'LICENSE').write_bytes(b'original')
            materials.verify_entry(root, entry)
            (root / 'LICENSE').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                materials.verify_entry(root, entry)

    def test_attribution_cannot_escape_the_release_tree(self):
        with self.assertRaises(ValueError):
            materials.verify_entry(ROOT, {'file': '../outside', 'sha256': ''})

    def test_lgpl_exception_description_is_correct_and_sources_are_identified(self):
        notice = (ROOT / 'NOTICE').read_text()
        self.assertIn('OpenSSL exception', notice)
        self.assertNotIn('libpq exception', notice)
        self.assertNotIn('removes the relinking obligation', notice)
        self.assertIn('third_party/sources/', notice)
