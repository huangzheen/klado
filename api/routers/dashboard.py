"""Dashboard — the HTTP surface, and the page a dashboard page is served at.

A dashboard is a document that asks for its own numbers. This router is thin on purpose
and is only three things:

* the CRUD + sharing surface, whose persistence lives in `services/dashboard_store.py`;
* the **shared SQL gateway** at `POST /api/dashboard/query`, which delegates to
  `services/dataset_query.py` — the *same* guard Data Center uses. A dashboard does not
  get a wider grant than its reader already has, and there is deliberately no second
  allow-list anywhere in this file: the one place that decides "may this caller read
  this table" is `dataset_query.assert_query_allowed`;
* the standalone reader at `GET /d/{slug}`, which serves the stored HTML in the same
  shell shape `/r/{slug}` uses, plus a small runtime that hands the document its slug
  and a `kldQuery` it can call.

The runtime trap (this is the same one `vendor/report-filters.js` has, and
`tests/test_dashboard.py` guards it): the block below is re-injected on every render and
an old copy is stripped first with a regex that looks for the tag. A literal `<script`
**inside the runtime's own text** makes that regex start matching in the middle of the
file, and a re-published document ends up with two runtimes — the older one winning,
bound to nothing. The opening tag is therefore assembled as `"<" + "script"`, and the
JSON payload has its `</` escaped.
"""
from __future__ import annotations

import functools
import json
import re
from typing import Any, Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from core import identity
from core.config import app_base_href
from core.i18n import pick, request_lang
from services import dashboard_store as store
from services import doc_state
from services import annotations
from services import dataset_query as dq
from services import report_cover
from routers.reports import _require_browser
# ⚠️ Reused, not re-implemented. `_inline_filter_runtime` is ~40 lines of runtime
# assembly that the report suite already exercises; a copy is 40 lines that
# drift on the first bug fix. Both are module-level in reports.py and stay
# there — this module is a consumer, not a second home.
from routers.reports import _inline_filter_runtime as _reports_inline_filter_runtime

# `main.py` mounts this at `/api/dashboard`. The standalone page is a SEPARATE router
# so the prefix does not swallow `/d/{slug}` — and so `needs_identity()` can name the
# document prefix (`/d/`) without the API prefix also matching it.
router = APIRouter()
standalone_router = APIRouter()

MAX_TITLE = store.MAX_TITLE


# ── the error shape ──────────────────────────────────────────────────────────
#
# 404 for "not yours" as well as "missing": the status code must never confirm that a
# slug exists to an account that cannot read it (the same rule the Workspace and the
# Calendar follow).

def _lang(request: Request | None):
    return request_lang(request) if request else None


def _fail(exc: store.DashboardError, request: Request | None = None) -> HTTPException:
    status = 404
    if isinstance(exc, store.DashboardConflict):
        status = 409
    elif isinstance(exc, store.DashboardTooLarge):
        status = 413
    elif isinstance(exc, store.DashboardPolicyError):
        status = exc.status
    elif not isinstance(exc, store.DashboardNotFound):
        status = 400
    return HTTPException(status_code=status, detail=pick(str(exc), _lang(request)))


def _viewer(request: Request) -> tuple[str, bool]:
    email, _kind = identity.current_identity(request)
    return email, identity.is_admin_caller(request)


# ── schema, memoised, and re-created once if a table disappears ───────────────

def _with_schema(fn):
    """Create the tables on first use and retry once when one is missing.

    `ensure_schema()` is memoised per process and is deliberately not a check on every
    request — but these tables can disappear at runtime, and without this one DROP would
    leave every dashboard endpoint answering 500 until the server restarted.
    """
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        store.ensure_schema()
        try:
            return await fn(*args, **kwargs)
        except psycopg2.errors.UndefinedTable:
            store.reset_schema_flag()
            store.ensure_schema()
            return await fn(*args, **kwargs)

    return guarded


# ── payloads ─────────────────────────────────────────────────────────────────

class DashboardIn(BaseModel):
    """The publish / update body. Every field is optional **except on create**, where
    `title` and `html` are what a page cannot do without — which is what the store
    enforces. A field the body does not mention keeps its stored value, so re-sending
    one field cannot wipe the rest of the document."""
    title: str = ""
    summary: str = ""
    summary_zh: str = ""
    # ⚠️ The English half of the card's one line (2026-10-05). It was added when HTML
    # started landing here, because a page that arrived from Workspace already carried
    # both languages and a dashboard column that only had the Chinese one showed an
    # empty line to an English reader. The same reason the report wall has kept a pair
    # since the beginning.
    summary_en: str = ""
    # Comma-separated, the way a report stores it (`en,zh`). Empty means "not declared",
    # and the reader falls back to the summary it has rather than inventing a language.
    langs: str = ""
    # Who asked for this page (an English name or pinyin from the chat request, NOT a
    # Chinese name). The publish contract requires it, so it is stored rather than
    # accepted-and-discarded.
    submitter: str = ""
    description: str = ""
    tags: str = ""
    # A `theme.css` token name. A hex colour or an `rgba(...)` is a 400: every colour
    # in this app lives in theme.css (AGENTS.md), and a stored literal would be right in
    # one theme and unreadable in the other.
    accent: str = ""
    # Which datasets this page declares it may ask about. Identifier-shaped names; the
    # real isolation is `services/dataset_query.py` at query time.
    datasets: list[str] = Field(default_factory=list)
    html: str = ""
    status: str = "published"
    visibility: str = "private"
    pinned: bool = False
    # Placement. Content-level, set by the agent at publish time, because a dashboard is
    # written knowing how it should be presented.
    in_nav: bool = False
    nav_order: int = 0
    in_home: bool = False
    home_order: int = 0
    home_collapsed: bool = False
    # Cover. Same three sources as a report, resolved by the same
    # `services/report_cover.py::resolve_cover`, so the two features cannot drift.
    cover_base64: Optional[str] = None      # data URI or bare base64
    cover_url: Optional[str] = None         # external image; "" clears
    generate_cover: bool = False            # server-side generation (needs a key)


class ShareIn(BaseModel):
    email: str = ""


class QueryIn(BaseModel):
    sql: str
    # Default 500, hard cap 2000. A page that asks for everything gets a truncated
    # result with `truncated: true` rather than an unbounded answer.
    max_rows: int = store.DEFAULT_QUERY_ROWS


# ── read ─────────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/", include_in_schema=False)
@_with_schema
async def list_dashboards(
    request: Request,
    q: str = Query("", description="Substring search over title / summary / slug / tags"),
    status: str = Query("published", description="published (default) | draft | archived | all"),
    scope: str = Query("mine", description="mine | public | shared"),
    limit: int = Query(200, ge=1, le=store.MAX_LIST_LIMIT),
):
    """Dashboard cards (metadata only — the HTML body is fetched on demand).

    Three DISJOINT areas, because they need three different sets of actions:
    `mine` is what I own and manage, `public` is the wall anybody may read, and
    `shared` is a colleague's private dashboard shared with my address. Folding them
    together is what makes a wall advertise actions the viewer does not have.
    """
    email, _admin = _viewer(request)
    try:
        rows = store.list_dashboards(email=email, scope=scope, q=q, status=status, limit=limit)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    return [store.card(row, email, shared_with_me=(scope.strip().lower() == "shared"))
            for row in rows]


@router.get("/{slug}")
@_with_schema
async def get_dashboard(slug: str, request: Request):
    """One dashboard including its HTML. 404 when this caller may not read it."""
    email, _admin = _viewer(request)
    # ⚠️ `include_html=True` is load-bearing: the docstring promises the body and the
    # agent needs it to re-publish an edited page. Without it the row comes back
    # without the column and the response carries `html: ""` — a 200 that looks fine
    # and silently loses the document.
    row = store.get_dashboard(slug, email=email, include_html=True)
    try:
        store.require_readable(row, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    # A public page and the owner's own page count a view; a page somebody was shared
    # does not, because that is a colleague looking on the owner's behalf.
    store.bump_views(slug, email)
    payload = store.card(row or {}, email)
    payload["html"] = (row or {}).get("html") or ""
    return payload


# ── write ────────────────────────────────────────────────────────────────────

@router.put("/{slug}")
@_with_schema
async def save_dashboard(slug: str, body: DashboardIn, request: Request):
    """Create the dashboard at this slug, or update the one already there.

    The slug is a URL and is **not** editable — the body has no `slug` field on purpose.
    A slug that belongs to another account is a 409: it is the one conflict a caller
    cannot route around, so it is named rather than hidden behind a 404.
    """
    email, _admin = _viewer(request)
    title = (body.title or "").strip()
    # Public visibility widens the audience; enforce the browser and organization
    # boundaries before cover generation or any content write.
    if body.visibility == 'public':
        _require_browser(request)
        try:
            store.assert_publication_allowed(email, slug)
        except store.DashboardError as exc:
            raise _fail(exc, request) from exc

    # ── cover, resolved before the write so a bad image never creates a half card ──
    cover = None
    try:
        cover = report_cover.resolve_cover(body, title)
    except ValueError as exc:                 # our own input validation → caller's fault
        raise HTTPException(400, pick(str(exc), request_lang(request))) from exc
    except RuntimeError as exc:               # provider refused / unreachable
        raise HTTPException(502, pick(f"封面生成失败: {exc} / cover generation failed: {exc}",
                                      request_lang(request))) from exc

    existing = store.get_dashboard(slug, email=email)
    if existing and existing.get('visibility') == 'public':
        _require_browser(request)
    if not existing and not cover:
        # A card on a wall with no image is a grey rectangle saying nothing. A dashboard
        # is *the* thing a reader skims before deciding to open it, so the picture is
        # part of the deliverable, not decoration: publishing one without it is a 400
        # with the two ways out named. Updating an existing card never re-requires it.
        raise HTTPException(400, pick(
            "新建仪表盘必须带封面：cover_base64（首选，自己生成的 16:9 图）或 cover_url，"
            "或设 generate_cover=true 让服务器生成"
            " / a new dashboard needs a cover: send cover_base64 (your own 16:9 image, "
            "preferred), or cover_url, or set generate_cover=true",
            request_lang(request)))

    try:
        store.save_dashboard(body.model_dump(exclude_unset=True,
                                             exclude={"cover_base64", "cover_url",
                                                      "generate_cover"}),
                             email, slug)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    if cover is not None:
        store.set_cover(slug, cover)
    row = store.get_dashboard(slug, email=email, include_html=True) or {}
    return store.card(row, email)


# ── reader-owned state (2026-10-05) ───────────────────────────────────────────
#
# HTML now lands here, so a dashboard can carry the same two pieces of reader state a
# Workspace report could: an annotation document (`/{slug}/state`) and a filter schema
# plus per-reader views (`/{slug}/filters*`). The rules are NOT re-implemented — the
# validation, the selection folder and the tolerant reader all come from
# `services/doc_state`, which is the reports.py code moved out whole. What is repeated
# here is only the parts that are genuinely about DASHBOARDS: whose page it is, who may
# write, and which table a pick goes in.


def _dashboard_kind(html: str, filters: dict, request: Request | None = None) -> str:
    """`static` / `interactive` / `dynamic`, derived the way the report wall derives it.

    ⚠️ Re-derived on every read of the document, never stored-and-assumed, for the same
    reason the report path does: with an empty schema a page whose HTML still carries
    `data-filter-when` blocks is still `dynamic` — its slices just all render at once —
    and a stored `kind` that disagrees with the document is a card lying about itself."""
    if filters.get("filters"):
        return "dynamic"
    from routers.reports import _report_kind

    return _report_kind(html or "", request=request)


def _readable(slug: str, request: Request) -> tuple[str, dict]:
    """The row behind a slug for a caller who may read it, else a 404-shaped raise.

    ⚠️ One function, because every state endpoint below has the same rule and they must
    not drift: 404 for "not yours" as well as "missing", so the status code never
    confirms that a slug exists to an account that cannot read it."""
    email, _admin = _viewer(request)
    row = store.get_dashboard(slug, email=email)
    try:
        return email, store.require_readable(row, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc


@router.get("/{slug}/state")
# Trailing-slash alias, matching the report route. This app answers a sub-route with a
# 404 (not a 307) when a trailing slash is present, and `…/state/` is an easy thing to
# write in a template literal — a silent 404 there reads as "my save vanished".
@router.get("/{slug}/state/", include_in_schema=False)
async def get_dashboard_state(slug: str, request: Request):
    """The stored annotation state of one dashboard, as `application/json`.

    `{}` when nothing has ever been saved, so a reader can always `await r.json()` and
    get an object with no "is it a 404 or an empty state?" branch in the document."""
    store.ensure_schema()
    _email, _row = _readable(slug, request)
    stored = store.get_state(slug, email=_email)
    return Response(
        content=stored.strip() or "{}",
        media_type="application/json",
        # no-store, not no-cache: a stale copy makes a freshly-added annotation look
        # like it was lost, which is exactly the failure this feature removes.
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/{slug}/state")
@router.post("/{slug}/state/", include_in_schema=False)   # see the GET alias above
async def save_dashboard_state(slug: str, request: Request):
    """Overwrite one dashboard's annotation state with the request body, verbatim.

    Browser-only, owner-only and `private`-only. The body is read raw rather than
    through a model because the contract is "GET gives back what you POSTed" and a
    re-dump would silently drop duplicate keys and reformat numbers."""
    email = _require_browser(request)
    store.ensure_schema()
    text = doc_state.state_text(await request.body(), request)
    try:
        updated = store.save_state(slug, text, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    return {"ok": True, "slug": slug, "updated_at": updated}


# ── Dynamic pages: the sidebar's two halves ──────────────────────────────────
#
#   GET/PUT/DELETE  /api/dashboard/{slug}/filters           — the author's schema
#   PUT             /api/dashboard/{slug}/filters/selection — this reader's view
#
# Exactly the report split, and for the same reason: the SCHEMA is a machine-caller's
# statement about the document ("this page HAS these filters"), while a SELECTION is one
# person's view of a shared page. So the schema PUT is open to the agent credential and
# the selection PUT is not.


class FilterSchemaIn(BaseModel):
    version: int = 1
    filters: list[Any] = []


class FilterSelectionIn(BaseModel):
    selection: dict[str, Any] = {}


@router.get("/{slug}/filters")
async def get_dashboard_filters(slug: str, request: Request):
    """The filter rail's whole state for THIS reader: schema + their saved picks.

    Readable by anyone who may read the page — the rail is part of reading a dynamic
    page, and a shared page whose reader cannot filter it is a screenshot."""
    store.ensure_schema()
    email, row = _readable(slug, request)
    schema = doc_state.load_filter_schema(row)
    saved, saved_at = store.saved_selection(row["id"], email)
    effective, dropped = doc_state.effective_selection(schema, saved)
    return {
        "slug": slug,
        "kind": row.get("kind") or "static",
        "configured": bool(schema.get("filters")),
        "schema": schema,
        "selection": saved,
        "effective": effective,
        # Names the picks that no longer apply to the current schema, so the rail can
        # say "this view was adjusted" instead of silently changing it.
        "dropped": dropped,
        # Whether THIS caller may rewrite the schema. The rail never offers it (that is
        # the product rule); it is reported so an agent can tell "I am the owner" from
        # "I am looking at someone else's page".
        "can_edit": bool((row.get("owner_email") or "") == email
                         and (row.get("visibility") or "private") == "private"),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "selection_updated_at": saved_at,
    }


@router.put("/{slug}/filters")
async def set_dashboard_filters(slug: str, body: FilterSchemaIn, request: Request):
    """Define (or replace) a page's filters — the agent-facing half.

    Setting a schema on a page that was `static` promotes it to `dynamic`, because the
    schema and the document's `data-filter-when` blocks are two halves of one decision.
    An empty list removes every filter and leaves the card static again."""
    email, _admin = _viewer(request)
    store.ensure_schema()
    schema = doc_state.clean_filter_schema(body.model_dump(), request)
    stored = json.dumps(schema, ensure_ascii=False) if schema["filters"] else ""
    row = store.get_dashboard(slug, email=email)
    try:
        owned = store.require_owner(row, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    # ⚠️ The document is re-read here for the derivation, not taken from the earlier
    # read: a caller that PUTs a schema and a caller that does not are different code
    # paths, and deriving from a stale row would leave `kind` describing a document
    # that no longer exists.
    full = store.get_dashboard(slug, email=email, include_html=True) or owned
    kind = _dashboard_kind(full.get("html") or "", schema, request)
    try:
        store.set_filters(slug, stored, kind, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    return {"ok": True, "slug": slug, "kind": kind,
            "configured": bool(schema["filters"]),
            "filter_count": len(schema["filters"]), "schema": schema}


@router.delete("/{slug}/filters")
async def clear_dashboard_filters(slug: str, request: Request):
    """Remove every filter from a page and re-derive its kind from the document."""
    email, _admin = _viewer(request)
    store.ensure_schema()
    try:
        owned = store.require_owner(store.get_dashboard(slug, email=email), email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    full = store.get_dashboard(slug, email=email, include_html=True) or owned
    kind = _dashboard_kind(full.get("html") or "", {"filters": []}, request)
    try:
        store.set_filters(slug, "", kind, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    return {"ok": True, "slug": slug, "kind": kind, "configured": False, "filter_count": 0}


@router.put("/{slug}/filters/selection")
async def save_dashboard_filter_selection(slug: str, body: FilterSelectionIn,
                                          request: Request):
    """Save THIS reader's filter values (the rail's state, not the document's).

    Browser-only on purpose: a selection is a person's view. A machine caller changes a
    page's DEFAULT by editing the schema, which is the honest place for "everyone should
    open on Q2"."""
    email = _require_browser(request)
    store.ensure_schema()
    _email, row = _readable(slug, request)
    schema = doc_state.load_filter_schema(row)
    if not schema.get("filters"):
        raise HTTPException(409, pick(
            "这个页面没有可设置的筛选器 / this page has no filters to set",
            request_lang(request) if request else None))
    effective, dropped = doc_state.effective_selection(schema, body.selection)
    saved_at = store.save_selection(row["id"], email, effective)
    return {"ok": True, "slug": slug, "selection": effective, "dropped": dropped,
            "updated_at": saved_at or None}


@router.get("/{slug}/cover")
@_with_schema
async def dashboard_cover(slug: str, request: Request):
    """The dashboard's cover image: served bytes, or a 302 to an external URL.

    Same contract as `/api/reports/{slug}/cover`, so the two card walls can share the
    client-side `<img>` logic and the cache-buster (`?v=<updated_at>`).
    """
    email, _admin = _viewer(request)
    row = store.get_dashboard(slug, email=email)
    if not row:
        raise HTTPException(404, pick("仪表盘不存在 / dashboard not found", request_lang(request)))
    try:
        store.require_readable(row, email)
    except store.DashboardNotFound as exc:
        raise _fail(exc, request) from exc
    cover = store.get_cover(slug)
    if not cover:
        raise HTTPException(404, pick("这个仪表盘还没有封面 / this dashboard has no cover",
                                      request_lang(request)))
    if not cover.get("data"):
        return Response(status_code=302, headers={"Location": cover["url"]})
    return Response(content=cover["data"],
                    media_type=cover.get("mime") or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.delete("/{slug}")
@_with_schema
async def delete_dashboard(slug: str, request: Request):
    """Delete one dashboard **and its shares**. Owner only; 404 for anyone else."""
    email, _admin = _viewer(request)
    try:
        store.delete_dashboard(slug, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    return {"deleted": slug}


# ── sharing ──────────────────────────────────────────────────────────────────

@router.get("/{slug}/shares")
@_with_schema
async def list_dashboard_shares(slug: str, request: Request):
    """Everyone this dashboard is shared with. Owner only — 404, not 403."""
    email, _admin = _viewer(request)
    try:
        return store.list_shares(slug, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc


@router.post("/{slug}/shares/colleagues")
@_with_schema
async def share_dashboard(slug: str, body: ShareIn, request: Request):
    """Grant one colleague read access. Idempotent; returns the share row."""
    email, _admin = _viewer(request)
    try:
        return store.share_dashboard(slug, body.email, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc


@router.delete("/{slug}/shares/colleagues/{recipient_email}")
@_with_schema
async def revoke_dashboard_share(slug: str, recipient_email: str, request: Request):
    """Remove one grant. Owner only — 404, not 403: see the module docstring."""
    email, _admin = _viewer(request)
    try:
        store.revoke_share(slug, recipient_email, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    return {"ok": True}


# ── pulling a page, so it survives its original author ────────────────────────

@router.post("/{slug}/pull")
@_with_schema
async def pull_dashboard(slug: str, request: Request):
    """Copy this page into my own account, datasets and all.

    A dashboard is a live query that names its datasets, so copying the row alone
    would produce a page that looks private, is not, and dies with the original
    author's account. Each dataset is pulled first and the copy is rewritten to the new
    names; `datasets_pulled` and `missing_datasets` in the response say exactly what
    became independent and what did not.

    Sharing hands over *access* (one grant row, dead when the owner closes their
    account). Pulling hands over the *data*. See `services/pulls.py`.

    Browser-only via the shared `_require_browser`, for the same reason reports and
    calendar events are: `/api/dashboard` IS in the agent write allow-list, so the
    prefix check alone would happily let an agent duplicate a colleague's page.
    """
    from services import pulls
    email = _require_browser(request)
    try:
        return pulls.pull_dashboard(slug, email)
    except pulls.PullError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), request_lang(request))) from exc


# ── the shared SQL gateway ───────────────────────────────────────────────────

@router.post("/query")
@_with_schema
async def run_dashboard_query(body: QueryIn, request: Request):
    """Run one read-only statement on behalf of the dashboard page.

    ⚠️ **This is Data Center's gateway, not a second one.** Every statement-shape check,
    the dataset-visibility rule and the `SET TRANSACTION READ ONLY` barrier live in
    `services/dataset_query.py` and are reached through `run_scoped_query` — so a
    dashboard's reader is entitled to exactly what that reader could type into the Data
    Center console, and cannot be widened by a module that forgot to ask.
    """
    email, admin = _viewer(request)
    max_rows = body.max_rows if body.max_rows and body.max_rows > 0 else store.DEFAULT_QUERY_ROWS
    return dq.run_scoped_query(request, body.sql, viewer_email=email, admin=admin,
                               max_rows=min(max_rows, store.MAX_QUERY_ROWS))


# ── the standalone reader: GET /d/{slug} ─────────────────────────────────────
#
# The same document shell shape `/r/{slug}` uses: a `<base href>` so relative asset and
# API paths resolve inside the app mount point, the stored HTML otherwise untouched, and
# no-store headers. The base tag and the runtime are assembled here rather than imported
# from `routers/reports.py`: that file is out of scope for this module, and a module
# that has to be sold on its own cannot depend on the 3400 lines of the one next to it.

_BASE_TAG_RE = re.compile(r"<base\b", re.IGNORECASE)
_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
# The tag the server looks for when it strips a previous copy. It must match the
# opening tag below and nothing else.
_RUNTIME_RE = re.compile(
    r"""<script\b[^>]*\bdata-dashboard-runtime\b[^>]*>[\s\S]*?</script\s*>""", re.IGNORECASE)


def _inject_base_tag(html: str, base: str) -> str:
    """Insert `<base href="…">` unless the document already declares one, so a page can
    write `api/dashboard/query` and reach the app in every deployment."""
    if _BASE_TAG_RE.search(html):
        return html
    tag = f'<base href="{base}">'
    head = _HEAD_RE.search(html)
    if head:
        return html[:head.end()] + tag + html[head.end():]
    doc = re.search(r"<html\b[^>]*>", html, re.IGNORECASE)
    if doc:
        return html[:doc.end()] + "<head>" + tag + "</head>" + html[doc.end():]
    return tag + html            # a fragment: a second file wins, same as a <base> would


# ⚠️ The opening tag is built by concatenation on purpose. The literal is spelled out
# here once, in the one place that owns the tag; a second occurrence inside the runtime's
# own text would make `_RUNTIME_RE` start matching mid-file (see the module docstring).
_RUNTIME_TAG = "<" + "script data-dashboard-runtime>"

_QUERY_RUNTIME = """
(() => {
  const cfg = window.kladoDashboard || {};
  const endpoint = new URL('api/dashboard/query', document.baseURI).href;
  // One entry point for the author: the same guard, the same read-only transaction, the
  // same dataset visibility rule the Data Center console has.
  window.kldQuery = async (sql, opts) => {
    const o = opts || {};
    const res = await fetch(endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: String(sql == null ? '' : sql),
                            max_rows: o.max_rows || cfg.maxRows || 500 })
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      throw new Error((data && (data.detail || data.error)) || ('HTTP ' + res.status));
    }
    return data;
  };
})();
"""


def runtime_html(row: dict, lang: str = "") -> str:
    """The injected block: who this page is, and how it may ask for numbers.

    Injected into `<head>`, so it runs before any script the author wrote. A title or
    dataset list containing `</script>` cannot close the block — `</` is escaped, the
    same way the filter runtime's state payload is.
    """
    payload = json.dumps({
        "slug": row.get("slug") or "",
        "title": row.get("title") or "",
        "datasets": store.datasets_of(row),
        "lang": lang or "",
        "maxRows": store.DEFAULT_QUERY_ROWS,
    }, ensure_ascii=False).replace("</", "<\\/")
    body = f"window.kladoDashboard = {payload};\n{_QUERY_RUNTIME}"
    return f"{_RUNTIME_TAG}\n{body}\n</" + "script>"


_KIT_TAG_RE = re.compile(
    r"""<!--\s*klado-document-kit\s*-->[\s\S]*?<!--\s*/klado-document-kit\s*-->""",
    re.IGNORECASE,
)
# Bumped whenever document.css / document.js change. The static mount already sends
# `no-store`, so this is belt-and-braces against a proxy that ignores it.
_KIT_VERSION = "20261003-doc1"

_DOCUMENT_KIT = f"""<!-- klado-document-kit -->
<link rel="stylesheet" href="theme.css?v={_KIT_VERSION}">
<link rel="stylesheet" href="document.css?v={_KIT_VERSION}">
<script src="theme.js?v={_KIT_VERSION}"></script>
<script src="document.js?v={_KIT_VERSION}"></script>
<!-- /klado-document-kit -->"""


def _insert_into_head(html: str, block: str) -> str:
    """Put `block` in `<head>`, **after `<base>`** and after any block we injected earlier.

    ONE function for both the document kit and the query runtime, on purpose. The
    ordering rule is not obvious — a relative URL resolves against whatever base is
    in effect at the instant it is parsed, so a `<link>` or a `new URL(…, baseURI)`
    emitted ahead of the base tag silently uses the document URL instead — and a rule
    written down twice in one file is a rule that survives exactly one of the two
    copies being read.

    The "after any earlier block" part is not cosmetic. Inserting at a fixed offset
    means a LATER call lands BEFORE an earlier one, so the document came out as
    `base → runtime → kit` while both functions believe they run in order. Harmless
    today (they only need to be after `<base>`) and a trap the day someone adds a
    third block and reasons about what it can rely on.
    """
    base = _BASE_TAG_RE.search(html)
    if base:
        pos = html.index(">", base.start()) + 1
    else:
        head = _HEAD_RE.search(html)
        if head:
            pos = head.end()
        else:
            doc = re.search(r"<html\b[^>]*>", html, re.IGNORECASE)
            if doc:
                return html[:doc.end()] + "<head>" + block + "</head>" + html[doc.end():]
            return block + html
    moved = True
    while moved:
        moved = False
        for rx in (_KIT_TAG_RE, _RUNTIME_RE):
            m = rx.match(html, pos)
            if m:
                pos = m.end()
                moved = True
    return html[:pos] + block + html[pos:]


def _inject_document_kit(html: str) -> str:
    """Give a standalone document the application's own design tokens and components.

    This is the single highest-leverage line in the whole dashboard feature, and it
    exists because of a failure that looks like the agent's fault and is not: the
    author writes `background: var(--bg)` — correctly, per the convention — and the
    document has no `theme.css`, so every `var()` resolves to nothing and the page
    renders as bare HTML **with the CSS still sitting in the source, looking right**.
    Nothing errors. A human reading the HTML cannot tell what is wrong.

    So the tokens are not a thing the author is asked to remember. `theme.css` is the
    app's own token file (273 lines, **zero** class selectors — it cannot leak app
    layout into a document), linked rather than copied so there is one source of
    colour, and `theme.js` makes the document follow the reader's theme choice: the
    two share an origin and the `klado-theme` key, and `theme.js::applyToFrames`
    pushes a toggle from the app straight into this frame.

    `document.css` is the component layer that has no counterpart in the app: a page
    frame, KPI tiles, tables, chips, bar meters. `document.js` is formatting plus
    four charts, and it redraws them on `klado:theme` — a chart reads its colours
    once, so without that a dark-theme reader keeps light-theme bars.

    Injected into `<head>`, and — this is the part that is easy to get wrong —
    **after `<base>`**, for the same reason the query runtime goes there: a relative
    `href` is resolved against whatever base is in effect *at the moment it is
    parsed*. Emitted before the base tag, `theme.css` resolves against the document
    URL (`/d/<slug>`) and 404s as `/d/theme.css` — silently, with the markup still
    reading correctly in the source. It is the same trap that once made every query
    on a dashboard fail for a reason unrelated to the query.

    It sits before the author's own `<style>`, which may still come later and win:
    the kit is the floor, not the ceiling.
    """
    html = _KIT_TAG_RE.sub("", html)
    return _insert_into_head(html, _DOCUMENT_KIT)


def _inject_runtime(html: str, script: str) -> str:
    """Strip a previous copy, then place ours **after `<base>`** in `<head>`.

    Order matters, and getting it wrong breaks the page silently. A `<base href>`
    only affects URLs resolved *after it is parsed*, so a runtime inserted directly
    after `<head>` — ahead of the base tag — computes `document.baseURI` from the
    document URL instead. For a page served at `/d/<slug>` that is
    `…/d/<slug>`, so `new URL('api/dashboard/query', baseURI)` lands on
    `…/d/api/dashboard/query`: the page renders, `kldQuery` exists, and every query
    it makes is refused for a reason that has nothing to do with the query. The
    block still has to sit in `<head>` rather than after the author's scripts,
    which is the point of injecting it at all.
    """
    html = _RUNTIME_RE.sub("", html)
    return _insert_into_head(html, script)


@standalone_router.get("/d/{slug}", response_class=HTMLResponse, include_in_schema=False)
@_with_schema
async def render_dashboard(slug: str, request: Request):
    """The dashboard as a **standalone page** — what the reader's iframe loads.

    Needs an identity, so `/d/` is in `main.py::needs_identity()`'s prefix list (that
    list is the single place that says which non-`/api/` paths resolve a caller; a new
    document route added without it gets a 401 inside the handler, for a signed-in
    reader, which reads as "the app is broken").

    A share counts as a read but not as a view: `bump_views` only fires for the public
    and owner paths.
    """
    email, _kind = identity.current_identity(request)
    row = store.get_dashboard(slug, email=email, include_html=True)
    try:
        store.require_readable(row, email)
    except store.DashboardError as exc:
        raise _fail(exc, request) from exc
    store.bump_views(slug, email)
    body = _inject_base_tag((row or {}).get("html") or "", app_base_href())
    body = _inject_document_kit(body)
    body = _inject_runtime(body, runtime_html(row or {}, request_lang(request) or ""))
    # ⚠️ The two reader runtimes, added 2026-10-05 when HTML started landing here.
    # Order matters and is the same as the Workspace reader's: the filter runtime goes
    # in BEFORE the deck runtime, because the deck counts visible pages and a hidden
    # slice that was still counted shows up in the page number.
    schema, selection = _filter_state_for(row or {}, email)
    body = _inline_filter_runtime(body, slug, schema, selection)
    body = _inline_annotation_layer(body, row or {}, viewer_email=email)
    return HTMLResponse(content=body, headers={
        # Dashboards are re-published under a stable slug; never let a stale copy linger.
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


# ── the two reader runtimes ──────────────────────────────────────────────────
#
# ⚠️ Both are the REPORTS implementations, imported rather than copied. `_inline_filter_
# runtime` and `_inline_annotation_layer` are ~70 lines of runtime assembly each and
# both are already exercised by the report test suite; a second copy would be 140 lines
# that drift on the first bug fix. What is dashboard-specific is only the URL they
# point at, and that is a parameter.


def _filter_state_for(row: dict, email: str) -> tuple[dict, dict]:
    """(schema, this reader's effective selection) for one page.

    ⚠️ Never raises on a page with no filters: a page that is not `dynamic` must be
    served exactly as it was stored, and an unreadable `filters_json` degrades to "no
    filters" rather than taking the page down for everybody."""
    schema = doc_state.load_filter_schema(row)
    if not schema.get("filters"):
        return schema, {}
    try:
        saved, _at = store.saved_selection(row.get("id"), email)
    except Exception:                        # a missing selection table is not a 500
        return schema, {}
    effective, _dropped = doc_state.effective_selection(schema, saved)
    return schema, effective


def _inline_filter_runtime(html: str, slug: str, schema: dict, selection: dict) -> str:
    """Bind a page to its filter schema and this reader's picks.

    ⚠️ The reports implementation, called directly — NOT copied and NOT rewritten with a
    URL swap. The first version of this did `out.replace("/api/reports/", …)`, and it was
    wrong for a reason worth recording: `vendor/report-filters.js` makes NO requests at
    all. The filter rail lives in the PARENT app's sidebar and reaches the document by
    postMessage, so there is no `/api/reports/…` inside the injected runtime to rewrite —
    a blanket replace would only have hit whatever the AUTHOR happened to write, silently
    breaking their links. The endpoint prefix is a frontend concern (the sidebar) and is
    handled there, not here.

    Order note: this goes in before the deck runtime, because the deck counts visible
    pages and a slice hidden by the filter would still be counted."""
    return _reports_inline_filter_runtime(html, slug, schema, selection)


def _inline_annotation_layer(html: str, row: dict, *, viewer_email: str = "") -> str:
    """Give a static page its note layer (right-click → a note at the click point).

    The shared `/api/annotations` endpoint already knows the `dashboard` family, so this
    is the reports call with `asset_type` changed. An `interactive` or `dynamic` page is
    skipped for the reason the report path skips it: it already owns the right-click
    gesture, and two menus on one click is worse than either."""
    if (row.get("kind") or "static") != "static":
        return html
    return annotations.inline_runtime(html, asset_type="dashboard",
                                      slug=row.get("slug") or "",
                                      viewer_email=viewer_email)
