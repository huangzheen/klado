"""
The per-report annotation state endpoints: `GET/POST /api/reports/{slug}/state`.

Two things are worth pinning down without a database:

* the body gate — the state is a client-owned black box, so the parser refuses
  invalid JSON and oversized payloads. Verbatim storage means a caller gets back
  exactly what it sent (no re-serialisation), which is what makes the document's
  round-trip trustworthy.
* the authentication boundary — state paths, standalone reports and other report
  endpoints require a session; public snapshots remain read-only.
"""
import json
import unittest

from fastapi import HTTPException

from routers import reports
from services import doc_state  # 抽取后：筛选/注解逻辑的归属


class StateBodyTests(unittest.TestCase):
    def test_valid_json_is_stored_verbatim(self):
        # Whitespace and key order survive — a caller must be able to diff what it
        # GETs against what it POSTed.
        body = b'{\n  "online": {"cells": {"3_2": 0}},\n  "title": "Q4"\n}'
        self.assertEqual(doc_state.state_text(body), body.decode())

    def test_the_documented_shape_passes(self):
        state = {
            "title": "t", "sub": "s",
            "online": {"cells": {"3_2": 0, "4_2": 1},
                       "colors": ["#dc2626", "#16a34a"],
                       "label": ["Callout 1", ""],
                       "desc": ["", ""]},
            "offline": {"cells": {}, "colors": [], "label": [], "desc": []},
        }
        raw = json.dumps(state).encode()
        self.assertEqual(json.loads(doc_state.state_text(raw)), state)

    def test_any_json_value_is_accepted_not_just_objects(self):
        # "任意 JSON" — we never look inside, so a bare array or scalar is the caller's
        # business (the client owns the schema).
        for raw in (b"[]", b"null", b"42", b'"a string"'):
            self.assertEqual(doc_state.state_text(raw), raw.decode())

    def test_size_limit_is_exactly_64kb(self):
        # A payload of exactly the cap must pass; the very next byte must not.
        filler = "x" * (reports.MAX_STATE_BYTES - len('{"a":""}'))
        at_limit = ('{"a":"%s"}' % filler).encode()
        self.assertEqual(len(at_limit), reports.MAX_STATE_BYTES)
        self.assertEqual(doc_state.state_text(at_limit), at_limit.decode())

        over = b'{"a":"' + (filler + "x").encode() + b'"}'
        self.assertEqual(len(over), reports.MAX_STATE_BYTES + 1)
        with self.assertRaises(HTTPException) as ctx:
            doc_state.state_text(over)
        self.assertEqual(ctx.exception.status_code, 413)

    def test_nan_and_infinity_are_refused(self):
        # Python's json accepts these, JSON.parse does not — storing one would hand the
        # reader a document it cannot parse.
        for raw in (b'{"v": NaN}', b'{"v": Infinity}', b'{"v": -Infinity}'):
            with self.assertRaises(HTTPException) as ctx:
                doc_state.state_text(raw)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_not_json_is_a_400(self):
        for raw in (b"", b"   ", b"not json", b"{'single': 'quotes'}", b'{"a":1,}'):
            with self.assertRaises(HTTPException) as ctx:
                doc_state.state_text(raw)
            self.assertEqual(ctx.exception.status_code, 400, raw)

    def test_non_utf8_is_a_400(self):
        with self.assertRaises(HTTPException) as ctx:
            doc_state.state_text(b'{"a": "\xff\xfe"}')
        self.assertEqual(ctx.exception.status_code, 400)


class StatePathAuthTests(unittest.TestCase):
    """Which paths skip authentication (main._is_public_path)."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def test_state_paths_require_authentication(self):
        for path in ("/api/reports/segment-annotator-demo/state",
                     "/api/reports/segment-annotator-demo/state/",
                     "/api/reports/r-20260927-1010-ab12cd/state",
                     "/api/reports/v1.2-demo/state"):
            self.assertFalse(self.main._is_public_path(path), path)

    def test_the_rest_of_a_report_is_still_private(self):
        # The exemption is scoped to /state and must not leak sideways: with auth on,
        # /raw and /cover are exactly the endpoints that must keep asking for a session.
        for path in ("/api/reports/segment-annotator-demo",
                     "/api/reports/segment-annotator-demo/raw",
                     "/api/reports/segment-annotator-demo/cover",
                     "/r/segment-annotator-demo",
                     "/api/reports/segment-annotator-demo/state/extra",
                     "/api/reports/state",
                     "/api/reports//state"):
            self.assertFalse(self.main._is_public_path(path), path)

    def test_admin_and_write_surfaces_are_untouched(self):
        for path in ("/api/auth/admin/users", "/api/data-center/datasets/products",
                     "/api/settings/build-version", "/api/reports/cover"):
            self.assertFalse(self.main._is_public_path(path), path)

    def test_the_original_exact_paths_still_count(self):
        self.assertTrue(self.main._is_public_path("/api/health"))
        self.assertTrue(self.main._is_public_path("/api/auth/login"))

    def test_standalone_documents_resolve_a_caller_before_their_handler_runs(self):
        """⚠️ The middleware is what puts the caller on `request.state`, and these handlers
        call `_identity()`. A document missing from this list answers 401 to a browser that
        IS signed in — an iframe can only offer the cookie, never an Authorization header.

        `/e/` was missing, and the calendar's event overlay rendered that 401 JSON in place
        of the event (found 2026-09-29 from a user screenshot).
        """
        for path in ("/api/reports/x", "/r/any-slug", "/e/any-slug"):
            self.assertTrue(self.main.needs_identity(path), path)

    def test_the_login_free_share_link_is_not_in_that_list(self):
        # `/s/{token}` is the app's ONLY login-free entry point: the token in the path IS the
        # credential, so requiring an identity there would break every shared link.
        for path in ("/s/some-token", "/s/some-token/state", "/index.html", "/",
                     "/favicon.ico", "/api", "/e", "/health"):
            self.assertFalse(self.main.needs_identity(path), path)


if __name__ == "__main__":
    unittest.main()
