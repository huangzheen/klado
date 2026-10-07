"""The admin console, without a server.

`verify_admin_console_http.py` proves the console is a real second process and that the
audience boundary holds. It cannot reach inside, and the things worth pinning here are
exactly the things inside:

* **the route table** — that every operator route lives under one prefix, that none of
  them is a main-app path, and that the one route allowed to run while the console is
  read-only is still exactly one route;
* **the two-layer gate** — `require_operator` for reads, `require_write` for changes, and
  the ordering between the 401, the 403 and the switch;
* **the SQL behind the console's new queries** — the counts on the organizations list, the
  one-statement transfer, and the validators `update_org` runs before it writes;
* **the deployment settings** — the blocklist cleaner, and the fact that it is a policy
  and not a boundary.

⚠️ The recurring lesson from this project is that a suite which only ever fakes the layer
*below* the thing it is testing passes against a build with the interesting rule deleted.
`test_org_admin.py` faked `klado_shared.orgs` and so skipped every rule inside it;
`test_org_members.py` then found seven mutations it had missed. This file fakes the
*database* and the *session*, and nothing else — the routers, the gate and the service
layer all run for real.

No database, no server, no `api-admin` import of `api.*`.
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
CONSOLE_DIR = os.path.join(REPO_DIR, "api-admin")
# ⚠️ The order is *set*, not merely appended, and that distinction is the whole reason
# this block is more than three lines.
#
# `api/main.py` and `api-admin/main.py` are both top-level files named `main.py`, and
# `sys.path` order decides which one a bare `import main` finds. Adding the console's
# directory at the front made every `import main` in the suite — eleven tests in
# `test_annotations` and `test_report_state` among them — resolve to the console. They
# passed alone and failed in a full run, which is the most expensive kind of failure to
# diagnose.
#
# A conditional `insert(0)` is not enough, and the first version of this block got that
# wrong twice. It does not fire at all for a path some earlier test already added — and
# `test_shared_layer.py` adds `api/` — so whether the console ended up first depended on
# which module happened to be imported first. So: drop all three, then put them back in a
# fixed order. REPO → API → CONSOLE. The console goes last, which is all it needs —
# `admin_console` is a uniquely named package and resolves from anywhere on the path.
for _p in (REPO_DIR, API_DIR, CONSOLE_DIR):
    while _p in sys.path:
        sys.path.remove(_p)
for _p in (CONSOLE_DIR, API_DIR, REPO_DIR):
    sys.path.insert(0, _p)

from fastapi import HTTPException                           # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402

from admin_console import access as console_access           # noqa: E402


def _load_console_app():
    """Import `api-admin/main.py` under a name of our choosing.

    ⚠️ **Not** `import main`. Both entry points are called `main.py` and both are top-level
    files, so a plain import registers the console's as `sys.modules["main"]` — and every
    later test that says `import main` expecting the *app* silently gets the console
    instead. The symptom is not a failure in this file: it is eleven errors in
    `test_annotations` and `test_report_state` that pass on their own and fail in a full
    run, which is about as expensive a bug to diagnose as this codebase has produced.

    Loading it by path under a private name is what makes the console importable *beside*
    the app rather than instead of it — the same reason `admin_console/` is not called
    `routers/`.
    """
    import importlib.util
    path = os.path.join(CONSOLE_DIR, "main.py")
    spec = importlib.util.spec_from_file_location("klado_admin_console_app", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


console_main = _load_console_app()
from klado_shared import accounts as console_accounts      # noqa: E402
from klado_shared import deployment, orgs                   # noqa: E402
from klado_shared.config import settings                    # noqa: E402

OPERATOR = {"id": 1, "email": "root@platform.example", "role": "admin", "disabled": False}
PLAIN_USER = {"id": 2, "email": "mate@corp.example", "role": "user", "disabled": False}


# ── the fake database ────────────────────────────────────────────────────────

class _Cursor:
    """Replays a scripted list of rows, one per `execute`, recording every statement.

    ⚠️ Scripted per statement, not a constant. The console's organization list is five
    correlated subqueries, and a fake that always answered the same thing would make a
    query that dropped two of them look fine.
    """

    def __init__(self, rows, log):
        self._rows, self._log, self._i = list(rows), log, 0
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._log.append((" ".join((sql or "").split()), params))
        self._rows_i = self._rows[self._i] if self._i < len(self._rows) else []
        self._i += 1
        self.rowcount = len(self._rows_i)
        return self

    def fetchone(self):
        return self._rows_i[0] if self._rows_i else None

    def fetchall(self):
        return self._rows_i


class _Conn:
    def __init__(self, rows, log):
        self._rows, self._log = rows, log

    def cursor(self, *a, **kw):
        return _Cursor(self._rows, self._log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass


def with_db(rows):
    """Patch the orgs module's connection with a scripted one. Yields the SQL log."""
    log: list = []

    class _Ctx:
        def __enter__(self_inner):
            self_inner.patcher = patch.object(orgs, "_db", return_value=_Conn(rows, log))
            self_inner.patcher.start()
            return log

        def __exit__(self_inner, *exc):
            self_inner.patcher.stop()
            return False

    return _Ctx()


# ── the route table ──────────────────────────────────────────────────────────

class RouteTableTests(unittest.TestCase):
    """What the console exposes, and — more importantly — what it does not."""

    ROUTES = [r for r in console_main.app.routes
              if getattr(r, "path", "").startswith("/api/admin-console/")]

    def test_there_are_routes(self):
        self.assertGreater(len(self.ROUTES), 20, len(self.ROUTES))

    def test_every_api_route_is_under_the_console_prefix(self):
        """A console route outside the prefix would be reachable at a path the main app
        also uses, and the two would answer on the same URL on different ports — which is
        the one shape of overlap this whole design exists to avoid."""
        others = [r.path for r in console_main.app.routes
                  if getattr(r, "path", "").startswith("/api/")
                  and not r.path.startswith("/api/admin-console/")]
        self.assertEqual(others, [],
                         f"routes outside /api/admin-console/: {others}")

    def test_no_main_app_route_is_mounted(self):
        """The boundary, stated as a fact about the live route table rather than as a
        static scan. If somebody adds `app.include_router(reports.router)` here, the main
        app's entire surface becomes reachable on the console's port."""
        main_only = {"/api/reports", "/api/data-center/datasets", "/api/calendar",
                     "/api/auth/me", "/api/inbox", "/api/dashboard"}
        mounted = {r.path for r in console_main.app.routes}
        self.assertEqual(sorted(main_only & mounted), [])

    def test_the_read_only_escape_hatch_is_used_exactly_once(self):
        """⚠️ One route may run while the console is read-only: the one that turns it back
        on. Two would mean the switch is not a control; zero would make it a one-way door,
        and the operator who used it in an incident would be locked out of the only place
        that can undo it.

        Parsed with `ast`, not grepped. A substring search for `exempt_switch=True` also
        matches the parameter's own definition and the three places the docstring spells
        the call out — which is three false positives on the very first run, and a guard
        that cries wolf gets deleted. The assertion is on the *call*, and the exact list is
        compared so the name of the one permitted route is pinned too.
        """
        found = exempt_switch_call_sites()
        self.assertEqual(
            [f"{os.path.relpath(path, REPO_DIR)}:{line}" for path, line in found],
            [f"{os.path.relpath(os.path.join(CONSOLE_DIR, 'admin_console', 'system.py'), REPO_DIR)}"
             f":{_line_of_escape()}"],
            "exactly one call site may pass exempt_switch=True, and it must be the one "
            "that turns the read-only switch back on")


    def test_loading_the_console_did_not_shadow_the_apps_main_module(self):
        """⚠️ A self-check on the loader above, because the damage is invisible from here.

        Both entry points are `main.py` and both are top-level. A plain `import main` in
        this file registers the console's under `sys.modules["main"]`, and the eleven tests
        in `test_annotations` / `test_report_state` that then say `import main` get the
        console instead — errors in files that pass on their own, in a full run only. This
        asserts the absence of that, so the loader cannot be "simplified" back.
        """
        import main as app_main
        self.assertTrue(app_main.__file__.endswith("api/main.py"),
                        f"sys.modules['main'] is {app_main.__file__}, not the app's")
        self.assertIsNot(app_main, console_main)

    def test_the_cookie_name_is_not_the_main_apps(self):
        """Cookies are matched on host and ignore the port, so a shared name means the two
        processes overwrite each other's login. The audience is the real control; this is
        the cheap half, and it is the half a future edit gets wrong by copying a line."""
        self.assertNotEqual(console_access.SESSION_COOKIE, "klado_session")
        self.assertEqual(console_access.SESSION_COOKIE,
                         settings.KLADO_ADMIN_SESSION_COOKIE)


def exempt_switch_call_sites() -> list:
    """`(path, lineno)` for every `require_write(..., exempt_switch=True)` call.

    ⚠️ An AST walk rather than a regex, for the reason in the test above: the substring
    `exempt_switch=True` appears in the signature's default, in the docstring and in
    comments, and a check that matches those fails on a correct file.
    """
    import ast
    found = []
    for dirpath, dirs, files in os.walk(CONSOLE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name_of = getattr(func, "attr", None) or getattr(func, "id", None)
                if name_of != "require_write":
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "exempt_switch" and \
                            isinstance(keyword.value, ast.Constant) and \
                            keyword.value.value is True:
                        found.append((path, node.lineno))
    return found


def _line_of_escape() -> int:
    """The line of the one permitted call, read the same way the guard reads it.

    ⚠️ Deriving it independently with a substring search is what made the first version of
    this test report a mismatch: the search found the docstring's mention of the call, the
    AST found the call, and the two disagreed on a line number for a file that was correct.
    """
    sites = exempt_switch_call_sites()
    return sites[0][1] if sites else -1


# ── the gate ─────────────────────────────────────────────────────────────────

class GateTests(unittest.TestCase):
    """401 for nobody, 403 for the wrong person, 403 again when the console is read-only.

    Driven through the real app with only the *session* faked, so the middleware, the
    router and the gate all execute.
    """

    def test_a_read_with_no_session_is_401(self):
        client = TestClient(console_main.app)
        with patch.object(console_access, "current_operator", return_value=None):
            resp = client.get("/api/admin-console/overview")
        self.assertEqual(resp.status_code, 401, resp.text[:200])

    def test_a_read_by_a_non_operator_is_403(self):
        client = TestClient(console_main.app)
        with patch.object(console_access, "current_operator", return_value=PLAIN_USER):
            resp = client.get("/api/admin-console/overview")
        self.assertEqual(resp.status_code, 403, resp.text[:200])

    def test_a_write_while_read_only_is_403(self):
        """The switch is in `require_write` and not in `require_operator` — so this is
        403 while the read above is still 200. That asymmetry is the whole design of the
        switch, and a suite that only checked one of them would pass against a version
        that had put it in the wrong function (making the console a lockout)."""
        user = OPERATOR
        client = TestClient(console_main.app)
        with patch.object(console_access, "require_operator", return_value=user), \
             patch.object(deployment, "console_login_enabled", return_value=False), \
             patch.object(deployment, "read", return_value=None):
            resp = client.get("/api/admin-console/modules")
            write = client.put("/api/admin-console/modules", json={"keys": []})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(write.status_code, 403, write.text[:200])

    def test_the_console_refuses_a_pre_upgrade_token_at_its_own_call_site(self):
        """⚠️ `allow_legacy=False` has to be pinned at the CALL SITE, not only inside
        `klado_shared/session.py`. The session tests prove the function honours the flag;
        nothing proved the console passes it, and flipping it to `True` is a one-word edit
        that would let any pre-audience token from the main app walk straight into the
        operator console — the exact hole the audience exists to close. This mutation
        survived the whole console suite until it was added.
        """
        import inspect
        source = inspect.getsource(console_access.current_operator)
        self.assertIn("ADMIN_AUDIENCE", source)
        self.assertIn("allow_legacy=False", source,
                      "the console must refuse a token with no audience")

    def test_the_gate_checks_the_session_before_the_role(self):
        """A missing session is 401 whether or not the switch is on, and a signed-in
        non-operator is 403 whether or not it is on. Reversing those two would turn the
        switch into a probe for which addresses have accounts."""
        with patch.object(console_access, "current_operator", return_value=None), \
             patch.object(deployment, "console_login_enabled", return_value=True):
            self.assertEqual(_status(lambda: console_access.require_operator(_req())), 401)
        with patch.object(console_access, "current_operator", return_value=PLAIN_USER), \
             patch.object(deployment, "console_login_enabled", return_value=True):
            self.assertEqual(_status(lambda: console_access.require_operator(_req())), 403)
        with patch.object(console_access, "current_operator", return_value=OPERATOR), \
             patch.object(deployment, "console_login_enabled", return_value=True):
            self.assertEqual(_status(lambda: console_access.require_operator(_req())), 200)

    def test_require_write_is_require_operator_plus_the_switch(self):
        with patch.object(console_access, "current_operator", return_value=OPERATOR), \
             patch.object(deployment, "console_login_enabled", return_value=False):
            self.assertEqual(_status(lambda: console_access.require_write(_req())), 403)
        with patch.object(console_access, "current_operator", return_value=OPERATOR), \
             patch.object(deployment, "console_login_enabled", return_value=True):
            self.assertEqual(_status(lambda: console_access.require_write(_req())), 200)
        # …and the escape hatch gets past it, which is the difference between a switch
        # and a lockout.
        with patch.object(console_access, "current_operator", return_value=OPERATOR), \
             patch.object(deployment, "console_login_enabled", return_value=False):
            self.assertEqual(
                _status(lambda: console_access.require_write(_req(), exempt_switch=True)),
                200)


def _req():
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [],
             "client": ("127.0.0.1", 1), "query_string": b""}
    return Request(scope)


def _status(fn) -> int:
    try:
        fn()
        return 200
    except Exception as exc:  # noqa: BLE001 — HTTPException carries the status
        return getattr(exc, "status_code", 500)


# ── the queries behind the console's new pages ───────────────────────────────

class OrgConsoleQueryTests(unittest.TestCase):
    """The SQL the console issues. A fake database, real service code."""

    def test_list_orgs_asks_for_each_number_it_displays(self):
        """Every column the list renders is its own subquery.

        ⚠️ Asserting only the ALIASES is not enough, and that is not a hypothetical: a
        mutation that replaced the admin count's `status = 'active' AND org_role IN
        ('owner','admin')` filter with a plain `count(*)` kept every alias, so the whole
        test passed while the page reported the same number for "members" and "admins" —
        a number that cannot be reconciled with the roster it links to. So the *filter*
        is asserted too, and separately from the alias.
        """
        with with_db([[]]) as log:
            orgs.list_orgs()
        sql = log[0][0]
        for alias in ("AS members", "AS invited", "AS admins", "AS pending_approvals",
                      "AS admin_emails"):
            self.assertIn(alias, sql, f"missing {alias} in:\n{sql}")

    def test_the_admin_count_is_not_the_member_count(self):
        """`admins` counts active owners and admins; `members` counts the whole roster.

        Collapsing the first into the second is the mistake the column exists to prevent,
        and it is invisible in the rendered page: both numbers are integers and both
        render. It only shows up when somebody compares the card against the roster.
        """
        with with_db([[]]) as log:
            orgs.list_orgs()
        sql = log[0][0]
        admin_clause = sql[sql.index("AS admins") - 200:sql.index("AS admins")]
        self.assertIn("org_role IN ('owner', 'admin')", admin_clause,
                      f"the admins count is not restricted to owners and admins:\n"
                      f"{admin_clause}")
        self.assertIn("status = 'active'", admin_clause,
                      f"the admins count includes disabled and merely-invited rows:\n"
                      f"{admin_clause}")

    def test_the_totals_card_asks_for_the_three_kinds_separately(self):
        """`enterprises` / `personal_users` / `suspended` are three `count(*) FILTER`
        clauses, so one statement answers all three. Deriving any of them in Python from
        a list the query already truncated is how a card stops matching its own link."""
        sql = org_totals_sql(orgs)
        for clause in ("kind = 'enterprise'", "kind = 'personal'", "status = 'suspended'"):
            self.assertIn(clause, sql, f"missing the {clause} count in:\n{sql}")

    def test_a_free_text_search_does_not_multiply_the_rows(self):
        """The member arm of the query is an EXISTS, not a JOIN. A JOIN would count the
        same organization once per matching member and inflate every number on the row."""
        with with_db([[]]) as log:
            orgs.list_orgs(q="acme")
        sql = log[0][0]
        self.assertIn("EXISTS", sql)
        self.assertNotIn("JOIN org_members m2", sql)
        self.assertEqual(len(log), 1, "the filter must not add a second statement")

    def test_an_unknown_kind_is_refused_rather_than_filtered_to_nothing(self):
        with self.assertRaises(orgs.OrgError):
            orgs.list_orgs(kind="not-a-kind")

    def test_update_org_refuses_to_empty_the_suffix_list(self):
        """⚠️ The domains are the company's identity. Clearing them means nobody can be
        invited and nobody new can be matched to it — which reads as a settings mistake
        and locks the company out of its own administration. Narrowing is supported;
        emptying is not."""
        with self.assertRaises(orgs.OrgError) as caught:
            orgs.update_org(1, email_domains=[])
        self.assertIn("至少要保留", str(caught.exception))

    def test_update_org_ignores_an_absent_slug(self):
        """There is no slug parameter at all, so this is really an assertion that the
        signature does not offer one. The slug is in every shared link already handed out;
        a field that edits it is a set of 404s nobody can predict."""
        import inspect
        params = set(inspect.signature(orgs.update_org).parameters)
        self.assertNotIn("slug", params)
        # …and the shape is pinned too, so "add a slug parameter" cannot be smuggled in
        # with a small, otherwise-harmless-looking edit.
        self.assertEqual(params - {"org_id", "kwargs"},
                         {"name", "email_domains", "share_scope", "public_scope",
                          "status"})

    def test_update_org_validates_before_it_writes(self):
        with with_db([[{"id": 1, "name": "Acme"}]]) as log:
            with self.assertRaises(orgs.OrgError):
                orgs.update_org(1, share_scope="nonsense")
        self.assertEqual(log, [], "a refused change must not have issued any SQL")

    def test_a_transfer_demotes_before_it_promotes(self):
        """⚠️ Two UPDATEs in one transaction, in that order. The other way round passes
        through a state with no owner, which is precisely the state
        `_refuse_removing_last_admin` exists to make unreachable — so doing it in this
        order is what lets that guard be obeyed rather than bypassed."""
        # ⚠️ Three scripted rows for three statements, and the third is a
        # `... RETURNING *` — it needs a row back or the function raises on `dict(None)`.
        # The first version of this fixture gave it none, so the test failed on the
        # fixture rather than on the ordering it was written to check.
        rows = [[{"id": 5, "org_id": 3, "status": "active", "org_role": "member"}],
                [],
                [{"id": 5, "org_id": 3, "org_role": "owner"}]]
        with with_db(rows) as log:
            orgs.transfer_org_owner(3, "new@corp.example")
        statements = [sql for sql, _ in log]
        self.assertEqual(len(statements), 3, statements)
        self.assertIn("SET org_role = 'admin'", statements[1])
        self.assertIn("SET org_role = 'owner'", statements[2])

    def test_a_transfer_refuses_an_address_that_is_not_an_active_member(self):
        rows = [[{"id": 5, "org_id": 3, "status": "invited", "org_role": "member"}]]
        with with_db(rows):
            with self.assertRaises(orgs.OrgError) as caught:
                orgs.transfer_org_owner(3, "pending@corp.example")
        self.assertIn("接受邀请", str(caught.exception))

    def test_personal_users_keep_their_row_after_the_account_is_closed(self):
        """A LEFT JOIN, deliberately. An INNER JOIN would hide every closed self-registered
        account, and the overview's personal-user count would stop matching the recycle
        bin — with nothing to say why."""
        with with_db([[]]) as log:
            orgs.list_personal_members()
        sql = log[0][0]
        self.assertIn("LEFT JOIN app_users", sql)
        self.assertIn("o.kind = 'personal'", sql)


def org_totals_sql(_module) -> str:
    with with_db([[{"enterprises": 1, "personal_orgs": 2, "suspended": 0}],
                  [{"total": 3, "active": 2, "invited": 1}],
                  [{"pending": 0}]]) as log:
        _module.org_totals()
    return "\n".join(sql for sql, _ in log)


class OrgTotalsTests(unittest.TestCase):
    def test_the_pending_queue_is_counted_independently_of_the_organization_list(self):
        """An approval whose organization is suspended still belongs in an operator's
        queue. Deriving the count from `list_orgs()` would filter exactly the cases worth
        looking at."""
        with with_db([[{"enterprises": 1, "personal_orgs": 0, "suspended": 1}],
                      [{"total": 1, "active": 1, "invited": 0}],
                      [{"pending": 4}]]) as log:
            totals = orgs.org_totals()
        self.assertEqual(totals["pending_approvals"], 4)
        self.assertEqual(totals["suspended_orgs"], 1)
        self.assertEqual(len(log), 3, "three independent counts, three statements")


# ── the deployment settings ──────────────────────────────────────────────────

class LimiterTests(unittest.TestCase):
    """The login limiter, and the two buckets it keeps.

    ⚠️ The IP-clearing case is here because of a real cross-tool failure, not a
    hypothetical: the counters are in-process, so the burst in
    `verify_admin_console_http.py` used to leave the console refusing *every* login from
    127.0.0.1 for five minutes — including the screenshot script's correct password,
    twenty seconds later, with nothing in either tool's output saying why. A successful
    login is evidence the person is not guessing, which is the only thing the IP limit
    exists to establish.
    """

    def setUp(self):
        console_access._hits.clear()

    def tearDown(self):
        console_access._hits.clear()

    def test_attempts_are_counted_in_both_buckets(self):
        request = _req()
        for _ in range(2):
            console_access.throttle_login("a@x.test", request)
        self.assertEqual(len(console_access._hits.get("email:a@x.test", [])), 2)
        self.assertTrue(any(k.startswith("ip:") for k in console_access._hits),
                        list(console_access._hits))

    def test_a_successful_login_clears_both(self):
        request = _req()
        for _ in range(3):
            console_access.throttle_login("a@x.test", request)
        console_access.clear_login_throttle("a@x.test", console_access.client_ip(request))
        self.assertNotIn("email:a@x.test", console_access._hits)
        self.assertFalse([k for k in console_access._hits if k.startswith("ip:")],
                         list(console_access._hits))
        # …and the next attempt is accepted again, which is the whole point.
        console_access.throttle_login("a@x.test", request)
        self.assertEqual(len(console_access._hits["email:a@x.test"]), 1)

    def test_clearing_one_address_leaves_the_others(self):
        request = _req()
        console_access.throttle_login("a@x.test", request)
        console_access.throttle_login("b@x.test", request)
        console_access.clear_login_throttle("a@x.test", console_access.client_ip(request))
        self.assertNotIn("email:a@x.test", console_access._hits)
        self.assertIn("email:b@x.test", console_access._hits)

    def test_a_legitimate_run_of_logins_never_locks_the_account_out(self):
        """The scenario the two rules above exist for: six correct logins in five minutes
        is a person with two browsers, not an attack."""
        request = _req()
        for _ in range(6):
            console_access.throttle_login("a@x.test", request)
            console_access.clear_login_throttle("a@x.test",
                                                console_access.client_ip(request))
        self.assertEqual(_status(lambda: console_access.throttle_login("a@x.test", request)),
                         200)

    def test_a_run_of_failures_does_lock_it_out(self):
        """…and the control is still a control."""
        request = _req()
        codes = [_status(lambda: console_access.throttle_login("a@x.test", request))
                 for _ in range(10)]
        self.assertIn(429, codes, codes)

    def test_the_ip_is_derived_one_way(self):
        """`x-forwarded-for` parsing lives in exactly one function, because the login and
        its cleanup have to agree on the bucket key. Two copies is two copies that can
        disagree, and when they do the "success resets the counter" rule silently stops
        firing."""
        import inspect
        source = inspect.getsource(console_access.throttle_login)
        self.assertNotIn("x-forwarded-for", source,
                         "throttle_login must call client_ip(), not re-parse the header")
        self.assertIn("client_ip(request)", source)


class BlocklistTests(unittest.TestCase):
    """The cleaner is pure; the read/write is faked at the connection."""

    def test_it_cleans_what_a_text_box_produces(self):
        cases = {
            "gmail.com": "gmail.com",
            "  Gmail.COM  ": "gmail.com",
            "@gmail.com": "gmail.com",
            "user@gmail.com": "gmail.com",
            # A pasted URL is DROPPED, not parsed. `_clean_domain` is a domain cleaner,
            # not a URL parser: stripping the scheme and path here would mean accepting
            # whatever else is in the string, and a blocklist that silently keeps part of
            # a paste is worse than one that drops the line and says so — which is what
            # `set_blocked_email_domains` returns to the page.
            "https://gmail.com/inbox": "",
            ".gmail.com.": "gmail.com",
            "": "",
            "notadomain": "",
            "has space.com": "",
            None: "",
            42: "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(deployment._clean_domain(raw), expected)

    def test_a_subdomain_is_not_blocked_by_its_parent(self):
        """Matching suffixes would block every address on any domain that merely *ends*
        with the string, which is not what "block the public mail providers" means."""
        stored = {"domains": ["gmail.com"]}
        with patch.object(deployment, "read", return_value=stored):
            self.assertTrue(deployment.is_blocked_domain("gmail.com"))
            self.assertFalse(deployment.is_blocked_domain("evil.gmail.com"))
            self.assertFalse(deployment.is_blocked_domain("gmail.com.evil.test"))

    def test_unreadable_settings_fall_back_to_the_defaults(self):
        """The console must come up on a database where nothing has been written yet, and
        a settings read must never be the reason an operator cannot look at anything."""
        with patch.object(deployment, "read", return_value=None):
            self.assertEqual(deployment.blocked_email_domains(),
                             list(deployment.DEFAULT_BLOCKED_DOMAINS))
        # ⚠️ Break the *connection*, not `read` itself. Replacing `read` with something that
        # raises tests the mock, not the fail-soft path — and the first version of this
        # test did exactly that, so the exception escaped and the assertion it was
        # written to make never ran.
        with patch.object(deployment, "_db", side_effect=RuntimeError("no database")):
            self.assertEqual(deployment.blocked_email_domains(),
                             list(deployment.DEFAULT_BLOCKED_DOMAINS))

    def test_the_default_is_unreadable_settings_is_not_a_lockout(self):
        """⚠️ Written after a real bug, and every case in it is one that used to answer
        "off". The switch read the row and did `bool(value)`, so a missing row, a row
        holding JSON `null`, and a database that could not be reached all turned the
        console read-only — silently, with no error, and with no way back for the operator
        who had not switched it off. Only a stored `false` may take authority away.
        """
        for label, stored in (("a missing row", None), ("a row holding null", None)):
            with self.subTest(label=label):
                with patch.object(deployment, "read", return_value=stored):
                    self.assertTrue(deployment.console_login_enabled())
        with patch.object(deployment, "read", return_value={}):
            self.assertTrue(deployment.console_login_enabled())
        with patch.object(deployment, "_db", side_effect=RuntimeError("boom")):
            self.assertTrue(deployment.console_login_enabled())
        with patch.object(deployment, "read", return_value={"enabled": False}):
            self.assertFalse(deployment.console_login_enabled())

    def test_the_write_returns_what_was_actually_stored(self):
        """A text box where somebody typed `Gmail.com, @163.com` and left a blank line is
        going to happen. Echoing back the *kept* list is the only way the operator learns
        which line was dropped."""
        with patch.object(deployment, "ensure_table"), \
             patch.object(deployment, "write") as writer:
            kept = deployment.set_blocked_email_domains(
                ["Gmail.com", " @163.com ", "", "gmail.com", "junk"], actor="Root@X.test")
        self.assertEqual(kept, ["gmail.com", "163.com"])
        # ⚠️ The *stored* payload is what is asserted, not the argument — this function's
        # contract is "here is what is now in force". The actor is lowercased one layer
        # down, inside `write`, and asserting it here would be asserting that a mock was
        # called, which is not a fact about the product.
        self.assertEqual(writer.call_args.args[1], {"domains": kept})


# ── the process boundary, restated where it can be checked statically ─────────

class ConsoleBoundaryTests(unittest.TestCase):
    def test_the_console_imports_no_main_app_module(self):
        """The rule `klado_shared` exists to enforce, asserted here as well as in
        `test_shared_layer.py` — near the console's own tests, so somebody editing a
        console router sees why the import cannot be added."""
        offenders = []
        for dirpath, dirs, files in os.walk(CONSOLE_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as handle:
                    for lineno, line in enumerate(handle, 1):
                        if re.match(r"^\s*(import\s+api\b|from\s+api[.\s])", line):
                            offenders.append(f"{name}:{lineno} {line.strip()}")
        self.assertEqual(offenders, [])

    def test_the_console_bootstrap_puts_the_repository_root_on_the_path(self):
        """Derived from its own file, not from the cwd — the two entry points are started
        from different directories, and a cwd-relative lookup would find nothing in one of
        them."""
        with open(os.path.join(CONSOLE_DIR, "_shared_path.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("os.path.abspath(__file__)", source)
        self.assertNotIn("os.getcwd", source)

    def test_the_console_does_not_mount_the_main_apps_frontend(self):
        """Serving the SPA from the console's own directory is what makes it a separate
        program. Pointing it at `frontend/out` would put the main app's 15,600 lines back
        on the console's port, which is the thing this phase exists to undo."""
        with open(os.path.join(CONSOLE_DIR, "main.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('"frontend", "out"', source)
        self.assertNotIn("resolve_frontend_dir", source)
        self.assertIn('"frontend"', source)


# ── the copy itself: a pair the splitter cannot split is a bilingual string ─────

#: The split rule, copied **character for character** from `api-admin/frontend/i18n.js` —
#: including its `\u4e00-\u9fff` escapes and its escaped slash, so that
#: `test_the_rule_here_is_the_rule_in_the_browser` is a string equality check rather than
#: a comparison of two things that merely look alike. (Writing the range as literal CJK
#: here was the first version, and it failed that test: same rule, different bytes, and a
#: transcription that cannot be compared is not a guard.)
#: ⚠️ It is restated rather than imported because `i18n.js` is browser code (an IIFE that
#: wants a `window`). The Python half is `core/i18n.py::_PAIR`, and the three copies are
#: documented as one rule.
PAIR = re.compile("([\\u4e00-\\u9fff][^/]*?)\\s\\/\\s([A-Za-z][^/]*)")

CJK = re.compile(r"[一-鿿]")


class ConsoleCopyTests(unittest.TestCase):
    """Every `中文 / English` string in the console has to be splittable.

    ⚠️ **This class exists because 25 of the console's strings were not.** Every one ended
    its Chinese half in a full stop — `…锁在自己外面。/ One per line.` — so the character
    before the slash was `。`, not a space, and the rule's `\s/\s` never matched. The string
    was returned whole and the reader saw both languages at once, on 25 lines of the live
    console. A screenshot test cannot catch it: the page renders, the layout is right, and
    two languages side by side looks like a translation choice.

    Two more ways to write an unsplittable pair, both found the same day:

    * **A slash inside either half.** `[^/]` is the whole reason a pair is a pair, so
      `edit its file under api/knowledge_docs` cannot match at all — not even after the
      space is added, and the fix that looks right is the one that makes it worse. A path
      is a *code sample*, not copy: it belongs in a `<code>` element built with `esc()`,
      which the walker does not touch, and never inside `T()`.
    * **A wrong separator.** `？/ Revoke` reads fine to a human and is invisible to the
      rule, which requires a space on **both** sides.
    """

    PAGE = os.path.join(CONSOLE_DIR, "frontend", "index.html")

    def _strings(self):
        with open(self.PAGE, encoding="utf-8") as handle:
            source = handle.read()
        return re.findall(r"'((?:[^'\\\n]|\\.)*)'", source), source

    def test_every_pair_in_the_console_splits_into_one_language_each(self):
        strings, _ = self._strings()
        broken = []
        for text in strings:
            match = PAIR.search(text)
            if not match:
                continue
            zh, en = match.group(1).strip(), match.group(2).strip()
            problems = []
            if CJK.search(en):
                problems.append("英文侧混入中文")
            if re.search(r"\b(?:the|and|is|are|to|of|it|this|not|only|by)\b", zh, re.I):
                problems.append("中文侧混入英文")
            # A pair whose English half ends mid-sentence is the tell for a slash inside
            # the text: `[^/]*` cannot cross one, so the half stops at the path.
            tail = text[match.end():].strip()
            if tail and not re.fullmatch(r"[。．.，,；;：:！!？?]+\s*", tail):
                problems.append(f"英文侧被句中斜杠截断，剩「{tail[:40]}」")
            if problems:
                broken.append(f"{'、'.join(problems)}：{text[:70]}")
        self.assertEqual([], broken,
                         "以下文案在页面上会中英并排：\n  " + "\n  ".join(broken))

    def test_no_copy_string_puts_a_separator_that_cannot_be_matched(self):
        """The other half of the same defect: a string that *means* to be a pair but whose
        separator the rule cannot see. The reader sees two languages; the compiler sees
        one string, so nothing complains.

        `*/` is excluded because it ends a block comment, and a comment is not copy."""
        strings, _ = self._strings()
        offenders = []
        for text in strings:
            if CJK.search(text) and re.search(r"(?<=[^\s/*])/\s[A-Za-z]", text):
                offenders.append(text[:80])
        self.assertEqual([], offenders,
                         "分隔符左侧缺少空格（或用了 */），整串不会被拆开：\n  "
                         + "\n  ".join(offenders))

    def test_copy_assembled_by_concatenation_is_still_just_one_string(self):
        """⚠️ The hole the two tests above walk straight into.

        They scan **single string literals**. Copy written as `T('第一段。' + '第二段。')` is
        two literals to that regex, and neither one looks like a pair — so a bilingual
        string assembled by concatenation is invisible to every other check in this class.
        That is not hypothetical: the accounts footer was written exactly that way, without
        the ` / ` separator, and shipped to the page with both languages printed side by
        side while this class reported nothing.

        So the fragments are joined first and *then* judged. The tell for "this is copy and
        it did not split" is a CJK run followed by a real English sentence with no
        separator between them.
        """
        _, source = self._strings()
        broken = []
        for expr in re.findall(r"T\(((?:'[^'\n]*'\s*\+\s*)+'[^'\n]*')\)", source):
            joined = "".join(re.findall(r"'([^'\n]*)'", expr))
            if not CJK.search(joined):
                continue
            # Already a well-formed pair: the checks above own this one.
            if PAIR.search(joined):
                continue
            # CJK, then a run of real English words, with no separator in between.
            if re.search(r"[\u4e00-\u9fff][^\n]{0,12}?(\s+[A-Za-z][A-Za-z'’-]*){4,}", joined):
                broken.append(joined[:90])
        self.assertEqual([], broken,
                         "以下文案由多个字符串拼接而成，拼接后不是合法的 pair，"
                         "页面上会中英并排：\n  " + "\n  ".join(broken))

    def test_the_rule_here_is_the_rule_in_the_browser(self):
        """The copy above is a transcription. If `i18n.js` changes its rule, this
        transcription is no longer evidence of anything — so it is checked against the
        real source, by name."""
        with open(os.path.join(CONSOLE_DIR, "frontend", "i18n.js"),
                  encoding="utf-8") as handle:
            browser = handle.read()
        literal = re.search(r"var PAIR = /(.*?)/;", browser)
        self.assertIsNotNone(literal, "i18n.js 里找不到 `var PAIR = /…/;`，"
                                      "这条测试的正则副本已经失去依据")
        self.assertIn(literal.group(1), PAIR.pattern,
                      "i18n.js 的 PAIR 与本测试抄的副本已经不一致，两边必须同步改")


if __name__ == "__main__":
    unittest.main()
