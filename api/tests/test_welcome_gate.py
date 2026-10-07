"""What `/` means for a caller — and the cookie that used to decide it wrongly.

The gate itself is exercised end-to-end over real HTTP in `verify_welcome_ui.py`.
What cannot be reached there is the DECISION, because a guard's browser has no
session and can therefore only ever see one column of the table. So the table
lives here, as a pure function with no server, no database and no credential.

⚠️ Why the decision is about the SESSION and not about a visit marker:

the gate used to ask "has this browser been here?" via `klado-welcome-seen`,
set for a year by the landing page's call to action. That made the landing page
a once-per-browser page instead of a page for people who are not logged in — so
a browser that had ever clicked "Sign in" could never be shown it again, signed
out or not, and the browser's Back button could not rescue it either (the
landing page is served AT `/` by a rewrite, so the previous history entry is `/`
itself, which then answered with the application). The symptom reported from
outside was "I clicked Sign in and now I can never get back".

The marker no longer exists. These tests pin that it stays gone: a write-only
cookie would outlive the reason it existed, and the failure it guards against is
silent — nothing breaks, the landing page just quietly stops being reachable.

The other half — that signing out still signs you out — is one line away from the
code that was deleted, and it is the more important of the two questions.
"""
import asyncio
import inspect
import os
import sys
import unittest

from starlette.responses import Response

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from routers import auth  # noqa: E402

WELCOME = "welcome.html"
INDEX = "index.html"


def _logout_cookies():
    """The `Set-Cookie` headers signing out produces, as (name, attrs) pairs."""
    response = Response()
    asyncio.run(auth.logout(response))
    out = []
    for raw in response.headers.getlist("set-cookie"):
        head, _, rest = raw.partition(";")
        name = head.split("=", 1)[0].strip()
        out.append((name, head, rest))
    return out


class RootDocumentTests(unittest.TestCase):
    """The whole table. One row per case a caller can actually be in."""

    def setUp(self):
        import main
        self.decide = main.root_document_for

    def _assert(self, want, **kwargs):
        got = self.decide(**kwargs)
        self.assertEqual(got, want, "输入 %r 得到 %s，期望 %s" % (kwargs, got, want))

    # ── the case the user actually hit ──────────────────────────────────────
    def test_a_signed_out_visitor_gets_the_landing_page_on_every_visit(self):
        self._assert(WELCOME, has_query=False, auth_enabled=True,
                     authenticated=False)
        self._assert(WELCOME, has_query=False, auth_enabled=True,
                     authenticated=False)

    def test_a_signed_in_caller_goes_straight_to_the_application(self):
        self._assert(INDEX, has_query=False, auth_enabled=True,
                     authenticated=True)

    # ── deep links must never be intercepted ───────────────────────────────
    def test_a_deep_link_reaches_the_application_whether_signed_in_or_not(self):
        self._assert(INDEX, has_query=True, auth_enabled=True,
                     authenticated=False)
        self._assert(INDEX, has_query=True, auth_enabled=True,
                     authenticated=True)

    # ── auth off: "nobody is signed in" is everybody, so there is no gate ───
    def test_auth_disabled_never_shows_the_landing_page(self):
        self._assert(INDEX, has_query=False, auth_enabled=False,
                     authenticated=False)
        self._assert(INDEX, has_query=True, auth_enabled=False,
                     authenticated=False)

    def test_every_input_combination_lands_on_a_real_document(self):
        """Not a behaviour claim — a type/shape claim, and the cheap one.

        A typo in a return value would sail past the rows above if that row's
        expectation happened to match, because nothing here checks the ANSWER is
        one of the two documents the server can actually serve.
        """
        for has_query in (False, True):
            for auth_enabled in (False, True):
                for authenticated in (False, True):
                    with self.subTest(has_query=has_query,
                                      auth_enabled=auth_enabled,
                                      authenticated=authenticated):
                        self.assertIn(
                            self.decide(has_query=has_query,
                                        auth_enabled=auth_enabled,
                                        authenticated=authenticated),
                            (WELCOME, INDEX))


class SignedInCallerReachesTheApplicationTests(unittest.TestCase):
    """The other column of the table, over real HTTP.

    ⚠️ This is the half `verify_welcome_ui` structurally cannot see: its browser
    has no session, so every request it makes lands in the "not signed in" column.
    The failure it protects against is the expensive one — if `/` served the
    landing page to somebody who IS signed in, every colleague would have to click
    "Sign in" on every visit of every day.

    The identity resolution is stubbed rather than faked with a cookie: minting a
    real session needs a database and a password, and the thing under test is
    `serve_index`'s REACTION to an identity, not the session's own validation.
    What is not stubbed is the route, the middleware ordering and `_attach_
    identity_if_possible`'s "already resolved, leave it alone" short-circuit.

    ⚠️ ⚠️ `AUTH_ENABLED` is pinned inside these tests, and that is not tidiness —
    it is the second version of a bug this class already had. The first version
    read "signed out ⇒ landing page" straight off the ambient config, so it passed
    on a developer machine (`AUTH_ENABLED = True` in `.env`) and failed in CI,
    where `ci_check.py` runs the container with no `.env` at all and the setting
    defaults to False — and with auth off, "nobody is signed in" is EVERYBODY, so
    `/` correctly serves the application and the test was simply asserting
    something false about the environment it happened to be in. A test that
    depends on configuration has to say so, or "green locally" means nothing.
    """

    def _get(self, monkey_user, path="/"):
        from fastapi.testclient import TestClient
        import main

        original_identity = main._local_identity
        original_enabled = main.settings.AUTH_ENABLED
        main._local_identity = lambda request: (monkey_user, "browser")
        main.settings.AUTH_ENABLED = True
        try:
            return TestClient(main.app).get(path)
        finally:
            main._local_identity = original_identity
            main.settings.AUTH_ENABLED = original_enabled

    def test_a_signed_in_browser_gets_the_application(self):
        r = self._get({"id": 1, "email": "someone@example.com", "role": "user"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("wl-hero", r.text,
                         "已登录的浏览器打开 / 拿到了落地页 —— 每次都要多点一次 Sign in")
        self.assertIn('id="auth-gate"', r.text)

    def test_a_signed_out_browser_gets_the_landing_page(self):
        r = self._get(None)
        self.assertEqual(r.status_code, 200)
        self.assertIn("wl-hero", r.text)
        self.assertNotIn('id="auth-gate"', r.text)

    def test_a_deep_link_still_wins_for_an_anonymous_caller(self):
        r = self._get(None, path="/?report=quarterly")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("wl-hero", r.text)

    def test_auth_disabled_serves_the_application_to_everybody(self):
        """The same request, with the setting the way CI has it.

        Stated as its own test rather than left implicit in the one above: this is
        the configuration that made the first version of this class fail in the
        container while passing on a laptop, and it is a real deployment shape
        (every `AUTH_ENABLED=false` install), not a hypothetical.
        """
        from fastapi.testclient import TestClient
        import main

        original_identity = main._local_identity
        original_enabled = main.settings.AUTH_ENABLED
        main._local_identity = lambda request: (None, "")
        main.settings.AUTH_ENABLED = False
        try:
            r = TestClient(main.app).get("/")
        finally:
            main._local_identity = original_identity
            main.settings.AUTH_ENABLED = original_enabled
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("wl-hero", r.text)


class TheDeadCookieStaysDeadTests(unittest.TestCase):
    @staticmethod
    def _code_only(module):
        """The module's source with every COMMENT token removed.

        ⚠️ Comments are excluded on purpose. Both files still *explain* what the
        marker used to do — that history is the most useful thing in either of
        them — and a plain `assertNotIn(name, getsource(module))` would trip over
        those sentences and force the explanation to be deleted, which is the
        opposite of what a reader needs. The invariant is about CODE.
        """
        import io
        import tokenize
        src = inspect.getsource(module)
        kept = [tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                if tok.type != tokenize.COMMENT]
        return kept

    def test_nothing_reads_or_writes_the_visit_marker_any_more(self):
        """The marker is gone from both files that used to carry it.

        ⚠️ This is a source-level assertion, which is normally the weaker kind —
        it is here because the alternative failure is invisible: the cookie comes
        back, the routing still says `index.html`, and every test in this file
        stays green while the landing page quietly becomes unreachable again.
        """
        import main

        for module in (main, auth):
            with self.subTest(module=module.__name__):
                self.assertNotIn("klado-welcome-seen", self._code_only(module),
                                 "%s 的代码里又出现了落地页标记 cookie" % module.__name__)

    def test_the_decision_does_not_take_cookies_at_all(self):
        """Its parameters ARE the whole input surface.

        If a cookie ever creeps back in as a hidden dependency, the signature
        is where it has to show up — and a signature that grew is a decision that
        grew.
        """
        import main
        params = list(inspect.signature(main.root_document_for).parameters)
        self.assertEqual(params, ["has_query", "auth_enabled", "authenticated"])


class LogoutTests(unittest.TestCase):
    def test_signing_out_still_clears_the_session(self):
        """The pre-existing behaviour, stated because the deleted line sat next
        to it and "did we keep signing people out?" matters more than the gate."""
        names = [n for n, _, _ in _logout_cookies()]
        self.assertIn(auth.SESSION_COOKIE, names)

    def test_the_session_is_deleted_at_the_root_path(self):
        """`Max-Age=0` at the same path it was written under.

        A deletion whose path does not match the original set is dropped by the
        browser without complaint, so the response looks entirely normal and
        nothing has actually been signed out.
        """
        for name, head, attrs in _logout_cookies():
            with self.subTest(cookie=name):
                self.assertIn("Max-Age=0", attrs, "%s 没有被置为立即过期" % name)
                self.assertIn("Path=/", attrs,
                              "%s 的删除路径与写入路径不一致，浏览器会直接忽略" % name)


if __name__ == "__main__":
    unittest.main()