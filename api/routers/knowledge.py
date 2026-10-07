"""Knowledge base HTTP router — read-only by construction.

Self-hosted knowledge retrieval backed by the deployment database
(``public.ai_knowledge_documents``). This router is the only knowledge entry
point left after the embedded AI assistant was retired, so an external agent
can pull the rules at runtime instead of depending on a shipped-in prompt.

Endpoints
---------
  * ``GET  /knowledge/search``            — keyword search, 500-char excerpts
  * ``GET  /knowledge/document/{id}``     — full document body
  * ``GET  /knowledge/documents``         — administrative listing
  * ``GET  /knowledge/manifest``          — integrity manifest

There is deliberately **no write endpoint**. The knowledge base is sourced from
the Markdown files under ``api/knowledge_docs/`` and reconciled on startup by
``services.ai.knowledge_docs``; the ``POST``/``DELETE`` routes were removed on
2026-09-23 so that sharing read access cannot leak write access. To change a
document, edit its file and deploy; to delete one, list it in ``_retired.txt``.

Two corpora, one search
-----------------------
``scope`` selects which corpus is searched:

  * ``all`` (**default**) — both corpora. Changed from ``curated`` on 2026-10-01 (user's
    ruling): an agent should reach what colleagues wrote down without having to know that
    a second corpus exists. The business half is permission-filtered **in SQL, before
    ranking** — own pages, published public snapshots, pages shared with the caller.
  * ``curated`` (alias ``manual``) — the project manual only: how to use this system.
    Kept for callers that want the manual alone.
  * ``business`` — the **business knowledge base** only: user-published pages,
    filtered to what *this account* may read (``services/ai/business_knowledge.py``).
    ``mine`` / ``public`` / ``shared`` narrow it to one visibility.

Business pages are permission-filtered **in SQL, before ranking**. That filtering
is the reason the two corpora are merged here rather than inside
``knowledge_store``: ``_all_documents()`` is by definition the same for everyone.
Writes live in ``routers/business_knowledge.py`` (``/api/knowledge/items``), so
this router stays GET-only and ``test_no_write_route_exists_at_all`` keeps meaning
what it says.

Mounted at ``/api/ai`` so existing callers keep the documented
``/api/ai/knowledge/...`` paths.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from core.i18n import pick, request_lang
from routers.reports import _identity
from services.ai import business_knowledge as business_store
from services.ai.business_knowledge import BusinessKnowledgeError
from services.ai.knowledge_store import (
    KnowledgeStoreError,
    get_document as _get_knowledge_document,
    list_documents as _list_knowledge_documents,
    manifest as _knowledge_manifest,
    search as _search_knowledge_documents,
)

router = APIRouter()

_EXCERPT_LENGTH = 500

# ``curated``/``manual`` are the same thing; the alias exists because both names
# are in circulation ("project manual" vs "the curated corpus").
_SCOPES = ("curated", "manual", "business", "all", "mine", "public", "shared")
_BUSINESS_SCOPES = {"mine", "public", "shared"}


def _resolve_scope(scope: str, request: Request | None = None) -> tuple[bool, str | None]:
    """→ (include the project manual?, business sub-scope or None)."""
    value = (scope or "curated").strip().lower()
    if value not in _SCOPES:
        raise HTTPException(
            status_code=400,
            detail=pick(f"scope 必须是以下之一: {', '.join(_SCOPES)}"
                        f" / scope must be one of: {', '.join(_SCOPES)}", request_lang(request) if request else None),
        )
    include_curated = value in {"curated", "manual", "all"}
    if include_curated and value == "all":
        return True, "business"
    if include_curated:
        return True, None
    return False, value


# ── Read endpoints (external agent contract) ─────────────────────────────────

@router.get("/knowledge/search")
async def search_knowledge_documents(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键词"),
    top_k: int = Query(default=5, ge=1, le=20, description="返回结果数量"),
    scope: str = Query(
        default="all",
        description="all (both corpora, default) | curated (project manual only) | business | mine | public | shared",
    ),
):
    """Search the knowledge base and return matching documents with excerpts.

    ⚠️ **The default became `all` on 2026-10-01** (user's ruling: "agent 默认需要连业务知识库搜，
    但是仅限有权限的"). It used to be `curated` — the manual only — which meant a colleague's
    business page could be invisible to an agent that never thought to pass `scope=business`.
    Both corpora now answer by default, and the business half is still **permission-filtered in
    SQL before ranking**: the caller sees its own pages, published public snapshots, and pages
    colleagues shared with it — nothing else. `scope=curated` keeps the old behaviour for anyone
    who wants the manual alone.
    """
    try:
        include_curated, business_scope = _resolve_scope(scope, request)
        extra = []
        if business_scope:
            email, _ = _identity(request)
            extra = business_store.searchable(email, business_scope)
        matches = _search_knowledge_documents(q, top_k=top_k, extra=extra,
                                              include_curated=include_curated)
        return {
            "scope": (scope or "curated").lower(),
            "results": [
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "document_type": document.document_type,
                    "source": document.source,
                    "version": document.version,
                    "excerpt": (document.content or "")[:_EXCERPT_LENGTH],
                }
                for document in matches
            ]
        }
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    except KnowledgeStoreError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc


@router.get("/knowledge/document/{document_id}")
async def get_knowledge_document(document_id: str, request: Request):
    """Retrieve a complete knowledge document by its ID (or title).

    ``kb:<slug>`` addresses a business knowledge page and is permission-checked
    against the caller's account; anything else is a deployed project-manual
    document, which every signed-in user may read.
    """
    if document_id.startswith(f"{business_store.ITEM_NAMESPACE}:"):
        slug = document_id.split(":", 1)[1]
        try:
            email, _ = _identity(request)
            item = business_store.get_item(slug, email)
        except BusinessKnowledgeError as exc:
            raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
        if not item:
            # 404 for "not yours" as well as "does not exist" — the code must not
            # confirm that another account has a page at this slug.
            raise HTTPException(status_code=404, detail=pick("知识文档不存在 / Knowledge document not found", request_lang(request) if request else None))
        return {
            "document_id": item.document_id(),
            "title": item.title,
            "document_type": item.document_type,
            "source": "business-knowledge",
            "version": item.version,
            "content": item.body,
            "metadata": {
                "tags": item.tag_list(),
                "category": item.category,
                "owner_email": item.owner_email,
                "visibility": item.visibility,
                "knowledge_tier": "business",
            },
        }
    try:
        document = _get_knowledge_document(document_id)
    except KnowledgeStoreError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    if not document:
        raise HTTPException(status_code=404, detail=pick("知识文档不存在 / Knowledge document not found", request_lang(request) if request else None))
    return {
        "document_id": document.document_id,
        "title": document.title,
        "document_type": document.document_type,
        "source": document.source,
        "version": document.version,
        "content": document.content,
        "metadata": document.metadata or {},
    }


# ── Management endpoints (operations, read-only) ─────────────────────────────

@router.get("/knowledge/documents")
async def list_knowledge_documents(request: Request, limit: int = Query(default=200, ge=1, le=500)):
    """List documents explicitly managed by the deployment knowledge store.

    Document Library entries are also searchable, but are not copied or exposed
    through this administrative listing.
    """
    try:
        return {"documents": _list_knowledge_documents(limit)}
    except KnowledgeStoreError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc


@router.get("/knowledge/manifest")
async def get_knowledge_manifest(request: Request, limit: int = Query(default=500, ge=1, le=500)):
    """Expose the deployed knowledge contract and its integrity status."""
    try:
        return _knowledge_manifest(limit)
    except KnowledgeStoreError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc