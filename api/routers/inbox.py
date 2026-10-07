"""
The Inbox — what colleagues and the data layer did, newest first.

Two rules, both enforced in the table rather than in this router:

* a row with a `recipient_email` belongs to that person alone;
* a row without one is a broadcast every signed-in user sees.

`GET` is open to agent credentials (an agent that was told to revise a document
should be able to ask "what did people say about it"), while every write — read,
unread, dismiss — is a person's action and needs a browser session.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from routers.reports import _identity, _require_browser
from services import inbox

router = APIRouter()


class IdsIn(BaseModel):
    ids: list[int] | None = None      # omitted = everything this reader can see


@router.get("")
@router.get("/", include_in_schema=False)
async def read_feed(request: Request,
                    limit: int = Query(default=60, ge=1, le=200),
                    before_id: int | None = Query(default=None, ge=1)):
    email, _ = _identity(request)
    return {
        "items": inbox.feed(email, limit=limit, before_id=before_id),
        "unread": inbox.unread_count(email),
    }


# ⚠️ All three writes below are a person's decision about their own screen, so each
# one goes through `_require_browser` exactly like marking read does — an agent
# credential may read the feed (the module docstring) but must not quietly tidy it.
@router.post("/read")
async def mark_read(payload: IdsIn, request: Request):
    _require_browser(request)
    email, _ = _identity(request)
    changed = inbox.mark_read(email, ids=payload.ids)
    return {"ok": True, "changed": changed, "unread": inbox.unread_count(email)}


@router.post("/unread")
async def mark_unread(payload: IdsIn, request: Request):
    _require_browser(request)
    email, _ = _identity(request)
    changed = inbox.mark_unread(email, ids=payload.ids)
    return {"ok": True, "changed": changed, "unread": inbox.unread_count(email)}


@router.post("/dismiss")
async def dismiss(payload: IdsIn, request: Request):
    _require_browser(request)
    email, _ = _identity(request)
    changed = inbox.dismiss(email, ids=payload.ids)
    return {"ok": True, "changed": changed, "unread": inbox.unread_count(email)}
