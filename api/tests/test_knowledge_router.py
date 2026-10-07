"""Knowledge base HTTP contract tests.

Covers the read endpoints an external agent depends on
(``/api/ai/knowledge/search`` and ``/api/ai/knowledge/document/{id}``) plus the
administrative listing and manifest. There is no write endpoint by design: the
knowledge base is sourced from the files under ``api/knowledge_docs/`` and the
write routes were removed on 2026-09-23, so that read access can be shared
without granting write or delete.
"""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import knowledge
from services.ai.knowledge_store import KnowledgeDocument, KnowledgeStoreError


def _document(document_id: str = "klado-v2:data-sql-cookbook") -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=document_id,
        title="data-sql-cookbook",
        content="SQL Cookbook\n" + ("x" * 900),
        document_type="rule",
        source="klado-runtime-curation",
        version="1.1",
        metadata={"tags": ["data", "sql"]},
    )


CALLER = {"email": "reader@klado.local", "role": "user"}


def _client(caller: dict | None = CALLER) -> TestClient:
    """The knowledge router plus a stand-in for the auth middleware.

    ⚠️ The middleware has to be here. The default scope is `all`, which asks the business
    store what *this caller* may read, so a request without an identity is a 401 by
    design. It used to survive on a hardcoded fallback administrator; that fallback is
    gone (the repository names nobody), so an anonymous call is now correctly rejected —
    pass `caller=None` to test that direction.
    """
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request, call_next):
        request.state.current_user = caller
        return await call_next(request)

    app.include_router(knowledge.router, prefix="/api/ai")
    return TestClient(app, raise_server_exceptions=False)


class KnowledgeSearchTests(unittest.TestCase):
    def _no_business(self):
        """The default scope now reaches the business store, so stub it.

        ⚠️ Since 2026-10-01 the DEFAULT is `all` (the user's ruling: "agent 默认需要连业务知识库搜，
        但是仅限有权限的"), so an unscoped call now asks `business_knowledge.searchable()` for the
        pages this account may read. Without this stub the test would open a real connection.
        """
        return patch.object(knowledge.business_store, "searchable", return_value=[])

    def test_returns_capped_excerpt_and_ids(self):
        with self._no_business(), \
                patch.object(knowledge, "_search_knowledge_documents", return_value=[_document()]):
            resp = _client().get("/api/ai/knowledge/search", params={"q": "sql", "top_k": 3})
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["document_id"], "klado-v2:data-sql-cookbook")
        self.assertEqual(results[0]["document_type"], "rule")
        self.assertEqual(results[0]["version"], "1.1")
        self.assertLessEqual(len(results[0]["excerpt"]), 500)

    def test_empty_match_returns_empty_list(self):
        with self._no_business(), \
                patch.object(knowledge, "_search_knowledge_documents", return_value=[]):
            resp = _client().get("/api/ai/knowledge/search", params={"q": "nothing-matches"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["results"], [])
        # `scope` is additive (2026-09-29): one endpoint now answers from two
        # corpora, so the caller must be able to tell which one it searched.
        # ⚠️ "all" since 2026-10-01 — the default now covers both corpora.
        self.assertEqual(body["scope"], "all")

    def test_the_default_scope_searches_both_corpora(self):
        """No `scope` at all must reach the business knowledge base.

        This is the user's ruling of 2026-10-01: an agent should find what colleagues wrote
        down without having to know that a second corpus exists. The permission filter is
        asserted at the store (`business_knowledge.searchable`), which is where it lives.
        """
        with patch.object(knowledge.business_store, "searchable", return_value=[]) as called, \
                patch.object(knowledge, "_search_knowledge_documents", return_value=[]) as ranked:
            resp = _client().get("/api/ai/knowledge/search", params={"q": "channel"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(called.call_count, 1, "the default must ask the business store")
        self.assertEqual(called.call_args[0][1], "business")
        # …and it must still ask for the manual.
        self.assertTrue(ranked.call_args[1]["include_curated"])

    def test_scope_curated_still_searches_the_manual_alone(self):
        """The old default stays reachable by name — an explicit caller is never surprised."""
        with patch.object(knowledge.business_store, "searchable", return_value=[]) as called, \
                patch.object(knowledge, "_search_knowledge_documents", return_value=[]) as ranked:
            resp = _client().get("/api/ai/knowledge/search",
                                 params={"q": "channel", "scope": "curated"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(called.call_count, 0, "curated must not touch the business store")
        self.assertTrue(ranked.call_args[1]["include_curated"])
        self.assertEqual(ranked.call_args[1]["extra"], [])

    def test_missing_query_is_422(self):
        resp = _client().get("/api/ai/knowledge/search")
        self.assertEqual(resp.status_code, 422)

    def test_top_k_out_of_range_is_422(self):
        resp = _client().get("/api/ai/knowledge/search", params={"q": "sql", "top_k": 99})
        self.assertEqual(resp.status_code, 422)

    def test_an_anonymous_caller_is_401_not_an_implicit_administrator(self):
        """No session and no configured administrator means no identity at all.

        This is the contract the removed fallback used to hide: the default scope reads
        *this caller's* business pages, so an anonymous request must be refused rather
        than quietly answered as somebody else.

        ⚠️ `AUTH_ENABLED` is pinned **on** for this one case. `reports._identity()` has a
        local-single-user branch — with auth disabled it hands the request the legacy
        owner instead of refusing — and this deployment runs `AUTH_ENABLED=false`, so
        without the pin the test read the *ambient* environment and answered 200. That is
        the opposite of what it claims to verify, and it failed only because the test
        suite happens to run inside the container.
        """
        from core.config import settings
        with patch.object(settings, "AUTH_ENABLED", True):
            resp = _client(caller=None).get("/api/ai/knowledge/search", params={"q": "channel"})
        self.assertEqual(resp.status_code, 401)

    def test_store_failure_is_503(self):
        with self._no_business(), patch.object(
            knowledge, "_search_knowledge_documents", side_effect=KnowledgeStoreError("db down")
        ):
            resp = _client().get("/api/ai/knowledge/search", params={"q": "sql"})
        self.assertEqual(resp.status_code, 503)


class KnowledgeDocumentTests(unittest.TestCase):
    def test_returns_full_content(self):
        with patch.object(knowledge, "_get_knowledge_document", return_value=_document()):
            resp = _client().get("/api/ai/knowledge/document/klado-v2:data-sql-cookbook")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["title"], "data-sql-cookbook")
        self.assertEqual(body["source"], "klado-runtime-curation")
        self.assertIn("SQL Cookbook", body["content"])
        self.assertGreater(len(body["content"]), 500)
        self.assertEqual(body["metadata"], {"tags": ["data", "sql"]})

    def test_unknown_document_is_404(self):
        with patch.object(knowledge, "_get_knowledge_document", return_value=None):
            resp = _client().get("/api/ai/knowledge/document/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        # No `Accept-Language` was sent, so the bilingual form is preserved verbatim
        # (see core/i18n.py) — a browser asking for English gets the English side.
        self.assertEqual(resp.json()["detail"], "知识文档不存在 / Knowledge document not found")
        with patch.object(knowledge, "_get_knowledge_document", return_value=None):
            en = _client().get("/api/ai/knowledge/document/does-not-exist",
                               headers={"Accept-Language": "en-US,en;q=0.9"})
            self.assertEqual(en.json()["detail"], "Knowledge document not found")
            zh = _client().get("/api/ai/knowledge/document/does-not-exist",
                               headers={"Accept-Language": "zh-CN,zh;q=0.9"})
            self.assertEqual(zh.json()["detail"], "知识文档不存在")

    def test_store_failure_is_503(self):
        with patch.object(
            knowledge, "_get_knowledge_document", side_effect=KnowledgeStoreError("db down")
        ):
            resp = _client().get("/api/ai/knowledge/document/anything")
        self.assertEqual(resp.status_code, 503)


class KnowledgeManagementTests(unittest.TestCase):
    def test_only_read_routes_are_registered(self):
        paths = {(route.path, tuple(sorted(getattr(route, "methods", ())))) for route in knowledge.router.routes}
        self.assertIn(("/knowledge/documents", ("GET",)), paths)
        self.assertIn(("/knowledge/manifest", ("GET",)), paths)
        self.assertIn(("/knowledge/search", ("GET",)), paths)
        self.assertIn(("/knowledge/document/{document_id}", ("GET",)), paths)

    def test_no_write_route_exists_at_all(self):
        """Sharing read access must not be able to leak write or delete access."""
        mutating = {
            (route.path, method)
            for route in knowledge.router.routes
            for method in getattr(route, "methods", ())
            if method != "GET"
        }
        self.assertEqual(mutating, set(), f"knowledge router exposes write routes: {mutating}")

    def test_manifest_shape_is_untouched(self):
        payload = {"documents": [{"document_id": "a", "title": "A"}], "valid": True, "issues": []}
        with patch.object(knowledge, "_knowledge_manifest", return_value=payload):
            resp = _client().get("/api/ai/knowledge/manifest")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), payload)

    def test_post_and_delete_are_not_routed(self):
        client = _client()
        self.assertIn(client.post("/api/ai/knowledge/documents", json={}).status_code, (404, 405))
        self.assertIn(client.delete("/api/ai/knowledge/documents/x").status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()