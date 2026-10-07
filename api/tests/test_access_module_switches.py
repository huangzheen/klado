"""`core.access.requires_admin` — which writes are operator-only.

This is the coarse policy layered *under* the module gate: it answers "may only an
operator do this at all", not "which module owns this". It is a list of path
patterns, which is exactly the kind of rule that rots quietly — a prefix that grows
one segment too far turns an operator-only write into a self-service one, and nothing
fails until somebody uses it.

The module switches are the case that matters today. `PUT /api/settings/modules/{key}`
is an account narrowing its OWN set, so it must not be operator-only; the admin
matrix at `/api/settings/modules/accounts/…` writes *somebody else's* row and must
be. A single `startswith` gets one of those two wrong, and the wrong one is a
privilege bug rather than a 403 that annoys somebody.
"""
import unittest

from core import access


class SettingsGroupTests(unittest.TestCase):
    def test_reads_are_never_admin_only(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertFalse(access.requires_admin("/api/settings/modules", method))
            self.assertFalse(access.requires_admin("/api/settings/modules/accounts", method))

    def test_the_settings_group_is_operator_only_by_default(self):
        for path in ("/api/settings/users", "/api/settings/llm", "/api/settings/modulesx",
                     "/api/settings/modules/extra/segments/here"):
            self.assertTrue(access.requires_admin(path, "PUT"), path)

    def test_an_account_may_narrow_its_own_modules(self):
        self.assertFalse(access.requires_admin("/api/settings/modules/dashboard", "PUT"))
        self.assertFalse(access.requires_admin("/api/settings/modules/dashboard", "DELETE"))
        self.assertFalse(access.requires_admin("/api/settings/modules", "DELETE"))

    def test_an_operator_writing_another_account_stays_operator_only(self):
        # The near-miss this whole rule exists for. A looser pattern would make the
        # operator's matrix self-service, i.e. any account could rewrite any other
        # account's entitlements.
        for path in ("/api/settings/modules/accounts",
                     "/api/settings/modules/accounts/7",
                     "/api/settings/modules/accounts/7/dashboard",
                     "/api/settings/modules/accounts/7/dashboard/extra"):
            self.assertTrue(access.requires_admin(path, "PUT"), path)
            self.assertTrue(access.requires_admin(path, "DELETE"), path)

    def test_app_settings_stay_split_by_key(self):
        # Pre-existing rule, asserted here because the module endpoints sit in the
        # same prefix and it would be easy to break while editing the pattern above.
        self.assertTrue(access.requires_admin("/api/settings/app/build_version", "PUT"))
        self.assertFalse(access.requires_admin("/api/settings/app/reports_settings", "PUT"))


class DestructiveDataCenterTests(unittest.TestCase):
    def test_content_writes_defer_to_router_ownership(self):
        for path in ("/api/data-center/datasets/foo",
                     "/api/data-center/datasets/foo/period",
                     "/api/data-center/files/by-path",
                     "/api/data-center/files/12"):
            self.assertFalse(access.requires_admin(path, "DELETE"), path)
            self.assertFalse(access.requires_admin(path, "GET"), path)

    def test_grant_management_is_not_admin_only(self):
        # Sharing a dataset is the owner's own call, and it is checked by ownership
        # inside the handler — not by blanket operator-only.
        self.assertFalse(
            access.requires_admin("/api/data-center/datasets/foo/shares", "POST"))
        self.assertFalse(
            access.requires_admin("/api/data-center/datasets/foo/shares/a@b.co", "DELETE"))


if __name__ == "__main__":
    unittest.main()
