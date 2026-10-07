"""Calendar — HTTP surface for events.

Deliberately the same shape as `/api/knowledge/items` (the wiki) and `/api/reports`
(Workspace), because an event is the same kind of object: owned by an account, readable by
the colleagues it was shared with, optionally published as a read-only snapshot.

Two rules worth stating out loud
--------------------------------
* **An agent may hand the event to colleagues.** `partners` is a write field for any
  identity, including an agent access code — "@ the person who owns this review" is the
  whole point of creating an event from Feishu. Publishing to the **public area** stays
  browser-only: that widens the audience to every signed-in account at once, which is the
  decision the Workspace and the wiki both refuse to let a machine make.
* **Read it back with `GET /events/{slug}/raw`.** That is the event's own 16:9 page, and
  `{base}/e/<slug>` serves the same document for the viewer's iframe.

Two mechanisms are **shared with the Workspace rather than copied**:

* the **cover** — `services/report_cover.py` generates it, the columns are the same, and
  `resolve_cover()` reads the same request fields (`cover_base64` / `cover_url` /
  `generate_cover` / `cover_ratio` …). `COVER_ALIASES` is imported so an
  agent that spells them the report way gets the same acceptance here;
* the **export** — `services/knowledge_export.py::export_document()` builds the Word /
  PDF / picture file from the HTML the browser rendered.

⚠️ **A metadata edit does not have to carry the page.** `PUT /events/{slug}` with `body`,
`partners` or `attachments` **absent** keeps what is stored; sending an *empty* list still
means "clear it", and an empty `body` is still a 400 (the page is required). Without this an
owner editing a deadline in the UI would have to round-trip several thousand characters of
HTML or silently destroy the event page.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import AliasChoices, BaseModel, Field

from routers.reports import (COVER_ALIASES, _base_href, _identity, _inject_base_tag,
                             _inline_deck_runtime, _reject_unknown_cover_keys, _require_browser)
from core.i18n import pick, request_lang
from klado_shared import orgs
from services import annotations, inbox, knowledge_export, report_cover
from services.ai import calendar_events as store
from services.ai import calendar_page
from services.ai.calendar_events import CalendarError

router = APIRouter()


class EventIn(BaseModel):
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    deadline: str = ""
    summary: str = ""
    summary_en: str = ""
    summary_zh: str = ""
    # The 项目描述 box on the page, one per language like the summaries (the page is bilingual,
    # so one field would print English on the Chinese slide). An empty one leaves that slide's
    # authored text alone — the same rule the to-do list follows.
    description_en: str = ""
    description_zh: str = ""
    category: str = ""
    tags: str = ""
    # ⚠️ Must match `store.KINDS`. The default is the fallback bucket, not a real type:
    # `upsert_event` coerces anything unrecognised to `other` too, so an omitted or
    # invented type lands in the same place instead of looking like a deliberate choice.
    kind: str = "other"
    status: str = "published"
    body: str = ""
    slug: str = ""
    # Colleagues who are *on* the event. Being a partner is being granted read access.
    partners: list[str] = Field(default_factory=list)
    # References, never copies:
    #   [{kind: "report"|"knowledge", slug: "..."}]           a Workspace report / wiki page
    #   [{kind: "file", slug: "datacenter-raw/<key>"}]        an object in the file library
    attachments: list[dict] = Field(default_factory=list)
    # The to-do list, as data rather than prose in the page: `{text, assignee, due, done}`.
    # ⚠️ `assignee` is a name, NOT a permission — see `store.Todo`. `due` is what makes the
    # line a milestone on the calendar.
    todos: list[dict] = Field(default_factory=list)
    # Set only after the owner has been shown which existing events the title collides with.
    confirm_conflict: bool = False
    # ── the cover: the Workspace's field names and spellings (see COVER_ALIASES) ──
    generate_cover: bool = Field(default=False,
                                 validation_alias=AliasChoices(*COVER_ALIASES["generate_cover"]))
    cover_base64: str = Field(default="",
                              validation_alias=AliasChoices(*COVER_ALIASES["cover_base64"]))
    cover_url: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_url"]))
    cover_prompt: str = Field(default="",
                              validation_alias=AliasChoices(*COVER_ALIASES["cover_prompt"]))
    cover_ratio: str = Field(default=report_cover.DEFAULT_RATIO,
                             validation_alias=AliasChoices(*COVER_ALIASES["cover_ratio"]))
    cover_seed: Optional[int] = Field(default=None,
                                      validation_alias=AliasChoices(*COVER_ALIASES["cover_seed"]))
    cover_extra: str = Field(default="",
                             validation_alias=AliasChoices(*COVER_ALIASES["cover_extra"]))
    cover_with_title: bool = Field(default=False,
                                   validation_alias=AliasChoices(*COVER_ALIASES["cover_with_title"]))
    # Explicitly drop the cover (there is otherwise no way to say "remove it").
    clear_cover: bool = False


class CoverIn(BaseModel):
    """A cover for one event, in the words a report uses.

    Field names **and** aliases are the report ones on purpose, so
    `services/report_cover.py::resolve_cover()` takes this object unchanged — that is what
    reusing the mechanism means in practice.

    ⚠️ This endpoint also accepts the **unprefixed** spellings (`ratio`, `seed`, `brand`,
    `extra`, `with_title`), i.e. the body of the stateless `POST /api/reports/cover`. An
    agent that has learned that call can point it at an event without relearning the field
    names; `cover_*` works too, for callers that only know the publish body.
    """

    generate_cover: bool = Field(default=True,
                                 validation_alias=AliasChoices(*COVER_ALIASES["generate_cover"]))
    cover_base64: str = Field(default="",
                              validation_alias=AliasChoices(*COVER_ALIASES["cover_base64"]))
    cover_url: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_url"]))
    cover_prompt: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_prompt"], "prompt"))
    cover_ratio: str = Field(default=report_cover.DEFAULT_RATIO,
                             validation_alias=AliasChoices(*COVER_ALIASES["cover_ratio"], "ratio"))
    cover_seed: Optional[int] = Field(default=None,
                                      validation_alias=AliasChoices(*COVER_ALIASES["cover_seed"], "seed"))
    cover_extra: str = Field(default="",
                             validation_alias=AliasChoices(*COVER_ALIASES["cover_extra"], "extra"))
    cover_with_title: bool = Field(default=False,
                                   validation_alias=AliasChoices(*COVER_ALIASES["cover_with_title"], "with_title"))


class ExportIn(BaseModel):
    """The event page as the reader's browser rendered it — same contract as the wiki's."""

    html: str = ""
    title: str = ""
    meta: str = ""


def _cover_or_error(payload, title: str, request: Request | None = None):
    """Resolve a submitted cover; 400 for bad input, 502 when the provider refuses."""
    try:
        return report_cover.resolve_cover(payload, title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=pick(f"封面生成失败: {exc} / cover generation failed: {exc}", request_lang(request) if request else None)) from exc


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else ("" if value is None else str(value))


def _shape(event: store.CalendarEvent, viewer: str, *, include_body: bool = False) -> dict:
    payload = {
        "slug": event.slug,
        "title": event.title,
        "start_date": _iso(event.start_date),
        "end_date": _iso(event.end_date),
        "deadline": _iso(event.deadline),
        "summary": event.summary,
        "summary_en": event.summary_en,
        "summary_zh": event.summary_zh,
        "description_en": event.description_en,
        "description_zh": event.description_zh,
        # The text each slide writes by hand, for the panel to show when the field is empty —
        # the same bridge `page_todos` is (see `calendar_page.authored_descs`). Keyed by the
        # slide's `data-lang`; a single-language page lands under "".
        "page_desc_en": "" if event.description_en else _authored_desc(event.body, "en"),
        "page_desc_zh": "" if event.description_zh else _authored_desc(event.body, "zh"),
        "category": event.category,
        "tags": event.tag_list(),
        "kind": event.kind,
        "status": event.status,
        "visibility": event.visibility,
        "owner_email": event.owner_email,
        "partners": event.partners,
        "attachments": [a.as_dict() for a in event.attachments],
        "todos": [t.as_dict() for t in event.todos],
        # The lines the PAGE writes by hand (`<ul class="ev-todo"><li>…`), which are what a
        # reader sees on an event that has no `todos` of its own. The editor shows them as
        # rows so nothing the owner can see is invisible to them — see
        # `calendar_page.authored_todos` for the misalignment this closes (2026-09-30).
        # Deliberately empty once the event HAS to-dos: those win on the page (see
        # `calendar_page.render`), so the authored lines are no longer what anyone reads.
        "page_todos": [] if event.todos else calendar_page.authored_todos(event.body),
        "source_event_id": event.source_event_id,
        "size_bytes": event.size_bytes,
        "created_at": event.created_at.isoformat() if event.created_at else "",
        "updated_at": event.updated_at.isoformat() if event.updated_at else "",
        "can_manage": event.owner_email == viewer and event.visibility == "private",
        # Cover metadata, shaped exactly like a report card's so the SPA reads both with one
        # helper (`reportsPage.coverSrc`). `has_cover` counts an external URL too; `cover_url`
        # is our own serving path whenever we hold the bytes — that route 302s to the
        # external URL otherwise, so the reader never needs to know which kind it is.
        "has_cover": event.has_cover or bool(event.cover_url),
        "cover_url": (f"{_base_href()}api/calendar/events/{event.slug}/cover"
                      if event.has_cover else (event.cover_url or "")),
        "cover_w": event.cover_w,
        "cover_h": event.cover_h,
        "cover_prompt": event.cover_prompt,
        "cover_model": event.cover_model,
    }
    if include_body:
        payload["body"] = event.body
    return payload


def _authored_desc(body: str, lang: str) -> str:
    """The description a slide writes by hand — "" when it has none.

    A bilingual page keys them by the slide's own `data-lang`; a single-language page has no
    sections at all and its one box lands under "", which is why `en` reads that too.
    """
    descs = calendar_page.authored_descs(body or "")
    if descs.get(lang):
        return descs[lang]
    return descs.get("", "") if lang == "en" else ""


def _not_found(request: Request | None = None) -> HTTPException:
    # 404 for "not yours" as well as "missing": the status code must not confirm that
    # another account owns an event at a guessed slug.
    return HTTPException(status_code=404, detail=pick(
        "日程不存在 / calendar event not found", request_lang(request) if request else None))


# ── read ─────────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(
    request: Request,
    scope: str = Query("mine", description="mine | public | shared"),
    date_from: str = Query("", alias="from", description="YYYY-MM-DD — window start"),
    date_to: str = Query("", alias="to", description="YYYY-MM-DD — window end"),
    limit: int = Query(500, ge=1, le=2000),
):
    """Events overlapping a date window — the calendar asks for a range, never for all."""
    try:
        email, _ = _identity(request)
        start = store._as_date(date_from, field_name="from") if date_from else None
        end = store._as_date(date_to, field_name="to") if date_to else None
        events = store.list_events(email, scope=scope, date_from=start, date_to=end,
                                   limit=limit)
    except CalendarError as exc:
        raise HTTPException(status_code=400,
                             detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return {"events": [_shape(e, email) for e in events], "count": len(events), "scope": scope}


@router.get("/events/{slug}")
async def get_event(slug: str, request: Request):
    """One event **with its 16:9 page** (what the detail viewer renders)."""
    email, _ = _identity(request)
    event = store.get_event(slug, email)
    if not event:
        raise _not_found(request)
    return _shape(event, email, include_body=True)


# ── write (owner, or the owner's agent) ──────────────────────────────────────

def _write_or_error(data: dict, email: str, request: Request, *, slug: str = "", cover=None,
                    clear_cover: bool = False) -> store.CalendarEvent:
    """One upsert with the error mapping every write path shares."""
    try:
        return store.upsert_event(data, email, slug=slug or None, cover=cover,
                                  clear_cover=clear_cover)
    except store.CalendarConflict as exc:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": pick("同名日程已存在 / an event with this title already exists", request_lang(request) if request else None),
            "title": exc.title,
            "conflicts": exc.conflicts,
        }) from exc
    except PermissionError as exc:
        raise _not_found(request) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc


def _merged_payload(payload: EventIn, current: store.CalendarEvent) -> dict:
    """A `PUT` merges: every field the caller did NOT send keeps its stored value.

    ⚠️ One rule for the whole model, not just a few fields. The alternative silently
    resets whatever the caller did not mention, and the two ways that bites are real:
    omitting `body` destroys the event's page, and omitting `summary_zh` turns a partner
    change on a bilingual event into a bogus "must carry a summary in both languages" 400.
    Clearing is still expressible — send an empty list/string — and an empty `body` is
    still refused by the store, because the event page is required.
    """
    data = payload.model_dump()
    given = payload.model_fields_set
    stored = {
        "title": current.title,
        "start_date": _iso(current.start_date),
        "end_date": _iso(current.end_date),
        "deadline": _iso(current.deadline),
        "summary": current.summary,
        "summary_en": current.summary_en,
        "summary_zh": current.summary_zh,
        "description_en": current.description_en,
        "description_zh": current.description_zh,
        "category": current.category,
        "tags": current.tags,
        "kind": current.kind,
        "status": current.status,
        "body": current.body,
        "slug": current.slug,
        "partners": list(current.partners),
        "attachments": [a.as_dict() for a in current.attachments],
        "todos": [t.as_dict() for t in current.todos],
        "confirm_conflict": True,          # an update is not the moment for the title gate
    }
    for key, value in stored.items():
        if key not in given:
            data[key] = value
    return data


async def _reject_unknown_cover_keys_of(request: Request) -> None:
    """Same guard as a report publish: a cover-shaped key we do not know is a 400."""
    try:
        await _reject_unknown_cover_keys(await request.json(), request)
    except HTTPException:
        raise
    except Exception:                       # malformed body → let FastAPI complain
        pass


@router.post("/events", status_code=201)
async def create_event(request: Request, payload: EventIn):
    """Create an event, or overwrite your own when `slug` matches.

    A title that already exists on an event this account can read answers **409** with the
    colliding events listed; re-send with `confirm_conflict` once the owner has seen them.
    """
    await _reject_unknown_cover_keys_of(request)
    email, _ = _identity(request)
    cover = _cover_or_error(payload, payload.title, request)
    event = _write_or_error(payload.model_dump(), email, request, cover=cover,
                            clear_cover=payload.clear_cover)
    return _shape(event, email, include_body=True)


@router.put("/events/{slug}")
async def update_event(slug: str, request: Request, payload: EventIn):
    """Update one event. **A field the body does not mention keeps its stored value.**

    Only the owner may edit: a partner who can read the event gets the same 404 a stranger
    gets, so an event never confirms to a reader that it can be written.
    """
    await _reject_unknown_cover_keys_of(request)
    email, _ = _identity(request)
    current = store.get_event(slug, email)
    if not current or current.owner_email != email:
        raise _not_found(request)
    data = _merged_payload(payload, current)
    # ⚠️ `partners` IS the read list (the comment on `_announce_new_partners` already
    # said so), so adding one is a share — and this path had no gate.
    #
    # ⚠️ Gated on the DIFF, not on the whole list. `PUT` is a merge and the client
    # re-sends the entire list every save, so testing the full list would make an event
    # that already had an out-of-company partner (always true for anything created
    # before this existed) permanently unsaveable — the owner would be unable to retitle
    # it. Same reasoning, and the same set operation, as the Inbox notification below:
    # only a genuinely NEW partner is a new share.
    _gate_new_partners(current, data, email, request)
    cover = _cover_or_error(payload, data.get("title") or current.title, request)
    event = _write_or_error(data, email, request, slug=current.slug, cover=cover,
                            clear_cover=payload.clear_cover)
    # `partners` IS the read list, so a partner added here is exactly a share.
    # Diffed against what was stored: re-saving an unchanged list must not spam
    # everybody's Inbox, and PUT is a merge, so the same list comes back every time.
    _announce_new_partners(current, event, email)
    return _shape(event, email, include_body=True)


def _partner_addresses(raw) -> set[str]:
    """Normalised, non-empty addresses out of a `partners` field."""
    return {str(v).strip().lower() for v in (raw or []) if str(v).strip()}


def _gate_new_partners(current, data, email: str, request: Request) -> None:
    """Refuse a partner the owner's organization may not share with.

    Raises 403/400 from the shared gate's own wording, so a calendar refusal reads
    exactly like a report refusal. Only the added addresses are tested — see the call
    site for why testing the whole list would be wrong.
    """
    added = sorted(_partner_addresses(data.get("partners")) - _partner_addresses(current.partners))
    if not added:
        return
    allowed, reason, _offenders = orgs.may_share_to_many(email, added)
    if not allowed:
        status, message = orgs.denial(reason)
        raise HTTPException(status_code=status,
                            detail=pick(message, request_lang(request) if request else None))


def _announce_new_partners(current, event, email: str) -> None:
    """Tell each newly added partner that they can now read this event."""
    try:
        before = _partner_addresses(current.partners)
        after = _partner_addresses(event.partners)
    except Exception:                      # noqa: BLE001 — a notification must not fail a write
        return
    for partner in sorted(after - before):
        if not partner or partner == email:
            continue
        inbox.emit(kind="share", actor_email=email, recipient_email=partner,
                   target_type="event", target_slug=event.slug, target_title=event.title or "",
                   target_url=annotations.doc_url("event", event.slug),
                   summary=f"把日程《{event.title or event.slug}》分享给了你 / shared the event \"{event.title or event.slug}\" with you")


@router.delete("/events/{slug}")
async def delete_event(slug: str, request: Request):
    email, _ = _identity(request)
    if not store.delete_event(slug, email):
        raise _not_found(request)
    return {"deleted": slug}


# ── the event's own page ─────────────────────────────────────────────────────

@router.get("/events/{slug}/raw", response_class=HTMLResponse)
async def render_event(slug: str, request: Request):
    """The event's 16:9 page as a **standalone document**.

    This is what `{base}/e/<slug>` serves and what the detail viewer loads in its iframe:
    the authored HTML plus the injected `<base>` and the deck/language runtime, so the
    reader gets the same pages, pagination and language switch a Workspace report does.
    """
    email, _ = _identity(request)
    event = store.get_event(slug, email)
    if not event:
        raise _not_found(request)
    body = _inline_deck_runtime(_inject_base_tag(event.body or "", _base_href()))
    # The template runs LAST so its stylesheet wins over an author's own CSS (same trick as
    # the deck runtime), and so the slots are filled from the event the reader may read —
    # never from whatever the author typed into the page.
    body = calendar_page.render(body, event, base_href=_base_href())
    # Notes last: the layer is anchored to the current slide, and the deck runtime
    # has to be in the document first for that slide to exist.
    body = annotations.inline_runtime(body, asset_type="event", slug=slug, viewer_email=email)
    return HTMLResponse(content=body, headers={
        "Cache-Control": "private, no-store",
        "X-Frame-Options": "SAMEORIGIN",
    })


# ── the cover: the Workspace's mechanism, reused ─────────────────────────────

@router.get("/events/{slug}/cover")
async def event_cover(slug: str, request: Request):
    """The event's cover image.

    The same contract as `GET /api/reports/{slug}/cover`: our stored bytes, or a 302 to the
    external URL the owner supplied, or a 404 when the event has no cover.
    """
    email, _ = _identity(request)
    found = store.get_cover(slug, email)
    if not found:
        raise _not_found(request)
    data, mime, external = found
    if not data:
        return RedirectResponse(url=external, status_code=302)
    return Response(content=data, media_type=mime or "image/jpeg",
                    headers={"Cache-Control": "private, no-store"})


@router.post("/events/{slug}/cover")
async def set_event_cover(slug: str, request: Request, payload: CoverIn):
    """Generate a cover for this event — or store one the owner supplied.

    Owner only: the generation runs on the deployment's image credentials, and a cover is
    part of the event's identity rather than something a reader may rewrite.
    """
    email, _ = _identity(request)
    event = store.get_event(slug, email)
    if not event or event.owner_email != email:
        raise _not_found(request)
    cover = _cover_or_error(payload, event.title, request)
    saved = store.set_cover(slug, email, cover)
    if not saved:
        raise _not_found(request)
    return _shape(saved, email)


@router.delete("/events/{slug}/cover")
async def clear_event_cover(slug: str, request: Request):
    email, _ = _identity(request)
    saved = store.set_cover(slug, email, clear=True)
    if not saved:
        raise _not_found(request)
    return _shape(saved, email)


# ── export: the wiki's exporter, reused ──────────────────────────────────────

@router.post("/events/{slug}/export/{kind}")
async def export_event(slug: str, kind: str, request: Request, payload: ExportIn):
    """Export the event page as Word, PDF or a picture.

    `kind` is `word` / `pdf` / `picture`, and the body is the **rendered** page: the SPA
    renders `body` inside `/e/<slug>` for the detail overlay and posts that HTML here.
    `services/knowledge_export.py` turns it into the file — one document shell, one font
    setup and one pagination rule for the wiki and the Calendar alike.
    """
    email, _ = _identity(request)
    event = store.get_event(slug, email)
    if not event:
        raise _not_found(request)
    kind = (kind or "").strip().lower()
    if kind not in {"word", "pdf", "picture"}:
        raise HTTPException(status_code=400, detail=pick("kind 只能是 word、pdf 或 picture / kind must be word, pdf or picture", request_lang(request) if request else None))
    title = (payload.title or event.title or slug).strip()
    try:
        data, media_type, extension = await knowledge_export.export_document(
            kind, payload.html or "", title, meta=(payload.meta or "").strip(),
            base_href=_base_href(), auth_headers=knowledge_export.auth_headers(request))
    except knowledge_export.ExportError as exc:
        raise HTTPException(status_code=422, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return Response(content=data, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{slug}.{extension}"',
        "Cache-Control": "private, no-store",
    })


# ── the owner's controls (browser session only, like the Workspace) ──────────

@router.post("/events/{slug}/publish")
async def publish_event(slug: str, request: Request):
    """Publish a read-only snapshot to the public area. The private event stays."""
    try:
        email = _require_browser(request)
        # ⚠️ Had no gate. Before `store.publish_to_public()`, so a refused publication
        # writes no snapshot and emits no Inbox notification.
        allowed, reason = orgs.may_publish(email, orgs.CHANNEL_PUBLIC,
                                           orgs.TARGET_CALENDAR, slug)
        if not allowed:
            status, message = orgs.denial(reason)
            raise HTTPException(status_code=status,
                                detail=pick(message, request_lang(request) if request else None))
        event = store.publish_to_public(slug, email)
    except PermissionError as exc:
        raise _not_found(request) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    inbox.emit(kind="publish", actor_email=email, recipient_email=None,
               target_type="event", target_slug=event.slug, target_title=event.title or "",
               target_url=annotations.doc_url("event", event.slug),
               summary=f"把日程《{event.title or event.slug}》发布到了公共区 / published the event \"{event.title or event.slug}\" to the public area")
    return _shape(event, email)


@router.delete("/events/{slug}/public")
async def withdraw_event(slug: str, request: Request):
    email = _require_browser(request)
    if not store.withdraw_public(slug, email):
        raise _not_found(request)
    return {"withdrawn": slug}


@router.post("/events/{slug}/pull")
async def pull_event(slug: str, request: Request):
    try:
        email = _require_browser(request)
        event = store.pull_copy(slug, email)
    except PermissionError as exc:
        raise _not_found(request) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return _shape(event, email, include_body=True)