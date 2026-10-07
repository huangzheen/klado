"""The membership service, against a fake database.

`test_org_admin.py` drives the router with the service faked, which is the right way to
pin the HTTP contract. It cannot reach anything below: every rule that lives *inside*
`klado_shared/orgs.py` — the last-administrator guard, the membership scope assertion,
the re-invite rule, the one-person-one-organization check — is skipped entirely.

That gap is not hypothetical. Writing these tests found seven mutations the router suite
did not catch, all of them real:

* removing the last-administrator guard (V1, V2) — a company that can never be
  administered again;
* removing the membership scope assertion (V3) — an enterprise admin editing a row in
  another company;
* letting a re-invite demote an accepted member (V4) — inviting a colleague is then a
  privilege-escalation path in reverse;
* removing the duplicate-membership check (V5) — one person silently in two companies.

⚠️ The pattern to recognise: a suite that only ever fakes the layer *below* the one it
is testing. The dashboard's share gate hid in `dashboard_store.share_dashboard` and a
test that stubbed it with a spy would have passed against a build with no gate at all.
Same shape, different module.

No database, no server: the connection is a scripted fake, and the SQL each function
issues is recorded so the shape can be asserted.
"""
import os
import sys
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from klado_shared import orgs                                  # noqa: E402

ORG_ID = 7
MEMBER_ID = 11
OTHER_ORG = 8
OWNER = "boss@corp.example"
MATE = "mate@corp.example"


# ── the fake ─────────────────────────────────────────────────────────────────

class _Cursor:
    """Replays a scripted list of rows, one per `execute`, and records every statement.

    ⚠️ A naive fake that returns a fixed row for every query looks fine and tests
    nothing: the scope assertions read `row["org_id"]`, so a fake that always answers
    "this member is in YOUR org" makes `_assert_member_in_scope` pass by accident, and
    the mutation that deletes that check survives. Rows are therefore scripted per
    statement, and the tests that matter are the ones that script the OTHER company.
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, cursor_factory=None):
        return _Cursor(self._rows, self._log)

    def commit(self):
        return self

    def rollback(self):
        return self

    def close(self):
        return self


def _run(rows, fn, *args, _profile=None, **kwargs):
    """Run `fn` against a scripted connection. `_profile` stubs the caller's policy.

    ⚠️ `effective_profile` is stubbed rather than driven by the fake rows because it
    reaches the database through `membership_of`, and wiring that would mean every
    caller-side test also had to script a membership SELECT. The policy itself is
    covered directly in `test_share_gate.GateLogicTests`.
    """
    log: list = []
    conn = _Conn(rows, log)
    patches = [patch.object(orgs, "connect_main", return_value=conn)]
    if _profile is not None:
        patches.append(patch.object(orgs, "effective_profile", return_value=_profile))
    with patches[0]:
        for extra in patches[1:]:
            extra.start()
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:                       # noqa: BLE001 — the test decides
            value = exc
        finally:
            for extra in reversed(patches[1:]):
                extra.stop()
    return log, value


def _sqls(log):
    return " ;; ".join(stmt for stmt, _ in log)


def _member(org_id=ORG_ID, role="admin", status="active", user_id=3):
    return {"id": MEMBER_ID, "org_id": org_id, "user_id": user_id, "email": OWNER,
            "org_role": role, "status": status, "invited_by": "", "org_name": "Corp"}


# ── the scope assertion ──────────────────────────────────────────────────────

class MembershipScopeTests(unittest.TestCase):
    """`_assert_member_in_scope` is the only thing standing between a guessed id and
    another company's member row."""

    def test_a_member_in_your_organization_is_accepted(self):
        log, value = _run([[_member()], [_member()]], orgs.set_member_role,
                          MEMBER_ID, "admin", expected_org_id=ORG_ID)
        self.assertIsInstance(value, dict)

    def test_a_member_in_another_organization_is_refused(self):
        """⚠️ The leak. The row EXISTS and the caller is a perfectly valid enterprise
        administrator — the only thing that makes it not theirs is the org_id on the
        row."""
        log, value = _run([[_member(org_id=OTHER_ORG)], []],
                          orgs.set_member_role, MEMBER_ID, "member",
                          expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.MemberScopeError)
        self.assertNotIn("UPDATE", _sqls(log).upper(),
                         "the refusal still wrote to the row")

    def test_the_refusal_reads_as_a_missing_member(self):
        """Not "you may not touch that member" — that confirms it exists."""
        _log, value = _run([[_member(org_id=OTHER_ORG)]], orgs.disable_member,
                           MEMBER_ID, expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.MemberScopeError)
        self.assertIn("不存在", str(value))

    def test_no_expected_scope_means_no_scope_check(self):
        """An operator passes `None`, and is allowed to act anywhere."""
        _log, value = _run([[_member(org_id=OTHER_ORG)], [(2,)],
                             [dict(_member(org_id=OTHER_ORG, role="member"))]],
                           orgs.set_member_role, MEMBER_ID, "member",
                           expected_org_id=None)
        self.assertIsInstance(value, dict)

    def test_a_row_that_is_not_there_is_the_same_refusal(self):
        _log, value = _run([[]], orgs.disable_member, MEMBER_ID, expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.MemberScopeError)


# ── the last administrator ───────────────────────────────────────────────────

class LastAdministratorTests(unittest.TestCase):
    """The failure is a company that can never be administered again, discovered by the
    next person to open the page, with the only fix being an operator in a database
    console."""

    def _attempt(self, rows, fn, *args, **kwargs):
        log, value = _run(rows, fn, *args, **kwargs)
        return log, value

    def test_demoting_the_only_administrator_is_refused(self):
        # 1. the member row, 2. the count of active admins
        log, value = self._attempt([[_member(role="admin")], [(1,)]],
                                   orgs.set_member_role, MEMBER_ID, "member",
                                   expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertIn("最后一位管理员", str(value))
        self.assertNotIn("UPDATE org_members", _sqls(log))

    def test_removing_the_only_administrator_is_refused(self):
        log, value = self._attempt([[_member(role="owner")], [(1,)]],
                                   orgs.disable_member, MEMBER_ID,
                                   expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertNotIn("UPDATE org_members", _sqls(log))

    def test_demoting_one_of_two_is_allowed(self):
        log, value = self._attempt([[_member(role="admin")], [(2,)], [dict(_member())]],
                                   orgs.set_member_role, MEMBER_ID, "member",
                                   expected_org_id=ORG_ID)
        self.assertIsInstance(value, dict)
        self.assertIn("UPDATE org_members", _sqls(log))

    def test_promoting_a_member_is_never_blocked(self):
        """The guard is about LOSING an administrator, not about the target role. A
        guard that also blocked promotions would lock a company into its current shape."""
        log, value = self._attempt([[_member(role="member")], [dict(_member(role="admin"))]],
                                   orgs.set_member_role, MEMBER_ID, "admin",
                                   expected_org_id=ORG_ID)
        self.assertIsInstance(value, dict)
        self.assertNotIn("最后一位管理员", str(value))

    def test_an_ordinary_member_can_be_demoted_freely(self):
        log, value = self._attempt([[_member(role="member")], [dict(_member())]],
                                   orgs.disable_member, MEMBER_ID,
                                   expected_org_id=ORG_ID)
        self.assertIsInstance(value, dict)


# ── invitations ──────────────────────────────────────────────────────────────

class InvitationTests(unittest.TestCase):
    def test_the_role_of_an_accepted_member_is_not_overwritten(self):
        """⚠️ Re-inviting a colleague must not be able to demote them. The role is only
        written while the row is still `invited`."""
        log, _value = _run([[]], orgs.upsert_invitation, ORG_ID, MATE, "member")
        joined = _sqls(log)
        self.assertIn("ON CONFLICT (org_id, email) DO UPDATE", joined)
        self.assertIn("org_members.status = 'invited'", joined)
        self.assertIn("THEN EXCLUDED.org_role", joined)
        self.assertIn("ELSE org_members.org_role", joined)

    def test_the_inviter_is_stamped_but_the_role_is_guarded(self):
        log, _value = _run([[{"id": 1, "status": "invited"}]],
                           orgs.upsert_invitation, ORG_ID, MATE, "admin",
                           invited_by=OWNER)
        self.assertIn("SET invited_by = EXCLUDED.invited_by", _sqls(log))
        self.assertEqual(log[0][1], (ORG_ID, MATE, "admin", "invited", OWNER))

    def test_a_malformed_address_never_reaches_the_database(self):
        log, value = _run([], orgs.upsert_invitation, ORG_ID, "not-an-address", "member")
        self.assertIsInstance(value, orgs.OrgError)
        self.assertEqual(log, [])


class ExistingAccountTests(unittest.TestCase):
    """`attach_existing_account` is the ONLY place the "one person, one company" rule can
    be enforced, because the unique index cannot see it while both rows have
    `user_id IS NULL`."""

    def test_an_account_in_no_company_is_joined(self):
        # 1. the app_users row, 2. the invitation, 3. "any other org?" (empty)
        log, value = _run([[(44,)], [(5,)], []], orgs.attach_existing_account,
                          ORG_ID, MATE)
        self.assertEqual(value, 44)
        self.assertIn("UPDATE org_members SET user_id = %s, status = %s", _sqls(log))

    def test_an_address_already_in_another_company_is_refused(self):
        """⚠️ The two-company case. Both invitations were legal when they were made; the
        only moment the conflict becomes real is here."""
        log, value = _run([[(44,)], [(5,)], [("Other Co",)]],
                          orgs.attach_existing_account, ORG_ID, MATE)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertIn("另一个企业", str(value))
        self.assertNotIn("UPDATE org_members", _sqls(log))

    def test_an_address_with_no_account_returns_none_without_touching_anything(self):
        """Somebody mid-registration. Binding them to nothing is correct: they accept
        the invitation and land in the company then."""
        log, value = _run([[]], orgs.attach_existing_account, ORG_ID, MATE)
        self.assertIsNone(value)
        self.assertEqual(len(log), 1)

    def test_an_address_with_no_invitation_cannot_be_jumped_in(self):
        """Otherwise `attach_existing_account` would be a way to add any existing account
        to any company by knowing its address."""
        log, value = _run([[(44,)], []], orgs.attach_existing_account, ORG_ID, MATE)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertNotIn("UPDATE org_members", _sqls(log))

    def test_the_other_company_query_excludes_this_one(self):
        """A bug here would make every join fail with "already belongs to another
        company", because the row being created is itself in this company."""
        log, value = _run([[(44,)], [(5,)], []], orgs.attach_existing_account,
                          ORG_ID, MATE)
        self.assertIn("m.org_id <> %s", _sqls(log))
        self.assertEqual(log[2][1], (44, ORG_ID))


# ── module entitlements ──────────────────────────────────────────────────────

class MemberModuleTests(unittest.TestCase):
    def test_a_member_without_an_account_cannot_be_given_modules(self):
        """An invitation is not an account. Writing a row keyed to nothing would leave
        an entitlement that silently never applies."""
        log, value = _run([[_member(user_id=None)]], orgs.set_member_module,
                          MEMBER_ID, "datacenter", False, expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertIn("还没有账号", str(value))
        self.assertNotIn("account_modules", _sqls(log))

    def test_the_hook_must_be_wired_before_anything_is_written(self):
        """⚠️ A console started against a database the main app never initialised has no
        `account_modules` helper. Failing with a clear message beats writing a row and
        reporting success."""
        log, value = _run([[_member()], []], orgs.set_member_module,
                          MEMBER_ID, "datacenter", False, expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertIn("尚未就绪", str(value))
        self.assertNotIn("account_modules", _sqls(log))

    def test_a_wired_hook_is_called_with_the_members_user_id(self):
        seen = []
        orgs.bind_account_modules(lambda uid, key, on: seen.append((uid, key, on)),
                                  lambda uid, key: seen.append((uid, key, None)))
        try:
            _log, value = _run([[_member(user_id=88)], [_member(user_id=88)], []],
                               orgs.set_member_module,
                               MEMBER_ID, "calendar", False, expected_org_id=ORG_ID)
        finally:
            orgs.bind_account_modules(None, None)
        self.assertIsInstance(value, dict)
        self.assertEqual(seen, [(88, "calendar", False)])

    def test_clearing_a_module_uses_the_other_hook(self):
        seen = []
        orgs.bind_account_modules(lambda *a: seen.append(("set", a)),
                                  lambda *a: seen.append(("clear", a)))
        try:
            _log, value = _run([[_member(user_id=88)], [_member(user_id=88)], []],
                               orgs.clear_member_module,
                               MEMBER_ID, "calendar", expected_org_id=ORG_ID)
        finally:
            orgs.bind_account_modules(None, None)
        self.assertIsInstance(value, dict)
        self.assertEqual(seen, [("clear", (88, "calendar"))])


class ClearAllMemberModulesTests(unittest.TestCase):
    """"恢复默认" on a whole matrix row — one call, not a loop over the modules.

    The interesting cases are the two that a per-key loop gets wrong: a member with no
    overrides at all (0, and that is SUCCESS — it is the state that was asked for), and a
    member who has not registered yet (there is no `user_id` to key a row on).
    """

    def _bind(self, seen):
        orgs.bind_account_modules(lambda *a: None, lambda *a: None,
                                  lambda uid: seen.append(uid))

    def test_it_drops_every_row_for_that_account(self):
        seen = []
        self._bind(seen)
        try:
            # SELECT the member, then SELECT the existing keys (two rows), then reset.
            # ⚠️ The rows are DICTS, because the function opens a RealDictCursor and the
            # fake has to say so — a tuple here would have hidden a KeyError that only
            # fires when a member actually HAS overrides.
            _log, value = _run([[_member(user_id=88)],
                                [{"module_key": "calendar"}, {"module_key": "datacenter"}]],
                               orgs.clear_member_modules, MEMBER_ID,
                               expected_org_id=ORG_ID)
        finally:
            orgs.bind_account_modules(None, None)
        self.assertEqual(value, 2, "the count of rows that went away is the return value")
        self.assertEqual(seen, [88])

    def test_a_member_who_was_never_narrowed_reports_zero_not_an_error(self):
        """⚠️ 0 is the success case. Reporting it as a failure would make a button that
        did exactly what it says look broken."""
        seen = []
        self._bind(seen)
        try:
            _log, value = _run([[_member(user_id=88)], []],
                               orgs.clear_member_modules, MEMBER_ID,
                               expected_org_id=ORG_ID)
        finally:
            orgs.bind_account_modules(None, None)
        self.assertEqual(value, 0)
        self.assertEqual(seen, [88], "the reset still runs, so the state is proven, "
                                     "not assumed")

    def test_a_member_without_an_account_is_refused(self):
        seen = []
        self._bind(seen)
        try:
            _log, value = _run([[_member(user_id=None)]], orgs.clear_member_modules,
                               MEMBER_ID, expected_org_id=ORG_ID)
        finally:
            orgs.bind_account_modules(None, None)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertEqual(seen, [], "no account means nothing to clear, and no silent pass")

    def test_an_unwired_reset_hook_refuses_instead_of_pretending(self):
        """⚠️ The two-argument bind is still legal — other callers use it. What must not
        happen is a reset that reports success while the rows are still there."""
        orgs.bind_account_modules(lambda *a: None, lambda *a: None)
        try:
            _log, value = _run([[_member(user_id=88)]], orgs.clear_member_modules,
                               MEMBER_ID, expected_org_id=ORG_ID)
        finally:
            orgs.bind_account_modules(None, None)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertIn("尚未就绪", str(value))

    def test_a_rebind_without_a_reset_clears_the_previous_one(self):
        """A stale hook outliving the test that installed it is how a later test ends up
        writing to a fake. Binding two args must unbind the third."""
        orgs.bind_account_modules(lambda *a: None, lambda *a: None, lambda uid: None)
        orgs.bind_account_modules(lambda *a: None, lambda *a: None)
        self.assertIsNone(orgs.account_modules_reset)
        orgs.bind_account_modules(None, None)

    def test_the_real_helpers_are_bound_to_the_hooks_that_fit(self):
        """⚠️ The bug this file exists for, in its purest form.

        `main.py` wires three functions of `services.account_modules` into three hooks,
        and the shared layer calls them at fixed arities: `set(user_id, key, on)`,
        `clear(user_id, key)`, `reset(user_id)`. Binding the wrong one of the two
        similarly-named clear helpers is invisible in review — both are called "clear" —
        and it fails at runtime with a TypeError, so "send this module back to following
        the deployment" returns 500 on the first click.

        So the assertion is on the REAL module, by name and by signature, not on a
        lambda that accepts anything.
        """
        import inspect

        from services import account_modules as am
        orgs.bind_account_modules(am.set_module, am.clear_one, am.clear_for)
        try:
            expected = {
                "account_modules_set": ("set_module", 3),
                "account_modules_clear": ("clear_one", 2),
                "account_modules_reset": ("clear_for", 1),
            }
            for hook, (attr, arity) in expected.items():
                with self.subTest(hook=hook):
                    fn = getattr(orgs, hook)
                    self.assertIsNotNone(fn, hook + " was never bound at all")
                    self.assertEqual(fn.__name__, attr,
                                     hook + " is bound to the wrong helper — "
                                     "clear_one takes (user_id, key), clear_for takes "
                                     "(user_id) and drops the whole row")
                    self.assertEqual(len(inspect.signature(fn).parameters), arity)
        finally:
            orgs.bind_account_modules(None, None)


# ── scopes and approvals ─────────────────────────────────────────────────────

class SingleFetchTests(unittest.TestCase):
    """No function may call `fetchone()` twice for one row.

    ⚠️ This is a real class of bug that was live three times over in this file, and it is
    invisible to every other test here because they all fake the cursor: the fake
    returned the same row every time, so `cur.fetchone() and dict(cur.fetchone())` looked
    fine. Against a real psycopg2 cursor the first call CONSUMES the row, the second
    returns None, and `dict(None)` raises TypeError — a 500 on every request.

    `membership_of` sits on the request path of every `/api/org/*` endpoint, so this was
    the whole enterprise-administration surface returning 500 while 992 unit tests passed.
    The rule is now `_one_row()`, and this asserts there is no second way to do it.
    """

    def test_no_function_consumes_a_row_twice(self):
        """Counts `fetchone` calls PER RESULT SET, not per file.

        ⚠️ Per file would be wrong: a function with two genuinely separate queries is
        fine, and so is a loop that reads one row per pass. What is never fine is two
        reads of the SAME query's result set, so the scan resets at every `execute` (a new
        result set) and at every loop body (a new row per pass).

        Parsed with `ast`, not with `inspect.getsource` + `str.count`: a text scan reads
        the DOCSTRING and the COMMENTS, and this file explains the very mistake in prose
        next to the fixed line — so a text check flags its own explanation.

        Only `fetchone` is counted. `fetchall` drains the set in one go and cannot be
        "read twice", and counting it produced a false positive on a function that
        legitimately does `execute` then `fetchall` per row.
        """
        import ast
        import inspect

        from klado_shared import orgs as module

        def offending_lines(fn):
            try:
                tree = ast.parse(inspect.getsource(fn).lstrip())
            except (SyntaxError, IndentationError, OSError):
                return []
            if not tree.body:
                return []

            # A `fetchone` inside a loop reads a different row on each pass, so it never
            # double-reads one result set. Recorded as its own reset, not as a read.
            per_pass = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.For, ast.While)):
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                                and inner.func.attr == "fetchone":
                            per_pass.add(inner.lineno)

            events = []
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                attr = node.func.attr
                if attr == "execute":
                    events.append((node.lineno, "arm"))
                elif attr == "fetchone" and node.lineno not in per_pass:
                    events.append((node.lineno, "read"))
            events.sort()

            out, pending = [], 0
            for lineno, kind in events:
                if kind == "arm":
                    pending = 0
                    continue
                pending += 1
                if pending > 1:
                    out.append(lineno)
            return out

        offenders = []
        for name, fn in vars(module).items():
            if not (inspect.isfunction(fn) and fn.__module__ == module.__name__):
                continue
            for lineno in offending_lines(fn):
                offenders.append(
                    f"{name}: line {lineno} reads a result set that was already read")
        self.assertEqual(offenders, [],
                         "⚠️ these read one row set twice without re-running the query; "
                         "the second read gets None. Use orgs._one_row(cur).")

    def test_one_row_returns_none_for_an_empty_result(self):
        self.assertIsNone(_one_row_of(None))
        self.assertEqual(_one_row_of({"id": 3}), {"id": 3})
        # And it consumes exactly one row: a second call on the same cursor must see the
        # result set drained, which is the whole reason the helper exists.
        cursor = _RowCursor([{"id": 3}])
        self.assertEqual(orgs._one_row(cursor), {"id": 3})
        self.assertIsNone(orgs._one_row(cursor))
        self.assertEqual(cursor.reads, 2)


class DictCursorTests(unittest.TestCase):
    """No function may index a row positionally when its cursor returns dicts.

    ⚠️ Two live 500s from this one mistake, both in P6's blast radius, and both with the
    same nasty shape:

    * `_member_with_modules` read `{r[0]: r[1] for r in cur.fetchall()}` on a
      `RealDictCursor`. A dict has no key `0`, so it raised KeyError — **but only when
      the member had at least one override**, because a comprehension never evaluates its
      body for an empty result. "Clear a module for somebody nobody narrowed" therefore
      answered 200 and the ordinary case was a 500, after the row was already written.
    * `clear_member_modules` counted rows with `r[0]` for the same reason.

    The other positional reads in this file are on a PLAIN cursor, which really does
    return tuples, and they are correct. So the check is not "no `r[0]` anywhere" — it is
    "no `r[0]` in a function that opened a dict cursor", resolved per function because
    every read here happens on the one cursor that function opened.
    """

    def test_positional_row_access_never_touches_a_dict_cursor(self):
        import ast
        import inspect

        from klado_shared import orgs as module

        offenders = []
        for name, fn in vars(module).items():
            if not (inspect.isfunction(fn) and fn.__module__ == module.__name__):
                continue
            try:
                tree = ast.parse(inspect.getsource(fn).lstrip())
            except (SyntaxError, IndentationError, OSError):
                continue
            uses_dict_cursor = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cursor"
                and "RealDictCursor" in ast.dump(node)
                for node in ast.walk(tree))
            if not uses_dict_cursor:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Name)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, int)):
                    offenders.append(
                        f"{name}: line {node.lineno} reads r[{node.slice.value}] from a "
                        f"dict cursor")
        self.assertEqual(offenders, [],
                         "⚠️ a RealDictCursor returns dicts; a positional read is a "
                         "KeyError at runtime. Use r['column'].")

    def test_the_dict_cursor_reads_that_exist_use_column_names(self):
        """The positive form: the reads this module does make are named, and they name
        the columns the SQL actually selects."""
        import inspect
        source = inspect.getsource(orgs._member_with_modules)
        self.assertIn('r["module_key"]', source)
        self.assertIn('r["enabled"]', source)


class _RowCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.reads = 0

    def fetchone(self):
        self.reads += 1
        return self._rows.pop(0) if self._rows else None


def _one_row_of(row):
    return orgs._one_row(_RowCursor([] if row is None else [row]))


class SetScopesTests(unittest.TestCase):
    def test_it_writes_both_columns(self):
        log, value = _run([[{"id": ORG_ID, "share_scope": "external",
                             "public_scope": "forbid"}]],
                          orgs.set_scopes, ORG_ID, "external", "forbid")
        self.assertIsInstance(value, dict)
        self.assertEqual(log[0][1], ("external", "forbid", ORG_ID))

    def test_a_missing_organization_is_refused(self):
        log, value = _run([[]], orgs.set_scopes, ORG_ID, "global", "allow")
        self.assertIsInstance(value, orgs.OrgError)


class ApprovalListTests(unittest.TestCase):
    def test_a_company_queue_is_narrowed(self):
        log, _value = _run([[]], orgs.list_approvals, org_id=ORG_ID, status="pending")
        self.assertIn("AND a.org_id = %s", _sqls(log))
        self.assertEqual(log[0][1], ("pending", ORG_ID))

    def test_an_operator_sees_every_company(self):
        log, _value = _run([[]], orgs.list_approvals, org_id=None, status="pending")
        self.assertNotIn("AND a.org_id", _sqls(log))
        self.assertEqual(log[0][1], ("pending",))

    def test_timestamps_become_strings(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        _log, rows = _run([[{"id": 1, "created_at": now, "decided_at": None}]],
                          orgs.list_approvals, org_id=ORG_ID)
        self.assertIsInstance(rows[0]["created_at"], str)


class ApprovalDecisionTests(unittest.TestCase):
    def test_a_decision_in_another_company_is_refused(self):
        """⚠️ The scope is asserted by READING the row before the UPDATE, not by adding it
        to the WHERE clause — because "no rows updated" and "row is not yours" are the
        same result and they need different answers."""
        log, value = _run([[{"id": 1, "org_id": OTHER_ORG}], []],
                          orgs.decide_public_approval, 1, "approved", OWNER,
                          expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.MemberScopeError)
        self.assertNotIn("UPDATE org_public_approvals", _sqls(log))

    def test_a_decision_in_your_company_is_written(self):
        log, value = _run([[{"id": 1, "org_id": ORG_ID}],
                           [{"id": 1, "org_id": ORG_ID, "status": "approved"}]],
                          orgs.decide_public_approval, 1, "approved", OWNER,
                          expected_org_id=ORG_ID)
        self.assertIsInstance(value, dict)
        self.assertIn("UPDATE org_public_approvals", _sqls(log))
        self.assertEqual(log[1][1], ("approved", OWNER, 1))

    def test_an_unknown_verb_is_refused_before_any_read(self):
        log, value = _run([], orgs.decide_public_approval, 1, "maybe", OWNER,
                          expected_org_id=ORG_ID)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertEqual(log, [], "an unknown verb must not read the table")

    def test_a_requester_re_requesting_resets_a_denial(self):
        """Otherwise a denied export is a permanent mark on the document, and the only
        way out is an operator editing rows."""
        log, value = _run([[{"id": 5, "status": "approved"}]],
                          orgs.request_public_approval, OWNER, "report", "q3", "public",
                          _profile=("internal", "approve", ORG_ID))
        self.assertIsInstance(value, dict)
        joined = _sqls(log)
        self.assertIn("DO UPDATE SET status = 'pending'", joined)
        self.assertIn("ON CONFLICT (org_id, target_type, target_id, kind, "
                      "lower(requester_email))", joined)

    def test_a_person_in_no_company_needs_no_approval(self):
        _log, value = _run([], orgs.request_public_approval, OWNER, "report", "q3",
                           "public", _profile=orgs.UNRESTRICTED)
        self.assertIsInstance(value, orgs.OrgError)
        self.assertIn("无需审批", str(value))

    def test_an_unknown_family_is_refused(self):
        _log, value = _run([], orgs.request_public_approval, OWNER, "invoice", "i1",
                           "public")
        self.assertIsInstance(value, orgs.OrgError)


if __name__ == "__main__":
    unittest.main()
