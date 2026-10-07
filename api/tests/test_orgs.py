"""Organizations: the model, and the two places it is easy to get quietly wrong.

No database here. The pure validators are tested directly; the SQL is tested by its
*shape* against a fake connection, because the failures worth preventing are not "wrong
answer from a wrong WHERE clause" — they are the two that a live database would happily
accept and only a user would notice:

**A backfill that half-works.** The first version of `backfill_personal_orgs` built the
organizations in a data-modifying CTE and joined them back by slug in the next one.
PostgreSQL runs the sub-statements of such a `WITH` against a single snapshot, so the
second statement could not see the rows the first had just inserted: the orgs were
created, the memberships were not, and the *next* startup repaired it. No error, no
warning, and an installation that had quietly made no progress. The tests below pin the
statement *order* for that reason, not just the presence of the right keywords.

**A person in two organizations.** `org_members_one_org_per_user` is a unique index, and
a unique index cannot catch this: at invitation time `user_id` is NULL for both rows and
PostgreSQL treats NULLs as distinct. So the invariant is application-level
(`pending_invitations_for`), and the test that matters is the one that plants two
outstanding invitations for the same address — an empty `org_members` table, or a fixture
with only accepted members, would never reach the branch.

The third thing worth naming: operators are deliberately **not** backfilled. A super
administrator is the platform level, above every organization. An operator who also had
a personal org would make "is this an operator" answerable two ways, and the two could
disagree after a demotion.
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from klado_shared import orgs  # noqa: E402


# ── fake connection ──────────────────────────────────────────────────────────

class _FakeCursor:
    """Consumes ONE entry of `results` per `execute()`, in order.

    ⚠️ The alignment is per *execute*, not per query that reads. `ensure_schema` issues
    three statements and only two of them read anything, so a test that passes a single
    row and expects it at the end will silently get `[]` and fail on a `None` subscript
    rather than on the thing it meant to check. Build the list by walking the statements:

        cur.execute(DDL)                       -> results[0] = []        (no read)
        cur.execute("SELECT to_regclass(...)")  -> results[1] = [(None,)] (one row, one column)
        cur.execute("DELETE ...")               -> results[2] = []        (no read)

    Rows are tuples, so `fetchone()[0]` reads a column the way psycopg2 does.
    """

    def __init__(self, log, results, factory=None):
        self._log, self._results, self._factory = log, list(results), factory
        self._rows: list = []
        self._i = 0
        # psycopg2 exposes this; `prune_orphan_approvals` sums it.
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._log.append((" ".join((sql or "").split()), params))
        self._rows = self._results[self._i] if self._i < len(self._results) else []
        self.rowcount = len(self._rows)
        self._i += 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _RaisingCursor(_FakeCursor):
    """A cursor whose `execute` blows up, to exercise the rollback path."""

    def execute(self, sql, params=None):
        raise RuntimeError("database is down")


class _FakeConn:
    def __init__(self, log, results):
        self._log, self._results, self.committed, self.rolled_back = log, results, False, False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._log, self._results, cursor_factory)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _run(results, fn, *args, **kwargs):
    """Call `fn` with `connect_main` replaced. Returns (log, conn, value_or_exception)."""
    log: list = []
    conn = _FakeConn(log, results)
    with patch.object(orgs, "connect_main", return_value=conn):
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:                       # noqa: BLE001 — the test decides
            value = exc
    return log, conn, value


def _sqls(log):
    return " ;; ".join(stmt for stmt, _params in log)


# ── validators ───────────────────────────────────────────────────────────────

class EmailAndDomainTests(unittest.TestCase):
    def test_addresses_are_normalised(self):
        for raw, want in (("  Ann@Acme.COM ", "ann@acme.com"),
                          ("a.b+c@sub.acme.co.uk", "a.b+c@sub.acme.co.uk")):
            self.assertEqual(orgs.clean_email(raw), want, raw)

    def test_unusable_addresses_return_empty_not_an_error(self):
        for raw in ("", "   ", "no-at-sign", "@acme.com", "a@", "a@b", "a b@acme.com",
                    "a@acme .com", None, 42):
            self.assertEqual(orgs.clean_email(raw), "", repr(raw))

    def test_suffixes_are_normalised(self):
        for raw, want in (("Acme.com", "acme.com"), ("@acme.com", "acme.com"),
                          (" acme.com. ", "acme.com")):
            self.assertEqual(orgs.clean_domain(raw), want, raw)

    def test_suffixes_needing_two_labels_are_rejected(self):
        """`localhost` and `com` are not company suffixes; accepting them would let a
        single typo create a company that claims a TLD."""
        for raw in ("localhost", "com", "acme", "a b.com", "acme..com", "", "-acme.com"):
            self.assertEqual(orgs.clean_domain(raw), "", repr(raw))

    def test_domain_list_splits_on_commas_and_whitespace(self):
        self.assertEqual(
            orgs.clean_domains(["Acme.com, other.com", "third.com", "acme.com"]),
            ["acme.com", "other.com", "third.com"])

    def test_a_malformed_suffix_raises_rather_than_being_dropped(self):
        """⚠️ The whole point. Silently discarding a bad entry leaves a company that
        half-matches, and the symptom — employees who "can't register" — reads like a
        mail problem, not a typo in an admin field.

        "not a domain" is split on whitespace first, so the offender reported is the
        first bad token rather than the whole string. What matters is that it names
        *something* the operator typed, not that it echoes the input verbatim.
        """
        with self.assertRaises(orgs.OrgError) as ctx:
            orgs.clean_domains(["acme.com", "not a domain"])
        message = str(ctx.exception)
        self.assertIn("「not」", message)
        # Bilingual: the message is a pair, so a router can `pick()` it as-is.
        self.assertIn(" / ", message)

    def test_too_many_suffixes_raises(self):
        with self.assertRaises(orgs.OrgError):
            orgs.clean_domains([f"d{i}.com" for i in range(orgs.MAX_DOMAINS + 1)])

    def test_org_error_messages_are_bilingual_pairs(self):
        """Every rejection a user can trigger has to survive `pick(str(exc), lang)`."""
        cases = (
            lambda: orgs.clean_name(""),
            lambda: orgs.clean_slug("bad/slug"),
            lambda: orgs._one_of("nope", orgs.SHARE_SCOPES, "share scope", "分享范围无效"),
            lambda: orgs.clean_domains(["not a domain"]),
        )
        for case in cases:
            with self.assertRaises(orgs.OrgError) as ctx:
                case()
            self.assertIn(" / ", str(ctx.exception), str(ctx.exception))


class SlugTests(unittest.TestCase):
    def test_plain_slugs_pass(self):
        self.assertEqual(orgs.clean_slug("Acme"), "acme")
        self.assertEqual(orgs.clean_slug("acme-tech"), "acme-tech")

    def test_chinese_slugs_are_allowed(self):
        """A company may well be named 某某科技, and the console links to it."""
        self.assertEqual(orgs.clean_slug("某某科技"), "某某科技")

    def test_path_breaking_slugs_are_rejected(self):
        for raw in ("a/b", "a b", "a?b=1", "../etc", ""):
            with self.assertRaises(orgs.OrgError, msg=raw):
                orgs.clean_slug(raw)

    def test_fallback_seed_survives_a_fully_chinese_name(self):
        seed = orgs._ascii_fallback("某某科技")
        self.assertTrue(orgs.clean_slug(seed), f"{seed!r} must still be a usable slug seed")

    def test_fallback_seed_keeps_ascii_runs(self):
        # The seed keeps its case; `clean_slug` is what lowercases it, and the caller
        # always goes through that. Asserted end-to-end so the two steps stay coupled.
        self.assertEqual(orgs.clean_slug(orgs._ascii_fallback("Acme 科技 Ltd")), "acme-ltd")


# ── schema shape ─────────────────────────────────────────────────────────────

class DDLTests(unittest.TestCase):
    """Asserted on the SQL text: the constraints are the product decision, and a later
    edit that drops one is a silent policy change nobody reviews."""

    def test_every_statement_is_idempotent(self):
        """Both processes call `ensure_schema()` on every start. A statement that is not
        idempotent fails the SECOND boot, which is a genuinely confusing symptom: the
        deployment works, the restart does not, and the log blames the database."""
        # Strip `--` comments first: they carry the reasoning for the constraints, and a
        # naive split on `;` would treat the leading comment block as a statement.
        body = re.sub(r"--[^\n]*", "", orgs.DDL)
        statements = [s.strip() for s in body.split(";") if s.strip()]
        # Pinned exactly, not a floor: 3 tables + 3 indexes on org_members +
        # 3 indexes on the approval queue + 3 ALTERs (one column, two constraints).
        self.assertEqual(len(statements), 12, [s.split("\n")[0][:60] for s in statements])
        for stmt in statements:
            upper = stmt.upper()
            # `CREATE … IF NOT EXISTS` and `DROP … IF EXISTS` are the two idempotent
            # spellings. `ADD CONSTRAINT` has neither and is allowed only because the
            # next assertion requires it to be paired with a DROP of the same name.
            if "ADD CONSTRAINT" in upper:
                continue
            self.assertTrue("IF NOT EXISTS" in upper or "IF EXISTS" in upper, stmt[:90])

    def test_every_added_constraint_is_preceded_by_a_drop_of_that_name(self):
        """The rule the check above stands on. `ADD CONSTRAINT` on an existing name
        raises "already exists", so an unpaired one breaks every restart after the
        first — and the failure is reported by the database, at boot, about a table
        nobody was touching."""
        body = re.sub(r"--[^\n]*", "", orgs.DDL)
        statements = [s.strip() for s in body.split(";") if s.strip()]
        added = 0
        for index, stmt in enumerate(statements):
            match = re.search(r"ADD\s+CONSTRAINT\s+(\w+)", stmt, re.I)
            if not match:
                continue
            added += 1
            name = match.group(1)
            with self.subTest(constraint=name):
                self.assertGreater(index, 0, "an ADD CONSTRAINT with nothing before it")
                self.assertRegex(statements[index - 1], rf"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+{name}\b",
                                 f"{name} is added without being dropped first")
        self.assertGreater(added, 0, "no ADD CONSTRAINT found; the check is not running")

    def test_share_and_public_scopes_are_constrained_to_their_vocabularies(self):
        self.assertIn("CHECK (share_scope IN ('internal', 'external', 'global'))",
                      orgs.DDL)
        self.assertIn("CHECK (public_scope IN ('forbid', 'approve', 'allow'))", orgs.DDL)

    def test_member_status_distinguishes_an_outstanding_invitation(self):
        self.assertIn("CHECK (status IN ('active', 'invited', 'disabled'))", orgs.DDL)

    def test_user_id_is_nullable_so_an_invitation_can_precede_the_account(self):
        """`user_id INT REFERENCES app_users(id)` with NO NOT NULL. The normal order is
        invite, then register."""
        m = re.search(r"user_id\s+INT[^,\n]*", orgs.DDL)
        self.assertIsNotNone(m)
        self.assertNotIn("NOT NULL", m.group(0))

    def test_one_person_one_organization_is_a_partial_unique_index(self):
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS org_members_one_org_per_user",
            orgs.DDL)
        # The WHERE clause is what makes it a *partial* index. Without it the index
        # would also cover invitations, where user_id is NULL.
        self.assertRegex(
            orgs.DDL,
            r"org_members_one_org_per_user\s*\n\s*ON org_members \(user_id\) "
            r"WHERE user_id IS NOT NULL")

    def test_the_approval_table_has_no_foreign_key_into_the_main_app(self):
        """⚠️ `target_id` deliberately has no REFERENCES. A foreign key from the shared
        layer to `ai_reports` (or the knowledge / calendar tables) would make the
        console depend on the main app's schema existing — the coupling
        `klado_shared/` exists to avoid. Orphans are pruned by
        `prune_orphan_approvals()` instead.

        The first version of this table carried a `report_id INT`, which could not even
        describe a knowledge item or a calendar event. That is why the assertion looks
        for `target_id` and not for any integer column.
        """
        # ⚠️ Comments are stripped first: the DDL explains WHY there is no report_id,
        # so a naive substring check finds the very word it is asserting is absent.
        body = re.sub(r"--[^\n]*", "", orgs.DDL)
        m = re.search(r"target_id\s+TEXT[^,\n]*", body)
        self.assertIsNotNone(m)
        self.assertNotIn("REFERENCES", m.group(0))
        self.assertNotIn("report_id", body)

    def test_the_approval_table_covers_every_publishing_family(self):
        """Four families, and the file one is the reason this list grew: `publish_file`
        mints a bearer token for an object in storage, which is the same kind of promise
        as an anyone-link."""
        body = re.sub(r"--[^\n]*", "", orgs.DDL)
        self.assertIn("'report', 'knowledge', 'calendar', 'file'", body)
        self.assertEqual(set(orgs.TARGET_TYPES),
                         {"report", "knowledge", "calendar", "file", "dashboard"})

    def test_one_live_request_per_document_channel_and_person(self):
        """⚠️ Without this index `ON CONFLICT` in `request_public_approval()` never
        conflicts, every click of the button appends another pending row, and the
        administrator's queue fills with copies of one document."""
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS org_public_approvals_one_request",
                      orgs.DDL)
        self.assertRegex(
            orgs.DDL,
            r"org_public_approvals_one_request\s*\n\s*ON org_public_approvals "
            r"\(org_id, target_type, target_id, kind, lower\(requester_email\)\)")

    def test_orgs_do_not_cascade_away_with_a_closing_account(self):
        """An organization is not owned by one person: closing an account must not take
        the company with it. `org_members` does cascade (its rows reference
        `app_users`); `orgs` must not."""
        self.assertIn("org_id      INT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE",
                      orgs.DDL)
        self.assertNotRegex(orgs.DDL, r"REFERENCES\s+app_users[^)]*ON DELETE CASCADE[^)]*\)\s*;")


class EnsureSchemaTests(unittest.TestCase):
    # execute #1 = the pre-release-table retirement check (reads nothing),
    # #2 = DDL (no read).
    RETIRE_AND_DDL = [[], []]

    def test_runs_the_ddl(self):
        log, _conn, value = _run(self.RETIRE_AND_DDL, orgs.ensure_schema)
        self.assertNotIsInstance(value, Exception)
        self.assertIn("CREATE TABLE IF NOT EXISTS orgs", _sqls(log))

    def test_the_pre_release_approval_table_is_dropped_and_recreated(self):
        """⚠️ The table was first shipped with a `report_id INT`, which could not describe
        a knowledge item or a calendar event. `CREATE TABLE IF NOT EXISTS` leaves an
        existing table alone, so the old shape would survive and the new columns would
        be missing at RUNTIME rather than at startup. The retirement step is what makes
        the reshape take effect — and it is a no-op once `target_type` exists, so it
        costs nothing from here on.
        """
        log, _conn, value = _run(self.RETIRE_AND_DDL, orgs.ensure_schema)
        self.assertNotIsInstance(value, Exception)
        joined = _sqls(log)
        self.assertIn("DROP TABLE org_public_approvals", joined)
        # Both halves of the condition: old shape AND empty. Dropping a populated table
        # would be data loss, so the emptiness test is not optional.
        self.assertIn("column_name = 'report_id'", joined)
        self.assertIn("column_name = 'target_type'", joined)
        self.assertIn("NOT EXISTS (SELECT 1 FROM org_public_approvals)", joined)

    def test_commits_and_closes(self):
        _log, conn, _value = _run(self.RETIRE_AND_DDL, orgs.ensure_schema)
        self.assertTrue(conn.committed)
        self.assertTrue(conn.closed)

    def test_a_failure_rolls_back_and_still_closes(self):
        """A partial schema must not be left half-applied, and a leaked connection would
        show up much later as "too many clients" on an unrelated request."""
        log: list = []
        conn = _FakeConn(log, [])
        conn.cursor = lambda cursor_factory=None: _RaisingCursor(log, [])
        with patch.object(orgs, "connect_main", return_value=conn):
            with self.assertRaises(RuntimeError):
                orgs.ensure_schema()
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)
        self.assertTrue(conn.closed)


class PruneOrphanApprovalsTests(unittest.TestCase):
    """The substitute for the foreign key `org_public_approvals` deliberately lacks.

    Each family is checked with its own table, and each existence test is guarded — on a
    fresh install the main app's tables may not exist yet, and the console must not die
    on a table it does not own.
    """

    def test_it_asks_before_touching_each_main_app_table(self):
        log, _conn, value = _run([[(None,)]] * 4, orgs.prune_orphan_approvals)
        self.assertEqual(value, 0)
        joined = _sqls(log)
        self.assertEqual(joined.count("to_regclass"), 4)
        # The table name is a BOUND PARAMETER, not part of the SQL text — asserting on
        # the string would pass against a function that checked the same table three
        # times, and fail against one that checked three different ones correctly.
        params = [p for _s, p in log if p]
        for table in ("ai_reports", "ai_knowledge_items", "ai_calendar_events", "ai_dashboards"):
            self.assertTrue(any(table in p[0] for p in params), table)

    def test_it_deletes_only_for_tables_that_exist(self):
        """A missing table is skipped, not treated as "every document is an orphan" —
        which would wipe the queue on a fresh install.

        Four executes, not three: the loop asks `to_regclass` for each of the three
        families, and the one that EXISTS costs an extra DELETE. Handing the fake only
        three results leaves the fourth call with nothing, which surfaces as a subscript
        error on `fetchone()[0]` rather than as anything about the behaviour.
        """
        log, _conn, value = _run([[("ai_reports",)], [(None,)], [(None,)], [(None,)], [(None,)]],
                                 orgs.prune_orphan_approvals)
        self.assertEqual(value, 1, "one existing table means one DELETE attempt")
        joined = _sqls(log)
        self.assertEqual(joined.count("DELETE FROM org_public_approvals"), 1)
        self.assertIn("target_type = %s", joined)


# ── backfill ─────────────────────────────────────────────────────────────────

class BackfillTests(unittest.TestCase):
    """The order of these statements is the test. See the module docstring."""

    def test_slug_is_computed_once_into_a_temp_table(self):
        """⚠️ The single-statement CTE version looked tidier and was broken: PostgreSQL
        gives every sub-statement of a data-modifying WITH the same snapshot, so the
        join could not see the rows the insert had just written. The temp table is what
        makes the slug a real value the next statement can read."""
        log, _conn, value = _run([[]], orgs.backfill_personal_orgs)
        self.assertNotIsInstance(value, Exception)
        joined = _sqls(log)
        self.assertIn("CREATE TEMP TABLE _org_backfill_candidates", joined)
        self.assertIn("JOIN orgs o ON o.slug = c.slug", joined)

    def test_the_member_insert_comes_after_the_org_insert(self):
        log, _conn, _value = _run([[]], orgs.backfill_personal_orgs)
        statements = [s for s, _ in log]
        org_at = next(i for i, s in enumerate(statements) if "INSERT INTO orgs" in s)
        member_at = next(i for i, s in enumerate(statements) if "INSERT INTO org_members" in s)
        self.assertLess(org_at, member_at,
                        "members cannot be inserted before the orgs they reference exist")

    def test_operators_are_excluded(self):
        log, _conn, _value = _run([[]], orgs.backfill_personal_orgs)
        joined = _sqls(log)
        self.assertIn("lower(coalesce(u.role, 'user')) <> 'admin'", joined)

    def test_soft_deleted_accounts_are_excluded(self):
        log, _conn, _value = _run([[]], orgs.backfill_personal_orgs)
        self.assertIn("u.deleted_at IS NULL", _sqls(log))

    def test_accounts_already_in_an_org_are_skipped(self):
        """⚠️ Otherwise a member mid-registration is handed a personal org by a startup
        sweep, and the moment they finish they are in two."""
        log, _conn, _value = _run([[]], orgs.backfill_personal_orgs)
        self.assertIn("NOT EXISTS (SELECT 1 FROM org_members m", _sqls(log))

    def test_personal_orgs_get_the_unrestricted_profile(self):
        """A personal user is not in a company, so nothing restricts them — the whole
        point of the two-column split."""
        log, _conn, _value = _run([[]], orgs.backfill_personal_orgs)
        joined = _sqls(log)
        self.assertIn("'personal', 'global', 'allow'", joined)

    def test_reports_what_it_created(self):
        # execute #1 CREATE TEMP TABLE, #2 INSERT orgs, #3 INSERT members (the one read).
        _log, _conn, value = _run([[], [], [{"id": 1}, {"id": 2}]],
                                  orgs.backfill_personal_orgs)
        self.assertEqual(value, {"members": 2})

    def test_reports_zero_on_a_settled_database(self):
        _log, _conn, value = _run([[], [], []], orgs.backfill_personal_orgs)
        self.assertEqual(value, {"members": 0})


# ── the double-invitation hole ───────────────────────────────────────────────

class MembershipLookupTests(unittest.TestCase):
    """`membership_of` is what the share gate will ask on every share. ⚠️ It matches on
    the ADDRESS, not on `user_id`, and that is load-bearing.

    A person mid-registration has no `user_id` at all, so a `user_id` lookup returns
    nothing exactly when the answer is most needed — and in the other direction, an
    account whose `user_id` was never backfilled (an installation upgraded without the
    backfill running) would silently look like "no organization", which for a share gate
    means the permissive answer. Every content table in this product keys on
    `owner_email`, so the address is the join key the rest of the code already holds.
    """

    def test_matches_on_the_address(self):
        log, _conn, value = _run([[{"id": 7, "org_id": 3}]], orgs.membership_of,
                                 "ann@acme.com")
        self.assertEqual(value["org_id"], 3)
        self.assertIn("lower(m.email) = %s", _sqls(log))
        self.assertNotIn("m.user_id =", _sqls(log))

    def test_the_address_is_normalised_before_it_is_bound(self):
        log, _conn, _value = _run([[]], orgs.membership_of, "  Ann@ACME.com  ")
        self.assertIn("ann@acme.com", [p for _s, p in log if p][0])

    def test_an_unusable_address_never_reaches_the_database(self):
        log, _conn, value = _run([], orgs.membership_of, "nonsense")
        self.assertIsNone(value)
        self.assertEqual(log, [])

    def test_it_returns_a_disabled_row_rather_than_filtering_it_out(self):
        """⚠️ "Which company is this address in" and "may this address act" are different
        questions. Filtering disabled rows out here would make a disabled member look
        like a personal user — and a personal user is unrestricted, so the row that
        says no would turn into the row that says yes to everything."""
        disabled = {"org_id": 3, "status": orgs.STATUS_DISABLED}
        _log, _conn, value = _run([[disabled]], orgs.membership_of, "ann@acme.com")
        self.assertEqual(value, disabled)

    def test_known_person_is_unknown_address(self):
        _log, _conn, value = _run([[]], orgs.membership_of, "boss@corp.example")
        self.assertIsNone(value)


class PendingInvitationTests(unittest.TestCase):
    """⚠️ The invariant PostgreSQL cannot enforce.

    `org_members_one_org_per_user` is a partial unique index on `user_id`. Two pending
    invitations for the same address have `user_id IS NULL` in both rows, and a unique
    index treats NULLs as distinct — so the database accepts both, happily. The only
    thing standing between "two companies invited the same person" and "one person
    silently in two companies" is a query like this one.
    """

    TWO_PENDING = [
        {"id": 1, "org_id": 10, "org_name": "Acme", "org_slug": "acme", "kind": "enterprise"},
        {"id": 2, "org_id": 20, "org_name": "Globex", "org_slug": "globex", "kind": "enterprise"},
    ]

    def test_two_outstanding_invitations_are_both_reported(self):
        log, _conn, value = _run([self.TWO_PENDING], orgs.pending_invitations_for,
                                 "ann@acme.com")
        self.assertEqual(len(value), 2, value)
        # Only OUTSTANDING ones: an accepted membership in another company is a
        # different situation, and counting it here would refuse every re-registration.
        # The status is a bound parameter, so assert on the parameters, not the SQL text.
        self.assertIn("m.status = %s", _sqls(log))
        self.assertIn(orgs.STATUS_INVITED, [p for _s, p in log if p][0])

    def test_it_is_addressed_by_email_not_by_user_id(self):
        """A person mid-registration has no `user_id` at all, so a user_id lookup would
        return nothing and the check would be a no-op exactly when it is needed."""
        log, _conn, _value = _run([[]], orgs.pending_invitations_for, "ann@acme.com")
        self.assertIn("lower(m.email) = %s", _sqls(log))
        self.assertNotIn("m.user_id", _sqls(log))

    def test_an_unusable_address_never_reaches_the_database(self):
        log, _conn, value = _run([], orgs.pending_invitations_for, "not-an-address")
        self.assertEqual(value, [])
        self.assertEqual(log, [])


class AdminRoleTests(unittest.TestCase):
    """`is_org_admin` gates every enterprise-admin capability, so each way of losing it
    is a test of its own."""

    BASE = {"org_role": orgs.ORG_ROLE_ADMIN, "status": orgs.STATUS_ACTIVE,
            "org_status": orgs.ORG_ACTIVE}

    def test_active_admin_of_an_active_org(self):
        _log, _conn, value = _run([[dict(self.BASE)]], orgs.is_org_admin, "a@acme.com")
        self.assertTrue(value)

    def test_owner_counts(self):
        row = dict(self.BASE, org_role=orgs.ORG_ROLE_OWNER)
        _log, _conn, value = _run([[row]], orgs.is_org_admin, "a@acme.com")
        self.assertTrue(value)

    def test_ordinary_member_does_not(self):
        row = dict(self.BASE, org_role=orgs.ORG_ROLE_MEMBER)
        _log, _conn, value = _run([[row]], orgs.is_org_admin, "a@acme.com")
        self.assertFalse(value)

    def test_a_disabled_member_loses_it(self):
        row = dict(self.BASE, status=orgs.STATUS_DISABLED)
        _log, _conn, value = _run([[row]], orgs.is_org_admin, "a@acme.com")
        self.assertFalse(value)

    def test_a_suspended_organization_loses_admin(self):
        """⚠️ Checking only the member row would leave a suspended company fully
        administrable — the exact state a suspension exists to prevent."""
        row = dict(self.BASE, org_status=orgs.ORG_SUSPENDED)
        _log, _conn, value = _run([[row]], orgs.is_org_admin, "a@acme.com")
        self.assertFalse(value)

    def test_a_non_member_is_not_an_org_admin(self):
        """This is how the platform operator is handled: no membership row, and
        `is_admin_identity` is the other half of the question."""
        _log, _conn, value = _run([[]], orgs.is_org_admin, "boss@corp.example")
        self.assertFalse(value)


class ScopePresetTests(unittest.TestCase):
    def test_the_three_presets_are_the_ones_the_product_promises(self):
        self.assertEqual(
            orgs.SCOPE_PRESETS,
            {"internal": ("internal", "approve"),
             "external": ("external", "approve"),
             "global": ("global", "allow")})

    def test_the_default_is_the_strictest_preset(self):
        self.assertEqual(
            (orgs.SCOPE_INTERNAL, orgs.PUBLIC_APPROVE),
            orgs.SCOPE_PRESETS["internal"])

    def test_every_preset_combination_is_a_valid_pair(self):
        for share, public in orgs.SCOPE_PRESETS.values():
            self.assertIn(share, orgs.SHARE_SCOPES)
            self.assertIn(public, orgs.PUBLIC_SCOPES)


if __name__ == "__main__":
    unittest.main()
