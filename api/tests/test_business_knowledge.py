"""Business knowledge base — permissions, markdown-only, and the agent boundary.

The property under test is the one the whole feature rests on: **a page is only
readable by its owner, by colleagues it was explicitly shared with, or by anyone
once a public snapshot exists** — and "not readable" must be indistinguishable
from "does not exist" (the status code itself must not confirm that someone else
owns a page at a guessed slug).

No database is required: the pure rules are tested directly and the router is
tested against a stubbed store, which is how ``test_knowledge_router.py`` works too.
"""
import inspect
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers import business_knowledge as router_module
from services.ai import business_knowledge as store
from services.ai.business_knowledge import BusinessKnowledgeError, KnowledgeItem

UNFILED_TEST_SLUG = "unfiled-test0001"
UNFILED_TEST_FOLDER = 42

# ⚠️ **Module-wide, on purpose.** Every router test below mocks the store, and the one
# thing the router now also does on a read is resolve the account's 未归档 container
# (`knowledge_projects.ensure_unfiled`). Left unstubbed it opens a real connection, so
# the whole file passed on a developer machine with a live database and then failed
# with 500s in CI, where `scripts/ci_check.py` points the database at loopback port 1
# on purpose (no production credentials in the test run). A test file that is green
# only when a database happens to be reachable is not self-sufficient, and CI is the
# only place that says so.
#
# Started once here rather than per-test: it is a fixture, not a behaviour under test,
# and per-test patching is what let this be forgotten in the first place.
patch.object(router_module.filing, "ensure_unfiled",
             return_value=(UNFILED_TEST_SLUG, UNFILED_TEST_FOLDER)).start()

OWNER = "owner@example.com"
COLLEAGUE = "mate@example.com"
STRANGER = "other@example.com"


def _item(**overrides) -> KnowledgeItem:
    base = dict(
        id=1, slug="channel-notes", title="渠道笔记", summary="", category="渠道",
        tags="渠道,甲公司", document_type="note", body="# 渠道笔记\n\n正文",
        author="agent", submitter="Zhen Huang", version="1.0", status="published",
        owner_email=OWNER, visibility="private", source_item_id=None, size_bytes=40,
    )
    base.update(overrides)
    return KnowledgeItem(**base)


def _client() -> TestClient:
    """Router + a stand-in for the auth middleware (sets the same request.state)."""
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": request.headers.get("X-Test-User", OWNER)}
        request.state.auth_kind = request.headers.get("X-Test-Kind", "browser")
        return await call_next(request)

    app.include_router(router_module.router, prefix="/api/knowledge")
    return TestClient(app, raise_server_exceptions=False)


# ── markdown-only ────────────────────────────────────────────────────────────

class MarkdownOnlyTests(unittest.TestCase):
    def test_markdown_is_accepted_and_stripped(self):
        self.assertEqual(store.validate_markdown("\n\n# 标题\n正文\n\n"), "# 标题\n正文")

    def test_html_document_is_rejected_with_a_pointer_to_the_right_endpoint(self):
        for payload in ("<!DOCTYPE html><html><body>x</body></html>", "<html>\n<body>hi</body>"):
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(BusinessKnowledgeError) as ctx:
                    store.validate_markdown(payload)
                self.assertIn("/api/reports", str(ctx.exception))

    def test_empty_and_missing_and_non_string_bodies_are_rejected(self):
        for payload in ("", "   ", None, {"a": 1}):
            with self.subTest(payload=payload):
                with self.assertRaises(BusinessKnowledgeError):
                    store.validate_markdown(payload)

    def test_oversize_body_is_rejected_with_the_actual_size(self):
        with self.assertRaises(BusinessKnowledgeError) as ctx:
            store.validate_markdown("x" * (store.MAX_BODY_BYTES + 10))
        self.assertIn("markdown limit", str(ctx.exception))

    def test_binary_blob_is_rejected(self):
        with self.assertRaises(BusinessKnowledgeError):
            store.validate_markdown("\x00\x01\x02\x03" * 50)

    def test_slug_is_deterministic_and_url_safe(self):
        self.assertEqual(store.clean_slug("", "A B/C!"), "a-b-c")
        self.assertEqual(store.clean_slug("Hello World", "x"), "hello-world")
        self.assertEqual(store.clean_slug("", ""), "untitled")


# ── the permission matrix ────────────────────────────────────────────────────

class PermissionTests(unittest.TestCase):
    def test_owner_reads_own_private_page(self):
        self.assertTrue(store.may_read(_item().__dict__, OWNER))

    def test_a_stranger_does_not_read_a_private_page(self):
        with patch.object(store, "_share_exists", return_value=False):
            self.assertFalse(store.may_read(_item().__dict__, STRANGER))

    def test_a_colleague_reads_it_only_when_shared(self):
        row = _item().__dict__
        with patch.object(store, "_share_exists", return_value=True):
            self.assertTrue(store.may_read(row, COLLEAGUE))
        with patch.object(store, "_share_exists", return_value=False):
            self.assertFalse(store.may_read(row, COLLEAGUE))

    def test_public_snapshot_is_readable_by_anyone_signed_in(self):
        row = _item(visibility="public", source_item_id=1).__dict__
        self.assertTrue(store.may_read(row, STRANGER))

    def test_a_draft_is_never_readable_by_others_even_when_public(self):
        row = _item(visibility="public", status="draft").__dict__
        with patch.object(store, "_share_exists", return_value=False):
            self.assertFalse(store.may_read(row, STRANGER))

    def test_an_archived_private_page_is_not_shared_readable(self):
        row = _item(status="archived").__dict__
        with patch.object(store, "_share_exists", return_value=True):
            self.assertFalse(store.may_read(row, COLLEAGUE))

    def test_the_sql_predicate_matches_the_python_rule(self):
        """The two implementations of one rule must not drift apart.

        Reads through the wiki go through `may_read`; reads through the agent's
        knowledge search go through READABLE_PREDICATE. A gap between them is a
        silent permission hole in whichever path was forgotten.
        """
        sql = store.READABLE_PREDICATE
        for fragment in ("owner_email = %s", "visibility = 'public'", "status = 'published'",
                         "ai_knowledge_item_shares", "recipient_email = %s",
                         # The folder grant (2026-10-03). Listed here because a share
                         # of a folder IS a share of its pages: dropping this fragment
                         # would make every list and every search silently omit the
                         # pages a folder share exists to deliver, while the direct
                         # read below still worked — the two rules disagreeing.
                         "ai_knowledge_folder_shares", "folder_id = ai_knowledge_items.folder_id"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sql)
        # exactly three placeholders: the owner and one per share branch
        self.assertEqual(sql.count("%s"), 3)


# ── router surface ───────────────────────────────────────────────────────────

class RouterTests(unittest.TestCase):
    def test_list_passes_the_caller_identity_to_the_store(self):
        captured = {}

        def fake_list(email, *, scope="mine", q="", limit=200, project="", folder_id=None,
                      unfiled_folder=None):
            captured.update(email=email, scope=scope, project=project, folder_id=folder_id,
                            unfiled_folder=unfiled_folder)
            return [_item()]

        with patch.object(store, "list_items", side_effect=fake_list), \
             patch.object(router_module.filing, "ensure_unfiled",
                          return_value=(UNFILED_TEST_SLUG, UNFILED_TEST_FOLDER)):
            resp = _client().get("/api/knowledge/items", headers={"X-Test-User": COLLEAGUE})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["email"], COLLEAGUE)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["categories"], ["渠道"])
        # A colleague is not the owner: the UI must not offer Edit / Delete.
        self.assertFalse(body["items"][0]["can_manage"])
        self.assertEqual(body["items"][0]["document_id"], "kb:channel-notes")
        # An unfiled page is reported under the reader's 未归档 container, never as
        # a null the client would have to interpret. `unfiled_folder` reaches the
        # store so the filter can match the NULL placement.
        self.assertEqual(captured["unfiled_folder"], UNFILED_TEST_FOLDER)
        self.assertEqual(body["items"][0]["project_slug"], UNFILED_TEST_SLUG)
        self.assertEqual(body["items"][0]["folder_id"], UNFILED_TEST_FOLDER)

    def test_owner_sees_can_manage(self):
        with patch.object(store, "list_items", return_value=[_item()]):
            resp = _client().get("/api/knowledge/items", headers={"X-Test-User": OWNER})
        self.assertTrue(resp.json()["items"][0]["can_manage"])

    def test_reading_someone_elses_page_is_404_not_403(self):
        """404 for "not yours" as well as "missing": the code must not confirm
        that another account owns a page at this slug."""
        with patch.object(store, "get_item", return_value=None):
            resp = _client().get("/api/knowledge/items/secret", headers={"X-Test-User": STRANGER})
        self.assertEqual(resp.status_code, 404)

    def test_get_returns_the_markdown_body_for_the_editor(self):
        with patch.object(store, "get_item", return_value=_item()):
            resp = _client().get("/api/knowledge/items/channel-notes")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["body"], "# 渠道笔记\n\n正文")
        self.assertEqual(resp.json()["tags"], ["渠道", "甲公司"])

    def test_create_rejects_a_non_markdown_body_with_400(self):
        with patch.object(store, "upsert_item", side_effect=BusinessKnowledgeError("body is empty")):
            resp = _client().post("/api/knowledge/items", json={"title": "x", "body": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("body is empty", resp.json()["detail"])

    def test_create_never_overwrites_another_accounts_slug(self):
        with patch.object(store, "upsert_item", side_effect=PermissionError("another account")):
            resp = _client().post("/api/knowledge/items",
                                  json={"title": "x", "body": "md"},
                                  headers={"X-Test-User": STRANGER})
        self.assertEqual(resp.status_code, 404)

    def test_agent_may_create_and_update_its_owners_pages(self):
        with patch.object(store, "upsert_item", return_value=_item()) as call:
            resp = _client().post("/api/knowledge/items", json={"title": "x", "body": "# x"},
                                  headers={"X-Test-Kind": "agent"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(call.call_args[0][1], OWNER)          # bound to the code's account

    def test_publishing_pulling_and_sharing_are_browser_only(self):
        """An agent access code must not be able to widen access.

        The middleware's prefix allowlist admits `/api/knowledge/items`, so this
        refusal is the only thing standing between a leaked agent code and
        publishing the owner's private notes to every account.
        """
        with patch("routers.reports.settings") as settings:
            settings.AUTH_ENABLED = True
            for method, path, body in (
                ("post", "/api/knowledge/items/channel-notes/publish", None),
                ("post", "/api/knowledge/items/channel-notes/pull", None),
                ("post", "/api/knowledge/items/channel-notes/shares", {"emails": [STRANGER]}),
                ("delete", "/api/knowledge/items/channel-notes/public", None),
                ("delete", "/api/knowledge/items/channel-notes/shares?email=x%40example.com", None),
            ):
                with self.subTest(path=path):
                    client = _client()
                    headers = {"X-Test-Kind": "agent"}
                    if method == "delete":
                        resp = client.delete(path, headers=headers)
                    else:
                        resp = client.post(path, json=body, headers=headers)
                    self.assertEqual(resp.status_code, 403)

    def test_browser_may_publish(self):
        with patch.object(router_module.orgs, "effective_profile",
                          return_value=router_module.orgs.UNRESTRICTED), \
             patch.object(router_module.inbox, "emit"), \
             patch.object(store, "publish_to_public", return_value=_item(visibility="public")):
            resp = _client().post("/api/knowledge/items/channel-notes/publish")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["visibility"], "public")

    def test_delete_reports_whether_anything_was_removed(self):
        with patch.object(store, "delete_item", return_value=False):
            self.assertEqual(_client().delete("/api/knowledge/items/nope").status_code, 404)
        with patch.object(store, "delete_item", return_value=True):
            self.assertEqual(_client().delete("/api/knowledge/items/mine").status_code, 200)


# ── the agent's knowledge retrieval ──────────────────────────────────────────

class KnowledgeScopeTests(unittest.TestCase):
    def test_scope_defaults_to_both_corpora_with_the_permission_filter_on(self):
        """⚠️ Reversed on 2026-10-01 by the user's ruling.

        The old contract was "no scope = the manual only", so a colleague's business page was
        invisible to an agent that never passed `scope=business`. The ruling: **agent 默认需要连
        业务知识库搜，但是仅限有权限的**. So the default now asks the business store — through
        `searchable`, which is the one place the read predicate lives — AND keeps the manual.
        """
        from routers import knowledge

        with patch.object(knowledge, "_search_knowledge_documents", return_value=[]) as search, \
             patch.object(knowledge.business_store, "searchable", return_value=[]) as business:
            resp = _client_for(knowledge).get("/api/ai/knowledge/search", params={"q": "deck"},
                                              headers={"X-Test-User": COLLEAGUE})
        self.assertEqual(resp.status_code, 200)
        # The permission filter is the reason this is safe: `searchable` decides what the
        # account may read (own pages, published public snapshots, pages shared with it).
        business.assert_called_once_with(COLLEAGUE, "business")
        self.assertTrue(search.call_args.kwargs["include_curated"])
        self.assertEqual(resp.json()["scope"], "all")

    def test_an_explicit_curated_scope_still_searches_the_manual_alone(self):
        from routers import knowledge

        with patch.object(knowledge, "_search_knowledge_documents", return_value=[]) as search, \
             patch.object(knowledge.business_store, "searchable") as business:
            resp = _client_for(knowledge).get("/api/ai/knowledge/search",
                                              params={"q": "deck", "scope": "curated"})
        self.assertEqual(resp.status_code, 200)
        business.assert_not_called()
        self.assertTrue(search.call_args.kwargs["include_curated"])

    def test_the_retrieval_sql_filters_permissions_before_ranking(self):
        """The default search path runs this query, so the read rule has to be IN it.

        ⚠️ Since 2026-10-01 the default scope is `all`, i.e. **every** agent search reaches
        `searchable()`. If the predicate were missing here, the default would hand an agent
        every colleague's private page — and filtering afterwards, in Python, would leak the
        same content through an excerpt (the same lesson as `services/annotations.py`).
        """
        src = inspect.getsource(store.searchable)
        self.assertIn("READABLE_PREDICATE", src)
        self.assertIn("AND status = 'published' AND enabled", src)

    def test_business_scope_searches_only_the_callers_readable_pages(self):
        from routers import knowledge

        with patch.object(knowledge, "_search_knowledge_documents", return_value=[]) as search, \
             patch.object(knowledge.business_store, "searchable", return_value=[]) as business:
            resp = _client_for(knowledge).get(
                "/api/ai/knowledge/search", params={"q": "渠道", "scope": "business"},
                headers={"X-Test-User": COLLEAGUE})
        self.assertEqual(resp.status_code, 200)
        business.assert_called_once_with(COLLEAGUE, "business")
        self.assertFalse(search.call_args.kwargs["include_curated"])

    def test_unknown_scope_is_a_400_not_a_silent_fallback(self):
        from routers import knowledge

        resp = _client_for(knowledge).get("/api/ai/knowledge/search",
                                          params={"q": "x", "scope": "everything"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("scope must be one of", resp.json()["detail"])

    def test_a_business_document_id_is_resolved_through_the_permission_check(self):
        from routers import knowledge

        with patch.object(knowledge.business_store, "get_item", return_value=_item()) as get:
            resp = _client_for(knowledge).get("/api/ai/knowledge/document/kb:channel-notes",
                                              headers={"X-Test-User": OWNER})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["content"], "# 渠道笔记\n\n正文")
        self.assertEqual(resp.json()["metadata"]["knowledge_tier"], "business")
        get.assert_called_once_with("channel-notes", OWNER)

    def test_someone_elses_business_page_is_404_from_the_document_endpoint(self):
        from routers import knowledge

        with patch.object(knowledge.business_store, "get_item", return_value=None):
            resp = _client_for(knowledge).get("/api/ai/knowledge/document/kb:secret",
                                              headers={"X-Test-User": STRANGER})
        self.assertEqual(resp.status_code, 404)


def _client_for(knowledge_module) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": request.headers.get("X-Test-User", OWNER)}
        request.state.auth_kind = request.headers.get("X-Test-Kind", "browser")
        return await call_next(request)

    app.include_router(knowledge_module.router, prefix="/api/ai")
    return TestClient(app, raise_server_exceptions=False)


class AgentAllowlistTests(unittest.TestCase):
    def test_the_allowlist_is_a_prefix_and_the_router_is_the_real_gate(self):
        """Know exactly what the allowlist does and does not guarantee.

        `_agent_may_write` admits everything under `/api/knowledge/items` — it is a
        prefix check, so it cannot distinguish "edit my page" from "publish my page
        to everyone". The load-bearing check for the widening operations is
        `_require_browser` inside the router (asserted in
        `test_publishing_pulling_and_sharing_are_browser_only`). Keep both: the
        allowlist keeps a leaked agent code out of settings/datasets, and the
        router keeps it from widening access.
        """
        from main import _agent_may_write

        self.assertTrue(_agent_may_write("/api/knowledge/items"))
        self.assertTrue(_agent_may_write("/api/knowledge/items/channel-notes"))
        self.assertTrue(_agent_may_write("/api/knowledge/items/channel-notes/publish"))
        # the 🌟 toggle is an edit of one's own page, so it rides the same prefix
        self.assertTrue(_agent_may_write("/api/knowledge/items/channel-notes/enabled"))
        self.assertFalse(_agent_may_write("/api/settings/app"))
        self.assertFalse(_agent_may_write("/api/ai/knowledge/documents"))


# ── retrieval adapter shape ──────────────────────────────────────────────────

class SearchableShapeTests(unittest.TestCase):
    def test_rows_become_documents_the_manual_retriever_can_score(self):
        rows = [{
            "slug": "channel-notes", "title": "渠道笔记", "body": "# 渠道笔记", "document_type": "note",
            "version": "1.0", "tags": "渠道,甲公司", "owner_email": OWNER, "visibility": "private",
            "source_item_id": None, "category": "渠道",
        }]
        with patch.object(store, "ensure_tables"), patch.object(store, "_db") as db:
            db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value \
                .fetchall.return_value = rows
            documents = store.searchable(OWNER, "business")
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document.document_id, "kb:channel-notes")
        self.assertEqual(document.source, "business-knowledge")
        self.assertEqual(document.metadata["tags"], ["渠道", "甲公司"])
        # The tier marker is what keeps the retriever ranking curated rules above
        # user notes; losing it silently demotes the project manual.
        self.assertEqual(document.metadata["knowledge_tier"], "business")


# ── duplicate titles: the conflict gate on a new page ────────────────────────

class ConflictGateTests(unittest.TestCase):
    """A duplicate page is a business decision, not a technical error.

    The wiki is written by people *and* by their agents, so the same subject gets
    written down twice from two accounts. Creating a page is when that knowledge is
    published, so the server refuses the write once and lists what it collides with;
    it goes through only when the caller confirms the owner has seen the list.
    """

    CONFLICT = {
        "slug": "channel-notes", "title": "渠道口径速查", "owner_email": STRANGER,
        "visibility": "public", "updated_at": "2026-09-29T00:00:00+00:00",
    }

    def test_title_normalisation_collapses_whitespace_and_case(self):
        self.assertEqual(store._normalise_title("  渠道  口径 速查 "), "渠道 口径 速查")
        self.assertEqual(store._normalise_title("Channel  Notes"), "channel notes")
        self.assertEqual(store._normalise_title("Channel Notes"), "channel notes")
        self.assertEqual(store._normalise_title(None), "")

    def test_the_conflict_query_is_read_permission_scoped(self):
        """It must never reveal — or count — a page this account cannot read.

        A title collision is a strong hint about what other accounts have written, so
        the same three-tier rule that guards reads has to guard this query too.
        """
        sql = store._CONFLICT_SQL
        self.assertIn(store.READABLE_PREDICATE, sql)
        self.assertIn("slug <> %s", sql)          # the page being written is not its own conflict
        self.assertEqual(sql.count("%s"), 5)      # exclude-slug, title, owner, item share, folder share

    def test_create_with_a_colliding_title_is_409_and_lists_the_pages(self):
        with patch.object(store, "upsert_item",
                          side_effect=store.BusinessKnowledgeConflict("渠道口径速查", [self.CONFLICT])):
            resp = _client().post("/api/knowledge/items",
                                  json={"title": "渠道口径速查", "body": "## 归属"})
        self.assertEqual(resp.status_code, 409)
        detail = resp.json()["detail"]
        self.assertEqual(detail["error"], "conflict")
        self.assertEqual(detail["title"], "渠道口径速查")
        self.assertEqual(detail["conflicts"][0]["slug"], "channel-notes")
        self.assertEqual(detail["conflicts"][0]["visibility"], "public")
        self.assertEqual(detail["conflicts"][0]["owner_email"], STRANGER)

    def test_confirmed_create_passes_the_flag_through_to_the_store(self):
        seen = {}

        def fake_upsert(payload, email, *, slug=None):
            seen.update(payload)
            return _item()

        with patch.object(store, "upsert_item", side_effect=fake_upsert):
            resp = _client().post("/api/knowledge/items", json={
                "title": "渠道口径速查", "body": "## 归属", "confirm_conflict": True})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(seen["confirm_conflict"])

    def test_an_unconfirmed_write_can_never_sneak_past_the_gate(self):
        """The flag has to be opt-in: a request that never mentions it must send False."""
        seen = {}

        def fake_upsert(payload, email, *, slug=None):
            seen.update(payload)
            return _item()

        with patch.object(store, "upsert_item", side_effect=fake_upsert):
            _client().post("/api/knowledge/items", json={"title": "x", "body": "## y"})
        self.assertIn("confirm_conflict", seen)
        self.assertFalse(seen["confirm_conflict"])

    def test_the_gate_covers_creation_only_so_editing_your_own_page_is_never_blocked(self):
        """Blocking edits would lock a page nobody can retitle, so the check sits in the
        create branch of ``upsert_item`` — after the UPDATE, before the INSERT."""
        import inspect

        source = inspect.getsource(store.upsert_item)
        gate = source.index("BusinessKnowledgeConflict")
        self.assertLess(source.index("UPDATE ai_knowledge_items"), gate)
        self.assertLess(gate, source.index("INSERT INTO ai_knowledge_items"))
        self.assertIn('if not payload.get("confirm_conflict")', source)

    def test_find_title_conflicts_searches_with_the_caller_email(self):
        captured = {}

        class _Cursor:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def fetchall(self):
                return []

        with patch.object(store, "ensure_tables"), patch.object(store, "_db") as db:
            db.return_value.__enter__.return_value.cursor.return_value.__enter__ \
                .return_value = _Cursor()
            store.find_title_conflicts("渠道口径速查", COLLEAGUE, exclude_slug="mine")
        self.assertIn("slug <> %s", captured["sql"])
        # exclude-slug, normalised title, then one email per READABLE_PREDICATE branch
        self.assertEqual(captured["params"],
                         ("mine", "渠道口径速查", COLLEAGUE, COLLEAGUE, COLLEAGUE))


# ── bilingual pages: the summary has to match ────────────────────────────────

class BilingualSummaryTests(unittest.TestCase):
    """A bilingual page must carry a summary in both of its languages.

    The page list's hover tooltip shows the lines, so a page that offers EN and ZH but only
    one summary hands one of its two readers a sentence in the wrong language — and nothing
    about the page looks broken. Same rule as a report card (`routers/reports.py`).
    """

    BILINGUAL = ("<!-- lang:zh -->\n## 渠道口径\n\n甲公司归 KA1\n\n"
                 "<!-- lang:en -->\n## Channel scope\n\nAcme Retail maps to KA1\n")

    def test_body_langs_reads_the_markers_in_order(self):
        self.assertEqual(store.body_langs(self.BILINGUAL), ["zh", "en"])
        self.assertEqual(store.body_langs("<!-- lang:zh-cn -->\n## x\n"), ["zh"])
        self.assertEqual(store.body_langs("## 没有标记\n"), [])

    def test_only_zh_and_en_are_languages_here(self):
        # An unknown marker is not a language the reader can pick, so it must not make the
        # page "bilingual" and start demanding a summary pair for it.
        self.assertEqual(store.body_langs("<!-- lang:fr -->\n## x\n"), [])
        self.assertFalse(store.is_bilingual_body("<!-- lang:fr -->\n## x\n<!-- lang:de -->\n## y\n"))

    def test_a_bilingual_body_is_detected(self):
        self.assertTrue(store.is_bilingual_body(self.BILINGUAL))
        self.assertFalse(store.is_bilingual_body("<!-- lang:zh -->\n## 只有中文\n"))

    def test_single_language_page_still_uses_the_plain_summary(self):
        self.assertEqual(store.bilingual_summaries("一句话", "", "", "## 中文页\n"),
                         ("一句话", "", ""))

    def test_bilingual_page_with_only_the_plain_summary_is_refused(self):
        with self.assertRaises(BusinessKnowledgeError) as ctx:
            store.bilingual_summaries("One line", "", "", self.BILINGUAL)
        self.assertIn("summary_en", str(ctx.exception))
        self.assertIn("summary_zh", str(ctx.exception))

    def test_half_a_pair_is_refused_and_names_the_missing_half(self):
        with self.assertRaises(BusinessKnowledgeError) as ctx:
            store.bilingual_summaries("", "One line", "", self.BILINGUAL)
        self.assertIn("summary_zh", str(ctx.exception))
        with self.assertRaises(BusinessKnowledgeError) as ctx2:
            store.bilingual_summaries("", "", "一句话", self.BILINGUAL)
        self.assertIn("summary_en", str(ctx2.exception))

    def test_a_complete_pair_passes_and_fills_the_plain_field(self):
        # `summary` is what the list tooltip falls back to and what older clients read;
        # leaving it empty on a bilingual page would put a hole in the tooltip.
        self.assertEqual(store.bilingual_summaries("", "One line", "一句话", self.BILINGUAL),
                         ("One line", "One line", "一句话"))

    def test_an_explicit_plain_summary_still_wins(self):
        self.assertEqual(store.bilingual_summaries("Card line", "One line", "一句话", self.BILINGUAL),
                         ("Card line", "One line", "一句话"))

    def test_whitespace_is_not_a_summary(self):
        with self.assertRaises(BusinessKnowledgeError):
            store.bilingual_summaries("  ", "   ", " ", self.BILINGUAL)

    def test_the_router_reports_the_pair_and_the_api_accepts_it(self):
        with patch.object(store, "upsert_item",
                          return_value=_item(summary_en="One line", summary_zh="一句话")):
            resp = _client().post("/api/knowledge/items", json={
                "title": "渠道笔记", "body": "## x",
                "summary_en": "One line", "summary_zh": "一句话"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["summary_en"], "One line")
        self.assertEqual(resp.json()["summary_zh"], "一句话")

    def test_the_router_turns_a_missing_half_into_400(self):
        with patch.object(store, "upsert_item",
                          side_effect=BusinessKnowledgeError("must carry a summary in both languages")):
            resp = _client().post("/api/knowledge/items",
                                  json={"title": "x", "body": "<!-- lang:zh -->\n## a\n<!-- lang:en -->\n## b\n"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("both languages", resp.json()["detail"])


class EnabledToggleTests(unittest.TestCase):
    """The curator's 🌟: lit = retrieval, dim = out of retrieval but still readable.

    Semantics fixed with the user (2026-09-30). The load-bearing property: the
    filter lives in the retrieval SQL **before ranking** — the same lesson as the
    permission predicate — while ``may_read`` and the wiki list deliberately keep
    showing the page, so toggling can lose nothing.
    """

    def test_retrieval_excludes_disabled_pages_in_the_sql(self):
        src = inspect.getsource(store.searchable)
        self.assertIn("AND enabled", src)

    def test_the_wiki_list_does_not_filter_by_enabled(self):
        """A dimmed page must stay visible in the TOC — that is where the star is."""
        src = inspect.getsource(store.list_items)
        self.assertNotIn("AND enabled", src)

    def test_may_read_ignores_enabled_a_dimmed_page_is_still_readable(self):
        row = _item().__dict__
        row["enabled"] = False
        with patch.object(store, "_share_exists", return_value=False):
            self.assertTrue(store.may_read(row, OWNER))

    def test_a_pulled_copy_inherits_the_star(self):
        """A copy of a dimmed page must not come back into retrieval.

        ⚠️ 2026-10-01: the INSERT in `pull_copy` listed seventeen columns and `enabled` was
        not one of them, so the copy took the column DEFAULT TRUE — a colleague could pull a
        page the curator had switched out of the agent's search and hand the agent the same
        text under a new slug. The star has to travel with the content.
        """
        src = inspect.getsource(store.pull_copy)
        self.assertIn("source_item_id, size_bytes, enabled)", src)
        self.assertIn("source_item_id, size_bytes, enabled", src)
        # …and it must SELECT the value, not just name the column.
        self.assertIn("id, size_bytes, enabled", src.replace("\n", " ").replace("  ", " "))

    def test_rows_carry_enabled_and_default_to_lit(self):
        self.assertIn("enabled", store._SELECT_COLS)
        self.assertIs(store._row_to_item({"id": 1, "slug": "s", "title": "t"}).enabled, True)
        self.assertIs(store._row_to_item({"id": 1, "slug": "s", "title": "t",
                                          "enabled": False}).enabled, False)

    def test_the_column_migration_is_idempotent_and_defaults_to_lit(self):
        src = inspect.getsource(store.ensure_tables)
        self.assertIn("ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE", src)

    def test_publish_propagates_enabled_to_the_snapshot(self):
        """The snapshot mirrors the original, so publish must copy the flag too —
        otherwise a page toggled off *before* publishing would sneak back into
        retrieval through its fresh public copy."""
        src = inspect.getsource(store.publish_to_public)
        self.assertIn("enabled", src)

    def test_shape_exposes_enabled_to_the_ui(self):
        shaped = router_module._shape(_item(), OWNER)
        self.assertIs(shaped["enabled"], True)


class EnabledRouterTests(unittest.TestCase):
    def test_toggle_calls_the_store_with_the_caller_and_payload(self):
        with patch.object(store, "set_enabled", return_value=_item(enabled=False)) as toggle:
            resp = _client().put("/api/knowledge/items/channel-notes/enabled",
                                 json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        toggle.assert_called_once_with("channel-notes", OWNER, False)
        self.assertIs(resp.json()["enabled"], False)

    def test_the_owners_agent_may_toggle_too(self):
        """Toggling is an edit, not a publication — no `_require_browser` here."""
        with patch.object(store, "set_enabled", return_value=_item()) as toggle:
            resp = _client().put("/api/knowledge/items/channel-notes/enabled",
                                 json={"enabled": True}, headers={"X-Test-Kind": "agent"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(toggle.call_args.args[1], OWNER)

    def test_a_page_you_cannot_manage_is_404_not_403(self):
        with patch.object(store, "set_enabled",
                          side_effect=PermissionError("knowledge item not found")):
            resp = _client().put("/api/knowledge/items/channel-notes/enabled",
                                 json={"enabled": False}, headers={"X-Test-User": STRANGER})
        self.assertEqual(resp.status_code, 404)

    def test_a_database_failure_is_503(self):
        with patch.object(store, "set_enabled",
                          side_effect=BusinessKnowledgeError("db down")):
            resp = _client().put("/api/knowledge/items/channel-notes/enabled",
                                 json={"enabled": True})
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()