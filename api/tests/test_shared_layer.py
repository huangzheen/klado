"""The shared layer: one implementation, and a credential that cannot cross processes.

The admin console is a second process on the same machine, on the same database, with
its own port and its own login page. Two things about that are easy to get quietly
wrong, and this file exists because both failures are silent:

**A token that crosses processes.** Cookies are matched on host, not port, so a cookie
set on `127.0.0.1:8000` is also sent to `127.0.0.1:8787`. The console therefore uses its
own cookie name — but that is a *naming* convention, and naming conventions do not
stop anybody from copying a token string. The real control is the audience bound into
the signature: a console session must not be replayable against the main app, where it
would reach the operator endpoints. That is the majority of the tests below.

**A second copy of a decision.** `api/core/access.py` re-exports
`is_admin_identity` rather than defining it, and so do `core.db` and `core.i18n`. If
anyone ever pastes the body back in, the two processes start answering "who is an
operator" independently — both keep working, they just disagree, and the only symptom
is a permission that behaves differently depending on which port you used. The
identity assertions here fail the moment that happens, which is the point: they compare
*function objects*, not behaviour, because behaviour is identical right up until it
isn't.

Nothing here needs a server, a database, or `api-admin/` to exist.
"""
import os
import re
import sys
import time
import unittest

# The repository root, and the shared package beside it.
API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from klado_shared import db as shared_db            # noqa: E402
from klado_shared import i18n as shared_i18n        # noqa: E402
from klado_shared import identity as shared_identity  # noqa: E402
from klado_shared import session as shared_session  # noqa: E402
from klado_shared.config import settings            # noqa: E402

USER = {"id": 4242, "email": "operator@example.com", "role": "admin"}


def _legacy_token(user_id: int, ttl: int = 3600) -> str:
    """A token in the pre-audience format: signed over `uid.expiry` with no tag.

    Built here rather than imported, because `_sign` is private and the whole point is
    to reproduce what a token minted *before* this change looks like.
    """
    import hashlib
    import hmac
    key = (settings.SECRET_KEY or "change-me").encode("utf-8")
    expiry = int(time.time()) + ttl
    payload = f"{user_id}.{expiry}".encode()
    return f"{user_id}.{expiry}.{hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]}"


class SessionAudienceTests(unittest.TestCase):
    """A token minted for one process must be worthless to the other."""

    def test_app_token_verifies_in_the_app(self):
        token = shared_session.issue_session(USER, shared_session.APP_AUDIENCE)
        self.assertEqual(
            shared_session.session_user_id(token, shared_session.APP_AUDIENCE), USER["id"])

    def test_console_token_verifies_in_the_console(self):
        token = shared_session.issue_session(USER, shared_session.ADMIN_AUDIENCE)
        self.assertEqual(
            shared_session.session_user_id(token, shared_session.ADMIN_AUDIENCE), USER["id"])

    def test_console_token_is_rejected_by_the_main_app(self):
        """The load-bearing one: replaying a console session against :8000."""
        token = shared_session.issue_session(USER, shared_session.ADMIN_AUDIENCE)
        self.assertIsNone(
            shared_session.session_user_id(token, shared_session.APP_AUDIENCE))

    def test_app_token_is_rejected_by_the_console(self):
        token = shared_session.issue_session(USER, shared_session.APP_AUDIENCE)
        self.assertIsNone(
            shared_session.session_user_id(token, shared_session.ADMIN_AUDIENCE))

    def test_token_shape_is_unchanged(self):
        """`uid.expiry.signature` — the parsers upstream check `count('.') != 2`."""
        for audience in (shared_session.APP_AUDIENCE, shared_session.ADMIN_AUDIENCE):
            token = shared_session.issue_session(USER, audience)
            self.assertEqual(token.count("."), 2, audience)
            self.assertTrue(token.split(".")[0].isdigit(), audience)

    def test_two_audiences_produce_different_signatures_for_the_same_payload(self):
        """Not a defence in depth detail — this is *why* the two differ at all."""
        app = shared_session.issue_session(USER, shared_session.APP_AUDIENCE)
        admin = shared_session.issue_session(USER, shared_session.ADMIN_AUDIENCE)
        self.assertNotEqual(app.split(".")[-1], admin.split(".")[-1])

    def test_wrong_secret_key_rejects_everything(self):
        token = shared_session.issue_session(USER, shared_session.APP_AUDIENCE)
        original = settings.SECRET_KEY
        settings.SECRET_KEY = "a-different-key"
        try:
            self.assertIsNone(
                shared_session.session_user_id(token, shared_session.APP_AUDIENCE))
        finally:
            settings.SECRET_KEY = original


class LegacyTokenMigrationTests(unittest.TestCase):
    """Upgrading must not log everybody out — but must not hand the console one either."""

    def test_main_app_still_accepts_a_pre_audience_token(self):
        token = _legacy_token(USER["id"])
        self.assertEqual(
            shared_session.session_user_id(
                token, shared_session.APP_AUDIENCE, allow_legacy=True),
            USER["id"])

    def test_console_refuses_a_pre_audience_token_even_with_allow_legacy(self):
        """⚠️ The asymmetric half. An untagged token has no audience, so there is nothing
        to check; accepting it in the console would defeat the whole mechanism. This is
        why the main app's compatibility branch cannot be copied over as-is."""
        token = _legacy_token(USER["id"])
        self.assertIsNone(
            shared_session.session_user_id(
                token, shared_session.ADMIN_AUDIENCE, allow_legacy=True))

    def test_main_app_stops_accepting_legacy_once_the_flag_is_off(self):
        token = _legacy_token(USER["id"])
        self.assertIsNone(
            shared_session.session_user_id(
                token, shared_session.APP_AUDIENCE, allow_legacy=False))

    def test_only_the_main_app_is_legacy_tolerant(self):
        """The invariant, stated directly rather than inferred from behaviour.

        Adding a third process must mean deciding here whether it tolerates pre-audience
        tokens — inheriting whatever the caller passed is what let a console login
        accept one in the first place.
        """
        self.assertIn(shared_session.APP_AUDIENCE,
                      shared_session.LEGACY_TOLERANT_AUDIENCES)
        self.assertNotIn(shared_session.ADMIN_AUDIENCE,
                         shared_session.LEGACY_TOLERANT_AUDIENCES)


class SessionRejectionTests(unittest.TestCase):
    """The pre-existing rejections must survive the move."""

    def setUp(self):
        self.token = shared_session.issue_session(USER, shared_session.APP_AUDIENCE)

    def _parts(self):
        uid, expiry, signature = self.token.split(".")
        return uid, expiry, signature

    def test_expired_token_is_rejected(self):
        uid, expiry, signature = self._parts()
        expired = f"{uid}.{int(expiry) - 10}.{signature}"
        self.assertIsNone(
            shared_session.session_user_id(expired, shared_session.APP_AUDIENCE,
                                           allow_legacy=True))

    def test_tampered_user_id_is_rejected(self):
        """Changing the id invalidates the signature — the payload is signed whole."""
        uid, expiry, signature = self._parts()
        forged = f"{int(uid) + 1}.{expiry}.{signature}"
        self.assertIsNone(
            shared_session.session_user_id(forged, shared_session.APP_AUDIENCE,
                                           allow_legacy=True))

    def test_malformed_tokens_are_rejected(self):
        for bad in ("", "garbage", "a.b", "a.b.c.d", "1.2.3.4", "x.y.z", None, 12345):
            self.assertIsNone(
                shared_session.session_user_id(bad, shared_session.APP_AUDIENCE,
                                               allow_legacy=True),
                msg=repr(bad))

    def test_audience_is_not_swappable_with_a_trailing_dot(self):
        """`uid.expiry.app` is not a payload — the extra dot must not parse as a new tag."""
        uid, expiry, signature = self._parts()
        self.assertIsNone(
            shared_session.session_user_id(
                f"{uid}.{expiry}.app.{signature}", shared_session.APP_AUDIENCE))


class SingleImplementationTests(unittest.TestCase):
    """`api/` re-exports; it must never re-define.

    Compared by identity, not by behaviour: right now every call agrees, and a copy
    would agree too. These assertions exist so the moment somebody pastes a body back
    in, the suite goes red instead of the two processes quietly diverging months later.
    """

    def test_access_re_exports_the_shared_operator_check(self):
        from core import access
        self.assertIs(access.is_admin_identity, shared_identity.is_admin_identity)
        self.assertIs(access.admin_emails, shared_identity.admin_emails)

    def test_db_re_exports_the_shared_connection_resolver(self):
        from core import db
        self.assertIs(db.connect_main, shared_db.connect_main)
        self.assertIs(db.pg_connection_kwargs, shared_db.pg_connection_kwargs)
        self.assertIs(db._pg_kwargs, shared_db._pg_kwargs)

    def test_i18n_re_exports_the_shared_pair_rules(self):
        from core import i18n
        self.assertIs(i18n.pick, shared_i18n.pick)
        self.assertIs(i18n.request_lang, shared_i18n.request_lang)
        self.assertIs(i18n.split_pair, shared_i18n.split_pair)
        self.assertIs(i18n.parse_accept_language, shared_i18n.parse_accept_language)

    def test_auth_store_re_exports_the_shared_session_functions(self):
        from services import auth_store
        self.assertIs(auth_store.issue_session, shared_session.issue_session)
        self.assertIs(auth_store.session_user_id, shared_session.session_user_id)

    def test_config_re_exports_one_settings_object(self):
        from core import config
        from klado_shared import config as shared_config
        self.assertIs(config.settings, settings)
        self.assertIs(config.Settings, shared_config.Settings)

    def test_main_app_files_do_not_define_the_shared_functions(self):
        """Belt to the re-export braces: no second `def is_admin_identity` etc.

        Scans for definitions rather than imports, so a redefinition is caught even if
        the name is then re-exported again on top.
        """
        forbidden = {
            "is_admin_identity", "issue_session", "session_user_id", "_sign",
            "pg_connection_kwargs", "split_pair", "request_lang", "parse_accept_language",
        }
        offenders = []
        for sub in ("core", "services", "routers", "tools"):
            for dirpath, _dirs, files in os.walk(os.path.join(API_DIR, sub)):
                if "__pycache__" in dirpath:
                    continue
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as handle:
                        for lineno, line in enumerate(handle, 1):
                            m = re.match(r"\s*def\s+(\w+)\s*\(", line)
                            if m and m.group(1) in forbidden:
                                rel = os.path.relpath(path, REPO_DIR)
                                offenders.append(f"{rel}:{lineno} def {m.group(1)}")
        self.assertEqual(offenders, [],
                         "shared functions must be defined only in klado_shared/")


# A real import out of the main app's tree, in either of the two spellings that mean
# the same thing. Kept as a module constant so the detector and the fixture cannot drift.
_FORBIDDEN = (
    re.compile(r"^\s*import\s+api\b", re.M),
    re.compile(r"^\s*from\s+api[.\s]", re.M),
    re.compile(r"^\s*from\s+api\s+import\b", re.M),
)


def forbidden_api_imports(root: str) -> list[str]:
    """`(path:line, line)` for every import of `api.*` under `root`."""
    hits = []
    if not os.path.isdir(root):
        return hits
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath or os.path.basename(dirpath) == "node_modules":
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, 1):
                    if any(rx.search(line) for rx in _FORBIDDEN):
                        hits.append(f"{os.path.relpath(path, REPO_DIR)}:{lineno} {line.strip()}")
    return hits


class AdminConsoleImportGuardTests(unittest.TestCase):
    """`api-admin/` must reach the shared layer, never the main app's internals.

    The console is a separate deployment unit. The moment it imports `api.services.*`,
    the two processes are one process wearing two hats, and the main app's router
    import chain drags in settings, database bootstrap and every service module — so the
    failure is not a layering complaint, it is a broken process boundary.
    """

    def test_detector_catches_a_planted_violation(self):
        """The guard is only worth having if it can fail. Prove it, with a fixture.

        A static check that has never rejected anything is indistinguishable from a
        check that does not work — so this plants the exact three spellings that are
        easy to miss and requires all of them to be caught.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "routers.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "import api.services.auth_store\n"
                    "from api.core import access\n"
                    "from services import auth_store\n"        # allowed: not `api.`
                    "import api\n"
                )
            hits = forbidden_api_imports(tmp)
        self.assertEqual(len(hits), 3, hits)
        self.assertTrue(any("import api.services.auth_store" in h for h in hits), hits)
        self.assertTrue(any("from api.core import access" in h for h in hits), hits)
        self.assertTrue(any(h.rstrip().endswith("import api") for h in hits), hits)

    def test_console_contains_no_main_app_imports(self):
        """⚠️ No longer a skip. It was one for the whole of P0–P3, which means the guard
        was never actually rejecting anything during the phases that built the console —
        a check that has not yet been able to fail is not evidence that the rule holds.
        The console exists now, so this is a real assertion, and the test above is what
        keeps it honest about being one."""
        console_dir = os.path.join(REPO_DIR, "api-admin")
        self.assertTrue(os.path.isdir(console_dir),
                        "api-admin/ is required from phase P4 on; a missing console "
                        "means the boundary is not being enforced at all")
        self.assertEqual(forbidden_api_imports(console_dir), [])


class ConsolePackageNamingTests(unittest.TestCase):
    """The console's packages must not be named like the main app's.

    ⚠️ This one was found the hard way, and it is worth a test because the failure is
    invisible from inside either process. The console originally had `api-admin/routers/`
    and `api-admin/core/`, and the main app has `api/routers/` and `api/core/` — both
    trees are top-level (each is started as `cd <dir> && uvicorn main:app`). Put both on
    one `sys.path` — which is exactly what a test checking the boundary has to do — and
    `import routers` resolves to whichever came first. The console's routers silently
    became the main app's, and the two processes were separated only by a working
    directory.

    Importable *beside* rather than *instead of* is what makes the boundary testable at
    all, so the names are pinned here.
    """

    def test_no_console_package_shadows_a_main_app_one(self):
        main_top = set()
        for name in os.listdir(API_DIR):
            if os.path.isdir(os.path.join(API_DIR, name)) and \
                    os.path.exists(os.path.join(API_DIR, name, "__init__.py")):
                main_top.add(name)
        console_top = set()
        for name in os.listdir(os.path.join(REPO_DIR, "api-admin")):
            if os.path.isdir(os.path.join(REPO_DIR, "api-admin", name)):
                console_top.add(name)
        self.assertTrue(console_top, "the console has no packages at all")
        self.assertEqual(sorted(console_top & main_top), [],
                         "these console package names collide with the main app's")

    def test_the_console_and_the_main_app_are_importable_together(self):
        """The real proof. Both trees on one `sys.path`, both `main` modules loaded, and
        neither resolved to the other's — which is the whole point of a separate process
        with a separate name space."""
        import subprocess
        script = (
            "import importlib, sys\n"
            f"sys.path.insert(0, {API_DIR!r})\n"
            f"sys.path.insert(0, {os.path.join(REPO_DIR, 'api-admin')!r})\n"
            "import klado_shared\n"
            "console = importlib.import_module('main')\n"
            "app = importlib.import_module('api.main')\n"
            "assert console.__file__.endswith('api-admin/main.py'), console.__file__\n"
            "assert app.__file__.endswith('api/main.py'), app.__file__\n"
            "import klado_shared.accounts as a, klado_shared.modules as m\n"
            "assert getattr(m, 'DATACENTER', None), 'the shared registry is empty'\n"
            "print('both loaded')\n"
        )
        env = dict(os.environ)
        proc = subprocess.run([sys.executable, "-c", script], cwd=REPO_DIR, env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("both loaded", proc.stdout)


class CallSiteContractTests(unittest.TestCase):
    """The main app must opt into legacy tokens. The console must not.

    `allow_legacy` defaults to False, which is the safe direction for a *new* process.
    For the process that already existed, the default is the dangerous one: drop the
    argument and every signed-in browser is logged out the moment the deployment is
    updated. Nothing about that failure looks like a permissions bug — it looks like
    "the session expired" — and it would be reported as a login problem, not as a
    missing keyword argument.

    So the call site is pinned here. A weaker assertion ("the flag exists somewhere in
    session.py") would pass while `main.py` still logged everybody out.
    """

    MAIN_APP_VERIFY = "auth_store.session_user_id(token, allow_legacy=True)"

    def test_main_app_opts_into_legacy_tokens(self):
        path = os.path.join(API_DIR, "main.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        # `assertTrue` rather than `assertIn`: on failure, assertIn prints the whole
        # container, which here is 40KB of main.py and buries the message.
        self.assertTrue(
            self.MAIN_APP_VERIFY in source,
            "api/main.py must verify session tokens with allow_legacy=True, or every "
            "existing session is invalidated on upgrade")

    def test_default_is_the_safe_direction(self):
        """Stated so the two call sites above are read against something."""
        self.assertIsNone(
            shared_session.session_user_id(
                _legacy_token(USER["id"]), shared_session.APP_AUDIENCE))
        self.assertEqual(
            shared_session.session_user_id(
                _legacy_token(USER["id"]), shared_session.APP_AUDIENCE, allow_legacy=True),
            USER["id"])

    def test_no_other_process_verifies_without_an_explicit_audience(self):
        """Every `session_user_id` call names its audience, so the cross-process test
        above cannot be passed by accident at a second call site."""
        offenders = []
        for sub in ("core", "services", "routers"):
            for dirpath, _dirs, files in os.walk(os.path.join(API_DIR, sub)):
                if "__pycache__" in dirpath:
                    continue
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as handle:
                        for lineno, line in enumerate(handle, 1):
                            if "session_user_id(" not in line or line.lstrip().startswith("#"):
                                continue
                            # Allowed: the re-export import, and the pinned call above.
                            if "import" in line or "allow_legacy" in line:
                                continue
                            offenders.append(
                                f"{os.path.relpath(path, REPO_DIR)}:{lineno} {line.strip()}")
        self.assertEqual(offenders, [],
                         "a session_user_id call with neither an audience nor an "
                         "explicit allow_legacy decision: " + "; ".join(offenders))


if __name__ == "__main__":
    unittest.main()
