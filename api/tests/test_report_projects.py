"""Workspace 项目 / 文件夹 — the containers a report is filed into.

What this file guards
---------------------
Four things, each of which is a way this feature can look perfect and still be wrong:

1. **The `%s` count.** A statement can be short by one parameter and still read
   perfectly in the source. The failure is a 500 from psycopg2 ("tuple index out of
   range") naming neither the statement nor the cause. Every statement here is
   therefore checked against **its own** placeholders rather than against a hand-kept
   number.

2. **The card count and the list behind it must be the same question.** The wall says
   "12 reports" and the directory shows rows; if the card counts public snapshots and
   the list does not (or either counts another owner's rows), the first number a
   reader sees is a lie. `MY_FILES` is asserted to be the same predicate
   `routers/reports.py::_SCOPE_SQL['mine']` uses.

3. **未归档 is real, and it is protected.** A project and a folder both have a system
   container; neither can be renamed or deleted, and the reserved name is refused in
   every spelling a person actually types.

4. **Route order.** `GET /api/reports/projects` registered *below* `@router.get(
   "/{slug}")` is not a second route — it is a report whose slug is the literal string
   "projects", i.e. a 404 on the very first request the new page makes. FastAPI has
   no "specific beats wildcard" resolution; the order **is** the routing table. This
   is asserted against the source, because it is true before the app ever boots.

Storage-level: no browser, no HTTP, no database. `verify_report_projects_ui.py` is
what proves the wall renders.
"""
from __future__ import annotations

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

from routers import reports as reports_router                 # noqa: E402
from services import report_projects as filing               # noqa: E402

OWNER = "owner@corp.example"
MATE = "mate@corp.example"
STRANGER = "stranger@other.example"

REPORTS_SOURCE = os.path.join(API_DIR, "routers", "reports.py")


def _cursor(recorder: list, rows_for=None):
    """A recording cursor whose `fetchone`/`fetchall` answers are chosen by SQL.

    ⚠️ The rows must be **dicts**: the real cursors are `RealDictCursor`, and a
    stand-in object fails on a subscript rather than on the thing under test — which
    is how a test like this passes nothing while reading as if it passed.
    """
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            recorder.append((sql, params or ()))

        def fetchone(self):
            sql = recorder[-1][0] if recorder else ""
            if rows_for:
                answer = rows_for(sql)
                if answer is not None:
                    return answer
            return None

        def fetchall(self):
            sql = recorder[-1][0] if recorder else ""
            if rows_for:
                answer = rows_for(sql)
                if answer is not None:
                    return answer
            return []

    return _Cursor()


def _db_for(recorder: list, rows_for=None):
    """A `_db` replacement — a **callable returning** the context manager.

    ⚠️ Passing the context manager itself does not work: `with _db() as conn` would
    call the MagicMock, get its `return_value`, and enter *that* — so the recording
    cursor is never reached and the test fails on a missing key rather than on the
    thing it is about.
    """
    import unittest.mock

    conn = unittest.mock.MagicMock()
    conn.cursor.return_value = _cursor(recorder, rows_for)
    ctx = unittest.mock.MagicMock()
    ctx.__enter__.return_value = conn
    return lambda: ctx


def _patched(recorder: list, rows_for=None):
    """Both halves of the seam: the schema call and the connection."""
    return (
        patch.object(filing, "_ensure_tables", lambda: None),
        patch.object(filing, "_db", _db_for(recorder, rows_for)),
    )


def _assert_placeholders(test, recorder, note):
    """Every statement issued, checked against its own placeholders."""
    test.assertTrue(recorder, f"{note}: no statement was issued at all")
    for sql, params in recorder:
        test.assertEqual(
            len(params), sql.count("%s"),
            f"{note}: the statement and its parameters disagree —\n  {sql.strip()}\n"
            f"  {len(params)} params for {sql.count('%s')} placeholders")


# ── 1. the placeholder count ────────────────────────────────────────────────

class PlaceholderTests(unittest.TestCase):
    """One owner email per `%s`. The bug this exists for produced a 500, not a test
    failure, because the mismatch was only ever visible to the database driver."""

    def test_the_predicate_has_exactly_one_placeholder(self):
        self.assertEqual(filing.MY_FILES.count("%s"), 1)

    def test_list_projects_passes_one_email_per_placeholder(self):
        recorder: list = []
        with _patched(recorder)[0], _patched(recorder)[1], \
                patch.object(filing, "ensure_unfiled", lambda e: ("unfiled-x", 1)):
            filing.list_projects(OWNER)
        _assert_placeholders(self, recorder, "list_projects")
        self.assertEqual({p for _sql, params in recorder for p in params}, {OWNER})

    def test_list_folders_counts_with_the_project_scoped_clause(self):
        """⚠️ This is the one that is easy to get wrong twice.

        The system folder holds the reports with `folder_id IS NULL` **inside this
        project**. Counting only `folder_id IS NULL` would give every project's
        catch-all the same account-wide number, so two cards would show the same
        count and one of them would be about a project the reader is not in.
        """
        recorded: list = []
        PROJECT = {"slug": "p1", "owner_email": OWNER, "system": False}
        FOLDER = {"id": 7, "project_slug": "p1", "title": "F", "system": False, "sort_order": 0}

        def rows_for(sql):
            if "FROM ai_report_projects WHERE slug" in sql:
                return PROJECT
            if "count(*)" in sql:
                return {"n": 0}
            return [FOLDER]

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            rows = filing.list_folders("p1", OWNER)

        self.assertEqual(len(rows), 1)
        counted = [(sql, params) for sql, params in recorded if "count(*)" in sql]
        self.assertTrue(counted, "no count query was issued at all")
        for sql, params in counted:
            self.assertEqual(len(params), sql.count("%s"), f"placeholder mismatch in: {sql}")
            # The project clause is not optional in either branch.
            self.assertIn("ai_reports.project_slug", sql,
                          "a folder count that does not name its project is account-wide")

    def test_the_system_folder_of_the_unfiled_project_matches_on_null(self):
        """A folder count for 未归档 matches `project_slug IS NULL`.

        ⚠️ This is the regression test for a real bug: `list_folders` did not select
        the project's own `system` flag, so the branch was always false and 未归档's
        catch-all counted `project_slug = 'unfiled-…'` — which matches no row, because
        unfiled reports carry NULL. The card then reported itself permanently empty.
        """
        recorded: list = []
        PROJECT = {"slug": "unfiled-abc", "owner_email": OWNER, "system": True}
        FOLDER = {"id": 3, "project_slug": "unfiled-abc", "title": "F", "system": True,
                  "sort_order": 0, "project_system": True}

        def rows_for(sql):
            if "FROM ai_report_projects WHERE slug" in sql:
                return PROJECT
            if "count(*)" in sql:
                return {"n": 0}
            return [FOLDER]

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            rows = filing.list_folders("unfiled-abc", OWNER)
        self.assertEqual(len(rows), 1)
        counted = [sql for sql, _ in recorded if "count(*)" in sql]
        self.assertTrue(counted)
        for sql in counted:
            self.assertIn("ai_reports.project_slug IS NULL", sql)
            self.assertIn("ai_reports.folder_id IS NULL", sql)

    def test_list_folders_selects_the_project_system_flag(self):
        """The column the count branches on has to be in the SELECT, or it is None.

        Asserted directly because the failure mode is invisible: the query runs, the
        folder is listed, the name is right — only the number is zero.
        """
        recorded: list = []

        def rows_for(sql):
            if "FROM ai_report_projects WHERE slug" in sql:
                return {"slug": "p1", "owner_email": OWNER, "system": False}
            if "count(*)" in sql:
                return {"n": 0}
            return [{"id": 7, "project_slug": "p1", "title": "F", "system": True,
                     "sort_order": 0}]

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            filing.list_folders("p1", OWNER)
        folder_select = [sql for sql, _ in recorded if "FROM ai_report_folders" in sql]
        self.assertTrue(folder_select, "list_folders issued no folder query")
        self.assertIn("p.system AS project_system", folder_select[0])


class JoinedStatementTests(unittest.TestCase):
    """⚠️ **A fake cursor accepts any SQL, so it cannot find a statement PostgreSQL
    rejects.** Every other test in this file stubs the connection, which means a
    statement that is syntactically fine and semantically impossible passes all of
    them. This class checks the one property the stub cannot see.

    The bug it exists for: `list_folders` JOINs `ai_report_projects` for the project's
    own `system` flag, and `id` / `system` / `created_at` / `updated_at` exist in
    **both** tables — so the unqualified column list became
    `AmbiguousColumn: column reference "id" is ambiguous`, a 500 that named neither the
    statement nor the endpoint. Found by running the endpoint, not by a test.
    """

    #: Columns that exist in both tables of the folder/project join.
    SHARED = ("id", "system", "created_at", "updated_at")

    def test_the_folder_column_list_is_fully_table_qualified(self):
        for column in filing._SELECT_FOLDER.split(","):
            column = column.strip()
            self.assertTrue(column, f"empty column in {filing._SELECT_FOLDER!r}")
            if column == "*":
                continue
            name = column.split()[-1]
            self.assertTrue(
                "." in name,
                f"{name!r} is unqualified; in this JOIN it is ambiguous with "
                f"ai_report_projects.{name}")

    def test_the_folder_column_list_names_no_column_the_project_table_also_has_unqualified(self):
        """Spelled out for the two columns this join really collides on."""
        text = filing._SELECT_FOLDER
        for name in self.SHARED:
            self.assertNotRegex(text, r"(?<![\w.])" + name + r"\b",
                                f"{name!r} appears unqualified in the JOIN column list")

    def test_every_join_site_qualifies_its_columns(self):
        """The general form of the rule, checked on the statements the code **issues**.

        ⚠️ Deliberately a runtime check and not a scan of the source. A regex over the
        file matches the word "join" inside a comment, and an assertion that fires on
        prose is an assertion a person turns off. Recording what actually goes to
        `execute()` cannot be fooled by a docstring, and it covers every call site
        rather than the one that broke today.
        """
        PROJECT = {"slug": "p1", "owner_email": OWNER, "system": False}
        FOLDER = {"id": 7, "project_slug": "p1", "title": "F", "system": True,
                  "sort_order": 0, "project_system": False}

        def rows_for(sql):
            if "FROM ai_report_projects WHERE slug" in sql:
                return PROJECT
            if "count(*)" in sql:
                return {"n": 0}
            if "FROM ai_report_folders" in sql:
                row = dict(FOLDER, owner_email=OWNER)
                # `get_folder` fetches ONE row (it also needs `p.owner_email`, which is
                # what distinguishes its statement); `list_folders` fetches MANY.
                # One fixture, two shapes — answering the wrong one fails on a
                # subscript instead of on the thing under test.
                return row if "p.owner_email" in sql else [row]
            return None

        recorded: list = []
        for call in (lambda: filing.list_folders("p1", OWNER),
                     lambda: filing.get_folder(7, OWNER)):
            recorded.clear()
            with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
                call()
            joins = [sql for sql, _ in recorded if " JOIN " in sql.upper()]
            self.assertTrue(joins, "expected the statement to join, found none")
            for sql in joins:
                select = sql.split(" FROM ", 1)[0]
                for name in self.SHARED:
                    self.assertFalse(
                        re.search(r"(?<![\w.])" + name + r"\b", select),
                        f"a JOIN selects {name!r} unqualified: {select.strip()[:140]}")

    def test_move_report_passes_one_owner_per_placeholder(self):
        recorded: list = []
        with _patched(recorded)[0], _patched(recorded)[1]:
            try:
                filing.move_report("r1", OWNER, "p1", 7)
            except Exception:
                pass          # the point is the statements, not the outcome
        _assert_placeholders(self, recorded, "move_report")


# ── 2. the card count and the list must be the same question ─────────────────

class CountMatchesListTests(unittest.TestCase):
    def test_the_card_predicate_is_the_mine_scope(self):
        """`MY_FILES` and the mine scope must agree, or the wall lies.

        Spelled out rather than derived: the two live in different files, and the day
        one of them gains a condition is the day a card reports a number the list
        behind it does not show.
        """
        scope_sql, count = reports_router._SCOPE_SQL["mine"]
        self.assertEqual(count, 1)
        self.assertIn("visibility = 'private'", scope_sql)
        self.assertIn("owner_email = %s", scope_sql)
        self.assertEqual(filing.MY_FILES, "owner_email = %s AND visibility = 'private'")

    def test_the_wall_counts_private_reports_only(self):
        """A published snapshot and a pulled copy are copies, not files filed.

        Asserted on the statement that **runs**, not on the source text: the SQL is
        assembled by concatenation, so grepping the file for the predicate would match
        a comment and prove nothing about the query the database receives.
        """
        recorder: list = []
        with _patched(recorder)[0], _patched(recorder)[1], \
                patch.object(filing, "ensure_unfiled", lambda e: ("unfiled-x", 1)):
            filing.list_projects(OWNER)
        counts = [sql for sql, _ in recorder if "count(*) AS n" in sql
                  or "AS report_count" in sql]
        self.assertTrue(counts, "the wall issued no count query at all")
        for sql in counts:
            self.assertIn("visibility = 'private'", sql)
            self.assertIn("owner_email = %s", sql)

    def test_the_wall_matches_the_unfiled_project_on_null(self):
        """未归档's own card counts `project_slug IS NULL`, not `= <its own slug>`."""
        recorder: list = []
        with _patched(recorder)[0], _patched(recorder)[1], \
                patch.object(filing, "ensure_unfiled", lambda e: ("unfiled-x", 1)):
            filing.list_projects(OWNER)
        sql = [s for s, _ in recorder if "AS report_count" in s][0]
        self.assertIn("CASE WHEN p.system THEN ai_reports.project_slug IS NULL", sql)

    def test_deleting_a_project_does_not_touch_a_public_snapshot(self):
        """Resetting placement is scoped to the caller's private files.

        A public snapshot in the project belongs to the public area, where placement
        is not a filing decision anybody made in that project.
        """
        source = open(REPORTS_SOURCE, encoding="utf-8").read()
        # The service owns this write, not the router — assert it there.
        service = open(os.path.join(API_DIR, "services", "report_projects.py"),
                       encoding="utf-8").read()
        block = service[service.index("def delete_project"):service.index("# ── folders")]
        self.assertIn("visibility = 'private'", block,
                      "delete_project would move somebody else's public row into 未归档")


def _project_count_sql():
    """The `report_count` subquery `list_projects` issues, as a standalone string."""
    source = open(os.path.join(API_DIR, "services", "report_projects.py"),
                  encoding="utf-8").read()
    start = source.index('" (SELECT count(*) FROM ai_reports WHERE"')
    end = source.index(' AS report_count,"', start)
    return source[start:end], ()

# ── 3. 未归档 ───────────────────────────────────────────────────────────────

class UnfiledTests(unittest.TestCase):
    def test_the_unfiled_slug_is_per_account_and_deterministic(self):
        a = filing.unfiled_slug(OWNER)
        self.assertEqual(a, filing.unfiled_slug(OWNER))
        self.assertNotEqual(a, filing.unfiled_slug(MATE))
        self.assertTrue(a.startswith(filing.UNFILED_SLUG_PREFIX))

    def test_the_slug_is_safe_for_a_url(self):
        """The slug goes in a path segment, so it must match the report slug rules."""
        slug = filing.unfiled_slug("渠道口径@corp.example")
        self.assertRegex(slug, r"^[a-z0-9][a-z0-9._-]{0,80}$")

    def test_ensure_unfiled_creates_the_project_and_the_folder(self):
        recorded: list = []

        def rows_for(sql):
            # The folder row is looked up, found missing, and then INSERTed RETURNING
            # an id — so `fetchone` has to answer the INSERT as well, or the function
            # fails on the subscript instead of on the behaviour under test.
            if "INSERT INTO ai_report_folders" in sql:
                return {"id": 11}
            return None

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            slug, folder_id = filing.ensure_unfiled(OWNER)
        self.assertEqual(slug, filing.unfiled_slug(OWNER))
        self.assertEqual(folder_id, 11)
        self.assertTrue(any("INSERT INTO ai_report_projects" in sql for sql, _ in recorded))
        self.assertTrue(any("INSERT INTO ai_report_folders" in sql for sql, _ in recorded))
        _assert_placeholders(self, recorded, "ensure_unfiled")

    def test_a_chinese_title_still_gets_a_usable_slug(self):
        """A title with no ASCII word must not be stripped to nothing.

        Otherwise every Chinese-named project would try the same empty slug and the
        second one created would collide with the first.
        """
        first = filing.clean_slug(None, "渠道口径")
        second = filing.clean_slug(None, "月度复盘")
        self.assertTrue(first)
        self.assertNotEqual(first, second)

    def test_both_halves_of_the_reserved_name_are_refused(self):
        """⚠️ A person types 未归档, not the two-language constant.

        Matching only the constant would let the Chinese half through — the half
        everybody actually types — and the wall would then show two cards reading
        未归档, with "put it in 未归档" ambiguous between them.
        """
        for typed in ("未归档", "Unfiled", "unfiled", filing.UNFILED_TITLE):
            self.assertTrue(filing._is_unfiled_name(typed), f"{typed!r} was accepted")
        self.assertFalse(filing._is_unfiled_name("未归档之外"))
        self.assertFalse(filing._is_unfiled_name("Archived"))

    def test_a_project_named_after_the_catch_all_is_refused(self):
        with self.assertRaises(filing.ReportProjectError):
            filing.create_project(OWNER, "未归档")

    def test_a_folder_named_after_the_catch_all_is_refused(self):
        with self.assertRaises(filing.ReportProjectError):
            filing.create_folder("p1", OWNER, "未归档")

    def test_the_system_project_cannot_be_renamed(self):
        recorded: list = []

        def rows_for(sql):
            if "SELECT id, title, system, owner_email FROM ai_report_projects" in sql:
                return {"id": 1, "title": filing.UNFILED_TITLE, "system": True,
                        "owner_email": OWNER}
            return None

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(filing.ReportProjectError):
                filing.update_project(filing.unfiled_slug(OWNER), OWNER, title="别的东西")

    def test_the_system_project_cannot_be_deleted(self):
        recorded: list = []

        def rows_for(sql):
            return {"id": 1, "system": True, "owner_email": OWNER}

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(filing.ReportProjectError):
                filing.delete_project(filing.unfiled_slug(OWNER), OWNER)

    def test_the_system_folder_cannot_be_deleted(self):
        recorded: list = []

        def rows_for(sql):
            return {"id": 3, "system": True, "owner_email": OWNER}

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(filing.ReportProjectError):
                filing.delete_folder(3, OWNER)

    def test_renaming_a_folder_to_the_reserved_name_is_refused(self):
        """The rename path is a second door into the same wall, so it needs the same lock."""
        recorded: list = []

        def rows_for(sql):
            if "FROM ai_report_folders f JOIN ai_report_projects" in sql:
                return {"id": 7, "system": False, "project_slug": "p1", "owner_email": OWNER}
            return None

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(filing.ReportProjectError):
                filing.rename_folder(7, OWNER, "未归档")


# ── 4. deleting a folder keeps the project ──────────────────────────────────

class DeleteSemanticsTests(unittest.TestCase):
    def test_deleting_a_folder_clears_only_the_folder_column(self):
        """⚠️ Resetting `project_slug` as well would drop the file out of the project.

        "I deleted a folder" must not also mean "I moved the reports out of the
        project" — and the two statements below are the whole difference.
        """
        recorded: list = []

        def rows_for(sql):
            if "FROM ai_report_folders f" in sql and "count(*)" not in sql:
                return {"id": 7, "system": False, "owner_email": OWNER}
            if "SELECT slug FROM ai_reports" in sql:
                return [{"slug": "r1"}, {"slug": "r2"}]
            return None

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            moved = filing.delete_folder(7, OWNER)

        self.assertEqual(moved, ["r1", "r2"])
        update = [sql for sql, _ in recorded if sql.strip().startswith("UPDATE ai_reports")]
        self.assertEqual(len(update), 1, "expected exactly one placement write")
        self.assertIn("folder_id = NULL", update[0])
        self.assertNotIn("project_slug = NULL", update[0],
                         "deleting a folder must not also unfile the report")

    def test_deleting_a_project_reports_what_fell_back(self):
        recorded: list = []

        def rows_for(sql):
            if "FROM ai_report_projects WHERE slug" in sql:
                # Match RealDictCursor: unselected fields must not appear.
                row = {"id": 2, "slug": "p1", "system": False, "owner_email": OWNER}
                columns = sql.split("FROM", 1)[0].removeprefix("SELECT ").strip().split(", ")
                return {column: row[column] for column in columns}
            if "SELECT slug FROM ai_reports" in sql:
                return [{"slug": "r9"}]
            return None

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            moved = filing.delete_project("p1", OWNER)
        self.assertEqual(moved, ["r9"])
        # The answer is what the client tells the reader, so it must be a real list.
        self.assertTrue(any("DELETE FROM ai_report_projects" in sql for sql, _ in recorded))
        for sql, params in recorded:
            if "FROM ai_reports WHERE project_slug" in sql or sql.startswith("UPDATE ai_reports"):
                self.assertIn("owner_email = %s", sql)
                self.assertEqual(params[-1], OWNER)


# ── 5. moving: ownership on both sides ──────────────────────────────────────

class MoveTests(unittest.TestCase):
    def test_a_folder_in_another_accounts_project_is_refused(self):
        """⚠️ The browser filters the target list; the server must not trust that.

        Checking only the report would let a caller file their own report into a
        stranger's folder, and the folder's own shares would then grant it.
        """
        recorded: list = []

        def rows_for(sql):
            if "FROM ai_reports WHERE slug" in sql:
                return {"id": 5, "slug": "r1", "owner_email": OWNER, "visibility": "private"}
            return None          # the folder lookup finds nothing

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(filing.ReportProjectError):
                filing.move_report("r1", OWNER, "theirs", 99)
        self.assertFalse(any("UPDATE ai_reports SET project_slug" in sql for sql, _ in recorded),
                         "the write happened even though the folder was refused")

    def test_a_report_that_is_not_mine_is_refused(self):
        recorded: list = []

        def rows_for(sql):
            return {"id": 5, "slug": "r1", "owner_email": STRANGER, "visibility": "private"}

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(PermissionError):
                filing.move_report("r1", OWNER, "mine", 7)

    def test_a_public_snapshot_cannot_be_filed(self):
        recorded: list = []

        def rows_for(sql):
            return {"id": 6, "slug": "r-public", "owner_email": OWNER, "visibility": "public"}

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            with self.assertRaises(PermissionError):
                filing.move_report("r-public", OWNER, "mine", 7)

    def test_normal_folder_in_system_project_keeps_its_folder_id(self):
        recorded = []

        def rows_for(sql):
            if "FOR UPDATE" in sql:
                return {"id": 5, "slug": "r1", "owner_email": OWNER, "visibility": "private"}
            if "FROM ai_report_folders f" in sql:
                return {"id": 7, "system": False, "project_system": True}
            return {"project_slug": None, "folder_id": 7}

        schema, database = _patched(recorded, rows_for)
        with schema, database:
            result = filing.move_report("r1", OWNER, "unfiled", 7)
        writes = [(sql, params) for sql, params in recorded if sql.startswith("UPDATE")]
        self.assertEqual(writes[0][1], (None, 7, 5))
        self.assertEqual(result["folder_id"], 7)

    def test_system_destinations_use_the_same_nulls_as_folder_counts(self):
        for project_system, folder_system, expected in (
            (False, False, ("p1", 7, 5)),
            (False, True, ("p1", None, 5)),
            (True, True, (None, None, 5)),
        ):
            with self.subTest(project_system=project_system, folder_system=folder_system):
                recorded = []

                def rows_for(sql):
                    if "FOR UPDATE" in sql:
                        return {"id": 5, "slug": "r1", "owner_email": OWNER, "visibility": "private"}
                    if "FROM ai_report_folders f" in sql:
                        self.assertIn("f.system", sql)
                        self.assertIn("p.system AS project_system", sql)
                        return {"id": 7, "system": folder_system, "project_system": project_system}
                    if "SELECT project_slug, folder_id" in sql:
                        return {"project_slug": expected[0], "folder_id": expected[1]}
                    return None

                schema, database = _patched(recorded, rows_for)
                with schema, database:
                    result = filing.move_report("r1", OWNER, "p1", 7)
                writes = [(sql, params) for sql, params in recorded if sql.startswith("UPDATE")]
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0][1], expected)
                self.assertEqual(result["folder_id"], expected[1])
                self.assertEqual(result["project_slug"], expected[0] or "")

    def test_moving_with_no_project_writes_the_null_pair(self):
        """Back to 未归档 is spelled as NULLs, not as the unfiled project's slug.

        Both spell the same place, and NULL is the value every report published
        before this feature already has — so a report filed and unfiled repeatedly
        ends up in the same state as one that was never filed.
        """
        recorded: list = []

        def rows_for(sql):
            if "FOR UPDATE" in sql:
                return {"id": 5, "slug": "r1", "owner_email": OWNER, "visibility": "private"}
            return {"project_slug": None, "folder_id": None}

        with _patched(recorded, rows_for)[0], _patched(recorded, rows_for)[1]:
            result = filing.move_report("r1", OWNER, "", None)
        self.assertEqual(result, {"slug": "r1", "project_slug": "", "folder_id": None})
        update = [sql for sql, _ in recorded if sql.strip().startswith("UPDATE ai_reports")]
        self.assertTrue(update and "project_slug = NULL" in update[0] and "folder_id = NULL" in update[0])


# ── 6. publish and pull must NOT copy placement ──────────────────────────────

class PlacementIsNotCopiedTests(unittest.TestCase):
    """⚠️ The trap: `_COPY_COLS` copies almost every column, so "just add the two new
    ones" looks like consistency and is not.

    A public snapshot and a pulled copy are *copies of a document*, not files filed in
    a folder. The pulled copy in particular has no folder of its own yet, and
    inheriting the source's would file a colleague's report into a folder the reader
    never chose — and would double the source project's count.
    """

    def test_the_two_columns_are_not_in_the_copy_list(self):
        self.assertNotIn("project_slug", reports_router._COPY_COLS)
        self.assertNotIn("folder_id", reports_router._COPY_COLS)

    def test_the_three_container_operations_are_the_only_writers(self):
        """Placement changes only on container writes and the legacy repair.

        Deleting a project or folder clears placement; moving files sets it.
        Schema initialization also normalizes old system-container IDs. Creating,
        publishing, pulling and uploading reports must never copy placement.
        """
        def writers_in(path: str) -> list[str]:
            text = open(path, encoding="utf-8").read()
            # Split on top-level defs so a docstring mention cannot be counted.
            chunks = re.split(r"(?m)^(?=def |@router\.)", text)
            found = []
            for chunk in chunks:
                if "UPDATE ai_reports SET project_slug" not in chunk:
                    continue
                name = re.search(r"(?m)^def (\w+)", chunk)
                found.append(name.group(1) if name else "<module level>")
            return sorted(found)

        service = os.path.join(API_DIR, "services", "report_projects.py")
        self.assertEqual(writers_in(service),
                         ["delete_project", "move_report", "normalize_legacy_placement"],
                         "a new writer of the placement columns appeared")
        # `delete_folder` clears only `folder_id`, so it is checked by its own column.
        folder_writers = writers_in(service)
        self.assertNotIn("delete_folder", folder_writers)
        text = open(service, encoding="utf-8").read()
        self.assertIn("DELETE FROM ai_report_folders", text)
        # …and the router writes neither, so the HTTP surface cannot fork the rule.
        self.assertEqual(writers_in(REPORTS_SOURCE), [],
                         "the router must not write placement; the service owns it")


# ── 7. the unfiled translations on the list path ────────────────────────────

class ListTranslationTests(unittest.TestCase):
    """⚠️ Both of these translate a container into a NULL test, and both translations
    are load-bearing: filtering on the ids instead matches **nothing**, so the wall's
    own catch-all would report itself permanently blank."""

    def test_project_system_folder_list_uses_null_and_its_project(self):
        import asyncio
        from types import SimpleNamespace

        recorded = []
        database = _db_for(recorded, lambda sql: [])
        folder = SimpleNamespace(system=True, project_slug="p1")
        with patch.object(reports_router, "_ensure_table", lambda: None), \
             patch.object(reports_router, "_identity", return_value=(OWNER, "user")), \
             patch.object(reports_router, "_db", database), \
             patch.object(filing, "ensure_unfiled", return_value=("unfiled", 1)), \
             patch.object(filing, "get_folder", return_value=folder):
            asyncio.run(reports_router.list_reports(
                request=None, q="", status="", scope="mine", category="", tag="",
                project="p1", folder_id=7, limit=200))
        sql, params = recorded[0]
        self.assertIn("folder_id IS NULL", sql)
        self.assertNotIn("folder_id = %s", sql)
        self.assertEqual(params.count("p1"), 2)
        self.assertEqual(sql.count("%s"), len(params))

    def test_the_unfiled_project_translates_to_is_null(self):
        source = open(REPORTS_SOURCE, encoding="utf-8").read()
        block = source[source.index("if project.strip():"):source.index("if folder_id is not None:")]
        self.assertIn("if project.strip() == unfiled_project:", block)
        self.assertIn('where.append("project_slug IS NULL")', block)

    def test_the_unfiled_folder_translates_to_is_null(self):
        source = open(REPORTS_SOURCE, encoding="utf-8").read()
        block = source[source.index("if folder_id is not None:"):source.index('sql = f"SELECT')]
        self.assertIn("if folder_id == unfiled_folder:", block)
        self.assertIn('where.append("folder_id IS NULL")', block)

    def test_a_normal_container_still_filters_by_its_own_id(self):
        source = open(REPORTS_SOURCE, encoding="utf-8").read()
        block = source[source.index("if project.strip():"):source.index('sql = f"SELECT')]
        self.assertIn('where.append("project_slug = %s")', block)
        self.assertIn('where.append("folder_id = %s")', block)

    def test_the_list_ships_the_raw_placement_columns(self):
        """The API answers with the stored pair, NOT resolved to a container id.

        Resolving here would call `ensure_unfiled()` — which WRITES — on the list
        path, so a plain read would create rows.
        """
        self.assertIn("project_slug", reports_router._SELECT_COLS)
        self.assertIn("folder_id", reports_router._SELECT_COLS)
        shaped = reports_router._meta({"id": 1, "slug": "r", "title": "t",
                                       "project_slug": None, "folder_id": None})
        self.assertEqual(shaped["project_slug"], "")
        self.assertIsNone(shaped["folder_id"])


# ── 8. route order (a source fact, true before the app boots) ───────────────

_ROUTE_RE = re.compile(r'^@router\.(?:get|post|put|patch|delete)\("(/[^"]*)"\)\s*$', re.M)


def _route_lines() -> list[tuple[int, str]]:
    with open(REPORTS_SOURCE, encoding="utf-8") as handle:
        return [(m.start(), m.group(1)) for m in _ROUTE_RE.finditer(handle.read())]


class RouteOrderTests(unittest.TestCase):
    def test_the_container_routes_are_registered_before_the_catch_all(self):
        with open(REPORTS_SOURCE, encoding="utf-8") as handle:
            text = handle.read()
        positions = [(m.start(), m.group(1)) for m in _ROUTE_RE.finditer(text)]
        catch_alls = [pos for pos, path in positions if path == "/{slug}"]
        self.assertTrue(catch_alls, "the /{slug} catch-all was renamed or removed — re-check this guard")
        first_catch = catch_alls[0]
        containers = [(pos, path) for pos, path in positions
                      if path.startswith("/projects") or path.startswith("/folders")]
        self.assertGreaterEqual(len(containers), 10, "the container routes are missing")
        after = [path for pos, path in containers if pos > first_catch]
        self.assertEqual(after, [],
                         f"these would be read as a report whose slug is that string: {after}")

    def test_the_probe_would_catch_a_planted_violation(self):
        """The parser is the thing that can silently break, so it gets its own test."""
        sample = (
            '@router.get("/projects")\n'
            '@router.get("/{slug}")\n'
            '@router.get("/projects/{slug}/cover")\n'
        )
        positions = [(m.start(), m.group(1)) for m in _ROUTE_RE.finditer(sample)]
        first_catch = next(pos for pos, path in positions if path == "/{slug}")
        after = [path for pos, path in positions
                 if pos > first_catch and (path.startswith("/projects") or path.startswith("/folders"))]
        self.assertEqual(after, ["/projects/{slug}/cover"])

    def test_every_container_path_is_reachable_in_the_imported_router(self):
        """The source order and the built table must agree.

        Source inspection proves the *file* is ordered; this proves the *app* is. A
        decorator that never runs (an early `return`, a conditional definition) would
        satisfy the first and fail this.
        """
        paths = {getattr(route, "path", "") for route in reports_router.router.routes}
        for required in ("/projects", "/projects/{slug}", "/projects/{slug}/folders",
                         "/projects/{slug}/cover", "/folders/{folder_id}", "/{slug}/move"):
            self.assertIn(required, paths, f"{required} is not registered on the router")


if __name__ == "__main__":
    unittest.main()
