"""Conditional dependencies and unknown/missing licences must never be hidden."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import license_audit as audit


class Metadata(dict):
    def get_all(self, key):
        return []


class Distribution:
    def __init__(self, name, dependencies=(), licence="MIT"):
        self.metadata = Metadata(Name=name, **{"License-Expression": licence})
        self.name = name
        self.requires = dependencies
        self.version = "1.0"


class LicenseAuditTests(unittest.TestCase):
    def test_spdx_gpl_suffix_is_blocked_but_lgpl_is_distinct(self):
        for value in ['GPL-3.0', 'GPL-3.0-only', 'AGPL-3.0-or-later']:
            self.assertEqual(audit.classify(value), 'blocking')
        self.assertEqual(audit.classify('LGPL-3.0-or-later'), 'weak-copyleft')

    def tree(self, requirements, distributions, *, runtime=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "api").mkdir()
            (root / "api/requirements.txt").write_text(requirements)
            with patch.object(audit.md, "distributions", return_value=distributions):
                return (audit.runtime_tree if runtime else audit.declared_tree)(root)

    def test_active_python_marker_is_included_and_inactive_marker_is_not(self):
        app = Distribution("app", ['active; python_version >= "3"', 'absent; python_version < "2"'])
        self.assertEqual(set(self.tree("app", [app, Distribution("active")])), {"app", "active"})

    def test_extras_propagate_and_late_extra_activation_revisits_a_package(self):
        items = [Distribution("app", ['child[feature]', 'child', 'optional; extra == "disabled"']),
                 Distribution("child", ['enabled; extra == "feature"']), Distribution("enabled")]
        self.assertEqual(set(self.tree("app", items)), {"app", "child", "enabled"})

    def test_platform_markers_use_the_actual_environment(self):
        platform = sys.platform
        items = [Distribution("app", [f'active; sys_platform == "{platform}"']), Distribution("active")]
        self.assertIn("active", self.tree("app", items))

    def test_missing_active_root_or_transitive_dependency_is_an_error(self):
        for requirements, items in [("missing", []), ("app", [Distribution("app", ["missing"])])]:
            with self.subTest(requirements=requirements), self.assertRaises(audit.LicenseAuditError):
                self.tree(requirements, items)

    def test_unsatisfied_installed_version_fails(self):
        with self.assertRaises(audit.LicenseAuditError):
            self.tree("app>=2", [Distribution("app")])

    def test_complete_runtime_includes_packages_outside_the_declared_graph(self):
        items = [Distribution("app"), Distribution("tool", licence="GPL-3.0")]
        self.assertEqual(audit.classify(self.tree("app", items, runtime=True)["tool"]), "blocking")

    def test_standalone_cli_fails_on_unknown_and_does_not_write(self):
        path = Path(__file__).resolve().parents[2] / "scripts/audit_licenses.py"
        spec = importlib.util.spec_from_file_location("audit_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, "runtime_tree", return_value={"unreviewed": ""}), \
                patch.object(sys, "argv", ["audit_licenses.py", "--write"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("builtins.open") as opening:
            self.assertEqual(cli.main(), 1)
            opening.assert_not_called()

    def test_standalone_cli_fails_on_missing_packages(self):
        path = Path(__file__).resolve().parents[2] / "scripts/audit_licenses.py"
        spec = importlib.util.spec_from_file_location("audit_missing_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, "runtime_tree", side_effect=audit.LicenseAuditError("missing")), \
                patch.object(sys, "argv", ["audit_licenses.py"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(), 1)
