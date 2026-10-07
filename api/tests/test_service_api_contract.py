"""Every `<module>.<name>` in the code has to be a name the module actually has.

This exists because of a shipped bug in `routers/org_admin.py`, the enterprise
invitation:

    code = auth_store.issue_invite_code(address)     # never existed
    mailer.send_invitation(address, code)             # never existed

Neither function has ever been in `services/auth_store.py` or `services/mailer.py` —
the real ones are `create_code` and `send_invite`. It shipped because both calls sat
inside a `try: ... except Exception` that exists to keep an SMTP hiccup from failing a
sign-up, and a bare `except Exception` swallows `AttributeError` exactly as happily as
it swallows `OperationalError`. So the failure was a **502 with a plausible message**
("could not create the invitation code") and a silently undelivered mail, and both
looked like an environment problem.

Why the unit tests missed it is the more useful half. `test_org_admin.py` fakes
`auth_store`, and the branch those two lines live in requires *no existing account* for
that address. Every fixture had already registered one, so `if not joined and not
auth_store.get_user_by_email(address)` was false, the code below it never ran, and a
whole set of assertions passed against a branch they had never entered. **A fake that
cannot walk into the branch is indistinguishable from a branch that works.**

So this file does not test behaviour — it tests the *name* at the boundary. It is cheap,
it needs no server and no database, and it fails at import-check time rather than at
"the invite button mysteriously 502s in production".

The gap it cannot close, stated so nobody assumes otherwise: `except Exception` still
hides a missing attribute on a module that is NOT in the table below. Adding a module
here is one line; leaving one out means that module's typos are back to being invisible.
"""
import ast
import os
import sys
import unittest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _path in (REPO_DIR, API_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from klado_shared import identity as shared_identity  # noqa: E402
from klado_shared import orgs as shared_orgs          # noqa: E402
from klado_shared import session as shared_session    # noqa: E402
from core import access, config, db, i18n, modules   # noqa: E402
from services import (account_lifecycle, account_modules, auth_store,  # noqa: E402
                      mail_config, mail_templates, mailer)

#: local alias → the object it must resolve to. A `from X import Y as Z` in any file
#: under scan is matched against these, and every `<Z>.<attr>` is checked with
#: `hasattr`. The alias is the key, not the dotted path, because what callers write is
#: the alias — `auth_store.issue_invite_code` is the bug, not `services.auth_store.…`.
WATCHED = {
    "auth_store": auth_store,
    "account_lifecycle": account_lifecycle,
    "account_modules": account_modules,
    "mailer": mailer,
    "mail_config": mail_config,
    "mail_templates": mail_templates,
    "orgs": shared_orgs,
    "core_modules": modules,
    "modules": modules,
    "access": access,
    "settings": config.settings,
}

SCAN_SUBDIRS = ("core", "services", "routers", "tools")
SCAN_FILES = ("main.py",)


def _module_bindings(tree: ast.AST) -> dict:
    """Local name → the object it is bound to, for the `from X import Y as Z` and
    `import X.Y as Z` forms. `import X.Y` (no alias) binds `X`, whose `.Y` is an
    attribute rather than a module object, so it is recorded as a miss by design —
    callers in this tree always use the `from … import` form."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = (
                        f"{node.module}.{alias.name}" if node.module else alias.name)
    return out


def _resolve(path: str) -> object:
    """Import a dotted path from a file's own point of view, or None."""
    root, _, tail = path.partition(".")
    if not tail:
        return None  # a bare module name in the table would be a mistake, not a hit
    try:
        module = __import__(root, fromlist=[tail])
    except Exception:  # noqa: BLE001 — an unimportable root is not this test's business
        return None
    return getattr(module, tail, None)


def missing_attributes(path: str, source: str) -> list[str]:
    """`file:line  module.name` for every attribute access a module does not have."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    bindings = _module_bindings(tree)
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        target = bindings.get(node.value.id)
        if target is None:
            continue
        obj = WATCHED.get(target) or _resolve(target)
        if obj is None:
            continue
        if not hasattr(obj, node.attr):
            rel = os.path.relpath(path, REPO_DIR)
            problems.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
    return problems


def _python_files() -> list[str]:
    files = []
    for sub in SCAN_SUBDIRS:
        base = os.path.join(API_DIR, sub)
        for dirpath, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            files += [os.path.join(dirpath, n) for n in sorted(names)
                      if n.endswith(".py")]
    files += [os.path.join(API_DIR, n) for n in SCAN_FILES
              if os.path.isfile(os.path.join(API_DIR, n))]
    return files


class DetectorTests(unittest.TestCase):
    """The detector has to be able to fail, or the scan below proves nothing."""

    def test_catches_a_missing_attribute(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "router.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "from services import auth_store\n"
                    "code = auth_store.issue_invite_code('a@b.c')\n"   # does not exist
                    "ok = auth_store.get_user_by_email('a@b.c')\n"      # does exist
                )
            found = missing_attributes(path, open(path, encoding="utf-8").read())
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].endswith("auth_store.issue_invite_code"), found)

    def test_ignores_a_local_name_that_happens_to_match(self):
        """`things.auth_store` is somebody's own object, not the service module.

        The check is against the module a name is BOUND to, not against its spelling:
        a local variable called `auth_store` is somebody else's business.
        """
        source = "things.auth_store.no_such_name\n"
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "router.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)
            self.assertEqual(missing_attributes(path, source), [])

    def test_resolves_both_spellings_of_the_same_module(self):
        """`import services.mailer as m` has to be checked as thoroughly as
        `from services import mailer` — the alias is what the call site writes."""
        source = "import services.mailer as m\nm.not_a_function()\n"
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "router.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)
            found = missing_attributes(path, source)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].endswith("m.not_a_function"), found)


class ServiceApiContractTests(unittest.TestCase):
    """The real scan. Every call site's module attribute has to resolve."""

    def test_no_call_site_names_a_function_that_does_not_exist(self):
        offenders = []
        for path in _python_files():
            with open(path, encoding="utf-8") as handle:
                offenders += missing_attributes(path, handle.read())
        self.assertEqual(
            offenders, [],
            "these names do not exist on the module they are called on — check the "
            "spelling, or whether the function was renamed:\n  "
            + "\n  ".join(offenders))

    def test_watched_table_is_not_emptying_itself(self):
        """Stated so the table cannot quietly shrink to nothing over the years.

        A scan whose watch list has been reduced to two modules while the code moved
        to a third would still be green, and would look exactly like a clean bill of
        health. These are the modules that carry a public contract: everything below
        here has a caller in `routers/` or in `main.py`.
        """
        for name in ("auth_store", "account_lifecycle", "mailer", "mail_config",
                     "orgs", "core_modules", "settings"):
            self.assertIn(name, WATCHED, name)

    def test_watched_table_is_importable(self):
        """A typo in a dotted path would turn its module into a silent no-op scan."""
        for name in ("services.auth_store", "services.mailer", "core.modules",
                     "klado_shared.orgs", "klado_shared.session",
                     "klado_shared.identity"):
            self.assertIsNotNone(_resolve(name), name)


class SharedIdentityReExportTests(unittest.TestCase):
    """Two process boundaries still have exactly one implementation of each.

    `api-admin/` imports these directly; `api/` re-exports them. If a definition is ever
    pasted back into `api/core/access.py` or `services/auth_store.py`, the two processes
    start answering independently and the only symptom is a permission that behaves
    differently depending on which port you used.
    """

    def test_operator_check_is_one_function(self):
        self.assertIs(access.is_admin_identity, shared_identity.is_admin_identity)

    def test_session_signing_is_one_function(self):
        self.assertIs(auth_store.issue_session, shared_session.issue_session)
        self.assertIs(auth_store.session_user_id, shared_session.session_user_id)

    def test_db_and_i18n_are_re_exports(self):
        from klado_shared import db as shared_db
        from klado_shared import i18n as shared_i18n
        self.assertIs(db.connect_main, shared_db.connect_main)
        self.assertIs(i18n.pick, shared_i18n.pick)


if __name__ == "__main__":
    unittest.main()
