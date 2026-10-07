"""项目 / 文件夹 — the containers a knowledge page is filed into.

What this file guards
---------------------
Three things, in the order they actually broke while being written:

1. **The `%s` count.** `READABLE_PREDICATE` carries one email per branch and it is
   embedded inside subqueries, so a statement can be short by two and still look
   perfect in the source. The failure is a 500 from psycopg2 ("tuple index out of
   range") naming neither the statement nor the cause — which is exactly how the
   first version of `list_projects` shipped. Every statement here therefore passes
   one email per placeholder, computed from the SQL rather than counted by hand.

2. **未归档 is real, and it is protected.** A project and a folder both have a
   system container; neither can be renamed or deleted, and an unfiled page
   resolves to the reader's own.

3. **A folder share is a share of every page in it, now and later.** The whole
   feature rests on that, and a change to the predicate that quietly dropped the
   third branch would leave every list and every search omitting exactly the pages
   the share exists to deliver — with no error anywhere.

Storage-level: no browser, no HTTP. `verify_knowledge_projects_ui.py` is what
proves the wall renders.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.ai import business_knowledge as pages            # noqa: E402
from services.ai import knowledge_projects as filing           # noqa: E402

OWNER = "owner@corp.example"
MATE = "mate@corp.example"
STRANGER = "stranger@other.example"

#: The predicate's placeholder count, by name. Every caller in this feature passes
#: one email per placeholder; the assertions below read this, so a new branch in the
#: predicate fails the callers instead of the database.
PREDICATE_EMAILS = 3


def _cursor(recorder: dict):
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            recorder["sql"] = sql
            recorder["params"] = params
            recorder.setdefault("all", []).append((sql, params))

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    return _Cursor()


def _db_for(recorder: dict):
    """A `_db` replacement — a **callable returning** the context manager.

    ⚠️ Passing the context manager itself does not work: `with _db() as conn` would
    call the MagicMock, get its `return_value`, and enter *that* — so the recording
    cursor below is never reached and the test fails on a missing key rather than on
    the thing it is about.
    """
    conn = unittest.mock.MagicMock()
    conn.cursor.return_value = _cursor(recorder)
    ctx = unittest.mock.MagicMock()
    ctx.__enter__.return_value = conn
    return lambda: ctx


# ── 1. the placeholder count ────────────────────────────────────────────────

class PlaceholderTests(unittest.TestCase):
    """One email per `%s`. The bug this exists for produced a 500, not a test
    failure, because the mismatch was only ever visible to the database driver."""

    def test_the_predicate_has_one_placeholder_per_branch(self):
        self.assertEqual(pages.READABLE_PREDICATE.count("%s"), PREDICATE_EMAILS)

    def test_the_projects_list_passes_one_email_per_placeholder(self):
        recorder: dict = {}
        with patch.object(filing.pages, "ensure_tables", lambda: None), \
             patch.object(filing, "ensure_unfiled", lambda e: ("unfiled-x", 1)), \
             patch.object(filing, "_db", _db_for(recorder)):
            filing.list_projects(OWNER)
        sql, params = recorder["sql"], recorder["params"]
        self.assertEqual(len(params), sql.count("%s"),
                         "list_projects: the statement and its parameters disagree")
        self.assertEqual(set(params), {OWNER})

    def test_the_folder_count_passes_one_email_per_placeholder(self):
        """Every statement this feature issues, checked against its own placeholders.

        The rows are dicts because the real cursors are `RealDictCursor`; returning a
        stand-in object instead fails on a subscript rather than on the count, which
        is how the first version of this test read while passing nothing.
        """
        recorded: list = []
        PROJECT = {"slug": "p1", "owner_email": OWNER, "system": False}
        FOLDER = {"id": 7, "project_slug": "p1", "title": "F", "system": False, "sort_order": 0}

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                recorded.append((sql, params))

            def fetchone(self):
                # The project lookup reads one row; the per-folder count reads an
                # aggregate. Both go through fetchone, so the branch is on the SQL.
                sql = recorded[-1][0] if recorded else ""
                return PROJECT if "FROM ai_knowledge_projects WHERE slug" in sql else {"n": 0}

            def fetchall(self):
                sql = recorded[-1][0] if recorded else ""
                # One row here is a folder row, a project row, or a share row, and
                # they are told apart by the table in the statement.
                if "FROM ai_knowledge_projects WHERE slug" in sql:
                    return [PROJECT]
                if "ai_knowledge_folder_shares WHERE folder_id" in sql:
                    return [{"recipient_email": MATE}]
                return [FOLDER]

        conn = unittest.mock.MagicMock()
        conn.cursor.return_value = _C()
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = conn
        with patch.object(filing.pages, "ensure_tables", lambda: None), \
             patch.object(filing, "_db", lambda: ctx):
            rows = filing.list_folders("p1", OWNER)

        self.assertEqual(len(rows), 1)
        counted = [(sql, params) for sql, params in recorded if "count(*)" in sql]
        self.assertTrue(counted, "no count query was issued at all")
        for sql, params in counted:
            self.assertEqual(len(params), sql.count("%s"),
                             f"placeholder mismatch in: {sql}")

    def test_the_folder_count_includes_the_folder_id_and_three_emails(self):
        """Spelled out, because this is the one that is easy to get wrong twice."""
        cursor_sql = ("SELECT count(*) AS n FROM ai_knowledge_items WHERE folder_id = %s AND "
                      + pages.READABLE_PREDICATE)
        self.assertEqual(cursor_sql.count("%s"), 1 + PREDICATE_EMAILS)


# ── 2. 未归档 ───────────────────────────────────────────────────────────────

class UnfiledTests(unittest.TestCase):
    def test_the_unfiled_slug_is_per_account_and_deterministic(self):
        a = pages.unfiled_slug(OWNER)
        self.assertEqual(a, pages.unfiled_slug(OWNER))
        self.assertNotEqual(a, pages.unfiled_slug(MATE))
        self.assertTrue(a.startswith(pages.UNFILED_SLUG_PREFIX))

    def test_the_unfiled_slug_is_not_a_guessable_address(self):
        """The slug is derived from the account's address, so it must not read as
        one: a URL in a shared screenshot would otherwise name the owner."""
        self.assertNotIn(OWNER.split("@")[0], pages.unfiled_slug(OWNER))
        self.assertNotIn(OWNER.split("@")[1].split(".")[0], pages.unfiled_slug(OWNER))

    def test_a_page_with_no_project_reads_back_as_the_unfiled_project(self):
        """NULL is a real answer, not missing data. A page written before projects
        existed must still be shown under a container, and the list filter has to
        agree with the shape the client is handed."""
        captured: dict = {}

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        conn = unittest.mock.MagicMock()
        conn.cursor.return_value = _C()
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = conn
        with patch.object(pages, "ensure_tables", lambda: None), \
             patch.object(pages, "_db", lambda: ctx):
            pages.list_items(OWNER, project=pages.unfiled_slug(OWNER))
        self.assertIn("project_slug IS NULL", captured["sql"])
        self.assertNotIn("project_slug = %s", captured["sql"])

    def test_a_real_project_filters_on_its_slug(self):
        captured: dict = {}

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        conn = unittest.mock.MagicMock()
        conn.cursor.return_value = _C()
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = conn
        with patch.object(pages, "ensure_tables", lambda: None), \
             patch.object(pages, "_db", lambda: ctx):
            pages.list_items(OWNER, project="channels")
        self.assertIn("project_slug = %s", captured["sql"])


# ── 3. the cascade ──────────────────────────────────────────────────────────

class CascadeTests(unittest.TestCase):
    def test_the_shared_view_asks_about_both_grants(self):
        """A page filed into a folder somebody shared with me is "shared with me".
        Leaving the second EXISTS out would not error — it would just quietly omit
        the pages the feature exists to deliver."""
        captured: dict = {}

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        conn = unittest.mock.MagicMock()
        conn.cursor.return_value = _C()
        ctx = unittest.mock.MagicMock()
        ctx.__enter__.return_value = conn
        with patch.object(pages, "ensure_tables", lambda: None), \
             patch.object(pages, "_db", lambda: ctx):
            pages.list_items(MATE, scope="shared")
        self.assertIn("ai_knowledge_item_shares", captured["sql"])
        self.assertIn("ai_knowledge_folder_shares", captured["sql"])
        # Three emails (owner, item share, folder share) plus the LIMIT. The LIMIT
        # is why this is written as "equals the placeholder count" rather than as a
        # literal: a count that omits it is a number that has to be re-derived every
        # time a filter is added.
        self.assertEqual(len(captured["params"]), captured["sql"].count("%s"))

    def test_the_python_rule_and_the_sql_rule_both_know_the_folder_grant(self):
        row = pages.KnowledgeItem(id=1, slug="s", title="t", owner_email=OWNER,
                                  visibility="private", status="published",
                                  folder_id=9).__dict__
        with patch.object(pages, "_share_exists", return_value=False), \
             patch.object(pages, "_folder_share_exists", return_value=True) as folder:
            self.assertTrue(pages.may_read(row, MATE))
        folder.assert_called_once_with(9, MATE)

    def test_a_page_that_is_not_filed_cannot_be_reached_through_a_folder_grant(self):
        """The reader's own 未归档 is per-account, so no other account's share can
        reach into it. Without the `folder_id` guard this would 500 on a NULL, or —
        worse, once someone fixes that — match every unfiled page."""
        row = pages.KnowledgeItem(id=1, slug="s", title="t", owner_email=OWNER,
                                  visibility="private", status="published").__dict__
        with patch.object(pages, "_share_exists", return_value=False), \
             patch.object(pages, "_folder_share_exists") as folder:
            self.assertFalse(pages.may_read(row, MATE))
        folder.assert_not_called()

    def test_a_draft_is_not_reachable_through_a_folder_share_either(self):
        """Same conditions as an item share: a folder grants access to *published*
        pages. A folder grant is not a way to publish."""
        row = pages.KnowledgeItem(id=1, slug="s", title="t", owner_email=OWNER,
                                  visibility="private", status="draft", folder_id=9).__dict__
        with patch.object(pages, "_share_exists", return_value=False), \
             patch.object(pages, "_folder_share_exists", return_value=True):
            self.assertFalse(pages.may_read(row, MATE))


# ── 4. the guard rails around the system containers ─────────────────────────

class SystemContainerTests(unittest.TestCase):
    """These are unit tests over the SOURCE, not over a database: each rule is a
    `raise` whose text is the promise the UI makes, and a promise nobody tests is a
    promise that quietly stops being kept."""

    def _source(self):
        import inspect
        return inspect.getsource(filing)

    def test_renaming_the_unfiled_project_is_refused(self):
        self.assertIn("cannot be renamed", self._source())

    def test_deleting_the_unfiled_project_is_refused(self):
        self.assertIn("cannot be deleted", self._source())

    def test_both_unfiled_containers_are_refused_by_name(self):
        # Two refusals, one for the project and one for the folder. Counted rather
        # than searched for, so removing one of them fails here.
        self.assertGreaterEqual(self._source().count("cannot be renamed"), 2)
        self.assertGreaterEqual(self._source().count("cannot be deleted"), 2)

    def test_deleting_a_container_never_deletes_the_pages_in_it(self):
        src = self._source()
        # Both delete paths re-file the pages rather than removing rows, and both say
        # so. An `UPDATE ... SET project_slug = NULL` and an `UPDATE ... SET
        # folder_id = NULL`; a `DELETE FROM ai_knowledge_items` here would be the bug.
        self.assertIn("UPDATE ai_knowledge_items SET project_slug = NULL, folder_id = NULL", src)
        self.assertIn("UPDATE ai_knowledge_items SET folder_id = NULL", src)
        self.assertNotIn("DELETE FROM ai_knowledge_items", src)

    def test_a_folder_share_is_one_row_not_one_per_page(self):
        """The reason the feature is worth building. One INSERT, no loop over the
        folder's contents; if this ever becomes a loop the cascade is gone."""
        src = inspect_src(filing.share_folder)
        self.assertIn("ON CONFLICT (folder_id, recipient_email) DO NOTHING", src)
        self.assertIn("for recipient in cleaned:", src)
        self.assertNotIn("ai_knowledge_items", src)

    def test_filing_a_page_checks_the_folder_belongs_to_the_caller(self):
        src = inspect_src(pages.move_item)
        self.assertIn("p.owner_email = %s", src)


def inspect_src(func) -> str:
    import inspect
    return inspect.getsource(func)


# ── 5. the schema the feature needs ─────────────────────────────────────────

class SchemaTests(unittest.TestCase):
    def test_the_three_tables_and_two_columns_are_created(self):
        import inspect
        src = inspect.getsource(pages.ensure_tables)
        for fragment in ("ai_knowledge_projects", "ai_knowledge_folders",
                         "ai_knowledge_folder_shares",
                         "ADD COLUMN IF NOT EXISTS project_slug",
                         "ADD COLUMN IF NOT EXISTS folder_id"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, src)

    def test_folders_hang_off_their_project_and_shares_off_their_folder(self):
        import inspect
        src = inspect.getsource(pages.ensure_tables)
        self.assertIn("REFERENCES ai_knowledge_projects(slug) ON DELETE CASCADE", src)
        self.assertIn("REFERENCES ai_knowledge_folders(id) ON DELETE CASCADE", src)

    def test_exactly_one_unfiled_folder_per_project_is_enforced_by_an_index(self):
        """Not by code. The folder every unfiled page resolves to has to be findable
        by a unique key, or two of them would split those pages in half — and the
        lazy creation path is reachable from every read."""
        import inspect
        src = inspect.getsource(pages.ensure_tables)
        self.assertIn("ai_knowledge_folders_system_idx", src)
        self.assertIn("ON ai_knowledge_folders (project_slug) WHERE system", src)

    def test_the_cover_reuses_the_workspace_columns_rather_than_a_second_shape(self):
        import inspect
        src = inspect.getsource(pages.ensure_tables)
        for column in ("cover_data", "cover_mime", "cover_w", "cover_h",
                       "cover_prompt", "cover_model", "cover_url"):
            with self.subTest(column=column):
                self.assertIn(column, src)

    def test_a_closed_accounts_projects_are_cascaded_away(self):
        """Otherwise the address could be registered again and inherit the old wall."""
        from klado_shared import lifecycle
        tables = {table for table, _, _ in lifecycle.OWNED_CONTENT}
        self.assertIn("ai_knowledge_projects", tables)
        sidecars = {table for table, _, _ in lifecycle.OWNED_SIDECARS}
        self.assertIn("ai_knowledge_folder_shares", sidecars)


# ── 6. the router surface ───────────────────────────────────────────────────

class RouterTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from routers import business_knowledge as router_module
        self.module = router_module
        app = FastAPI()

        @app.middleware("http")
        async def _identity(request: Request, call_next):
            request.state.current_user = {"email": OWNER, "role": "user"}
            request.state.auth_kind = "browser"
            return await call_next(request)

        app.include_router(router_module.router, prefix="/api/knowledge")
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_the_agent_may_create_a_project_and_a_folder(self):
        """The agent's entry point for "make me a folder". It is a write on the
        account's own content, the same category as POST /items, so it must NOT be
        behind the browser-only gate that guards sharing."""
        import inspect
        for name in ("create_project", "create_folder"):
            with self.subTest(endpoint=name):
                src = inspect.getsource(getattr(self.module, name))
                self.assertNotIn("_require_browser", src)

    def test_sharing_a_folder_stays_browser_only_and_gated(self):
        import inspect
        src = inspect.getsource(self.module.share_folder)
        self.assertIn("_require_browser", src)
        self.assertIn("may_share_to_many", src)

    def test_the_cover_is_a_separate_endpoint_not_part_of_the_project_put(self):
        """A cover-only save must not blank the title, and the project PUT is a
        whole-row overwrite."""
        import inspect
        self.assertNotIn("cover", inspect.getsource(self.module.update_project).lower())
        # Through `_cover_from_body`, which is the one place that maps this body's
        # errors to 400/502 — asserting on the direct call would pin a refactor.
        self.assertIn("_cover_from_body", inspect.getsource(self.module.set_project_cover))
        self.assertIn("report_cover.resolve_cover", inspect.getsource(self.module._cover_from_body))

    def test_a_move_needs_no_body_to_be_well_formed(self):
        """An empty `project_slug` IS the instruction to file under 未归档.

        The store is stubbed, so the assertion is about the two things in front of it:
        the body parses (a 422 here would mean "file it under 未归档" is not
        expressible), and an unknown page is a 404 rather than a 403 — a stranger must
        not learn that a page with this slug exists.
        """
        with patch.object(self.module.store, "move_item",
                          side_effect=PermissionError("knowledge item not found")):
            move = self.client.post("/api/knowledge/items/x/move", json={})
        self.assertEqual(move.status_code, 404)

    def test_a_move_reaches_the_store_with_the_body_it_was_given(self):
        """The empty-string spelling of 未归档, end to end through the router."""
        seen = {}

        def fake_move(slug, email, project_slug, folder_id):
            seen.update(slug=slug, email=email,
                        project_slug=project_slug, folder_id=folder_id)
            return pages.KnowledgeItem(id=1, slug=slug, title="T", owner_email=email)

        with patch.object(self.module.store, "move_item", side_effect=fake_move):
            resp = self.client.post("/api/knowledge/items/k1/move",
                                    json={"project_slug": "channels", "folder_id": 7})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen, {"slug": "k1", "email": OWNER,
                                "project_slug": "channels", "folder_id": 7})


if __name__ == "__main__":
    unittest.main()
