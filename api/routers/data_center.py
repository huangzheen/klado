"""Data Center Router — /api/data-center
数据导入、数据集管理、SQL 查询
对接唯一的 Klado PostgreSQL（klado.public）
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import time
import traceback
from urllib.parse import quote
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
)
from fastapi.responses import Response
from pydantic import AliasChoices, BaseModel, Field
from psycopg2 import errors as pg_errors
from psycopg2 import sql

from processors.excel import dataframe_to_records, parse_excel, preview_excel
from processors.column_types import infer_dataframe
from processors.transforms import apply_transform
from processors.cleaner import apply_cleaning_rules, df_to_preview
import re as _re

from core import identity
from core.i18n import pick, request_lang
from klado_shared import orgs
from services import data_center_db as db
from services import dataset_groups
from services import dataset_query as dq
from services import dataset_shares as shares
from services import external_sync
from services import file_shares
from services import pulls
from routers.reports import _require_browser


def _viewer(request: Request) -> tuple[str, bool]:
    """`(email, is_admin)` of the caller — the key Data Center scopes its rows by.

    Every read filters on it and every write stamps it; a dataset/file is visible to
    its owner plus operators (and to everyone when the row predates ownership — see
    `services.data_center_db`).
    """
    email, _kind = identity.current_identity(request)
    return email, False  # operators use the same personal content boundary


def _deny_unless_readable(request: Request, object_name: str, viewer_email: str, admin: bool) -> None:
    """404 when the caller may not read this OSS object (see `db.may_read_object`)."""
    if not db.may_read_object(object_name, viewer_email, admin):
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))


def _assert_dataset_writable(request: Request, table_name: str, viewer_email: str, admin: bool) -> None:
    """Refuse to import into a dataset that belongs to somebody else.

    Without this, a replace-mode import would DROP and recreate — or TRUNCATE —
    another account's table just by naming it. Ownerless rows are not writable
    until the configured legacy owner has adopted them.
    """
    table_name = db._safe_id(table_name)
    row = db.get_dataset(table_name)          # unfiltered: ask who owns it
    if row is None:
        with db.get_pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", ("public." + table_name,))
            if cur.fetchone()[0] is not None:
                raise HTTPException(409, pick("目标名称已被系统表占用 / The target name is reserved by an existing system table", request_lang(request)))
        return
    owner = row.get("owner_email") or ""
    if not owner or owner != viewer_email:
        raise HTTPException(409, pick(f"数据集 {table_name} 属于其他用户，无法导入 / dataset {table_name} belongs to another user; import refused", request_lang(request) if request else None))


def _infer_cleaned_frame(df, rules):
    forced = {str(r.get("rename") or r.get("original")): r.get("dtype")
              for r in (rules or {}).get("columns", [])}
    return infer_dataframe(df, forced)


def _assert_file_owner(file_id: int, request: Request) -> str:
    email = _viewer(request)[0]
    row = db.get_file(file_id, viewer_email=email)
    if not row or row.get("owner_email") != email:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request)))
    return email


def _assert_object_owner(path: str, request: Request) -> str:
    email = _viewer(request)[0]
    row = db._get_library_row_by_object(path)
    if not row or row.get("owner_email") != email:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request)))
    return email


def _build_download_headers(filename: str) -> dict[str, str]:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    ascii_name = ascii_name.replace('"', "")
    encoded_name = quote(filename)
    return {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
    }

router = APIRouter()
logger = logging.getLogger("klado.smart_import")
logger.setLevel(logging.INFO)


def _qi_slug(filename: str) -> str:
    """Mirror frontend _qiSlug: strip timestamp prefix, extension, normalise."""
    s = _re.sub(r"^\d{8}_\d{6}_", "", filename or "")
    s = _re.sub(r"\.[^.]+$", "", s)
    s = _re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s


def _display_filename(path_or_name: str) -> str:
    """Basename with the OSS upload prefix (YYYYMMDD_HHMMSS_) stripped.

    save_source_file stores objects as `<YYYY-MM>/<ts>_<origname>`; cleaning
    rules that derive columns from the file name must see the original name.
    """
    name = (path_or_name or "").rsplit("/", 1)[-1]
    return _re.sub(r"^\d{8}_\d{6}_", "", name)


# ─── 启动初始化 ───────────────────────────────────────────────────────────────
# 在应用 lifespan 中调用，或首次请求时懒初始化

_initialized = False

def _ensure_init():
    global _initialized
    if not _initialized:
        db.init_registry()
        db.init_file_library()
        file_shares.ensure_schema()
        db.init_qi_mappings()
        # The container layer. Same reasoning as the calls above: `/datasets`
        # reads it on every list, so leaving it to "first call creates it" would
        # turn a DDL hiccup into a 500 on the main data-center screen.
        dataset_groups.ensure_schema()
        _initialized = True


# ─── 常量 ──────────────────────────────────────────────────────────────────────

ALLOWED_EXTS = {".xlsx", ".xls", ".xlsm", ".csv"}
TARGETS = {"pg"}
TARGET_LABELS = {"pg": "klado.public"}




# ─── 文件库 ───────────────────────────────────────────────────────────────────

@router.post("/files/stage")
async def stage_file(request: Request, file: UploadFile = File(...), folder: str = Form("")):
    """上传任意文件到 OSS 文件库。Excel/CSV 额外解析 sheets_meta。folder 指定目标目录。"""
    _ensure_init()
    if not file.filename:
        raise HTTPException(400, pick("文件名为空 / file name is empty", request_lang(request) if request else None))
    folder = _safe_folder(request, folder)
    email, _admin = _viewer(request)
    data = await file.read()
    ext = Path(file.filename).suffix.lower()
    sheets_meta = []
    if ext in ALLOWED_EXTS:
        try:
            sheets = parse_excel(data, file.filename)
            sheets_meta = [
                {
                    "sheet_name": s["sheet_name"],
                    "suggested_table_name": s["suggested_table_name"],
                    "row_count": s["row_count"],
                    "columns": s["columns"],
                    "type_hints": s.get("type_hints", []),
                    "fingerprint": s["fingerprint"],
                }
                for s in sheets
            ]
        except Exception:
            pass
    try:
        record = db.stage_file(file.filename, data, sheets_meta, folder=folder, owner_email=email)
    except Exception as e:
        raise HTTPException(500, pick(f"文件存储失败: {e} / failed to store the file: {e}", request_lang(request) if request else None))
    return record


class RegisterOssReq(BaseModel):
    path: str

@router.post("/files/register-oss")
async def register_oss_file(req: RegisterOssReq, request: Request):
    """将 OSS 上已有文件注册到文件库（仅建 DB 记录，不重传文件）。"""
    _ensure_init()
    email, admin = _viewer(request)
    from services import oss_storage
    # 别人的文件不能按路径注册进来（注册会在没有库行时新建一行，并把它归到自己名下）
    _assert_object_owner(req.path, request)
    try:
        data = oss_storage.get_object("datacenter-raw", req.path)
    except Exception as e:
        raise HTTPException(404, pick(f"OSS 文件不存在: {e} / file not found on OSS: {e}", request_lang(request) if request else None))
    sheets_meta = []
    ext = Path(req.path).suffix.lower()
    if ext in ALLOWED_EXTS:
        try:
            sheets = parse_excel(data, req.path)
            sheets_meta = [
                {
                    "sheet_name": s["sheet_name"],
                    "suggested_table_name": s["suggested_table_name"],
                    "row_count": s["row_count"],
                    "columns": s["columns"],
                    "type_hints": s.get("type_hints", []),
                    "fingerprint": s["fingerprint"],
                }
                for s in sheets
            ]
        except Exception:
            pass
    return db.register_oss_file(req.path, sheets_meta, len(data), owner_email=email)



@router.get("/files/browse")
def browse_files(request: Request, prefix: str = ""):
    """列出 OSS 指定目录下的文件夹和文件（只列当前用户可见的）。"""
    _ensure_init()
    email, admin = _viewer(request)
    return db.browse_files(prefix, viewer_email=email, admin=admin)


@router.get("/files/tree")
def files_tree(request: Request):
    """递归列出所有对象，供前端构建完整文件树（只列当前用户可见的）。"""
    _ensure_init()
    email, admin = _viewer(request)
    try:
        return db.list_all_objects(viewer_email=email, admin=admin)
    except Exception as exc:
        return {"folders": [], "files": [],
                "error": pick(str(exc), request_lang(request) if request else None)}


@router.get("/files/orphans")
def list_orphan_files(request: Request):
    """自检：OSS 上有对象、但文件库里没有对应行的文件。

    这类文件曾无法生成 Publish Link（前端报「无法获取文件 ID」）。
    发布现在走 /files/publish-by-path 会按需登记，这里用于发现其它漏登记的写路径。

    ⚠️ 有意保留为**全局**视图：它比对的就是「OSS 全部对象 vs 文件库」，答案天然是全量的，
    按用户过滤会失去自检意义。风险已知：它会把对象路径名暴露给任意登录用户（对象本身无归属）。
    """
    _ensure_init()
    if not identity.is_admin_caller(request):
        raise HTTPException(403, pick("需要管理员身份 / admin required", request_lang(request)))
    return db.list_orphan_objects()


# ── Clean Preview ──────────────────────────────────────────────────────────────

class CleanPreviewRequest(BaseModel):
    path:       str
    sheet:      str | None = None
    skip_rows:  int = 0
    rules:      dict = {}


@router.post("/files/clean-preview")
def clean_preview(req: CleanPreviewRequest, request: Request):
    """解析文件并应用清洗规则，返回预览数据（前 100 行）。"""
    _ensure_init()
    email, admin = _viewer(request)
    _deny_unless_readable(request, req.path, email, admin)
    try:
        data, _ = db.get_source_file(req.path)
    except Exception as e:
        raise HTTPException(404, pick(f"文件读取失败: {e} / failed to read the file: {e}", request_lang(request) if request else None))

    filename = req.path.rsplit("/", 1)[-1]
    try:
        sheets = parse_excel(data, filename)
    except Exception as e:
        raise HTTPException(422, pick(f"解析失败: {e} / failed to parse the file: {e}", request_lang(request) if request else None))

    sheet_name = req.sheet or (sheets[0]["sheet_name"] if sheets else None)
    sheet = next((s for s in sheets if s["sheet_name"] == sheet_name), None)
    if not sheet:
        raise HTTPException(404, pick(f"Sheet '{sheet_name}' 不存在 / sheet '{sheet_name}' not found", request_lang(request) if request else None))

    df = sheet["df"].copy()

    # Apply rules if provided, otherwise return raw
    rules = dict(req.rules)
    if rules:
        try:
            df = apply_cleaning_rules(df, rules, source_filename=_display_filename(req.path))
        except Exception as e:
            raise HTTPException(422, pick(f"清洗规则错误: {e} / cleaning rule error: {e}", request_lang(request) if request else None))

    return {
        "sheet_name":    sheet_name,
        "all_sheets":    [s["sheet_name"] for s in sheets],
        "type_hints": infer_dataframe(df)[2],
        **df_to_preview(df, limit=100),
    }


# ── Import with cleaning rules ────────────────────────────────────────────────

class ImportWithRulesRequest(BaseModel):
    file_id:       int
    sheet_name:    str | None = None
    table_name:    str
    target_db:     str = "pg"
    mode:          str = "replace"
    cleaning_rules: dict = {}


@router.post("/files/import-with-rules")
def import_with_rules(req: ImportWithRulesRequest, request: Request):
    """从文件库导入，应用清洗规则后写入数据库。"""
    _ensure_init()
    email, admin = _viewer(request)
    file_rec = db.get_file(req.file_id, viewer_email=email, admin=admin)
    if not file_rec:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))

    try:
        data, _ = db.get_source_file(file_rec["object_name"])
    except Exception as e:
        raise HTTPException(500, pick(f"文件读取失败: {e} / failed to read the file: {e}", request_lang(request) if request else None))

    try:
        sheets = parse_excel(
            data,
            file_rec["filename"],
            sheet_names=[req.sheet_name] if req.sheet_name else None,
        )
    except Exception as e:
        raise HTTPException(422, pick(f"解析失败: {e} / failed to parse the file: {e}", request_lang(request) if request else None))

    sheet_name = req.sheet_name or (sheets[0]["sheet_name"] if sheets else None)
    sheet = next((s for s in sheets if s["sheet_name"] == sheet_name), None)
    if not sheet:
        raise HTTPException(404, pick(f"Sheet '{sheet_name}' 不存在 / sheet '{sheet_name}' not found", request_lang(request) if request else None))

    df = sheet["df"].copy()

    # Apply cleaning rules
    if req.cleaning_rules:
        try:
            df = apply_cleaning_rules(
                df, req.cleaning_rules,
                source_filename=_display_filename(file_rec["filename"]),
            )
        except Exception as e:
            raise HTTPException(422, pick(f"清洗规则错误: {e} / cleaning rule error: {e}", request_lang(request) if request else None))

    if df.empty:
        raise HTTPException(422, pick("清洗后数据为空，请检查清洗规则 / the cleaned data is empty — check the cleaning rules", request_lang(request) if request else None))

    df, columns, type_hints = _infer_cleaned_frame(df, req.cleaning_rules)
    records = dataframe_to_records(df, columns)

    table_name = db._safe_id(req.table_name.strip())

    # 归属护栏：replace 模式会 DROP 再建，命名别人的表就会置换掉对方的数据
    _assert_dataset_writable(request, table_name, email, admin)

    try:
        # Keep the existing table until schema and records have passed validation.
        db.create_pg_table(
            table_name=table_name,
            columns=columns,
            fingerprint="",
            display_name=table_name,
            source_file=file_rec["filename"],
            sheet_name=sheet_name or "",
            modules=[],
            source_file_path=file_rec["object_name"],
            owner_email=email,
        )
        rows = db.insert_pg_data(table_name, records, req.mode, owner_email=email)
        logger.info(
            "Smart Import succeeded file=%s sheet=%s target=public.%s mode=%s rows=%s",
            file_rec["filename"], sheet_name, table_name, req.mode, rows,
        )
    except db.DatasetSchemaError as e:
        # A re-import that would narrow a column. 400, not 500: the two files disagree
        # about a type, which is a fact about the caller's request, and nothing was written.
        raise HTTPException(400, pick(
            f"数据集列类型冲突：{e} / dataset column type conflict: {e}",
            request_lang(request) if request else None))
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))

    # Persist cleaning_rules to _import_registry
    if req.cleaning_rules:
        db.update_cleaning_rules(table_name, req.cleaning_rules)

    # Auto-save QI mapping so Quick Import remembers the rules
    file_slug = _qi_slug(file_rec["filename"])
    if file_slug:
        db.save_qi_mappings(file_slug, [{
            "sheet_name": sheet_name or "",
            "table_name": table_name,
            "mode": req.mode,
            "cleaning_rules": req.cleaning_rules or None,
        }], owner_email=email)

    return {
        "table":      table_name,
        "file":       file_rec["filename"],
        "sheet":      sheet_name,
        "rows_written": rows,
        "cleaning_applied": bool(req.cleaning_rules),
    }


@router.get("/files")
def list_files(request: Request):
    """返回文件库列表（供 Update modal 使用）；只含当前用户可见的行。"""
    _ensure_init()
    email, admin = _viewer(request)
    files = db.list_files(viewer_email=email, admin=admin)
    for f in files:
        for s in (f.get("sheets_meta") or []):
            existing = db.find_by_fingerprint(s.get("fingerprint", ""), viewer_email=email, admin=admin)
            if not existing:
                existing = db.get_dataset(s.get("suggested_table_name", ""), viewer_email=email, admin=admin)
            s["existing_table"] = existing["table_name"] if existing else None
            s["existing_target"] = existing.get("target_db") if existing else None
    return files


@router.get("/files/content")
def file_content(path: str, request: Request):
    """从 MinIO 流式返回文件内容，供前端图片/PDF 预览。"""
    _ensure_init()
    email, admin = _viewer(request)
    _deny_unless_readable(request, path, email, admin)
    from fastapi.responses import StreamingResponse
    import mimetypes
    try:
        data, content_type = db.get_source_file(path)
    except Exception as e:
        raise HTTPException(404, pick(str(e), request_lang(request) if request else None))
    # 如果 get_source_file 猜不出类型，用 mimetypes 兜底
    if content_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(path)
        if guessed:
            content_type = guessed
    import io
    return StreamingResponse(io.BytesIO(data), media_type=content_type)


@router.get("/files/preview-data")
def file_preview_data(path: str, request: Request, sheet: str = "", limit: int = 200):
    """解析 Excel/CSV 文件，返回指定 sheet 的前 N 行供预览。"""
    _ensure_init()
    email, admin = _viewer(request)
    _deny_unless_readable(request, path, email, admin)
    try:
        data, _ = db.get_source_file(path)
    except Exception as e:
        raise HTTPException(404, pick(str(e), request_lang(request) if request else None))
    filename = path.rsplit("/", 1)[-1]
    # 去掉时间戳前缀
    if len(filename) > 16 and filename[8] == "_" and filename[15] == "_":
        filename = filename[16:]
    try:
        # Fast preview path: only the requested sheet, capped rows/columns.
        # parse_excel() would parse every sheet (and infer a PG type per
        # column), which times out the ALB on large workbooks (504).
        return preview_excel(data, filename, sheet=sheet or None, limit=limit)
    except Exception as e:
        raise HTTPException(422, pick(str(e), request_lang(request) if request else None))


class MkdirRequest(BaseModel):
    path: str


@router.post("/files/mkdir")
def mkdir(req: MkdirRequest, request: Request):
    """在 MinIO 中创建文件夹。"""
    _ensure_init()
    if not req.path.strip():
        raise HTTPException(400, pick("路径不能为空 / path is empty", request_lang(request) if request else None))
    try:
        db.create_folder(req.path, owner_email=_viewer(request)[0])
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))
    return {"created": req.path}


@router.delete("/files/by-path")
def delete_by_path(path: str, request: Request, is_folder: bool = False):
    """按路径删除文件或文件夹（仅所有者可操作，见 core/access.py）。"""
    _ensure_init()
    email, admin = _viewer(request)
    if is_folder:
        if not db.may_manage_folder(path, email):
            raise HTTPException(404, pick("文件夹不存在 / folder not found", request_lang(request)))
    else:
        _assert_object_owner(path, request)
    try:
        if is_folder:
            db.delete_folder_by_path(path, owner_email=email)
        else:
            db.delete_file_by_path(path)
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))
    return {"deleted": path}


class MoveRequest(BaseModel):
    from_path: str
    to_prefix:  str


class RenameRequest(BaseModel):
    from_path: str
    new_name:  str


@router.post("/files/rename")
def rename_file(req: RenameRequest, request: Request):
    """在同一目录下重命名文件。"""
    _ensure_init()
    if not req.new_name.strip() or "/" in req.new_name or "\\" in req.new_name:
        raise HTTPException(400, pick("文件名不合法 / invalid file name", request_lang(request) if request else None))
    email, admin = _viewer(request)
    _assert_object_owner(req.from_path, request)
    try:
        new_path = db.rename_file(req.from_path, req.new_name.strip())
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))
    return {"renamed_to": new_path}


@router.post("/files/move")
def move_file(req: MoveRequest, request: Request):
    """移动文件到指定目录。"""
    _ensure_init()
    email, admin = _viewer(request)
    _assert_object_owner(req.from_path, request)
    try:
        new_path = db.move_file(req.from_path, req.to_prefix)
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))
    return {"moved_to": new_path}


@router.delete("/files/{file_id}")
def delete_file(file_id: int, request: Request):
    """按 file_id 从文件库和 MinIO 删除文件（仅所有者可操作）。"""
    _ensure_init()
    email = _assert_file_owner(file_id, request)
    admin = False
    record = db.get_file(file_id, viewer_email=email, admin=admin)
    if not record:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))
    db.delete_file(file_id)
    return {"deleted": file_id}


class PublishByPathReq(BaseModel):
    path: str


def _gate_file_token(email: str, target_id: str, request: Request) -> None:
    """Refuse minting a file's bearer token when the company may not publish outward.

    ⚠️ This is a share path and it was the tenth one found by the inventory, not by
    design. `db.publish_file` writes a `share_token` — the same kind of capability as a
    report's anyone-link: the token IS the key, and `data_center_db.get_file_by_token`
    looks a file up by it.

    ⚠️ As of this writing that lookup has no caller, so the token is minted and never
    served. The gate is here anyway rather than "when somebody wires the endpoint up":
    a credential that exists with no reader is still a credential, and the endpoint that
    eventually reads it would otherwise be the first ungated share path in the product.
    """
    allowed, reason = orgs.may_publish(email, orgs.CHANNEL_FILE_TOKEN,
                                       orgs.TARGET_FILE, str(target_id))
    if not allowed:
        status, message = orgs.denial(reason)
        raise HTTPException(status_code=status,
                            detail=pick(message, request_lang(request) if request else None))


@router.post("/files/publish-by-path")
def publish_file_by_path(req: PublishByPathReq, request: Request):
    """按路径生成公开分享链接（幂等）。

    文件库里没有对应行时按需登记 —— 不再依赖上传时是否登记成功，
    因此 OSS 上早已存在的文件（历史孤儿、Smart Import 源文件、快照 zip）也能直接发布。
    """
    _ensure_init()
    email, admin = _viewer(request)
    _assert_object_owner(req.path, request)
    _gate_file_token(email, req.path, request)
    try:
        return db.publish_file_by_path(req.path, owner_email=email)
    except FileNotFoundError:
        raise HTTPException(404, pick("OSS 上找不到该文件 / file not found on OSS", request_lang(request) if request else None))
    except Exception as e:
        raise HTTPException(500, pick(f"发布失败: {e} / publish failed: {e}", request_lang(request) if request else None))


@router.post("/files/{file_id}/publish")
def publish_file(file_id: int, request: Request):
    """生成文件的公开分享链接 token（幂等，多次调用返回同一 token）。"""
    _ensure_init()
    email = _assert_file_owner(file_id, request)
    admin = False
    if not db.get_file(file_id, viewer_email=email, admin=admin):
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))
    _gate_file_token(email, str(file_id), request)
    try:
        token = db.publish_file(file_id)
    except ValueError:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))
    return {"token": token}


@router.get("/share/{token}")
def download_shared_file(token: str, request: Request):
    """通过分享 token 下载文件，无需登录。"""
    from fastapi.responses import StreamingResponse
    import mimetypes, os as _os
    from services import oss_storage

    record = db.get_file_by_token(token)
    if not record:
        raise HTTPException(404, pick("链接无效或已失效 / this link is invalid or has expired", request_lang(request) if request else None))

    object_name = record["object_name"]
    # object_name 形如 "some/path/file.xlsx"，OSS bucket 为 datacenter-raw
    try:
        stream, content_type = oss_storage.get_object_stream("datacenter-raw", object_name)
    except Exception:
        raise HTTPException(502, pick("文件存储错误 / object storage error", request_lang(request) if request else None))

    filename = record["filename"]
    if content_type in ("application/octet-stream", "binary/octet-stream", ""):
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            content_type = guessed

    def _iter():
        for chunk in stream:
            yield chunk

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers=_build_download_headers(filename),
    )


# ─── Step 1: 分析（不写库）─────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_excel(request: Request, file: UploadFile = File(...)):
    """
    解析上传的 Excel / CSV，返回每个 sheet 的导入计划（不写入数据库）。
    客户端拿到结果后让用户确认表名、目标数据库、导入模式，再调 /upload。
    """
    _ensure_init()
    _validate_file(request, file.filename)
    email, admin = _viewer(request)
    data = await file.read()
    try:
        sheets = parse_excel(data, file.filename)
    except Exception as e:
        raise HTTPException(422, pick(f"解析失败: {e} / failed to parse the file: {e}", request_lang(request) if request else None))

    plan = []
    for s in sheets:
        existing = db.find_by_fingerprint(s["fingerprint"], viewer_email=email, admin=admin)
        plan.append({
            "sheet_name":          s["sheet_name"],
            "suggested_table_name": s["suggested_table_name"],
            "fingerprint":         s["fingerprint"],
            "row_count":           s["row_count"],
            "columns":             s["columns"],
            "existing_table":      existing["table_name"] if existing else None,
            "existing_target":     existing.get("target_db") if existing else None,
            "action":              "update" if existing else "create",
        })
    return {"file": file.filename, "plan": plan}


# ─── Step 2: 执行导入 ──────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_excel(
    request: Request,
    file: UploadFile = File(...),
    overrides: str = Form("{}"),
    # overrides JSON 格式：
    # {
    #   "Sheet1": {
    #     "table_name": "my_table",
    #     "target_db": "pg",  # legacy field; all tables live in public
    #     "mode": "replace" | "append" | "skip",
    #     "modules": ["sell-through", ...]
    #   }
    # }
):
    """
    执行 Excel 导入。
    所有目标表均写入 klado.public；旧 target_db 参数只为兼容客户端。
    """
    _ensure_init()
    _validate_file(request, file.filename)
    email, admin = _viewer(request)

    try:
        overrides_map: dict = json.loads(overrides)
    except Exception:
        overrides_map = {}

    data = await file.read()

    # ── 保存原始文件到 MinIO ──────────────────────────────────────────────────
    raw_path: str | None = None
    try:
        raw_path = db.save_source_file(file.filename, data)
    except Exception:
        logger.warning("Smart Import source upload to OSS failed", exc_info=True)

    try:
        sheets = parse_excel(data, file.filename)
    except Exception as e:
        raise HTTPException(422, pick(f"解析失败: {e} / failed to parse the file: {e}", request_lang(request) if request else None))

    # 源文件也要登记归属：它写进 OSS 时还不带所有者，若没有库行，文件树就会对所有人
    # 显示它（对象本身没有归属，归属只在库行上）。登记失败不影响导入本身。
    if raw_path:
        try:
            db.register_oss_file(
                raw_path,
                [
                    {
                        "sheet_name": s["sheet_name"],
                        "suggested_table_name": s["suggested_table_name"],
                        "row_count": s["row_count"],
                        "columns": s["columns"],
                    "type_hints": s.get("type_hints", []),
                        "fingerprint": s["fingerprint"],
                    }
                    for s in sheets
                ],
                len(data),
                owner_email=email,
            )
        except Exception:
            logger.warning("Smart Import source file registration failed", exc_info=True)

    results = []
    for s in sheets:
        ovr        = overrides_map.get(s["sheet_name"], {})
        mode       = ovr.get("mode", "replace")
        target     = "pg"  # legacy target_db input is accepted but ignored
        table_name = ovr.get("table_name", s["suggested_table_name"])
        modules    = ovr.get("modules", [])

        if mode == "skip":
            results.append({"sheet": s["sheet_name"], "table": "-", "action": "skipped", "rows": 0})
            continue

        if target not in TARGETS:
            target = "pg"

        try:
            explicit_table_name = "table_name" in ovr
            existing = db.get_dataset(table_name, viewer_email=email, admin=admin)
            # 没有显式表名时再按 fingerprint 查找旧导入，避免 CSV 分块 append 串表。
            if existing is None and not explicit_table_name:
                existing = db.find_by_fingerprint(s["fingerprint"], viewer_email=email, admin=admin)
            if existing is None:
                # 表名可能被别的账号占着（不可见 ≠ 不存在）——命名别人的表不能置换它
                _assert_dataset_writable(request, table_name, email, admin)

            # ── 应用表级清洗策略（列名归一、过滤合计行等，见 processors/transforms.py）──
            # Database CSV restores are already normalized exports, so keep them raw.
            if ovr.get("raw_csv"):
                s_df, s_cols = s["df"], s["columns"]
            else:
                s_df, s_cols = apply_transform(table_name, s["df"], s["columns"])
            records  = dataframe_to_records(s_df, s_cols)

            if existing and mode in ("replace", "append"):
                # ⚠️ This path does NOT go through `create_pg_table` (the table already
                # exists, so only the metadata is reused), which means the type
                # reconciliation has to be asked for explicitly. Without it a corrected
                # re-import lands in the OLD columns: a `pct` column first imported as
                # [-20, -22] (BIGINT) keeps its type and [-20.44, -22.31] is written as
                # [-20, -22] — and the response still says 200 / "updated".
                db.reconcile_column_types(existing["table_name"], s_cols, owner_email=email)

            actual_name = (
                existing["table_name"]
                if existing and mode in ("replace", "append")
                else db.create_pg_table(
                    table_name, s_cols, s["fingerprint"],
                    table_name, file.filename, s["sheet_name"], modules,
                    source_file_path=raw_path,
                    owner_email=email,
                )
            )
            if existing and mode in ("replace", "append"):
                db.update_modules(actual_name, modules)
                if raw_path:
                    db.update_source_file_path(actual_name, raw_path)
            rows = db.insert_pg_data(actual_name, records, mode if existing else "replace", owner_email=email)
            logger.info(
                "Smart Import succeeded file=%s sheet=%s target=public.%s mode=%s rows=%s",
                file.filename, s["sheet_name"], actual_name, mode, rows,
            )

            results.append({
                "sheet":   s["sheet_name"],
                "table":   actual_name,
                "target":  TARGET_LABELS[target],
                "action":  "updated" if existing else "created",
                "rows":    rows,
            })

        except Exception as e:
            results.append({
                "sheet":  s["sheet_name"],
                "table":  table_name,
                "target": TARGET_LABELS.get(target, target),
                "action": "error",
                "rows":   0,
                "error":  str(e),
                "detail": traceback.format_exc(limit=3),
            })

    return {"file": file.filename, "sheets": results}


# ─── 数据集列表 ────────────────────────────────────────────────────────────────

@router.get("/datasets")
def list_datasets(request: Request):
    """当前用户可见的数据集（自己的内容和指定授权）；只返回真实存在的表。

    ⚠️ 这仍然是**平铺的表列表** —— `dataset_query` 拿这里的 `table_name` 当 SQL 白名单，
    分享端点也是按表名收发的，所以这里不能改成「一个容器一条」。容器是加在上面的：
    每行多了 `dataset_slug` / `dataset_name`（无容器时为 `null`），前端据此把表归到
    它的数据集下面。`dataset_groups.attach_group_labels()` 原地打标签，不改任何既有字段。
    """
    _ensure_init()
    email, admin = _viewer(request)
    items = db.list_datasets(viewer_email=email, admin=admin)
    dataset_groups.attach_group_labels(items)
    for item in items:
        item["can_manage"] = item.get("owner_email") == email
        if not item["can_manage"]:
            for private_key in ("source_file", "source_file_path", "sheet_name"):
                item.pop(private_key, None)
        for k in ("created_at", "updated_at"):
            if item.get(k):
                item[k] = item[k].isoformat()
        if isinstance(item.get("columns"), list):
            item["column_count"] = len(item["columns"])
    return items


# ─── 数据集容器（一个数据集 = N 张表） ────────────────────────────────────────
#
# 路径用 `dataset-groups` 而不是 `datasets/{slug}`：`/datasets/{table_name}` 下面已经挂
# 了 preview / shares / period 等一串按**表名**收发的路由，而 slug 允许中文（见
# `dataset_groups.slugify`），与表名的取值域并不互斥。分开写两个前缀，就不必去证明
# 两者永远不会撞。
#
# 容器和表是两层：这里的每个端点只管容器与「往容器里放一张表」；一张表被放进去之后，
# 它和普通数据集在查询网关、仪表盘、分享上完全一样，不需要再单独处理。

def _group_error(exc: dataset_groups.GroupError, request: Request) -> HTTPException:
    """`GroupError` 已经带了该用的状态码（404 不泄露存在性 / 403 不是主人）。"""
    return HTTPException(exc.status,
                        pick(exc.message, request_lang(request) if request else None))


class DatasetGroupCreate(BaseModel):
    name: str
    description: str = ""


class MemberTableWrite(BaseModel):
    table_name: str
    columns: list[list[str]] = []      # [[name, type], ...] —— JSON 数组，不是元组
    records: list[dict] = []
    # overwrite 先 TRUNCATE 再写，append 只追加。存进表层的名字是 "replace"，
    # 这里对外一律说 overwrite，因为那才是读者能对照的语义。
    mode: str = "overwrite"


class ExternalSpec(BaseModel):
    host: str
    port: int = 5432
    database: str
    user: str
    password: str = ""
    # A field cannot be *named* `schema` — pydantic v2 still has `BaseModel.schema`
    # as a classmethod, so the name is taken. Accept the wire name `schema` anyway,
    # because that is what a PostgreSQL connection form calls it, and accept the
    # field name too so this model is also usable directly from Python.
    # ⚠️ `alias=` alone did NOT work (pydantic warned and dropped it), and
    # `validation_alias=` alone is not enough either: it only binds the INPUT name, so
    # `model_dump(by_alias=True)` still emitted `schema_name` and the service layer
    # quietly read its own default. Both are needed — `alias` for the OUTPUT key that
    # `dataset_groups` reads, `validation_alias` to accept `schema` on the wire.
    schema_name: str = Field(default="public", alias="schema",
                             validation_alias=AliasChoices("schema", "schema_name"))

    model_config = {"populate_by_name": True}


class ExternalImport(BaseModel):
    spec: ExternalSpec
    source_table: str
    table_name: str = ""               # 留空就用源表名
    mode: str = "overwrite"


class AttachTable(BaseModel):
    table_name: str


@router.get("/dataset-groups")
def list_dataset_groups(request: Request):
    """一个数据集 = N 张表。容器对主人可见；被分享了其中一张表的人也可见，
    但只看到自己有权的那几张 —— 可见性仍然是逐表算的。"""
    _ensure_init()
    email, admin = _viewer(request)
    try:
        groups = dataset_groups.list_groups(email, admin=admin)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)
    for g in groups:
        for k in ("created_at", "updated_at"):
            if g.get(k):
                g[k] = g[k].isoformat()
        for t in g["tables"]:
            for k in ("created_at", "updated_at"):
                if t.get(k):
                    t[k] = t[k].isoformat()
    return groups


@router.post("/dataset-groups")
def create_dataset_group(body: DatasetGroupCreate, request: Request):
    _ensure_init()
    email, _admin = _viewer(request)
    try:
        g = dataset_groups.create_group(body.name, body.description, owner_email=email)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)
    for k in ("created_at", "updated_at"):
        if g.get(k):
            g[k] = g[k].isoformat()
    return g


@router.get("/dataset-groups/{slug}")
def get_dataset_group(slug: str, request: Request):
    _ensure_init()
    email, admin = _viewer(request)
    try:
        g = dataset_groups.get_group(slug, email, admin=admin)
        if not g:
            # 看不见就是 404，和写路径同一套语义：报 403 等于承认它存在。
            raise dataset_groups.GroupError(
                f"数据集 {slug} 不存在 / No dataset named {slug}", status=404)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)
    for k in ("created_at", "updated_at"):
        if g.get(k):
            g[k] = g[k].isoformat()
    return g


@router.get("/dataset-groups/{slug}/delete-preview")
def preview_dataset_group_delete(slug: str, request: Request):
    """「按下去会发生什么」—— 服务端自己算，**不是**前端上次渲染时数的那份。

    确认框里要写「连带删除 N 张表」，这个 N 如果来自客户端的 `_state.groups`，就是页面
    打开那一刻的快照：别人刚往里加了一张表，框里仍写着旧数字，用户点 OK 才发现。
    所以表名、行数和有没有定时任务关联，都以这一次查询为准。

    ⚠️ 定时任务按**容器的主人**算，不是调用者：管理员删别人的数据集时，那个人挂在
    这个 slug 上的定时任务才是要处理的，不是管理员自己那几个（挂不上）。
    """
    _ensure_init()
    email, admin = _viewer(request)
    try:
        return dataset_groups.delete_preview(slug, email, admin=admin)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)


@router.delete("/dataset-groups/{slug}")
def delete_dataset_group(slug: str, request: Request):
    """删掉数据集**和里面的每一张物理表**，全有或全无。

    这是本模块唯一真正删数据的端点，所以只有主人能做。两个要点：

    * 要如实回传删掉了哪些表 —— 前端据此在确认框里把表名列出来。
    * 要回传 `schedules`（**不删**它们）。定时任务是另一个决定，所以删完表之后再问；
      用户同意才走 `DELETE /dataset-groups/{slug}/schedules`。
    """
    _ensure_init()
    email, admin = _viewer(request)
    try:
        res = dataset_groups.delete_group(slug, email, admin=admin)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)
    return res


@router.delete("/dataset-groups/{slug}/schedules")
def delete_dataset_group_schedules(slug: str, request: Request):
    """清掉一个已经没有容器的 slug 上的定时任务（删完表后的第二步）。

    容器还在就 409：这是删除流程的清理环节，不是「给某个活数据集停掉自动刷新」的入口 ——
    后者走 `external_sync` 自己的任务删除，那里会说明停的是哪一条。
    """
    _ensure_init()
    email, admin = _viewer(request)
    try:
        return dataset_groups.delete_schedules_for_slug(slug, email, admin=admin)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)


@router.post("/dataset-groups/{slug}/tables")
def write_group_table(slug: str, body: MemberTableWrite, request: Request):
    """往数据集里存一张表。`overwrite` 换掉整张表，`append` 追加。

    重复导入靠表名对上号：表名不变，成员身份和下游引用（分享、仪表盘）都保持有效。
    """
    _ensure_init()
    email, _admin = _viewer(request)
    columns = [(str(c[0]), str(c[1])) for c in body.columns if len(c) >= 2]
    if not columns:
        raise HTTPException(400, pick("至少要有一列 / at least one column is required",
                                      request_lang(request) if request else None))
    if not body.records:
        raise HTTPException(400, pick("至少要有一行数据 / at least one row is required",
                                      request_lang(request) if request else None))
    try:
        return dataset_groups.write_member_table(
            slug, body.table_name, columns, body.records,
            mode=body.mode, owner_email=email)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)


@router.post("/dataset-groups/{slug}/attach")
def attach_group_table(slug: str, body: AttachTable, request: Request):
    """把一张**已经存在**的表挂到这个数据集下。不搬数据。

    表格导入那条路（`batch-update-from-file`）本来就会建表，但它不知道「数据集」是
    什么；与其把整个清洗/暂存链复制一份去教它，不如照常导入、再把结果关联过来。
    """
    _ensure_init()
    email, admin = _viewer(request)
    try:
        return dataset_groups.attach_table(slug, body.table_name, owner_email=email, admin=admin)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)


@router.post("/dataset-groups/peek-external")
def peek_external(body: ExternalSpec, request: Request):
    """列出外部库里的表，让表单给出选择，而不是让人去敲一个自己都看不见的表名。"""
    _ensure_init()
    try:
        return {"tables": dataset_groups.peek_external_tables(
            body.model_dump(by_alias=True))}
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)


@router.post("/dataset-groups/{slug}/import-external")
def import_external(slug: str, body: ExternalImport, request: Request):
    """从另一个 PostgreSQL 往数据集里导一张表。

    只做一条通用 PG 通路 —— Aliyun RDS / AWS RDS / Cloud SQL 都是 PostgreSQL，
    一条连接参数就都覆盖了，不需要一堆厂商专用接口。源库**只读**，而且是服务端强制
    的（`set_session(readonly=True)`，不是靠约定）；密码只在这一次请求里用一下，
    不落库、不写日志，出错时也只报 host 和 database。
    """
    _ensure_init()
    email, _admin = _viewer(request)
    target = (body.table_name or body.source_table).strip()
    try:
        return dataset_groups.import_external_postgres(
            body.spec.model_dump(by_alias=True), slug, target, body.source_table,
            mode=body.mode, owner_email=email)
    except dataset_groups.GroupError as exc:
        raise _group_error(exc, request)
    except db.DatasetSchemaError as e:
        # ⚠️ This one used to reach the client as a bare `500 Internal Server Error`
        # (verified against a live server, 2026-10-05). `GroupError` above only
        # covers what `dataset_groups` itself raises; the table layer underneath
        # raises `DatasetSchemaError` for "name already reserved" / "not yours", so
        # those escaped the handler entirely.
        #
        # It is not an exotic path: the target name DEFAULTS TO THE SOURCE NAME
        # (`body.table_name or body.source_table`, and the form leaves "save as"
        # blank), so picking any table whose name is already taken here — a system
        # table like `access_log`, or somebody else's dataset — lands here. 400 is
        # what `DatasetSchemaError` documents itself as, and what the file-import
        # path in this same file already returns; nothing was written either way.
        raise HTTPException(400, pick(
            f"无法存成这个表名：{e} / cannot save under that table name: {e}",
            request_lang(request) if request else None))


# ─── 保存的连接 + 定期拉取 ────────────────────────────────────────────────────
#
# ⚠️ Every handler below takes the owner from `_viewer(request)` and passes it down;
# none of them has an `admin` branch. The isolation rule the product owner set is that
# a database connection is private to the person operating it — being an operator is
# a capability over ACCOUNTS, and it is not a reason to hold somebody's database
# password. What may be shared is the COPY the pull produces, which is an ordinary
# dataset and travels through the ordinary sharing path.
#
# ⚠️ The response shapes are `external_sync.public_view` / `job_view`, which do not
# contain a password in any form. There is no endpoint here that can return one,
# including to the owner — so there is nothing to leak through a screenshot, a log,
# or a future endpoint that forgets to think about it.


class ConnectionSave(ExternalSpec):
    """A saved connection is the SAME shape as a one-off import spec, plus a label.

    ⚠️ Subclassed rather than re-declared: `ExternalSpec` carries the `schema` /
    `schema_name` alias handling with a paragraph explaining why both are needed.
    A second model with its own copy of those aliases is a second answer to a
    question the service layer asks by name.
    """
    label: str = ""


class JobCreate(BaseModel):
    connection_id: int
    slug: str
    source_table: str
    target_table: str = ""              # 留空就用源表名
    mode: str = "overwrite"
    interval_minutes: int = 1440
    enabled: bool = True


class JobPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    mode: str | None = None


def _sync_error(exc: external_sync.SyncError, request: Request,
                status: int = 400) -> HTTPException:
    return HTTPException(status, pick(str(exc), request_lang(request) if request else None))


@router.get("/db-connections")
def list_db_connections(request: Request):
    email, _admin = _viewer(request)
    return {"connections": external_sync.list_connections(email)}


@router.post("/db-connections")
def create_db_connection(body: ConnectionSave, request: Request):
    email, _admin = _viewer(request)
    try:
        return external_sync.save_connection(email, body.model_dump(by_alias=True))
    except external_sync.SyncError as e:
        raise _sync_error(e, request)


@router.patch("/db-connections/{connection_id}")
def update_db_connection(connection_id: int, body: ConnectionSave, request: Request):
    email, _admin = _viewer(request)
    try:
        return external_sync.save_connection(email, body.model_dump(by_alias=True),
                                             connection_id=connection_id)
    except external_sync.SyncError as e:
        # ⚠️ 404, not 403: a connection belonging to somebody else must be
        # indistinguishable from one that does not exist, or this endpoint becomes
        # a way to enumerate who has saved which database.
        raise _sync_error(e, request, status=404)


@router.delete("/db-connections/{connection_id}")
def delete_db_connection(connection_id: int, request: Request):
    email, _admin = _viewer(request)
    try:
        external_sync.delete_connection(email, connection_id)
    except external_sync.SyncError as e:
        raise _sync_error(e, request, status=404)
    return {"deleted": connection_id}


@router.get("/db-sync-jobs")
def list_db_sync_jobs(request: Request):
    email, _admin = _viewer(request)
    return {"jobs": external_sync.list_jobs(email),
            "intervals": list(external_sync.INTERVAL_CHOICES)}


@router.post("/db-sync-jobs")
def create_db_sync_job(body: JobCreate, request: Request):
    email, _admin = _viewer(request)
    try:
        return external_sync.create_job(email, body.model_dump())
    except external_sync.SyncError as e:
        raise _sync_error(e, request)
    except dataset_groups.GroupError as e:
        raise _group_error(e, request)


@router.patch("/db-sync-jobs/{job_id}")
def update_db_sync_job(job_id: int, body: JobPatch, request: Request):
    email, _admin = _viewer(request)
    try:
        return external_sync.update_job(email, job_id, body.model_dump(exclude_none=True))
    except external_sync.SyncError as e:
        raise _sync_error(e, request, status=404)


@router.delete("/db-sync-jobs/{job_id}")
def delete_db_sync_job(job_id: int, request: Request):
    email, _admin = _viewer(request)
    try:
        external_sync.delete_job(email, job_id)
    except external_sync.SyncError as e:
        raise _sync_error(e, request, status=404)
    return {"deleted": job_id}


@router.post("/db-sync-jobs/{job_id}/run")
def run_db_sync_job(job_id: int, request: Request):
    """Run it now. Same code path as the timer, so "run now" cannot drift from it."""
    email, _admin = _viewer(request)
    try:
        return external_sync.run_job(email, job_id)
    except external_sync.SyncError as e:
        raise _sync_error(e, request, status=404)
    except dataset_groups.GroupError as e:
        raise _group_error(e, request)


# ─── 数据集预览 ────────────────────────────────────────────────────────────────

@router.get("/datasets/{table_name}/preview")
def preview_dataset(table_name: str, request: Request, limit: int = 100):
    _ensure_init()
    email, admin = _viewer(request)
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))
    try:
        result = db.preview_pg(table_name, limit)
        return {"table": table_name, "target": "klado.public", **result}
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))


# ─── 时间段统计（自动识别日期列）────────────────────────────────────────────────
# 列名优先级关键词（匹配子串，忽略大小写）
_DATE_COL_KEYWORDS = [
    "period", "month", "date", "time", "year", "ym",
    "年", "月", "日期", "时间", "周期",
]
_DATE_COL_TYPES = {"timestamp", "date", "datetime"}

def _detect_date_col(columns: list) -> str | None:
    """从列定义 [(name, type), ...] 中自动找最合适的日期/周期列。"""
    candidates_type_and_name, candidates_type, candidates_name = [], [], []
    for col in columns:
        if isinstance(col, (list, tuple)) and len(col) >= 2:
            name, col_type = str(col[0]).lower(), str(col[1]).lower()
            raw_name = col[0]
        elif isinstance(col, dict):
            raw_name = col.get("name", "")
            name = str(raw_name).lower()
            col_type = str(col.get("type", "")).lower()
        else:
            continue
        is_date_type = any(t in col_type for t in _DATE_COL_TYPES)
        is_date_name = any(k in name for k in _DATE_COL_KEYWORDS)
        if is_date_type and is_date_name:
            candidates_type_and_name.append(raw_name)
        elif is_date_type:
            candidates_type.append(raw_name)
        elif is_date_name:
            candidates_name.append(raw_name)
    for group in [candidates_type_and_name, candidates_type, candidates_name]:
        if group:
            return group[0]
    return None

@router.get("/datasets/{table_name}/period-stats")
def period_stats(table_name: str, request: Request, col: str | None = None):
    """
    自动识别日期列，返回分组统计。
    可通过 ?col=xxx 手动指定列名覆盖自动识别。
    """
    _ensure_init()
    email, admin = _viewer(request)
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))

    date_col = col
    if not date_col:
        columns = info.get("columns") or []
        date_col = _detect_date_col(columns)
    if not date_col:
        return {"has_period": False, "periods": [], "col": None}

    try:
        result = db.period_stats_pg(table_name, date_col)
        rows = result.get("rows", [])
        if not rows:
            return {"has_period": False, "periods": [], "col": date_col}
        return {"has_period": True, "periods": rows, "col": date_col}
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ("column", "does not exist", "unknown", "binder")):
            return {"has_period": False, "periods": [], "col": date_col, "error": str(e)}
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))

# ─── 删除指定月份数据 ─────────────────────────────────────────────────────────

@router.delete("/datasets/{table_name}/period")
def delete_period(table_name: str, request: Request, col: str, period: str):
    """
    删除数据集中指定月份的数据行。
    col: 日期列名（如 period, date, month）
    period: 年月值，如 "2025-01" 或 "2025-01-01T00:00:00"（取前7位匹配 YYYY-MM）
    """
    _ensure_init()
    email = _assert_dataset_owner(table_name, request)
    admin = False
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))
    try:
        count = db.delete_period_pg(table_name, col, period)
        updated = db.get_dataset(table_name, viewer_email=email, admin=admin)
        new_rows = updated.get("row_count", 0) if updated else 0
        return {"deleted_rows": count, "new_row_count": new_rows, "table": table_name, "col": col, "period": period[:7]}
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))


# ─── SQL 查询 ──────────────────────────────────────────────────────────────────
# The guard itself lives in `services.dataset_query` so every module that lets a
# document read data shares ONE implementation. These names stay as thin delegations
# because the unit tests (and readers) know them from here.

_leading_keyword = dq.leading_keyword
_referenced_relations = dq.referenced_relations
_assert_query_allowed = dq.assert_query_allowed



class QueryRequest(BaseModel):
    sql: str
    # Every table lives in klado.public now; the field is kept for client
    # compatibility and only "pg" is accepted.
    target: str = "pg"
    # Honored since 2026-09-27. It used to be silently dropped (pydantic ignores unknown
    # fields) while every document told callers to send it — so a "LIMIT 2000" that the
    # agent believed in did nothing.
    max_rows: int = 2000


@router.post("/query")
def run_query(req: QueryRequest, request: Request):
    _ensure_init()
    viewer_email, admin = _viewer(request)
    if req.target != "pg":
        raise HTTPException(400, pick(f"未知 target: {req.target} / unknown target: {req.target}", request_lang(request) if request else None))
    return dq.run_scoped_query(request, req.sql, viewer_email=viewer_email,
                               admin=admin, max_rows=req.max_rows)


# ─── 下载源文件 ────────────────────────────────────────────────────────────────

@router.get("/datasets/{table_name}/source-file")
def download_source_file(table_name: str, request: Request):
    """下载该数据集对应的原始上传文件（来自 MinIO）。"""
    _ensure_init()
    email = _assert_dataset_owner(table_name, request)
    admin = False
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))
    raw_path = info.get("source_file_path")
    if not raw_path:
        raise HTTPException(404, pick("该数据集没有保存原始文件 / this dataset has no stored source file", request_lang(request) if request else None))
    _deny_unless_readable(request, raw_path, email, admin)
    try:
        data, content_type = db.get_source_file(raw_path)
    except Exception as e:
        raise HTTPException(500, pick(f"文件读取失败: {e} / failed to read the file: {e}", request_lang(request) if request else None))
    filename = raw_path.rsplit("/", 1)[-1]
    # 去掉时间戳前缀 (20250101_120000_xxx.xlsx → xxx.xlsx)
    if len(filename) > 16 and filename[8] == "_" and filename[15] == "_":
        filename = filename[16:]
    return Response(
        content=data,
        media_type=content_type,
        headers=_build_download_headers(filename),
    )


@router.get("/files/download")
def download_file_by_path(request: Request, path: str = Query(..., description="MinIO object path")):
    """Download a raw file from MinIO by its object path."""
    _ensure_init()
    if not path or ".." in path:
        raise HTTPException(400, pick("路径不合法 / invalid path", request_lang(request) if request else None))
    email, admin = _viewer(request)
    _deny_unless_readable(request, path, email, admin)
    try:
        data, content_type = db.get_source_file(path)
    except Exception as e:
        raise HTTPException(404, pick(f"文件不存在: {e} / file not found: {e}", request_lang(request) if request else None))
    filename = path.rsplit("/", 1)[-1]
    if len(filename) > 16 and filename[8] == "_" and filename[15] == "_":
        filename = filename[16:]
    return Response(
        content=data,
        media_type=content_type,
        headers=_build_download_headers(filename),
    )


# ─── 从文件库更新数据集 ────────────────────────────────────────────────────────

class UpdateFromFileRequest(BaseModel):
    file_id:    int
    sheet_name: str
    mode:       str = "replace"   # replace | append
    cleaning_rules: dict = {}     # saved cleaning rules to apply


# ─── QI Mappings ──────────────────────────────────────────────────────────────

@router.get("/qi-mappings")
def get_qi_mappings(file_slug: str, request: Request):
    _ensure_init()
    return db.get_qi_mappings(file_slug, owner_email=_viewer(request)[0])


class QiSaveMappingsRequest(BaseModel):
    file_slug: str
    mappings: list[dict]


@router.post("/qi-mappings")
def save_qi_mappings(req: QiSaveMappingsRequest, request: Request):
    _ensure_init()
    email = _viewer(request)[0]
    for mapping in req.mappings:
        _assert_dataset_owner(mapping.get("table_name", ""), request)
    db.save_qi_mappings(req.file_slug, req.mappings, owner_email=email)
    return {"saved": len(req.mappings)}


# ─── 批量从同一文件更新多个数据集（一次 OSS 下载 + 一次解析）──────────────────

class BatchSheetMapping(BaseModel):
    sheet_name:     str
    table_name:     str
    mode:           str = "replace"
    cleaning_rules: dict = {}


class BatchUpdateFromFileRequest(BaseModel):
    file_id:  int
    mappings: list[BatchSheetMapping]


def _run_batch_update_from_file(job_id: str, request_key: str, req: BatchUpdateFromFileRequest, owner_email: str = ""):
    """从同一文件批量更新多个数据集。只做一次 OSS 下载和一次 Excel 解析。"""
    started = time.monotonic()
    results: list[dict] = []
    detail = {"request_key": request_key, "owner_email": owner_email, "results": results, "current_sheet": None}
    db.update_import_job(job_id, status="running", rows_ok=0, rows_error=0, detail=detail)
    try:
        file_rec = db.get_file(req.file_id, viewer_email=owner_email)
        if not file_rec:
            raise RuntimeError("文件不存在 / file not found")

        # Resolve idempotent duplicate appends before downloading/parsing a
        # potentially large workbook. A retry can therefore finish instantly.
        pending_mappings: list[BatchSheetMapping] = []
        dataset_info: dict[str, dict] = {}
        for mapping in req.mappings:
            info = db.get_dataset(mapping.table_name, viewer_email=owner_email)
            if info and info.get("owner_email") != owner_email:
                info = None
            if not info:
                results.append({"table": mapping.table_name, "sheet": mapping.sheet_name, "error": pick("数据集不存在 / dataset not found",
                                                          None)})
                continue
            dataset_info[mapping.table_name] = info
            if mapping.mode == "append" and info.get("source_file_path") == file_rec["object_name"]:
                logger.info(
                    "Smart Import job=%s skipped duplicate append file=%s sheet=%s target=public.%s",
                    job_id, file_rec["filename"], mapping.sheet_name, mapping.table_name,
                )
                results.append({
                    "table": mapping.table_name,
                    "sheet": mapping.sheet_name,
                    "rows": info.get("row_count") or 0,
                    "mode": mapping.mode,
                    "reused": True,
                })
            else:
                pending_mappings.append(mapping)

        if not pending_mappings:
            detail["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            db.update_import_job(
                job_id, status="completed",
                rows_ok=sum(1 for item in results if not item.get("error")),
                rows_error=sum(1 for item in results if item.get("error")),
                detail=detail,
            )
            return

        download_started = time.monotonic()
        data, _ = db.get_source_file(file_rec["object_name"])
        logger.info(
            "Smart Import job=%s downloaded file=%s bytes=%d elapsed_ms=%d",
            job_id, file_rec["filename"], len(data), int((time.monotonic() - download_started) * 1000),
        )
        parse_started = time.monotonic()
        # Only parse the sheets that are actually mapped in this job. Large
        # BSR workbooks contain many reference sheets but are commonly
        # imported one sheet at a time.
        selected_sheet_names = {mapping.sheet_name for mapping in pending_mappings}
        sheets = parse_excel(data, file_rec["filename"], sheet_names=selected_sheet_names)
        logger.info(
            "Smart Import job=%s parsed file=%s sheets=%d elapsed_ms=%d",
            job_id, file_rec["filename"], len(sheets), int((time.monotonic() - parse_started) * 1000),
        )
        sheets_by_name = {s["sheet_name"]: s for s in sheets}

        for mapping in pending_mappings:
            detail["current_sheet"] = mapping.sheet_name
            db.update_import_job(
                job_id, status="running",
                rows_ok=sum(1 for item in results if not item.get("error")),
                rows_error=sum(1 for item in results if item.get("error")),
                detail=detail,
            )
            sheet = sheets_by_name.get(mapping.sheet_name)
            if not sheet:
                results.append({"table": mapping.table_name, "sheet": mapping.sheet_name, "error": pick("Sheet 不存在 / sheet not found",
                                                          None)})
                continue

            info = dataset_info[mapping.table_name]

            rules = mapping.cleaning_rules or info.get("cleaning_rules") or {}
            import copy
            import pandas as pd
            df = copy.copy(sheet["df"])
            if rules:
                try:
                    # Normalise the OSS object name before the rules run: files
                    # that reach the library through register-oss /
                    # publish-by-path keep the `<YYYYMMDD_HHMMSS>_` upload
                    # prefix, and a `(\d{6})` filename rule would then read the
                    # upload date instead of the data month.
                    df = apply_cleaning_rules(
                        df, rules,
                        source_filename=_display_filename(file_rec["filename"]),
                    )
                except Exception as exc:
                    results.append({"table": mapping.table_name, "sheet": mapping.sheet_name,
                                    "error": str(exc)})
                    continue

            df, effective_cols, _hints = _infer_cleaned_frame(df, rules)
            sheet_started = time.monotonic()
            try:
                s_df, s_cols = apply_transform(mapping.table_name, df, effective_cols)
                records = dataframe_to_records(s_df, s_cols)
                db.reconcile_column_types(mapping.table_name, s_cols, owner_email=owner_email)
                rows = db.insert_pg_data(mapping.table_name, records, mapping.mode, owner_email=owner_email)
                db.update_source_file_path(mapping.table_name, file_rec["object_name"])
                elapsed_ms = int((time.monotonic() - sheet_started) * 1000)
                logger.info(
                    "Smart Import job=%s succeeded file=%s sheet=%s target=public.%s mode=%s rows=%s elapsed_ms=%d",
                    job_id, file_rec["filename"], mapping.sheet_name, mapping.table_name, mapping.mode, rows, elapsed_ms,
                )
                results.append({"table": mapping.table_name, "sheet": mapping.sheet_name, "rows": rows, "mode": mapping.mode, "elapsed_ms": elapsed_ms})
            except Exception as exc:
                logger.exception("Smart Import job=%s failed sheet=%s target=public.%s", job_id, mapping.sheet_name, mapping.table_name)
                results.append({"table": mapping.table_name, "sheet": mapping.sheet_name,
                                "error": pick(str(exc), None)})

        detail["current_sheet"] = None
        detail["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        db.update_import_job(
            job_id, status="completed",
            rows_ok=sum(1 for item in results if not item.get("error")),
            rows_error=sum(1 for item in results if item.get("error")),
            detail=detail,
        )
    except Exception as exc:
        logger.exception("Smart Import job=%s failed", job_id)
        detail["error"] = str(exc)
        detail["current_sheet"] = None
        detail["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        db.update_import_job(
            job_id, status="error", rows_ok=0, rows_error=max(1, len(req.mappings)), detail=detail,
        )


@router.post("/datasets/batch-update-from-file", status_code=202)
def batch_update_from_file(req: BatchUpdateFromFileRequest, request: Request):
    """Start a persistent background Smart Import job and return immediately."""
    _ensure_init()
    if not req.mappings:
        return {"status": "completed", "results": []}
    email, admin = _viewer(request)
    file_rec = db.get_file(req.file_id, viewer_email=email, admin=admin)
    if not file_rec:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))
    # Authorise every target up front: the job thread re-reads the registry without a
    # viewer, so this is what stops a hand-crafted payload naming somebody else's table.
    for mapping in req.mappings:
        _assert_dataset_owner(mapping.table_name, request)
    payload = {
        "owner_email": email,
        "file_id": req.file_id,
        "mappings": [m.model_dump() if hasattr(m, "model_dump") else m.dict() for m in req.mappings],
    }
    request_key = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    job, reused = db.create_import_job(file_rec["filename"], request_key, len(req.mappings), owner_email=email)
    if not reused:
        threading.Thread(
            target=_run_batch_update_from_file,
            args=(job["id"], request_key, req, email),
            daemon=True,
            name=f"smart-import-{job['id'][:8]}",
        ).start()
    return {"task_id": job["id"], "status": job["status"], "reused": reused}


@router.get("/import-jobs/{job_id}")
def get_import_job(job_id: str, request: Request):
    _ensure_init()
    job = db.get_import_job(job_id)
    if not job or job.get("owner_email") != _viewer(request)[0]:
        raise HTTPException(404, pick("导入任务不存在 / import job not found", request_lang(request) if request else None))
    return job


# ─── Update dataset from file ─────────────────────────────────────────────────

@router.post("/datasets/{table_name}/update-from-file")
def update_dataset_from_file(table_name: str, req: UpdateFromFileRequest, request: Request):
    """从文件库中选取一个 sheet，更新指定数据集。"""
    _ensure_init()
    email = _assert_dataset_owner(table_name, request)
    admin = False
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))

    file_rec = db.get_file(req.file_id, viewer_email=email, admin=admin)
    if not file_rec:
        raise HTTPException(404, pick("文件不存在 / file not found", request_lang(request) if request else None))

    # 从 MinIO 取文件内容
    try:
        data, _ = db.get_source_file(file_rec["object_name"])
    except Exception as e:
        raise HTTPException(500, pick(f"文件读取失败: {e} / failed to read the file: {e}", request_lang(request) if request else None))

    # 解析指定 sheet
    try:
        sheets = parse_excel(
            data,
            file_rec["filename"],
            sheet_names=[req.sheet_name] if req.sheet_name else None,
        )
    except Exception as e:
        raise HTTPException(422, pick(f"解析失败: {e} / failed to parse the file: {e}", request_lang(request) if request else None))

    sheet = next((s for s in sheets if s["sheet_name"] == req.sheet_name), None)
    if not sheet:
        raise HTTPException(404, pick(f"Sheet '{req.sheet_name}' 不存在 / sheet '{req.sheet_name}' not found", request_lang(request) if request else None))

    # Apply cleaning rules (from request or from saved registry)
    rules = req.cleaning_rules or info.get("cleaning_rules") or {}
    if rules:
        try:
            sheet["df"] = apply_cleaning_rules(
                sheet["df"], rules,
                source_filename=_display_filename(file_rec["filename"]),
            )
        except Exception as exc:
            raise HTTPException(422, pick(f"清洗规则错误: {exc} / cleaning rule error: {exc}", request_lang(request)))

    try:
        sheet["df"], eff_cols, _hints = _infer_cleaned_frame(sheet["df"], rules)
        s_df, s_cols = apply_transform(table_name, sheet["df"], eff_cols)
        records = dataframe_to_records(s_df, s_cols)
        db.reconcile_column_types(table_name, s_cols, owner_email=email)
        rows = db.insert_pg_data(table_name, records, req.mode, owner_email=email)
        db.update_source_file_path(table_name, file_rec["object_name"])
        logger.info(
            "Smart Import update succeeded file=%s sheet=%s target=public.%s mode=%s rows=%s",
            file_rec["filename"], req.sheet_name, table_name, req.mode, rows,
        )
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))

    return {
        "table":  table_name,
        "file":   file_rec["filename"],
        "sheet":  req.sheet_name,
        "mode":   req.mode,
        "rows":   rows,
    }


# ─── 删除数据集 ────────────────────────────────────────────────────────────────

@router.delete("/datasets/{table_name}")
def delete_dataset(table_name: str, request: Request):
    _ensure_init()
    email = _assert_dataset_owner(table_name, request)
    admin = False
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))
    db.delete_pg_table(table_name)
    return {"deleted": table_name}


# ─── 数据集分享 ────────────────────────────────────────────────────────────────
# 分享的是**数据集**（可以查询的行），不是上传的源文件：被分享者拿不到你的原文件、
# 对象键和你文件库里的任何东西。授权只写一张表，没有任何写路径会查它——删掉那一行
# 就是收回访问，不存在需要推理的竞态。见 services/dataset_shares.py。

class ShareRequest(BaseModel):
    email: str


def _assert_dataset_owner(table_name: str, request: Request) -> str:
    """404 unless the caller owns `table_name`. Returns the owner address."""
    email, _admin = _viewer(request)
    info = db.get_dataset(table_name, viewer_email=email)
    if not info or info.get("owner_email") != email:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request)))
    return email


@router.get("/files/shared-with-me")
def files_shared_with_me(request: Request):
    _ensure_init()
    email, _ = _viewer(request)
    return file_shares.shared_with_me(email)


@router.get("/files/{file_id}/shares")
def list_file_shares(file_id: int, request: Request):
    _ensure_init()
    _assert_file_owner(file_id, request)
    return file_shares.shares_of(file_id)


@router.post("/files/{file_id}/shares")
def share_file(file_id: int, req: ShareRequest, request: Request):
    _ensure_init()
    owner = _assert_file_owner(file_id, request)
    allowed, reason, _ = orgs.may_share_to_many(owner, [req.email])
    if not allowed:
        status, message = orgs.denial(reason)
        raise HTTPException(status, pick(message, request_lang(request)))
    try:
        return file_shares.share(file_id, req.email, owner)
    except file_shares.ShareError as exc:
        raise HTTPException(400, pick(str(exc), request_lang(request)))


@router.delete("/files/{file_id}/shares/{grantee}")
def revoke_file_share(file_id: int, grantee: str, request: Request):
    _ensure_init()
    owner = _assert_file_owner(file_id, request)
    try:
        removed = file_shares.revoke(file_id, grantee, owner)
    except file_shares.ShareError as exc:
        raise HTTPException(400, pick(str(exc), request_lang(request)))
    if not removed:
        raise HTTPException(404, pick("没有这条分享记录 / no such share", request_lang(request)))
    return {"ok": True}


@router.delete("/files/{file_id}/publish")
def revoke_file_link(file_id: int, request: Request):
    _ensure_init()
    _assert_file_owner(file_id, request)
    with db.get_pg_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE public._file_library SET share_token=NULL WHERE id=%s", (file_id,))
        conn.commit()
    return {"ok": True}


@router.get("/datasets/{table_name}/shares")
def list_dataset_shares(table_name: str, request: Request):
    """谁被分享了这张表。仅所有者可见（管理员也不能代替所有者发授权）。"""
    _ensure_init()
    _assert_dataset_owner(table_name, request)
    try:
        return shares.shares_of(table_name)
    except shares.ShareError as e:
        raise HTTPException(400, pick(str(e), request_lang(request)))


@router.post("/datasets/{table_name}/shares")
def share_dataset(table_name: str, req: ShareRequest, request: Request):
    """把数据集分享给一个同事（邮箱点名）。幂等：重复分享刷新时间戳。"""
    _ensure_init()
    owner = _assert_dataset_owner(table_name, request)
    # ⚠️ Had no gate at all before. A dataset share is a grant on *queryable rows*, and
    # `dataset_query.run_scoped_query` is deliberately the only way to read a shared
    # table — which means an ungated share here is the most consequential of the nine
    # write paths, not the least. Checked before the service call so nothing is written.
    allowed, reason, _offenders = orgs.may_share_to_many(owner, [req.email])
    if not allowed:
        status, message = orgs.denial(reason)
        raise HTTPException(status_code=status, detail=pick(message, request_lang(request)))
    try:
        return shares.share(table_name, req.email, owner)
    except shares.ShareError as e:
        raise HTTPException(400, pick(str(e), request_lang(request)))


@router.delete("/datasets/{table_name}/shares/{grantee}")
def revoke_dataset_share(table_name: str, grantee: str, request: Request):
    _ensure_init()
    owner = _assert_dataset_owner(table_name, request)
    try:
        removed = shares.revoke(table_name, grantee, owner)
    except shares.ShareError as e:
        raise HTTPException(400, pick(str(e), request_lang(request)))
    if not removed:
        raise HTTPException(404, pick("没有这条分享记录 / no such share", request_lang(request)))
    return {"ok": True, "table": table_name, "revoked": grantee}


@router.get("/datasets/shared-with-me")
def datasets_shared_with_me(request: Request):
    """别人分享给我的数据集。只读视图：这些表不会出现在文件库里。"""
    _ensure_init()
    email, _admin = _viewer(request)
    try:
        return shares.shared_with_me_rows(email)
    except Exception:
        logger.exception("shared-with-me lookup failed")
        raise HTTPException(500, pick("读取分享列表失败 / failed to read shares", request_lang(request)))


# ─── 拉取（把别人的数据变成自己的）────────────────────────────────────────────────
# 分享给的是「查询权」：一行 dataset_shares，对方能查不能改，主人账号一删就没了。
# 拉取给的是**数据本身**：新表 / 新对象 / 新页面，新的主人，之后与来源无关。
# 两者不是一回事，所以拉取不是更好看的分享 —— 见 services/pulls.py。


@router.post("/datasets/{table_name}/pull")
def pull_dataset(table_name: str, request: Request):
    """把这个数据集复制成我自己的。副本独立存在，原主人删除后不受影响。

    `_require_browser` 而不是仅靠中间件：`_AGENT_WRITE_ALLOWED_PREFIXES` 按**前缀**
    放行，所以 `/api/data-center` 不在其中确实挡住了 agent，但也挡不住将来有人
    把前缀改宽。拉取是一次性的数据转移，必须由人来点。
    """
    _ensure_init()
    email = _require_browser(request)
    try:
        return pulls.pull_dataset(table_name, email)
    except pulls.PullError as e:
        raise HTTPException(404, pick(str(e), request_lang(request)))


@router.post("/files/{file_id}/pull")
def pull_file(file_id: int, request: Request):
    """把我的上传复制一份留底。副本有自己的对象键，原件删除后仍然可用。

    只有文件的主人能拉取：分享从不暴露源文件（`_FILE_VISIBLE`），拉取不能成为
    绕过这条边界的办法。
    """
    _ensure_init()
    email = _require_browser(request)
    try:
        return pulls.pull_file(file_id, email)
    except pulls.PullError as e:
        raise HTTPException(404, pick(str(e), request_lang(request)))


# ─── 更新 metadata ─────────────────────────────────────────────────────────────

class PatchRequest(BaseModel):
    modules:      list[str] | None = None
    description:  str | None       = None
    display_name: str | None       = None


@router.patch("/datasets/{table_name}")
def patch_dataset(table_name: str, req: PatchRequest, request: Request):
    _ensure_init()
    email = _assert_dataset_owner(table_name, request)
    admin = False
    info = db.get_dataset(table_name, viewer_email=email, admin=admin)
    if not info:
        raise HTTPException(404, pick("数据集不存在 / dataset not found", request_lang(request) if request else None))
    if req.modules is not None:
        db.update_modules(table_name, req.modules)
    if req.description is not None:
        db.update_description(table_name, req.description)
    if req.display_name is not None:
        db.update_display_name(table_name, req.display_name)
    return {"ok": True, "table": table_name}


# ─── 内部辅助 ──────────────────────────────────────────────────────────────────

def _validate_file(request: Request, filename: str | None):
    if not filename:
        raise HTTPException(400, pick("文件名为空 / file name is empty", request_lang(request) if request else None))
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, pick(f"不支持的格式: {ext}，支持 xlsx、xls、xlsm、csv / unsupported format {ext}: use xlsx, xls, xlsm or csv", request_lang(request) if request else None))


def _safe_folder(request: Request, folder: str | None) -> str:
    """把上传目标目录收成一个干净的相对前缀。

    ⚠️ 这个字段以前只来自 `_fbPrefix`（页面上当前的目录），所以它从来不需要
    校验；「导入整个本地文件夹」之后它多了一条**用户可控**的来源 ——
    浏览器给的 `webkitRelativePath`（"Q3/华东/销售.xlsx"），前端只去掉最后一段
    文件名后原样传上来。`..` 和绝对路径因此第一次真的可能出现在这里。

    `stage_file` 把它拼进 object name（`{folder}/{ts}_{uuid}_{filename}`），
    MinIO 会拒绝带 `..` 的 key，于是表现是 500 而不是数据越界 —— 但拒绝发生在
    存储层、错误信息也不对。在边界上挡掉，给出可读的 400。
    """
    raw = (folder or "").strip().strip("/")
    if not raw:
        return ""
    parts = []
    for seg in raw.replace("\\", "/").split("/"):
        seg = seg.strip()
        if not seg or seg == ".":
            continue
        if seg == "..":
            raise HTTPException(400, pick("目录路径不合法 / invalid folder path", request_lang(request) if request else None))
        parts.append(seg)
    return "/".join(parts)


# ─── DB Explorer (live, real data) ────────────────────────────────────────────

import time as _time

_EXCLUDE_PG = {"_import_registry", "_ocr_jobs"}  # internal metadata tables

# Sources response cached for 30 s to avoid reconnect overhead on every page refresh.
# ⚠️ The list is per-viewer now, so the cache is keyed by viewer — one shared slot
# would hand one account another account's table names.
_SRC_CACHE: dict = {}
_SRC_TTL = 30.0


def _all_public_tables() -> list[dict]:
    """Single canonical public table list."""
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.table_name, COALESCE(s.n_live_tup, 0)
                FROM information_schema.tables t
                LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name AND s.schemaname = t.table_schema
                WHERE t.table_schema = 'public'
                ORDER BY t.table_name
            """)
            return [{"name": r[0], "rows": r[1]} for r in cur.fetchall()]


def _visible_dataset_names(viewer_email: str, admin: bool) -> set[str] | None:
    """Registry table names the caller may browse, or None for an operator."""
    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT table_name FROM {db.META_TABLE} "
                f"WHERE {db._ROW_VISIBLE}",
                (viewer_email, viewer_email),
            )
            return {r[0] for r in cur.fetchall()}


def _pg_tables(all_tables: list[dict], visible: set[str] | None) -> list[dict]:
    return [t for t in all_tables
            if t["name"] not in _EXCLUDE_PG
            and (visible is None or t["name"] in visible)]


def _assert_explorer_table_visible(request: Request, table: str, viewer_email: str, admin: bool) -> None:
    visible = _visible_dataset_names(viewer_email, admin)
    if visible is not None and table not in visible:
        raise HTTPException(404, pick(f"数据集不存在: {table} / unknown table: {table}", request_lang(request) if request else None))


def _oss_folders() -> list[dict]:
    """List top-level OSS prefixes (former bucket names) with object counts."""
    try:
        from services import oss_storage
        result = []
        for prefix in oss_storage.list_top_level_prefixes():
            cnt = len(oss_storage.list_objects(prefix, recursive=True))
            result.append({"name": prefix, "rows": cnt})
        return result
    except Exception as exc:
        logging.getLogger(__name__).error("db explorer OSS folder discovery failed: %s", exc)
        return []


@router.get("/db-explorer/sources")
def db_explorer_sources(request: Request):
    """All data sources with table count summaries (cached 30 s per viewer)."""
    _ensure_init()
    email, admin = _viewer(request)
    now = _time.monotonic()
    cached = _SRC_CACHE.get(email)
    if cached and now - cached["ts"] < _SRC_TTL:
        return cached["data"]
    try:
        # Every table lives in klado.public: there is one source, not the old
        # per-project groups (sales / market) that named particular datasets.
        pg  = _pg_tables(_all_public_tables(), _visible_dataset_names(email, admin))
        oss = []  # raw bucket inventories are not personal file libraries
        result = [
            {"id": "pg",  "name": "Public",     "badge": "PG",  "tables": pg},
            {"id": "oss", "name": "Aliyun OSS", "badge": "OSS", "tables": oss},
        ]
        _SRC_CACHE[email] = {"data": result, "ts": now}
        return result
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))


@router.get("/db-explorer/{source}/{table}/data")
def db_explorer_data(source: str, table: str, request: Request, limit: int = 100):
    """Preview up to `limit` rows from a table."""
    _ensure_init()
    email, admin = _viewer(request)
    try:
        if source == "pg":
            _assert_explorer_table_visible(request, table, email, admin)
            safe_limit = max(1, min(int(limit), 1000))
            with db.get_pg_conn() as conn:
                with conn.cursor() as cur:
                    relation = sql.Identifier("public", table)
                    cur.execute(sql.SQL("SELECT * FROM {} LIMIT %s").format(relation), (safe_limit,))
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(relation))
                    total = cur.fetchone()[0]
            return {"columns": cols, "rows": rows, "total": total}

        elif source == "oss":
            try:
                from services import oss_storage
                objs = oss_storage.list_objects(table, recursive=True)
                total = len(objs)
                rows = [
                    {
                        "name": o.key,
                        "size_bytes": o.size,
                        "last_modified": o.last_modified,
                        "url": f"/api/dc/files/stream?path={table}/{o.key}",
                    }
                    for o in objs[:int(limit)] if not o.is_dir
                ]
                cols = ["name", "size_bytes", "last_modified", "url"]
                return {"columns": cols, "rows": rows, "total": total}
            except Exception as e:
                raise HTTPException(500, pick(str(e), request_lang(request) if request else None))

        else:
            raise HTTPException(404, pick(f"未知来源: {source} / unknown source: {source}", request_lang(request) if request else None))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))


@router.get("/db-explorer/{source}/{table}/schema")
def db_explorer_schema(source: str, table: str, request: Request):
    """Column names and types for a table."""
    _ensure_init()
    email, admin = _viewer(request)
    try:
        if source == "pg":
            _assert_explorer_table_visible(request, table, email, admin)
            with db.get_pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT column_name, data_type, character_maximum_length, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=%s
                        ORDER BY ordinal_position
                    """, (table,))
                    return [{"column_name":r[0],"data_type":r[1],"max_length":r[2],"nullable":r[3]} for r in cur.fetchall()]

        elif source == "oss":
            return [
                {"column_name": "name",          "data_type": "string",    "nullable": "NO"},
                {"column_name": "size_bytes",     "data_type": "integer",   "nullable": "YES"},
                {"column_name": "last_modified",  "data_type": "timestamp", "nullable": "YES"},
                {"column_name": "url",            "data_type": "string",    "nullable": "YES"},
            ]

        else:
            raise HTTPException(404, pick(f"未知来源: {source} / unknown source: {source}", request_lang(request) if request else None))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request) if request else None))
