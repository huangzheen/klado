"""
Notes pinned to a spot inside a document — the HTTP surface.

Three contracts worth stating out loud, because each one is a decision rather
than an implementation detail:

* **`GET` is never gated on the auth kind.** An agent reading a document must be
  able to read what the humans wrote about it; that is the entire reason this
  feature exists. Basic/Bearer credentials are agents, not colleagues.
* **Writes need a browser session.** A note is attributed to a person and shows
  up in their colleagues' Inbox, so it is signed by a session, not by a machine
  credential. (Same rule as the interactive-report state endpoint.)
* **Not-found is 404, everywhere.** "Not yours" must not be distinguishable from
  "does not exist", or the endpoint becomes a probe for other people's documents.

Reads go through `services.annotations`, which delegates the access question to
each asset family's own `may_read`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.i18n import pick, request_lang
from routers.reports import _identity, _require_browser
from services import annotations

router = APIRouter()


class NoteIn(BaseModel):
    asset_type: str = Field(..., description="report | knowledge | event")
    slug: str
    body: str
    # Percentages of the anchor box (a deck slide, or the whole document).
    x: float = 0
    y: float = 0
    page: int = 0          # 1-based slide index in the UI; 0 = long-form


class NotePatch(BaseModel):
    body: str | None = None
    resolved: bool | None = None


def _fail(exc: annotations.AnnotationError, request: Request | None = None) -> HTTPException:
    return HTTPException(status_code=exc.status_code,
                        detail=pick(exc.detail, request_lang(request) if request else None))


@router.get("")
@router.get("/", include_in_schema=False)
async def list_notes(request: Request,
                     asset_type: str = Query(..., alias="type"),
                     slug: str = Query(...)):
    """Every note on one document. Reachable by an agent on purpose."""
    email, _ = _identity(request)
    try:
        return {"notes": annotations.list_notes(asset_type, slug, email)}
    except annotations.AnnotationError as exc:
        raise _fail(exc, request) from exc


@router.post("", status_code=201)
@router.post("/", status_code=201, include_in_schema=False)
async def add_note(payload: NoteIn, request: Request):
    """Pin a new note where the reader clicked. Browser session required."""
    _require_browser(request)
    email, _ = _identity(request)
    try:
        return annotations.create_note(asset_type=payload.asset_type, slug=payload.slug, email=email,
                                      body=payload.body, x=payload.x, y=payload.y, page=payload.page)
    except annotations.AnnotationError as exc:
        raise _fail(exc, request) from exc


@router.patch("/{note_id}")
async def patch_note(note_id: int, payload: NotePatch, request: Request):
    """Edit the text and/or mark the note resolved. Browser session required."""
    _require_browser(request)
    email, _ = _identity(request)
    try:
        return annotations.update_note(note_id, email, body=payload.body, resolved=payload.resolved)
    except annotations.AnnotationError as exc:
        raise _fail(exc, request) from exc


@router.delete("/{note_id}")
async def delete_note(note_id: int, request: Request):
    """The note's author, or the document's owner. Browser session required."""
    _require_browser(request)
    email, _ = _identity(request)
    try:
        annotations.delete_note(note_id, email)
    except annotations.AnnotationError as exc:
        raise _fail(exc, request) from exc
    return {"deleted": note_id}
