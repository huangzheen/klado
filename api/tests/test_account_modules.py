"""Per-account module entitlements: the THREE states, and the clear that is not two.

This file exists because of one bug and one missing state.

**The bug.** `account_modules` grew two similarly-named helpers: `clear_for(user_id)`
drops the account's whole row, `clear_one(user_id, key)` drops one module. Every
`DELETE /api/settings/modules/{module_key}` handler called `clear_for` while its own
docstring promised "return THAT module to the default" — so clicking reset on one
module silently discarded every other narrowing the account had. Nothing in the router
suite caught it, because the router suite fakes `account_modules` entirely.

**The missing state.** The control has three positions — opened, closed, and *nobody
said anything, so follow the deployment* — and only the first two are booleans. With
`enabled: bool` on the request model, a `null` collapsed through `bool(None)` to
`False` and wrote a row that outlives the deployment setting. That is precisely the
stale entitlement the intersection in `core.modules.resolve()` exists to neutralise,
and it was one click away from existing.

No database and no server: the connection is a scripted fake, the SQL is recorded, and
the router is driven through a TestClient with `account_modules` faked at its real
attribute names — faked by NAME, so binding the wrong helper is still visible.
"""
import os
import sys
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import modules as core_modules                       # noqa: E402
from routers import settings as settings_router                # noqa: E402
from services import account_modules                          # noqa: E402

USER_ID = 41


# ── the store ────────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, log, rows):
        self._log = log
        self._rows = list(rows)
        self.description = None

    def execute(self, sql, params=()):
        self._log.append((" ".join(str(sql).split()), params))
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    # The store writes `with conn.cursor() as cur:` — psycopg2 cursors are context
    # managers, so the fake has to be one too. Without this the failure is a TypeError
    # about the protocol rather than anything about the SQL being asserted.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, log, rows=()):
        self._log = log
        self._rows = list(rows)

    def cursor(self, *a, **k):
        return _Cur(self._log, self._rows)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _call(fn, *args, rows=()):
    """Run one store function against a fake connection; hand back (sql, result)."""
    log = []

    class _P:
        @staticmethod
        def connect_main():
            return _Conn(log, rows)

    with patch.object(account_modules, "connect_main", _P.connect_main), \
         patch.object(account_modules, "_schema_ready", True):
        result = fn(*args)
    return log, result


class ClearOneTests(unittest.TestCase):
    """`clear_one` and `clear_for` are different acts. Proved at the SQL."""

    def test_clear_one_deletes_exactly_one_key(self):
        log, _ = _call(account_modules.clear_one, USER_ID, "calendar")
        self.assertEqual(len(log), 1, "one statement, not a read-modify-write")
        sql, params = log[0]
        self.assertIn("DELETE", sql.upper())
        self.assertIn("module_key", sql, "⚠️ a DELETE without the key column is "
                                         "`clear_for` wearing the other helper's name")
        self.assertEqual(params, (USER_ID, "calendar"))

    def test_clear_for_deletes_the_whole_row(self):
        log, _ = _call(account_modules.clear_for, USER_ID)
        self.assertEqual(len(log), 1)
        sql, params = log[0]
        self.assertNotIn("module_key", sql)
        self.assertEqual(params, (USER_ID,))

    def test_clearing_a_key_that_has_no_row_is_a_success(self):
        """The caller asked for "nobody has said anything about this module". That is
        now true, so reporting an error would make a correct click look broken."""
        log, result = _call(account_modules.clear_one, USER_ID, "never-set")
        self.assertEqual(len(log), 1, "the statement still runs: the state is proven, "
                                      "not assumed")
        self.assertIsNone(result)


# ── the three states over HTTP ───────────────────────────────────────────────

def _client(identity):
    app = FastAPI()

    @app.middleware("http")
    async def _who(request: Request, call_next):
        request.state.current_user = identity
        return await call_next(request)

    # ⚠️ The prefix lives in `main.py`, not in the router — the same thing the real app
    # does. Building the client with the bare router would make every path in this file
    # 404 for a reason that has nothing to do with the behaviour under test.
    app.include_router(settings_router.router, prefix="/api/settings")
    return TestClient(app, raise_server_exceptions=False)


OPERATOR = {"email": "root@platform.example", "role": "admin", "id": USER_ID}
MEMBER = {"email": "mate@corp.example", "role": "user", "id": USER_ID}

# ⚠️ The dev deployment sets no `KLADO_MODULES`, so `deployment_default()` is the four
# REQUIRED modules and every switchable one is withdrawn. That is a true fact about this
# environment, not a useful fixture: with nothing on sale the whole matrix renders inert
# and none of the grant/withdraw branches below could be reached. So the tests declare
# which modules are on sale, rather than inheriting whatever the shell happens to have.
_ON_SALE = ("core", "inbox", "settings", "workspace")
_OFF_SALE = "dashboard"          # switchable, but not offered


class _Deployment:
    """`core_modules.deployment_default()` as a declared fixture."""

    def __enter__(self):
        self._p = patch.object(core_modules, "deployment_default", return_value=_ON_SALE)
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()
        return False


_TOGGLEABLE = "workspace"
_REQUIRED = "core"
assert _TOGGLEABLE in _ON_SALE and _OFF_SALE not in _ON_SALE
assert _REQUIRED in _ON_SALE
assert core_modules.BY_KEY[_TOGGLEABLE].toggleable
assert core_modules.BY_KEY[_OFF_SALE].toggleable
assert core_modules.BY_KEY[_REQUIRED].required


class _Probe:
    """Fakes `account_modules` by ATTRIBUTE, so the assertions can name which helper ran.

    ⚠️ A single `patch.object(..., autospec=True)` on the module would have hidden the
    whole bug: `clear_for` and `clear_one` are different functions, and the mistake is
    in *which one* the router reaches for.
    """

    def __init__(self):
        self.calls = []

    def set_module(self, user_id, module_key, enabled):
        self.calls.append(("set", user_id, module_key, enabled))

    def clear_one(self, user_id, module_key):
        self.calls.append(("clear_one", user_id, module_key))

    def clear_for(self, user_id):
        self.calls.append(("clear_for", user_id))

    # ⚠️ Only the 4-tuples are rows. A clear leaves no row behind, which is the whole
    # point of it — so the read helpers must not try to unpack it as one.
    def overrides_for(self, user_id):
        return {c[2]: c[3] for c in self.calls if c[0] == "set" and c[1] == user_id}

    def list_all(self):
        return [{"user_id": c[1], "module_key": c[2], "enabled": c[3]}
                for c in self.calls if c[0] == "set"]


class _Patched:
    def __init__(self, probe):
        self._probe = probe
        self._started = []

    def __enter__(self):
        for name in ("set_module", "clear_one", "clear_for", "overrides_for", "list_all"):
            self._started.append(patch.object(account_modules, name,
                                              getattr(self._probe, name)))
            self._started[-1].start()
        return self._probe

    def __exit__(self, *exc):
        for p in reversed(self._started):
            p.stop()
        return False


class SelfServiceToggleTests(unittest.TestCase):
    """`PUT /api/settings/modules/{key}` — the caller's own three states."""

    def _put(self, body, identity=MEMBER):
        probe = _Probe()
        with _Deployment(), _Patched(probe):
            resp = _client(identity).put("/api/settings/modules/" + _TOGGLEABLE, json=body)
        return resp, probe

    def test_true_writes_a_row(self):
        resp, probe = self._put({"enabled": True})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("set", USER_ID, _TOGGLEABLE, True)])

    def test_false_writes_a_row(self):
        resp, probe = self._put({"enabled": False})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("set", USER_ID, _TOGGLEABLE, False)])

    def test_null_drops_the_row_and_writes_nothing(self):
        """⚠️ THE third state. `null` must not arrive as `False`: "closed for this
        person" and "nobody has said anything" are different facts about the account, and
        only one of them is a row that outlives the deployment setting."""
        resp, probe = self._put({"enabled": None})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("clear_one", USER_ID, _TOGGLEABLE)])
        self.assertNotIn(("set", USER_ID, _TOGGLEABLE, False), probe.calls)

    def test_an_empty_body_is_the_third_state_too(self):
        """A form that posts `{}` is asking "no change", and the honest reading of "no
        change" for a tri-state control is inherit — not `bool(None) == False`."""
        resp, probe = self._put({})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("clear_one", USER_ID, _TOGGLEABLE)])


class SelfServiceDeleteTests(unittest.TestCase):
    """`DELETE /api/settings/modules/{key}` — reset ONE module.

    ⚠️ This is the route that shipped calling `clear_for`. The URL, the handler name and
    the docstring all say one module; only the body disagreed, and the response (the
    caller's whole override map) could not be used to tell, because the caller's other
    rows were gone.
    """

    def test_it_clears_one_module_not_the_whole_row(self):
        probe = _Probe()
        with _Patched(probe):
            resp = _client(MEMBER).delete("/api/settings/modules/" + _TOGGLEABLE)
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("clear_one", USER_ID, _TOGGLEABLE)])
        self.assertNotIn(("clear_for", USER_ID), probe.calls,
                         "⚠️ the whole row went: every other narrowing this account had "
                         "was discarded by a click that named one module")


class AdminToggleTests(unittest.TestCase):
    """`PUT /api/settings/modules/accounts/{user_id}/{key}` — the matrix cell."""

    def _put(self, body, key=None, identity=OPERATOR):
        probe = _Probe()
        with _Deployment(), _Patched(probe):
            resp = _client(identity).put(
                "/api/settings/modules/accounts/%d/%s" % (USER_ID, key or _TOGGLEABLE),
                json=body)
        return resp, probe

    def test_null_is_the_third_state(self):
        resp, probe = self._put({"enabled": None})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("clear_one", USER_ID, _TOGGLEABLE)])

    def test_a_member_cannot_reach_the_admin_matrix(self):
        resp, probe = self._put({"enabled": False}, identity=MEMBER)
        self.assertEqual(resp.status_code, 403, resp.text[:200])
        self.assertEqual(probe.calls, [], "the refusal has to happen before the write")

    def test_a_module_the_deployment_withdrew_cannot_be_granted(self):
        resp, probe = self._put({"enabled": True}, key=_OFF_SALE)
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        self.assertEqual(probe.calls, [], "a row that reads as a grant was refused; it "
                                          "must not be written either")

    def test_but_it_can_still_be_cleared(self):
        """⚠️ Ordering, and it matters in both directions. Narrowing for a module the
        deployment has since withdrawn is exactly the stale row worth cleaning up, so
        the "this deployment does not offer it" rule must gate the GRANT and not the
        CLEAR. Getting this backwards leaves a company permanently unable to tidy up
        after the operator turns a module off."""
        resp, probe = self._put({"enabled": None}, key=_OFF_SALE)
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("clear_one", USER_ID, _OFF_SALE)])

    def test_an_unknown_module_is_404_before_anything_is_written(self):
        resp, probe = self._put({"enabled": True}, key="not-a-module")
        self.assertEqual(resp.status_code, 404, resp.text[:200])
        self.assertEqual(probe.calls, [])


class RequiredModuleTests(unittest.TestCase):
    """A `required` module cannot be CLOSED — but it can be released back to the
    default, which is not the same act and used to be refused by the same check."""

    def test_false_is_refused(self):
        probe = _Probe()
        with _Deployment(), _Patched(probe):
            resp = _client(OPERATOR).put(
                "/api/settings/modules/accounts/%d/%s" % (USER_ID, _REQUIRED),
                json={"enabled": False})
        self.assertEqual(resp.status_code, 400, resp.text[:200])
        self.assertEqual(probe.calls, [])

    def test_null_is_allowed(self):
        """⚠️ A required module is one the deployment cannot run without, so the account
        cannot be *denied* it. Releasing an override on it is the opposite: it hands the
        decision back to the deployment, which is the only party entitled to make it.
        Refusing that too leaves a row nobody can ever remove."""
        probe = _Probe()
        with _Deployment(), _Patched(probe):
            resp = _client(OPERATOR).put(
                "/api/settings/modules/accounts/%d/%s" % (USER_ID, _REQUIRED),
                json={"enabled": None})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(probe.calls, [("clear_one", USER_ID, _REQUIRED)])


if __name__ == "__main__":
    unittest.main()
