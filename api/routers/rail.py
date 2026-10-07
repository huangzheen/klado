"""The left rail's HTTP surface: folders, filed items, and the short module labels.

The persistence is `services/rail_store.py`; this file is the thin layer that turns
its exceptions into status codes and the caller's identity into an owner.

⚠️ **This router's prefix is registered on the `core` module, not on a feature one.**
The middleware answers 403 for a path no module claims, so an unregistered
`/api/rail/*` makes the entire rail unreachable in the running app while every unit
test still passes — the suites mount a router on a bare FastAPI app, without the
middleware that would have refused it. `core` is the right owner: the rail is chrome
that spans every module, it has no page of its own, and `core` is `required`, so it
can never be switched off and strand the navigation.
"""
from __future__ import annotations

import functools

import psycopg2
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core import identity
from core.i18n import pick, request_lang
from services import rail_store as store

router = APIRouter()


def _lang(request: Request | None):
    return request_lang(request) if request else None


def _fail(exc: store.FolderError, request: Request | None = None) -> HTTPException:
    """Map a store error to a status. 404 for "not yours" as well as "missing": the
    code must never confirm that a folder exists to an account that cannot see it."""
    status = 404 if isinstance(exc, store.FolderNotFound) else 400
    return HTTPException(status_code=status, detail=pick(str(exc), _lang(request)))


def _owner(request: Request) -> str:
    email, _kind = identity.current_identity(request)
    if not email:
        raise HTTPException(status_code=401, detail=pick(
            "请登录后使用左侧栏 / sign in to use the left rail", _lang(request)))
    return email


def _with_schema(fn):
    """Create the tables on first use and retry once when one is missing.

    Same escape hatch as `routers/dashboard.py`: `ensure_schema()` is memoised per
    process, and without the retry one DROP leaves every rail endpoint answering 500
    until the server restarts."""
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        store.ensure_schema()
        try:
            return await fn(*args, **kwargs)
        except psycopg2.errors.UndefinedTable:
            store.reset_schema_cache()
            store.ensure_schema()
            return await fn(*args, **kwargs)

    return guarded


# ── payloads ─────────────────────────────────────────────────────────────────

class FolderIn(BaseModel):
    name: str = ""
    parent_id: int | None = None
    position: int = 0


class FolderMove(BaseModel):
    parent_id: int | None = None
    position: int = 0


class ItemIn(BaseModel):
    item_type: str = ""
    item_slug: str = ""
    position: int = 0


class MoveIn(BaseModel):
    folder_id: int


class LabelsIn(BaseModel):
    labels: dict = {}


# ── folders ──────────────────────────────────────────────────────────────────

@router.get("/folders")
@_with_schema
async def list_folders(request: Request):
    """Every folder this account owns, flat. The rail builds the tree from
    `parent_id` — one query beats one query per level."""
    return {"folders": store.list_folders(_owner(request))}


@router.post("/folders")
@_with_schema
async def create_folder(body: FolderIn, request: Request):
    try:
        return store.create_folder(_owner(request), body.name, body.parent_id, body.position)
    except store.FolderError as exc:
        raise _fail(exc, request) from exc


@router.put("/folders/{folder_id}")
@_with_schema
async def rename_folder(folder_id: int, body: FolderIn, request: Request):
    """Rename. The bytes are not touched, so this cannot lose a file — which is the
    whole reason a folder is a virtual tag and not a directory."""
    try:
        return store.rename_folder(_owner(request), folder_id, body.name)
    except store.FolderError as exc:
        raise _fail(exc, request) from exc


@router.delete("/folders/{folder_id}")
@_with_schema
async def delete_folder(folder_id: int, request: Request):
    try:
        store.delete_folder(_owner(request), folder_id)
    except store.FolderError as exc:
        raise _fail(exc, request) from exc
    return {"ok": True}


# ── items ────────────────────────────────────────────────────────────────────

@router.get("/items")
@_with_schema
async def list_items(request: Request):
    """Every filed object, with the live title behind it.

    ⚠️ `missing: true` is a first-class answer, not an error: a filed slug whose
    document was deleted stays in its folder (the user may want to see that it is
    gone) but must never render as a link to nothing."""
    return {"items": store.list_items(_owner(request))}


@router.post("/folders/{folder_id}/items")
@_with_schema
async def add_item(folder_id: int, body: ItemIn, request: Request):
    try:
        return store.add_item(_owner(request), folder_id, body.item_type,
                              body.item_slug, body.position)
    except store.FolderError as exc:
        raise _fail(exc, request) from exc


@router.put("/items/{item_id}/move")
@_with_schema
async def move_item(item_id: int, body: MoveIn, request: Request):
    try:
        return store.move_item(_owner(request), item_id, body.folder_id)
    except store.FolderError as exc:
        raise _fail(exc, request) from exc


@router.delete("/items/{item_id}")
@_with_schema
async def remove_item(item_id: int, request: Request):
    try:
        store.remove_item(_owner(request), item_id)
    except store.FolderError as exc:
        raise _fail(exc, request) from exc
    return {"ok": True}


# ── module short labels ──────────────────────────────────────────────────────

@router.get("/module-labels")
@_with_schema
async def list_module_labels(request: Request):
    """This account's short labels. A module with no row is ABSENT from the map —
    the rail falls back to the registry label, because absence means "never renamed"
    rather than "renamed to nothing"."""
    return {"labels": store.list_module_labels(_owner(request))}


@router.put("/module-labels")
@_with_schema
async def set_module_labels(body: LabelsIn, request: Request):
    try:
        return {"labels": store.set_module_labels(_owner(request), body.labels)}
    except store.FolderError as exc:
        raise _fail(exc, request) from exc
