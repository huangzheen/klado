"""The 项目说明书 page: the instruction set an agent operates under.

Read-only, and reachable while the console is read-only — like `overview`, this is a page
an operator needs *most* when they cannot change anything. The question it answers is
"what have we told the agent it may do, and is that still current", and during an
incident that question comes up precisely when writes are switched off.

What is on this page, and what deliberately is not
--------------------------------------------------
The **project manual** (``ai_knowledge_documents``): the version-controlled Markdown an
agent retrieves at runtime. Every document, its type, its version, its tags, its source
file and its content hash.

It is **not** the business knowledge base — what people write at runtime into
``ai_knowledge_items``, which the main app labels 知识库. That is a different corpus with a
different writer and a different lifecycle, and an earlier version of this page led with
it, which answered a question nobody had asked. See `klado_shared/knowledge.py`.

The manual is **Chinese-only** since 2026-10-03: the canonical English-side files are
deleted and their ids retired, so every id here ends in ``:zh`` and there is no variant to
switch to.

⚠️ This page is a **second reader, not a second authority.** It reports each document's
own fields and stops there. Whether the manual is *valid* — duplicate titles, version
syntax — is the main app's call, published at `/api/ai/knowledge/manifest`; recomputing
that verdict here would create a second answer to the same question, which is the one
thing this package exists to prevent.

No write route
--------------
The manual changes by editing a file under `api/knowledge_docs/` and deploying. A
console-side write would bypass the startup reconciler, the `_retired.txt` deletion
protocol and the content hashes in a single step.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from admin_console import access
from klado_shared import knowledge
from klado_shared.i18n import pick, request_lang

router = APIRouter(prefix="/api/admin-console", tags=["项目说明书"])


@router.get("/manual")
async def manual_index(
    request: Request,
    type: str = Query("", description="空或 all | format | rule"),
    search: str = Query("", max_length=200, description="标题、标签、源文件等自由文本"),
    limit: int = Query(200, ge=1, le=knowledge.MAX_LIMIT),
):
    """The document list plus the counts the page header shows.

    One request rather than three: the header numbers and the list are read together, so
    the page cannot render a count next to a list that disagrees with it.
    """
    access.require_operator(request)
    try:
        documents = knowledge.list_documents(
            document_type=type, search=search, limit=limit)
    except ValueError as exc:
        # The shared module refuses an unknown filter rather than widening it; this is the
        # one place a bad query string becomes a status code.
        raise HTTPException(status_code=400, detail=pick(
            f"未知的说明书筛选条件：{exc} / unknown manual filter",
            request_lang(request)))
    return {
        "type": (type or "all").strip().lower(),
        "search": search,
        "documents": documents,
        "summary": knowledge.summary(),
    }


@router.get("/manual/{document_id}")
async def manual_document(request: Request, document_id: str):
    """One manual document, with its full content.

    A separate call because the list omits bodies: this is the only route that can return
    a large payload, so it is the only route where that matters.
    """
    access.require_operator(request)
    document = knowledge.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=pick(
            "说明书文档不存在 / manual document not found", request_lang(request)))
    return document
