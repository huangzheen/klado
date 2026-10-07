"""Business knowledge base — HTTP surface (the wiki).

Read **and write** live here, deliberately outside ``routers/knowledge.py``: that
router's whole promise is "no write endpoint, so sharing read access cannot leak
write access", and ``tests/test_knowledge_router.py`` asserts it stays GET-only.
Keeping the business knowledge API in its own router preserves that property and
lets the write surface be reviewed on its own.

What is here
------------
* ``/items``            — the wiki's pages: list / create / read / update / delete
* ``/items/{slug}/move`` — file a page under a project + folder (2026-10-03)
* ``/projects`` ``/folders`` — the containers a page is filed into: 项目 (a card)
  → 文件夹 → 页面, with a 未归档 container at both levels
* ``/items/{slug}/publish`` · ``/public`` — the public snapshot (the reports model)
* ``/items/{slug}/pull`` — take a private copy of a public or shared page
* ``/items/{slug}/shares`` · ``/folders/{id}/shares`` — named colleague accounts,
  owner-only. A **folder** share is one row that covers every page filed in it.

Who may do what
---------------
Reads are permission-filtered per account (owner → colleague-shared → public
snapshot). Writes follow the Workspace convention exactly:

* **Creating/updating your own page, project or folder** — any identity, including
  an agent access code (the Feishu agent edits the owner's knowledge, and creating
  a folder with a generated cover is a plain write). This is the agent's entry
  point for `POST /projects` and `POST /projects/{slug}/folders`.
* **Publishing to public, pulling a copy, changing shares** — **browser session
  only**. Public knowledge is silently retrieved by *other people's* agents, so
  the same rule that guards report sharing applies with more force here.
"""
from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import AliasChoices, BaseModel, Field

from core.i18n import pick, request_lang
from klado_shared import orgs
from routers.reports import COVER_ALIASES, _base_href, _identity, _require_browser
from services import annotations, inbox, knowledge_export, report_cover
from services.ai import business_knowledge as store
from services.ai import knowledge_projects as filing
from services.ai.business_knowledge import BusinessKnowledgeError

router = APIRouter()


class ItemIn(BaseModel):
    title: str = ""
    body: str = ""
    summary: str = ""
    # A bilingual page (one with `<!-- lang:zh -->` and `<!-- lang:en -->`) must carry a
    # summary in both languages — the page list tooltip shows the lines. `summary` is for
    # single-language pages and defaults to the English line on a bilingual one.
    summary_en: str = ""
    summary_zh: str = ""
    category: str = ""
    tags: str = ""
    document_type: str = "note"
    author: str = "agent"
    submitter: str = ""
    version: str = "1.0"
    status: str = "published"
    slug: str = ""
    # Set only after the owner has been shown which existing pages the new title
    # collides with (the server answers 409 and lists them). Creating a page is the
    # moment knowledge is published, so a duplicate is a decision, not an accident.
    confirm_conflict: bool = False


class ShareIn(BaseModel):
    emails: list[str] = Field(default_factory=list)


class ExportIn(BaseModel):
    """The page as the reader's browser rendered it.

    The wiki's markdown renderer, syntax highlighting, Mermaid pass and image-width
    handling all live in the SPA, so the export takes the rendered body instead of
    growing a second markdown implementation in Python (see services/knowledge_export.py).
    """

    html: str = ""
    title: str = ""
    meta: str = ""


class AssetIn(BaseModel):
    """One image to embed in a page.

    Base64 rather than multipart: the caller is usually an agent with a JSON body
    already, and a second content type is one more thing to get wrong. A data URI in
    `content_base64` is accepted too.
    """

    filename: str = ""
    content_base64: str = ""
    content_type: str = ""


def _shape(item: store.KnowledgeItem, viewer: str, *, include_body: bool = False,
           placement: tuple[str, int] | None = None) -> dict:
    project_slug, folder_id = placement if placement else (item.project_slug, item.folder_id)
    payload = {
        "slug": item.slug,
        "document_id": item.document_id(),
        "title": item.title,
        "summary": item.summary,
        "summary_en": item.summary_en,
        "summary_zh": item.summary_zh,
        "category": item.category,
        "tags": item.tag_list(),
        "document_type": item.document_type,
        "status": item.status,
        "enabled": item.enabled,
        "version": item.version,
        "author": item.author,
        "submitter": item.submitter,
        "owner_email": item.owner_email,
        "visibility": item.visibility,
        "source_item_id": item.source_item_id,
        "size_bytes": item.size_bytes,
        # Where the page is filed. Resolved, never raw: a page stored before
        # projects existed carries NULLs (see KnowledgeItem.project_slug) and the
        # client must never have to know that — it only ever renders containers.
        "project_slug": project_slug or "",
        "folder_id": folder_id,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
        "shared_emails": item.shared_emails,
        # The client shows Edit / Delete / Publish only when it may actually do it.
        "can_manage": item.owner_email == viewer and item.visibility == "private",
    }
    if include_body:
        payload["body"] = item.body          # markdown source; the client renders it
    return payload


def _placement(email: str):
    """The reader's unfiled container, resolved once per response.

    Resolving per item would mean one `ensure_unfiled()` query per page, and a 200
    page list would be 200 round trips for an answer that is the same for all of
    them.
    """
    return filing.ensure_unfiled(email)


def _shape_project(project: filing.KnowledgeProject, viewer: str) -> dict:
    """A project card. `cover_url` points at our own endpoint when we hold bytes."""
    return {
        "slug": project.slug,
        "title": project.title,
        "summary": project.summary,
        "system": project.system,
        "owner_email": project.owner_email,
        "has_cover": project.has_cover,
        "cover_url": (f"{_base_href()}api/knowledge/projects/{quote(project.slug, safe='')}/cover"
                      if project.has_cover else (project.cover_url or "")),
        "cover_w": project.cover_w,
        "cover_h": project.cover_h,
        "folder_count": project.folder_count,
        "item_count": project.item_count,
        "shared_count": project.shared_count,
        "created_at": project.created_at.isoformat() if project.created_at else "",
        "updated_at": project.updated_at.isoformat() if project.updated_at else "",
        "can_manage": project.owner_email == viewer,
    }


def _shape_folder(folder: filing.KnowledgeFolder) -> dict:
    return {
        "id": folder.id,
        "project_slug": folder.project_slug,
        "title": folder.title,
        "system": folder.system,
        "sort_order": folder.sort_order,
        "item_count": folder.item_count,
        "shared_emails": folder.shared_emails,
        "can_manage": folder.can_manage,
    }


def _not_found(lang: str | None = None) -> HTTPException:
    # 404 for "forbidden" as well as "missing": the status code must not tell one
    # account that another account owns a page with this slug.
    return HTTPException(status_code=404,
                         detail=pick("知识页面不存在 / knowledge item not found", lang))


# ── list / read ──────────────────────────────────────────────────────────────

@router.get("/items")
async def list_items(
    request: Request,
    scope: str = Query("mine", description="mine | public | shared"),
    q: str = Query(""),
    project: str = Query("", description="narrow to one project slug"),
    folder_id: Optional[int] = Query(None, description="narrow to one folder id"),
    limit: int = Query(200, ge=1, le=500),
):
    """Wiki page list for one view. ``mine`` never leaks another owner's pages.

    ``project`` / ``folder_id`` narrow the list without widening it: the
    permission clause is unchanged, so a folder the caller was given shows exactly
    the pages that folder's grant makes readable.
    """
    try:
        email, _ = _identity(request)
        # The unfiled container is resolved **before** the store call, and its folder
        # id is passed down: a request for 未归档 has to match the pages that carry
        # NULL placement, and `folder_id = <unfiled>` matches none of them.
        unfiled_project, unfiled_folder = _placement(email)
        items = store.list_items(email, scope=scope, q=q, limit=limit,
                                 project=project, folder_id=folder_id,
                                 unfiled_folder=unfiled_folder)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    categories = sorted({item.category for item in items if item.category})
    return {
        "items": [_shape(item, email, placement=_resolved(item, (unfiled_project, unfiled_folder)))
                  for item in items],
        "count": len(items),
        "categories": categories,
        "scope": scope,
    }


def _resolved(item: store.KnowledgeItem, unfiled: tuple[str, int]) -> tuple[str, int]:
    """An item's concrete container, falling back to the reader's 未归档."""
    if item.project_slug and item.folder_id:
        return item.project_slug, int(item.folder_id)
    return unfiled


@router.get("/items/{slug}")
async def get_item(slug: str, request: Request):
    """One page **with its markdown body** (the editor's source of truth)."""
    try:
        email, _ = _identity(request)
        item = store.get_item(slug, email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    if not item:
        raise _not_found(request_lang(request) if request else None)
    return _shape(item, email, include_body=True, placement=_resolved(item, _placement(email)))


# ── write (owner, or the owner's agent) ──────────────────────────────────────

@router.post("/items", status_code=201)
async def create_item(request: Request, payload: ItemIn):
    """Create a page, or overwrite your own when ``slug`` matches.

    Markdown only — an HTML document or a binary blob is rejected (400) rather
    than stored and rendered into someone else's page.

    A title that already exists on a page this account can read answers **409** with
    the colliding pages listed; re-send with ``confirm_conflict`` once the owner has
    seen them. Creating a page is when knowledge is published, so a duplicate is a
    decision for the owner, not something the server should silently allow.
    """
    try:
        email, _ = _identity(request)
        item = store.upsert_item(payload.model_dump(), email)
    except store.BusinessKnowledgeConflict as exc:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": pick("同名页面已存在 / a page with this title already exists",
                                   request_lang(request) if request else None),
            "title": exc.title,
            "conflicts": exc.conflicts,
        }) from exc
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return _shape(item, email, include_body=True, placement=_resolved(item, _placement(email)))


@router.put("/items/{slug}")
async def update_item(slug: str, request: Request, payload: ItemIn):
    try:
        email, _ = _identity(request)
        item = store.upsert_item(payload.model_dump(), email, slug=slug)
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return _shape(item, email, include_body=True, placement=_resolved(item, _placement(email)))


class EnabledIn(BaseModel):
    enabled: bool


@router.put("/items/{slug}/enabled")
async def set_item_enabled(slug: str, request: Request, payload: EnabledIn):
    """The curator's 🌟: lit = the page feeds the agent's knowledge search.

    Owner-only (browser **or** their agent — this is an edit, not a publication,
    so unlike publish/pull/shares there is no `_require_browser` here). A disabled
    page leaves retrieval in every scope but stays readable and listed; the public
    snapshot follows the original. A dedicated endpoint rather than a PUT of the
    whole item, because the item PUT is a full-row overwrite.
    """
    try:
        email, _ = _identity(request)
        item = store.set_enabled(slug, email, payload.enabled)
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return _shape(item, email, placement=_resolved(item, _placement(email)))


@router.delete("/items/{slug}")
async def delete_item(slug: str, request: Request):
    try:
        email, _ = _identity(request)
        deleted = store.delete_item(slug, email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    if not deleted:
        raise _not_found(request_lang(request) if request else None)
    return {"deleted": slug}


# ── images a page can embed ──────────────────────────────────────────────────

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)
MAX_ASSET_BYTES = 8 * 1024 * 1024


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    for magic, extension, content_type in _IMAGE_MAGIC:
        if data.startswith(magic):
            return extension, content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


@router.post("/assets", status_code=201)
async def upload_asset(request: Request, payload: AssetIn):
    """Store one image and return the URL to put in a page's markdown.

    An agent has no filesystem the reader can reach, so a page that needs a picture gets
    one through here. The type is sniffed from the bytes rather than trusted from the
    filename — a `.png` that is really something else would be stored and then served
    back as an image. What comes back is a **relative** URL: the browser resolves it
    against the mount point, and the exporter resolves it against the same one.
    """
    _identity(request)
    raw = (payload.content_base64 or "").strip()
    if raw.startswith("data:"):
        _header, _, raw = raw.partition(",")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=pick("content_base64 不是合法的 base64 / content_base64 is not valid base64",
                                   request_lang(request) if request else None)) from exc
    if not data:
        raise HTTPException(status_code=400, detail=pick("content_base64 为空 / content_base64 is empty",
                                    request_lang(request) if request else None))
    if len(data) > MAX_ASSET_BYTES:
        raise HTTPException(
            status_code=413,
            detail=pick(f"图片 {len(data)} 字节，超过上限 {MAX_ASSET_BYTES} 字节 / "
                           f"the image is {len(data)} bytes; the limit is {MAX_ASSET_BYTES} bytes",
                           request_lang(request) if request else None))
    sniffed = _sniff_image(data)
    if not sniffed:
        raise HTTPException(status_code=400,
                            detail=pick("只接受 PNG / JPEG / GIF / WebP 图片 / only PNG / JPEG / GIF / WebP images are accepted",
                                request_lang(request) if request else None))
    extension, content_type = sniffed
    stem = re.sub(r"[^a-z0-9]+", "-", (payload.filename or "image").rsplit(".", 1)[0].lower())
    key = (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
           + "-" + (stem.strip("-")[:40] or "image") + extension)
    from services import oss_storage

    try:
        oss_storage.put_object("knowledge-assets", key, data, content_type=content_type)
    except Exception as exc:                       # storage is best-effort by nature
        raise HTTPException(status_code=503, detail=pick(f"图片存储失败: {exc} / could not store the image: {exc}",
                             request_lang(request) if request else None)) from exc
    path = f"knowledge-assets/{key}"
    url = "/api/storage/serve?path=" + quote(path, safe="")
    return {
        "path": path,
        "url": url,
        "bytes": len(data),
        "content_type": content_type,
        "markdown": f"![caption]({url})",
    }


# ── exports ──────────────────────────────────────────────────────────────────

@router.post("/items/{slug}/export/{kind}")
async def export_item(slug: str, kind: str, request: Request, payload: ExportIn):
    """Export the page the reader is looking at as Word, PDF or a picture.

    `kind` is `word` / `pdf` / `picture`. The body is the **rendered** page (see
    `ExportIn`), so what lands in the file is what was on screen — including images, their
    widths, and the language the reader had selected.
    """
    email, _ = _identity(request)
    item = store.get_item(slug, email)
    if not item:
        raise _not_found(request_lang(request) if request else None)
    title = (payload.title or item.title or slug).strip()
    meta = (payload.meta or "").strip()
    base_href = _base_href()
    kind = (kind or "").strip().lower()
    if kind not in {"word", "pdf", "picture"}:
        raise HTTPException(status_code=400, detail=pick("kind 必须是 word、pdf 或 picture / kind must be word, pdf or picture",
                       request_lang(request) if request else None))

    # The assembly itself is shared with the Calendar's event export — see
    # `knowledge_export.export_document()`.
    try:
        data, media_type, extension = await knowledge_export.export_document(
            kind, payload.html or "", title, meta=meta, base_href=base_href,
            auth_headers=knowledge_export.auth_headers(request))
    except knowledge_export.ExportError as exc:
        raise HTTPException(status_code=422, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    filename = f"{slug}.{extension}"
    return Response(content=data, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store",
    })


@router.get("/items/{slug}/markdown")
async def download_markdown(slug: str, request: Request):
    """The page's own source — what the wiki is actually stored as."""
    email, _ = _identity(request)
    item = store.get_item(slug, email)
    if not item:
        raise _not_found(request_lang(request) if request else None)
    body = item.body or ""
    return Response(content=body.encode("utf-8"), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.md"',
                             "Cache-Control": "private, no-store"})


# ── the owner's controls (browser session only, like the report equivalents) ──

@router.post("/items/{slug}/publish")
async def publish_item(slug: str, request: Request):
    """Publish a read-only snapshot to the public area. The private page stays."""
    try:
        email = _require_browser(request)
        # ⚠️ Had no gate. Checked before the store call so a refused publication writes
        # no snapshot and emits no Inbox notification.
        allowed, reason = orgs.may_publish(email, orgs.CHANNEL_PUBLIC,
                                           orgs.TARGET_KNOWLEDGE, slug)
        if not allowed:
            status, message = orgs.denial(reason)
            raise HTTPException(status_code=status,
                                detail=pick(message, request_lang(request) if request else None))
        item = store.publish_to_public(slug, email)
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    inbox.emit(kind="publish", actor_email=email, recipient_email=None,
               target_type="knowledge", target_slug=item.slug, target_title=item.title,
               target_url=annotations.doc_url("knowledge", item.slug),
               summary=f"把知识库页面《{item.title}》发布到了公共区")
    return _shape(item, email, placement=_resolved(item, _placement(email)))


@router.delete("/items/{slug}/public")
async def withdraw_item(slug: str, request: Request):
    try:
        email = _require_browser(request)
        withdrawn = store.withdraw_public(slug, email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    if not withdrawn:
        raise _not_found(request_lang(request) if request else None)
    return {"withdrawn": slug}


@router.post("/items/{slug}/pull", status_code=201)
async def pull_item(slug: str, request: Request):
    """Copy a public or colleague-shared page into my own knowledge base."""
    try:
        email = _require_browser(request)
        item = store.pull_copy(slug, email)
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return _shape(item, email, include_body=True, placement=_resolved(item, _placement(email)))


@router.post("/items/{slug}/shares")
async def share_item(slug: str, request: Request, payload: ShareIn):
    try:
        email = _require_browser(request)
        # ⚠️ Had no gate. Gated here rather than inside `store.share_with()` because the
        # org question is about the *owner*, and the store layer is also reached by the
        # agent API, which must not be able to route around a company policy.
        allowed, reason, _offenders = orgs.may_share_to_many(email, payload.emails)
        if not allowed:
            status, message = orgs.denial(reason)
            raise HTTPException(status_code=status,
                                detail=pick(message, request_lang(request) if request else None))
        shared = store.share_with(slug, email, payload.emails, email)
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    title = store.title_of(slug)
    for recipient in shared:
        inbox.emit(kind="share", actor_email=email, recipient_email=recipient,
                   target_type="knowledge", target_slug=slug, target_title=title,
                   target_url=annotations.doc_url("knowledge", slug),
                   summary=f"把知识库页面《{title or slug}》分享给了你")
    return {"slug": slug, "shared_emails": shared}


@router.delete("/items/{slug}/shares")
async def unshare_item(slug: str, request: Request, email: str = Query(...)):
    try:
        owner = _require_browser(request)
        shared = store.unshare(slug, owner, email)
    except PermissionError as exc:
        raise _not_found(request_lang(request) if request else None) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return {"slug": slug, "shared_emails": shared}

# ── projects and folders (2026-10-03) ────────────────────────────────────────
#
# 项目 (a card) → 文件夹 → 页面, with a 未归档 container at each level. Creating a
# project or a folder is an ordinary write on the account's own content, so these
# are open to an agent access code like `POST /items` is — that is the agent's
# entry point for "make me a folder called 渠道口径, with a cover". Moving a page,
# renaming and deleting are the same. Only the two share endpoints below are
# browser-only, exactly like a page's.


class ProjectIn(BaseModel):
    """A project, and the cover a card wants.

    The cover fields are the Workspace's own names and spellings, so
    `services/report_cover.py::resolve_cover()` takes this object unchanged — the
    same reuse the Calendar documents at `routers/calendar.py::CoverIn`. An agent
    that has learned `POST /api/reports/cover` can send the same body here.
    """

    title: str = ""
    summary: str = ""
    generate_cover: bool = Field(default=False,
                                 validation_alias=AliasChoices(*COVER_ALIASES["generate_cover"]))
    cover_base64: str = Field(default="",
                              validation_alias=AliasChoices(*COVER_ALIASES["cover_base64"]))
    cover_url: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_url"]))
    cover_prompt: str = Field(default="",
                              validation_alias=AliasChoices(*COVER_ALIASES["cover_prompt"]))
    cover_ratio: str = Field(default=report_cover.DEFAULT_RATIO,
                             validation_alias=AliasChoices(*COVER_ALIASES["cover_ratio"], "ratio"))
    cover_seed: Optional[int] = Field(default=None,
                                      validation_alias=AliasChoices(*COVER_ALIASES["cover_seed"], "seed"))
    cover_extra: str = Field(default="",
                             validation_alias=AliasChoices(*COVER_ALIASES["cover_extra"], "extra"))
    cover_with_title: bool = Field(default=False,
                                   validation_alias=AliasChoices(*COVER_ALIASES["cover_with_title"],
                                                                 "with_title"))


class ProjectPatch(BaseModel):
    title: str = ""
    summary: str = ""


class FolderIn(BaseModel):
    title: str = ""


class MoveIn(BaseModel):
    """Where to file a page.

    ⚠️ **An empty ``project_slug`` means 未归档** — that is how a page is moved back
    out of a project, and it is the same spelling the stored NULL resolves to. The
    alternative (a separate `unfiled: true` flag) would let a body say both at once.
    """

    project_slug: str = ""
    folder_id: Optional[int] = None


def _project_not_found(lang: str | None = None) -> HTTPException:
    return HTTPException(status_code=404,
                         detail=pick("知识项目不存在 / knowledge project not found", lang))


def _folder_not_found(lang: str | None = None) -> HTTPException:
    return HTTPException(status_code=404,
                         detail=pick("知识文件夹不存在 / knowledge folder not found", lang))


def _cover_from_body(body, title: str, request: Request | None = None):
    """Resolve a submitted cover; 400 for bad input, 502 when the provider refuses.

    The decision itself lives in `report_cover.resolve_cover`, shared with the
    reports, the dashboards and the calendar; only the error mapping is ours.
    """
    try:
        return report_cover.resolve_cover(body, title)
    except ValueError as exc:
        raise HTTPException(status_code=400,
                            detail=pick(str(exc), request_lang(request) if request else None)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502,
                            detail=pick(f"封面生成失败 / could not generate the cover: {exc}",
                                        request_lang(request) if request else None)) from exc


@router.get("/projects")
async def list_projects(request: Request):
    """The project card wall: mine, plus any project holding a folder shared with me.

    The 未归档 project is always present and always first — it is where a page that
    was never filed is shown, so a wall without it would hide those pages.
    """
    try:
        email, _ = _identity(request)
        projects = filing.list_projects(email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503,
                            detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return {"projects": [_shape_project(project, email) for project in projects],
            "count": len(projects)}


@router.post("/projects", status_code=201)
async def create_project(request: Request, payload: ProjectIn):
    """Create a project, its 未归档 folder, and — when asked — a cover from its name.

    **Open to an agent access code.** "Create a folder called X with a cover that
    looks like X" is a write on this account's own content, the same category as
    `POST /items`.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        cover = _cover_from_body(payload, payload.title, request)
        project = filing.create_project(email, payload.title,
                                        summary=payload.summary, cover=cover)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape_project(project, email)


@router.get("/projects/{slug}")
async def get_project(slug: str, request: Request):
    email, _ = _identity(request)
    try:
        project = filing.get_project(slug, email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request))) from exc
    if not project:
        raise _project_not_found(request_lang(request) if request else None)
    return _shape_project(project, email)


@router.put("/projects/{slug}")
async def update_project(slug: str, request: Request, payload: ProjectPatch):
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        project = filing.update_project(slug, email, title=payload.title, summary=payload.summary)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape_project(project, email)


@router.delete("/projects/{slug}")
async def delete_project(slug: str, request: Request):
    """Delete a project. Its pages fall back to 未归档 — a container is a filing
    decision, the pages are the knowledge, and deleting a folder must never be a
    way to lose a page."""
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        moved = filing.delete_project(slug, email)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return {"deleted": slug, "moved_to_unfiled": moved, "count": len(moved)}


# ── folders ─────────────────────────────────────────────────────────────────

@router.get("/projects/{slug}/folders")
async def list_folders(slug: str, request: Request):
    """The folders in one project the caller may see.

    A reader who was given one folder sees that folder and not the owner's other
    ones — their titles are the owner's filing decisions.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        if not filing.get_project(slug, email):
            raise _project_not_found(lang)
        folders = filing.list_folders(slug, email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), lang)) from exc
    return {"folders": [_shape_folder(folder) for folder in folders], "count": len(folders)}


@router.post("/projects/{slug}/folders", status_code=201)
async def create_folder(slug: str, request: Request, payload: FolderIn):
    """Create a folder inside a project the caller owns. **Open to an agent.**
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        folder = filing.create_folder(slug, email, payload.title)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape_folder(folder)


@router.put("/folders/{folder_id}")
async def rename_folder(folder_id: int, request: Request, payload: FolderIn):
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        folder = filing.rename_folder(folder_id, email, payload.title)
    except PermissionError as exc:
        raise _folder_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape_folder(folder)


@router.delete("/folders/{folder_id}")
async def delete_folder(folder_id: int, request: Request):
    """Delete a folder; its pages fall back to 未归档, and its shares go with it."""
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        moved = filing.delete_folder(folder_id, email)
    except PermissionError as exc:
        raise _folder_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return {"deleted": folder_id, "moved_to_unfiled": moved, "count": len(moved)}


# ── moving a page ───────────────────────────────────────────────────────────

@router.post("/items/{slug}/move")
async def move_item(slug: str, request: Request, payload: MoveIn):
    """File a page under a project + folder, or back into 未归档.

    Ownership is checked on both the page and the target folder, server-side — a
    menu that filtered the target list in the browser would still be a request the
    browser can be made to send.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        item = store.move_item(slug, email, payload.project_slug, payload.folder_id)
    except PermissionError as exc:
        raise _not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape(item, email, include_body=False, placement=_resolved(item, _placement(email)))


# ── folder sharing: one row, every page in the folder ───────────────────────

@router.post("/folders/{folder_id}/shares")
async def share_folder(folder_id: int, request: Request, payload: ShareIn):
    """Share a whole folder — and therefore every page filed in it.

    The grant is **one row on the folder**, not one row per page. That is what makes
    the feature honest: a page filed into the folder tomorrow is shared tomorrow
    with no second write, and revoking is a single delete rather than a recomputation
    over whatever the folder happened to hold today. The read side is
    `business_knowledge.READABLE_PREDICATE`.

    Browser session only, and behind the same company gate as a page share: sharing
    a folder grants strictly more than sharing one of its pages.
    """
    lang = request_lang(request) if request else None
    try:
        owner = _require_browser(request)
        # ⚠️ Gated before the store call, so a refused share writes no row and emits
        # no Inbox notification — the same ordering as `share_item`.
        allowed, reason, _offenders = orgs.may_share_to_many(owner, payload.emails)
        if not allowed:
            status, message = orgs.denial(reason)
            raise HTTPException(status_code=status, detail=pick(message, lang))
        shared = filing.share_folder(folder_id, owner, payload.emails, owner)
    except PermissionError as exc:
        raise _folder_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    title = filing.folder_title(folder_id)
    project = filing.folder_project(folder_id)
    for recipient in shared:
        inbox.emit(kind="share", actor_email=owner, recipient_email=recipient,
                   target_type="knowledge", target_slug=project, target_title=title,
                   # A folder has no page to open, so the link goes to the project the
                   # reader lands in. `appAbsUrl()` is the browser's helper; here the
                   # mount point has to come from the server, like `annotations.doc_url`.
                   target_url=f"{_base_href()}?kbproj={quote(project, safe='')}",
                   summary=f"把知识库文件夹《{title or project}》分享给了你")
    return {"folder_id": folder_id, "project_slug": project, "shared_emails": shared}


@router.delete("/folders/{folder_id}/shares")
async def unshare_folder(folder_id: int, request: Request, email: str = Query(...)):
    lang = request_lang(request) if request else None
    try:
        owner = _require_browser(request)
        shared = filing.unshare_folder(folder_id, owner, email)
    except PermissionError as exc:
        raise _folder_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return {"folder_id": folder_id, "shared_emails": shared}


# ── the project cover, reusing the Workspace mechanism ──────────────────────

@router.get("/projects/{slug}/cover")
async def project_cover(slug: str, request: Request):
    """The project's cover: served bytes, a 302 to an external URL, or 404.

    The same contract as `GET /api/reports/{slug}/cover`, so the card wall shares
    the client-side `<img>` logic and the `?v=<updated_at>` cache-buster.
    """
    from fastapi.responses import RedirectResponse

    email, _ = _identity(request)
    try:
        data, mime, external = filing.cover_bytes(slug, email)
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request))) from exc
    if data:
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "private, max-age=86400"})
    if external:
        return RedirectResponse(external, status_code=302)
    raise HTTPException(status_code=404,
                        detail=pick("这个项目还没有封面 / this project has no cover",
                                    request_lang(request) if request else None))


@router.post("/projects/{slug}/cover")
async def set_project_cover(slug: str, request: Request, payload: ProjectIn):
    """Generate a project's cover from its name — or store one the owner supplied.

    A dedicated endpoint rather than part of `PUT /projects/{slug}`: the project
    PUT is a whole-row overwrite, so a cover-only save would blank the title.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        project = filing.get_project(slug, email)
        if not project or not project.owner_email == email:
            raise _project_not_found(lang)
        cover = _cover_from_body(payload, payload.title or project.title, request)
        project = filing.set_cover(slug, email, cover)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape_project(project, email)


@router.delete("/projects/{slug}/cover")
async def clear_project_cover(slug: str, request: Request):
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        project = filing.set_cover(slug, email, None, clear=True)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except BusinessKnowledgeError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), lang)) from exc
    return _shape_project(project, email)
