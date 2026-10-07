"""Privacy and report-type contracts, independent of the production database."""
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import Response

from klado_shared import orgs
from routers import reports


class ReportWorkspaceTests(unittest.TestCase):
    def test_legacy_reports_move_to_the_configured_admin(self):
        # The repository names no administrator. With AUTH_ADMIN_EMAILS unset an
        # unowned row keeps its NULL owner rather than being adopted by a stranger,
        # and a request with no session gets the ordinary 401 instead of an identity.
        #
        # ⚠️ Patch the setting rather than asserting the ambient value: a developer
        # machine has a real `.env` (which this suite now loads), so "the default is
        # empty" is a statement about configuration, not about this function.
        with patch.object(reports.settings, "AUTH_ADMIN_EMAILS", ""):
            self.assertEqual(reports._legacy_owner(), "")
        with patch.object(reports.settings, "AUTH_ADMIN_EMAILS", "First@Local, second@local"):
            self.assertEqual(reports._legacy_owner(), "first@local")

    def test_identity_without_a_session_or_an_admin_is_unauthenticated(self):
        with patch.object(reports.settings, "AUTH_ENABLED", False), \
             patch.object(reports.settings, "AUTH_ADMIN_EMAILS", ""):
            request = SimpleNamespace(state=SimpleNamespace(current_user=None))
            with self.assertRaises(HTTPException) as exc:
                reports._identity(request)
            self.assertEqual(exc.exception.status_code, 401)

    def test_public_is_readable_but_private_is_owner_only(self):
        private = {"owner_email": "a@example.com", "visibility": "private", "status": "published"}
        public = {**private, "visibility": "public"}
        self.assertTrue(reports._may_read(private, "a@example.com"))
        self.assertFalse(reports._may_read(private, "b@example.com"))
        self.assertTrue(reports._may_read(public, "b@example.com"))
        self.assertFalse(reports._may_read({**public, "status": "draft"}, "b@example.com"))
        with self.assertRaises(HTTPException) as exc:
            reports._require_owner(public, "b@example.com")
        self.assertEqual(exc.exception.status_code, 404)

    def test_report_kind_marker_and_edit_controls(self):
        static = '<div class="deck"><section class="slide"><h1>Summary</h1></section></div>'
        interactive = '<meta name="report-kind" content="interactive"><div>Summary</div>'
        self.assertEqual(reports._report_kind(static), "static")
        self.assertEqual(reports._report_kind(interactive), "interactive")
        self.assertEqual(reports._report_kind('<h1 contenteditable>Summary</h1>'), "interactive")
        self.assertEqual(reports._report_kind('<h1 contenteditable="false">Summary</h1>'), "static")
        self.assertEqual(reports._report_kind(static, "interactive"), "interactive")

    def test_a_pulled_legacy_report_binds_state_to_the_new_slug(self):
        html = '<html><head></head><body><script>fetch("api/reports/original/state")</script></body></html>'
        copy = reports._inject_report_context(html, "pulled-copy", False)
        self.assertLess(copy.index("data-report-workspace"), copy.index('fetch("api/reports/original/state")'))
        self.assertIn('"pulled-copy"', copy)
        self.assertIn('window.fetch =', copy)

    def test_named_colleague_can_read_only_published_private_report(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, params):
                self.assertion = (sql, params)
            def fetchone(self):
                return (1,) if self.assertion[1] == (19, "reader@example.com") else None

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def cursor(self, **_): return Cursor()

        with patch.object(reports, "_db", Connection):
            row = {"id": 19, "owner_email": "owner@example.com",
                   "visibility": "private", "status": "published"}
            self.assertTrue(reports._may_read(row, "reader@example.com"))
            self.assertFalse(reports._may_read(row, "stranger@example.com"))
            self.assertFalse(reports._may_read({**row, "status": "draft"}, "reader@example.com"))
            self.assertFalse(reports._may_read({**row, "visibility": "public",
                                                "status": "draft"}, "reader@example.com"))

    def test_anonymous_link_signature_revocation_and_publish_state(self):
        link_id = "a" * 24
        token = reports._anyone_token(link_id)
        self.assertNotIn(link_id, reports._anyone_token("b" * 24))
        row = {"id": 19, "slug": "report", "html": "<h1>Report</h1>",
               "state_json": "{}", "kind": "static", "status": "published"}

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *_): pass
            def fetchone(self): return available[0]

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def cursor(self, **_): return Cursor()

        available = [row]
        with patch.object(reports, "_ensure_table"), patch.object(reports, "_db", Connection):
            self.assertEqual(reports._resolve_anyone_link(token)["slug"], "report")
            for invalid in ("wrong", token[:-1] + ("A" if token[-1] != "A" else "B")):
                with self.assertRaises(HTTPException) as exc:
                    reports._resolve_anyone_link(invalid)
                self.assertEqual(exc.exception.status_code, 404)
            available[0] = {**row, "status": "draft"}
            with self.assertRaises(HTTPException):
                reports._resolve_anyone_link(token)
            available[0] = None  # The owner revoked the link.
            with self.assertRaises(HTTPException):
                reports._resolve_anyone_link(token)

    def test_colleague_share_rejects_malformed_addresses(self):
        """⚠️ This used to be `test_colleague_share_follows_the_configured_domain`, and it
        tested `reports.is_allowed_recipient` — a five-line function that asked "does
        this end in AUTH_ALLOWED_EMAIL_DOMAIN", duplicated character-for-character in
        `services/dashboard_store.py`, and guarded two of the nine share paths.

        The suffix check is gone. What remains at the router is format validation, and
        the organization question now lives in one place
        (`klado_shared.orgs.may_share_to_many`) with its own tests in `test_orgs.py`.
        """
        with patch.object(orgs, "effective_profile", return_value=orgs.UNRESTRICTED):
            for bad in ("not-an-email", "", "   ", "reader@", "@example.com",
                        "reader @example.com", "a@b", None, 42):
                allowed, reason, _offenders = orgs.may_share_to_many(
                    "owner@example.com", [bad])
                self.assertFalse(allowed, repr(bad))
                self.assertEqual(reason, orgs.BAD_RECIPIENT, repr(bad))

    def test_a_spoofed_domain_is_no_longer_a_suffix_question(self):
        """⚠️ The old check rejected `reader@example.com.evil.test` — not because it is
        malformed (it is a perfectly good address, `evil.test` being a real TLD) but
        because `endswith("@example.com")` did not match.

        That defence is now provided by something stronger: an enterprise owner's
        recipients must be on the organization roster by *exact address*. An address
        that merely resembles a colleague's is not on the list, under any scope. So the
        address passes format validation, and is refused on organization grounds —
        which is the assertion that matters. It is stated explicitly because the
        behaviour moved rather than disappeared, and "it used to be rejected" is exactly
        the kind of note that gets lost and then re-litigated.
        """
        with patch.object(orgs, "effective_profile", return_value=orgs.UNRESTRICTED):
            allowed, reason, offenders = orgs.may_share_to_many(
                "owner@corp.example", ["reader@example.com.evil.test"])
        self.assertTrue(allowed, "no organization means no restriction, as before")

        # Now the same address, from a member of an org that does not list them.
        with patch.object(orgs, "effective_profile",
                          return_value=("internal", "approve", 42)), \
             patch.object(orgs, "_org_addresses",
                          return_value={"owner@corp.example", "real@corp.example"}):
            allowed, reason, offenders = orgs.may_share_to_many(
                "owner@corp.example", ["reader@example.com.evil.test"])
        self.assertFalse(allowed)
        self.assertEqual(reason, orgs.NOT_SAME_ORG)
        self.assertEqual(offenders, ["reader@example.com.evil.test"])

    def test_anonymous_asset_proxy_is_limited_to_document_images(self):
        row = {"html": '<img src="api/storage/serve?path=product-images/Fridge.jpg">',
               "state_json": '{"icon":"competitor-images/Brand/Cold.webp"}'}
        self.assertEqual(reports._guest_image_keys(row),
                         {"product-images/Fridge.jpg", "competitor-images/Brand/Cold.webp"})
        rewritten = reports._rewrite_guest_images(row["html"], "signed.token")
        self.assertIn("/s/signed.token/asset?path=product-images/Fridge.jpg", rewritten)

        from routers import storage
        with patch.object(reports, "_resolve_anyone_link", return_value=row), \
             patch.object(storage, "serve_file", new_callable=AsyncMock,
                          return_value=Response(b"image", media_type="image/jpeg")) as serve:
            response = asyncio.run(reports.anyone_link_asset(
                "signed.token", "product-images/Fridge.jpg"))
            self.assertEqual(response.body, b"image")
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            serve.assert_awaited_once()
            with self.assertRaises(HTTPException) as exc:
                asyncio.run(reports.anyone_link_asset("signed.token", "datacenter-raw/secret.xlsx"))
            self.assertEqual(exc.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()


class WorkspaceScopeTests(unittest.TestCase):
    """The three areas must stay disjoint — this is a permission boundary.

    A colleague's report used to be folded into `mine`, which put documents the
    viewer does not own on a wall whose actions are rendered from ownership. The
    predicates are data (`_SCOPE_SQL`) precisely so this stays assertable without a
    database.
    """

    def test_mine_is_strictly_the_viewers_own_rows(self):
        from routers.reports import _SCOPE_SQL, _scope_predicate

        sql, params = _scope_predicate("mine", "me@example.com")
        self.assertIn("owner_email = %s", sql)
        # The whole point: `mine` must not reach into the share table.
        self.assertNotIn("ai_report_colleague_shares", sql)
        self.assertEqual(params, ["me@example.com"])

    def test_shared_is_the_share_table_and_never_the_viewers_own_rows(self):
        from routers.reports import _scope_predicate

        sql, params = _scope_predicate("shared", "me@example.com")
        self.assertIn("ai_report_colleague_shares", sql)
        self.assertIn("owner_email <> %s", sql)
        # An unpublished colleague draft must not be visible to anyone else.
        self.assertIn("status = 'published'", sql)
        self.assertEqual(params, ["me@example.com", "me@example.com"])

    def test_public_is_published_snapshots_only(self):
        from routers.reports import _scope_predicate

        sql, params = _scope_predicate("public", "me@example.com")
        self.assertIn("visibility = 'public'", sql)
        self.assertIn("status = 'published'", sql)
        self.assertNotIn("ai_report_colleague_shares", sql)
        self.assertEqual(params, [])

    def test_every_scope_has_the_parameters_its_sql_asks_for(self):
        from routers.reports import _SCOPE_SQL, _scope_predicate

        for scope, (sql, count) in _SCOPE_SQL.items():
            with self.subTest(scope=scope):
                self.assertEqual(sql.count("%s"), count)
                self.assertEqual(len(_scope_predicate(scope, "me@example.com")[1]), count)
