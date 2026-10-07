"""
Workspace — private documents and public snapshots

Agent（或人）写好的独立 HTML 通过 `POST /api/reports` 进入当前账号的私人工作区。
所有者可发布一份公共快照；其他用户可拉取到自己的私人工作区独立编辑。

存储：PostgreSQL 表 `ai_reports`，HTML 原文直接落库，不依赖对象存储。
建表在首次访问时完成（`_ensure_table`）。

发布契约（给 agent 用）
------------------------
    POST /api/reports
    {
      "slug":    "q3-channel-deepdive",     # 可选，缺省由 title 自动生成
      "title":   "Q3 Channel Deep Dive",
      "summary": "一句话摘要，展示在卡片上",
      "category":"Ad-hoc",                  # 卡片上的分类标签
      "tags":    "channel,q3",              # 逗号分隔
      "author":  "agent",              # 谁生成的（通常 agent）
      "submitter": "张三",              # 谁提交的（飞书用户名）—— 卡片上显示的是这个
      "status":  "published",               # published | draft | archived
      "pinned":  false,
      "html":    "<html>…</html>"           # 完整 HTML 文档
    }

同一个属于当前账号的私有 slug 重复 POST 即为覆盖更新，updated_at 自动刷新。

报告里引用本站资源请用**相对路径**（如 `api/products`、`logo.png`）；
服务端会在 `<head>` 后注入 `<base href="${APP_BASE_PATH}/">`，因此相对路径在
任何环境（本地预览 / test / 生产）都能解析到正确位置。

读取按账号鉴权：私有报告可授予指定邮箱只读访问，公共快照
对已登录用户可见。所有者还可生成可撤销的匿名只读链接。

**16:9 分页报告（deck）**：报告里写 `<div class="deck"><section class="slide">…`
时，`/raw` 会在返回前把 16:9 分页运行时（`vendor/report-deck.{css,js}`）**内联**进去，
报告因此变成键盘整页翻页的横版文档；普通长文报告不受影响。约定见知识库
`klado-v2:format-report-deck`（源文件 `api/knowledge_docs/klado-v2__format-report-deck.md`，
密度口径、图表配方与 CSS 类名表都在那里）。
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import string
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote, unquote

import psycopg2
import psycopg2.extras
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import AliasChoices, BaseModel, Field
from typing import Any, Optional

from core.config import app_base_href, resolve_frontend_dir, settings
from core.i18n import pick, request_lang
from core.db import connect_main
from core import htmlkit
from services import doc_state
from services.doc_state import (
    MAX_FILTER_OPTIONS,
    MAX_FILTER_VALUE_LEN,
    MAX_FILTERS,
    MAX_STATE_BYTES,
    clean_filter_schema,
    effective_selection,
    filter_label,
    filter_number,
    load_filter_schema,
    no_json_constant,
    state_text,
    tolerant_schema,
)
from html import escape as html_escape

from klado_shared import orgs
# ⚠️ The ONE list of formats that can be edited in place. `doc_preview` owns it
# (it is the module that both renders the `data-oid` the editor writes to and
# enforces on save), and it is imported here at module level only for the
# constant: its heavy dependencies — python-docx / python-pptx / openpyxl — are
# all imported INSIDE its functions, so this costs nothing. The `try` is still
# there because `_doc_preview_module` is lazy on purpose: a container missing a
# preview library must boot and serve every other page.
try:
    from services.doc_preview import EDITABLE_DOC_TYPES
except Exception:                                          # noqa: BLE001
    EDITABLE_DOC_TYPES = ()

from services import (annotations, inbox, oss_storage, report_cover,
                      report_pptx, report_projects, report_visual_export)

_LOG = logging.getLogger(__name__)

router = APIRouter()

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_HTML_BYTES = 8 * 1024 * 1024          # 8 MB per report document
# Interactive reports (see /api/reports/{slug}/state below) keep their annotation
# state here. 64 KB is the whole black box: cells + colours + labels + descriptions
# for a couple of views is a few KB, so this is ~10× headroom and not a data store.
MAX_TITLE_LEN = 240
MAX_SUMMARY_LEN = 600
MAX_TAGS_LEN = 300
MAX_CATEGORY_LEN = 80
MAX_AUTHOR_LEN = 80

# Documents (pptx / xlsx / pdf / docx) arrive as base64 inside JSON, the same way
# a knowledge-page image does. 24 MB of raw bytes is ~32 MB of base64 and covers a
# normal deck or workbook; anything larger needs a different transfer path (there is
# deliberately none: the share link, the preview cache and the OSS copy all assume
# one request). The error message says so rather than reporting a bare 413.
MAX_DOC_BYTES = 24 * 1024 * 1024
MAX_DOC_SLIDES = 300
DOC_BUCKET_PREFIX = "workspace-docs"
DOC_MIME = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
VALID_DOC_TYPES = tuple(DOC_MIME)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
VALID_STATUS = ("published", "draft", "archived")
# A report's `kind` is how it BEHAVES, and the three values are mutually exclusive:
#   static      — a snapshot; the reader looks, and may leave notes (annotation layer)
#   interactive — the reader edits it and the edits are saved to /state
#   dynamic     — the reader switches slices from the SPA sidebar; the document itself
#                 is read-only and shows no control (see `_inline_filter_runtime`)
#   document    — not HTML at all: an uploaded pptx / xlsx / pdf / docx card
VALID_KIND = ("static", "interactive", "dynamic", "document")
# `kind` values an HTML publish may declare. "document" is set by the upload endpoint
# only — a POST /api/reports with html cannot claim to be a binary document.
VALID_DECLARED_KIND = ("static", "interactive", "dynamic")

# ⚠️ `is_allowed_recipient()` used to live here, and an identical copy sat
# in `services/dashboard_store.py`. It answered "does this address end in
# AUTH_ALLOWED_EMAIL_DOMAIN" — a single deployment-wide suffix — which is not the
# question the product asks. Two copies of one policy in two files, covering two of
# the nine share paths, is how the other seven ended up with no check at all.
#
# The policy is now `klado_shared.orgs.may_share_to_many()`: one function, one
# implementation, and it asks "may THIS owner hand it to THAT person" instead of
# guessing from a suffix. `AUTH_ALLOWED_EMAIL_DOMAIN` survives only as a backstop for
# a single-domain deployment; see `auth_store.allowed_domain`.


# ── Dynamic reports: agent-authored filters ──────────────────────────────────
#
# The filter CONTROLS live in the SPA viewer sidebar (never in the document), the
# filter DEFINITIONS come from the agent, and the document only declares which
# content belongs to which slice (`data-filter-when="period=2026-06&metric=gto"`).
# These limits keep one card's sidebar a sidebar: a dozen controls is already a lot
# of rail, and 80 options fit a scrolling list without becoming a data dump.


def _legacy_owner() -> str:
    """The identity adopted by rows that predate authenticated ownership.

    Two jobs, both of which used to rest on a hardcoded colleague address:

    * rows with a NULL `owner_email` (published before ownership existed) are handed
      to this account so that no report becomes invisible;
    * with `AUTH_ENABLED` off there is no login middleware, so this is also the
      identity an unauthenticated request acts as — the single-user local mode.

    It comes from `AUTH_ADMIN_EMAILS` (first entry), which is **empty by default**:
    this repository names no administrator. An empty value means there is no implicit
    identity, and such a request gets the ordinary 401 from `_identity`.
    """
    return (settings.AUTH_ADMIN_EMAILS or "").split(",")[0].strip().lower()


def _report_kind(html: str, declared: str = "", request: Request | None = None) -> str:
    """Classify authoring controls, not the deck's pagination JavaScript.

    Precedence matters and is asserted by `test_report_workspaces`:

    1. what the caller DECLARED (`kind` in the publish body) — an explicit statement wins;
    2. `<meta name="report-kind" content="…">` / `[data-report-kind]` in the document;
    3. `data-filter-when` blocks → dynamic. Checked BEFORE the editable-control sniff
       because a dynamic report is a read-only document: it must not be reclassified as
       interactive (which would hand it the `/state` editor contract it does not honor);
    4. editable controls / a saved-state URL → interactive;
    5. otherwise static.

    "document" is deliberately not accepted here: a binary card is created by the upload
    endpoint, so an HTML publish cannot claim to be one.
    """
    if declared and declared not in VALID_DECLARED_KIND:
        raise HTTPException(status_code=400, detail=pick("kind 只能是 static、interactive 或 dynamic / kind must be static, interactive or dynamic", request_lang(request) if request else None))
    if declared:
        return declared
    soup = BeautifulSoup(html or "", "html.parser")
    marker = soup.select_one('meta[name="report-kind"], [data-report-kind]')
    marked = ((marker.get("content") or marker.get("data-report-kind") or "").strip().lower()
              if marker else "")
    if marked in VALID_DECLARED_KIND:
        return marked
    if soup.select_one("[data-filter-when]"):
        return "dynamic"
    if soup.select_one('[contenteditable]:not([contenteditable="false"]), input, textarea, select'):
        return "interactive"
    if re.search(r"(?:api/)?reports/[^'\"`\s]+/state|/state\b", html or "", re.I):
        return "interactive"
    return "static"


def _identity(request: Request) -> tuple[str, str]:
    user = getattr(request.state, "current_user", None) or {}
    email = str(user.get("email") or user.get("username") or "").strip().lower()
    if not email and not settings.AUTH_ENABLED:
        # Local single-user development mode has no login middleware.
        email = _legacy_owner()
    if not email:
        raise HTTPException(status_code=401, detail=pick("请登录后使用工作区 / sign in to access Workspace", request_lang(request) if request else None))
    return email, getattr(request.state, "auth_kind", "browser") or "browser"


def _may_read(row: dict, email: str) -> bool:
    if row.get("owner_email") == email or (row.get("visibility") == "public"
                                            and row.get("status") == "published"):
        return True
    report_id = row.get("id")
    if not report_id or row.get("visibility") != "private" or row.get("status") != "published":
        return False
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ai_report_colleague_shares "
                        "WHERE report_id = %s AND recipient_email = %s", (report_id, email))
            return cur.fetchone() is not None


def _require_read(row: dict | None, email: str, request: Request | None = None) -> dict:
    if not row or not _may_read(row, email):
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    return row


def _require_owner(row: dict | None, email: str, request: Request | None = None, *, private: bool = False) -> dict:
    if not row or row.get("owner_email") != email or (private and row.get("visibility") != "private"):
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    return row


def _require_browser(request: Request) -> str:
    email, kind = _identity(request)
    if kind == "agent" and settings.AUTH_ENABLED:
        raise HTTPException(status_code=403, detail=pick("发布与拉取需要浏览器会话 / publish and pull require a browser session", request_lang(request) if request else None))
    return email


# ── Dynamic reports: the filter schema ───────────────────────────────────────
#
# Read `_report_kind` first: `dynamic` means "the reader switches slices from the SPA
# sidebar". Two pieces of state make that work and they are deliberately separate:
#
#   * the SCHEMA (`ai_reports.filters_json`) — written by the agent, one per report;
#   * the SELECTION (`ai_report_filter_selections`) — one per (report, reader), because
#     two colleagues looking at the same report are entitled to look at different
#     quarters. It never rides in `state_json`: dynamic reports are not interactive,
#     and mixing a reader's view preference into an editable document's state would
#     make every sidebar click look like an edit to the document.
#
# The server never interprets the numbers in a filter — it validates the SHAPE only.
# What a slice means is between the agent's `data-filter-when` blocks and the runtime.

# ── Schema ───────────────────────────────────────────────────────────────────

_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_table() -> None:
    """
    Create `ai_reports` on first use (idempotent, memoised per process).

    Memoisation is safe because nothing in this app drops a table at runtime
    *by itself*: the only way `ai_reports` disappears is an operator using the
    Data Center's drop-table action. If that happens, the memoised flag would keep
    requests failing with "relation does not exist" until the server restarts — a
    loud, obvious failure, and the reason to keep DROP out of any maintenance script.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = connect_main()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ai_reports (
                        id          SERIAL PRIMARY KEY,
                        slug        TEXT UNIQUE NOT NULL,
                        title       TEXT NOT NULL,
                        summary     TEXT NOT NULL DEFAULT '',
                        -- The card's one line, per language. A bilingual report must carry
                        -- both (see `_bilingual_summaries`): the cover veil shows the pair,
                        -- and a reader who only gets `summary` would be reading a summary
                        -- in one of the two languages the report itself offers.
                        summary_en  TEXT NOT NULL DEFAULT '',
                        summary_zh  TEXT NOT NULL DEFAULT '',
                        category    TEXT NOT NULL DEFAULT 'Ad-hoc',
                        tags        TEXT NOT NULL DEFAULT '',
                        author      TEXT NOT NULL DEFAULT 'agent',
                        submitter   TEXT NOT NULL DEFAULT '',
                        status      TEXT NOT NULL DEFAULT 'published',
                        pinned      BOOLEAN NOT NULL DEFAULT FALSE,
                        accent      TEXT NOT NULL DEFAULT '',
                        cover_data  BYTEA,
                        cover_mime  TEXT NOT NULL DEFAULT '',
                        cover_w     INTEGER,
                        cover_h     INTEGER,
                        cover_prompt TEXT NOT NULL DEFAULT '',
                        cover_model TEXT NOT NULL DEFAULT '',
                        cover_url   TEXT NOT NULL DEFAULT '',
                        langs       TEXT NOT NULL DEFAULT '',
                        html        TEXT NOT NULL DEFAULT '',
                        -- Annotation state of an INTERACTIVE report (right-click a cell →
                        -- colour / label / description). Opaque JSON text; NULL = never
                        -- saved. Deliberately its own timestamp instead of reusing
                        -- `updated_at`: saving an annotation must not reorder the card wall
                        -- (it sorts by `updated_at DESC`) nor invalidate the `?v=` cover
                        -- cache-buster — an annotation is not a re-publication.
                        state_json  TEXT,
                        state_updated_at TIMESTAMPTZ,
                        -- A DYNAMIC report's filter schema (see doc_state.clean_filter_schema). Empty
                        -- for every other kind. JSON text, not jsonb: like state_json this is
                        -- written and read whole and only ever validated in Python.
                        filters_json TEXT NOT NULL DEFAULT '',
                        -- A DOCUMENT card's payload: which of the four accepted types it is,
                        -- where the bytes live in OSS, and the name the reader downloads it
                        -- under. `html` stays empty for these rows; `doc_type` is what tells
                        -- the two apart (never the emptiness of `html`, which a half-finished
                        -- report also has).
                        doc_type    TEXT NOT NULL DEFAULT '',
                        doc_object  TEXT NOT NULL DEFAULT '',
                        doc_mime    TEXT NOT NULL DEFAULT '',
                        doc_name    TEXT NOT NULL DEFAULT '',
                        doc_pages   INTEGER,
                        owner_email TEXT,
                        visibility TEXT,
                        kind TEXT,
                        source_report_id INTEGER,
                        size_bytes  INTEGER NOT NULL DEFAULT 0,
                        views       INTEGER NOT NULL DEFAULT 0,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ai_reports_status_idx ON ai_reports (status)")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_reports_updated_idx ON ai_reports (updated_at DESC)")
                # 已存在的库：CREATE TABLE IF NOT EXISTS 不会补列，逐条幂等 ALTER
                for _stmt in (
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_data BYTEA",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_mime TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_w INTEGER",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_h INTEGER",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_prompt TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_model TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS cover_url TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS langs TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS summary_en TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS summary_zh TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS submitter TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS state_json TEXT",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS filters_json TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS doc_type TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS doc_object TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS doc_mime TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS doc_name TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS doc_pages INTEGER",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS owner_email TEXT",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS visibility TEXT",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS kind TEXT",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS source_report_id INTEGER",
                    # ── placement (2026-10-03): 项目 → 文件夹 → 报告 ──────────────
                    # Nullable **on purpose**, and not to be backfilled: every report
                    # published before this feature existed is unfiled, and NULL is
                    # the honest description of that. `services/report_projects.py::
                    # move_report()` is the only writer, so the moment a report is
                    # filed the value is real. A backfill would have had to invent a
                    # project row per owner anyway — which is exactly what
                    # `ensure_unfiled()` does, lazily, on the first read.
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS project_slug TEXT",
                    "ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS folder_id INTEGER",
                ):
                    cur.execute(_stmt)
                # `cover_brand` no longer exists: a cover carries no brand palette.
                # Drop it from any database an earlier deployment created it in
                # (idempotent, safe to re-run; never destructive to other columns).
                cur.execute("ALTER TABLE ai_reports DROP COLUMN IF EXISTS cover_brand")
                # Rows published before ownership existed become private to the
                # configured administrator. Only NULL rows are migrated; later
                # publications keep their owner. Skipped entirely when no
                # administrator is configured — adopting them for "" would leave
                # the column non-null but unowned, which is worse than NULL.
                if _legacy_owner():
                    cur.execute("UPDATE ai_reports SET owner_email = %s WHERE owner_email IS NULL",
                                (_legacy_owner(),))
                else:
                    print("NOTICE: no AUTH_ADMIN_EMAILS configured — reports without an owner "
                          "are left as NULL (set AUTH_ADMIN_EMAILS to adopt them)", flush=True)
                cur.execute("UPDATE ai_reports SET visibility = 'private' WHERE visibility IS NULL")
                cur.execute("SELECT id, html FROM ai_reports WHERE kind IS NULL")
                for report_id, html in cur.fetchall():
                    cur.execute("UPDATE ai_reports SET kind = %s WHERE id = %s",
                                (_report_kind(html), report_id))
                cur.execute("ALTER TABLE ai_reports ALTER COLUMN visibility SET NOT NULL")
                # Tighten owner_email only when nothing is left unowned. Without a
                # configured administrator the adoption above is skipped on purpose,
                # and forcing NOT NULL then would fail on exactly the rows it left
                # alone (a fresh database has none, so this is the legacy-DB path).
                cur.execute("SELECT 1 FROM ai_reports WHERE owner_email IS NULL LIMIT 1")
                if cur.fetchone() is None:
                    cur.execute("ALTER TABLE ai_reports ALTER COLUMN owner_email SET NOT NULL")
                cur.execute("ALTER TABLE ai_reports ALTER COLUMN kind SET NOT NULL")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_reports_workspace_idx "
                            "ON ai_reports (owner_email, visibility, updated_at DESC)")
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ai_reports_public_source_idx "
                            "ON ai_reports (source_report_id) WHERE visibility = 'public' AND source_report_id IS NOT NULL")
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_report_colleague_shares (
                    report_id INTEGER NOT NULL REFERENCES ai_reports(id) ON DELETE CASCADE,
                    recipient_email TEXT NOT NULL,
                    shared_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (report_id, recipient_email)
                )""")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_report_colleague_recipient_idx "
                            "ON ai_report_colleague_shares (recipient_email, report_id)")
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_report_anyone_links (
                    report_id INTEGER PRIMARY KEY REFERENCES ai_reports(id) ON DELETE CASCADE,
                    link_id TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
                # One readability row per (report, reader): the sidebar's state. Kept out
                # of `ai_reports` because it is per READER — a column there would let one
                # colleague's quarter change another's. Cascades with the report, so
                # deleting a card cannot leave stray selection rows behind.
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_report_filter_selections (
                    report_id INTEGER NOT NULL REFERENCES ai_reports(id) ON DELETE CASCADE,
                    user_email TEXT NOT NULL,
                    selection_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (report_id, user_email)
                )""")
                # ── projects and folders (2026-10-03) ────────────────────────────
                #
                # A project is the top-level container the user thinks of as a "main
                # folder" and sees as a card; a folder is the one level below it.
                # Every project gets exactly one `system` folder — 未归档 — and every
                # account gets exactly one `system` project, its own catch-all. Both
                # are created lazily by `services/report_projects.py::
                # ensure_unfiled()`, which is why this DDL creates no rows.
                #
                # ⚠️ The cover is **the same seven columns `ai_reports` keeps**, so
                # `services/report_cover.py::resolve_cover()` serves reports, projects,
                # dashboards and calendar events from one function instead of a second
                # copy drifting.
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_report_projects (
                    id           SERIAL PRIMARY KEY,
                    slug         TEXT UNIQUE NOT NULL,
                    title        TEXT NOT NULL,
                    summary      TEXT NOT NULL DEFAULT '',
                    system       BOOLEAN NOT NULL DEFAULT FALSE,
                    cover_data   BYTEA,
                    cover_mime   TEXT NOT NULL DEFAULT '',
                    cover_w      INTEGER,
                    cover_h      INTEGER,
                    cover_prompt TEXT NOT NULL DEFAULT '',
                    cover_model  TEXT NOT NULL DEFAULT '',
                    cover_url    TEXT NOT NULL DEFAULT '',
                    owner_email  TEXT NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_report_projects_owner_idx "
                            "ON ai_report_projects (owner_email, updated_at DESC)")
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_report_folders (
                    id           SERIAL PRIMARY KEY,
                    project_slug TEXT NOT NULL
                               REFERENCES ai_report_projects(slug) ON DELETE CASCADE,
                    title        TEXT NOT NULL,
                    system       BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_order   INTEGER NOT NULL DEFAULT 0,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_report_folders_project_idx "
                            "ON ai_report_folders (project_slug, sort_order, id)")
                # One 未归档 per project, enforced by the index rather than by code —
                # a second one would make "put it in 未归档" ambiguous between two
                # rows the reader cannot tell apart, and `create_project` has no way
                # to know a lazy `ensure_unfiled()` already inserted it.
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ai_report_folders_system_idx "
                            "ON ai_report_folders (project_slug) WHERE system")
                report_projects.normalize_legacy_placement(cur)
                # The directory view lists by placement; without this the filter is a
                # sequential scan of every report on the account.
                cur.execute("CREATE INDEX IF NOT EXISTS ai_reports_placement_idx "
                            "ON ai_reports (project_slug, folder_id, updated_at DESC)")
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()


def _with_schema(fn):
    """
    Recreate the table and retry once when a request finds it missing.

    `_ensure_table()` is memoised per process and it is deliberately NOT a
    "check on every request" — but `ai_reports` can disappear at runtime: the
    Data Center UI exposes drop-table for public tables. Without this guard a
    single DROP would leave every reports endpoint answering 500 until the server
    restarted; with it, the next request heals itself.

    Must sit closest to `def` so the router decorators register the wrapper.
    """
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        _ensure_table()
        try:
            return await fn(*args, **kwargs)
        except psycopg2.errors.UndefinedTable:
            global _schema_ready
            _LOG.warning("ai_reports vanished at runtime — recreating and retrying")
            _schema_ready = False
            _ensure_table()
            return await fn(*args, **kwargs)

    return guarded


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pg():
    conn = connect_main()
    conn.autocommit = False
    return conn


@contextmanager
def _db():
    """
    Connection scope that always closes.

    ⚠️ `with connect_main() as conn:` is NOT this: psycopg2's connection context
    manager only commits/rolls back — it never closes, so the connection leaks
    back to the pool. Every report endpoint here leaks a connection per request
    if that shape is used.
    """
    conn = _pg()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _slugify(text: str) -> str:
    """Derive a URL-safe slug from a human title."""
    raw = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", raw)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    if not s:
        # Fully non-ASCII title (e.g. Chinese): a constant "report" would make
        # every such publication collide on one slug — and silently overwrite.
        return "report-" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    return s[:80].strip("-.") or "report"


def _slug_taken(cur, slug: str) -> Optional[str]:
    """Return the title of an existing row with this slug, else None."""
    cur.execute("SELECT title FROM ai_reports WHERE slug = %s", (slug,))
    row = cur.fetchone()
    return row["title"] if row else None


def _clean_slug(raw: str, fallback_title: str, request: Request | None = None) -> str:
    slug = (raw or "").strip().lower()
    if not slug:
        slug = _slugify(fallback_title)
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail=pick("slug 只能是小写字母、数字和 . _ -（最长 80 字符） / slug must match ^[a-z0-9][a-z0-9._-]{0,80}$ (lowercase letters, digits, . _ -)", request_lang(request) if request else None),
        )
    return slug


def _clean_status(raw: str, request: Request | None = None) -> str:
    status = (raw or "published").strip().lower()
    if status not in VALID_STATUS:
        raise HTTPException(status_code=400, detail=pick(f"status 必须是 {', '.join(VALID_STATUS)} 之一 / status must be one of {', '.join(VALID_STATUS)}", request_lang(request) if request else None))
    return status


def _check_html(html: str, request: Request | None = None) -> int:
    if not isinstance(html, str) or not html.strip():
        raise HTTPException(status_code=400, detail=pick("html 必须是非空 HTML 文档 / html must be a non-empty HTML document", request_lang(request) if request else None))
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        raise HTTPException(
            status_code=413,
            detail=pick(f"html 为 {size} 字节，上限 {MAX_HTML_BYTES} 字节（8 MB） / html is {size} bytes; the limit is {MAX_HTML_BYTES} bytes (8 MB)", request_lang(request) if request else None),
        )
    return size


def _meta(row: dict, viewer_email: str = "") -> dict:
    """Shape a DB row into the metadata payload the cards render (no HTML)."""
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "summary": row.get("summary") or "",
        # The per-language pair a bilingual report carries. The veil shows these two
        # lines instead of `summary` when they are there.
        "summary_en": row.get("summary_en") or "",
        "summary_zh": row.get("summary_zh") or "",
        "category": row.get("category") or "",
        "tags": [t for t in (row.get("tags") or "").split(",") if t.strip()],
        "author": row.get("author") or "",
        # Who SENT it in (a Feishu username, typically) — distinct from `author`,
        # which is what generated the document. The card shows submitter || author.
        "submitter": row.get("submitter") or "",
        "status": row.get("status") or "published",
        "owner_email": row.get("owner_email") or "",
        "visibility": row.get("visibility") or "private",
        "kind": row.get("kind") or "static",
        # A dynamic card advertises its sidebar without shipping the schema: the wall does
        # not need up to a dozen filter definitions per row, and the viewer fetches them
        # from `GET /api/reports/{slug}/filters` when the card is actually opened.
        "has_filters": bool(row.get("has_filters")),
        # A document card: which of pptx/xlsx/pdf/docx it is, the name it downloads
        # under, and (for pptx/pdf) how many pages the preview has. `doc_url` is
        # deliberately NOT here — consumers build the mount-correct URL themselves (the
        # SPA with `appAbsUrl`, agents from the publish response's absolute
        # `document_url`), which is the one rule that keeps downloads off the host root.
        "doc_type": row.get("doc_type") or "",
        "doc_name": row.get("doc_name") or "",
        "doc_pages": row.get("doc_pages"),
        "source_report_id": row.get("source_report_id"),
        # Where the report is filed (项目 → 文件夹 → 报告). ⚠️ **Shipped as the raw
        # columns, not resolved to a project/folder id**: a NULL pair is 未归档 and the
        # client's wall is where that turns into a name. Resolving it here would mean
        # calling `ensure_unfiled()` — which WRITES — on the list path, so a plain read
        # would create rows. The list already calls it once for the wall; these two
        # fields only have to say "filed or not, and where".
        "project_slug": row.get("project_slug") or "",
        "folder_id": row.get("folder_id"),
        "public_slug": row.get("public_slug") or "",
        "can_manage": bool(viewer_email and row.get("owner_email") == viewer_email),
        # ⚠️ "Can this file be edited in place", answered by the SAME list the
        # server enforces in `apply_edits` (`doc_preview.EDITABLE_DOC_TYPES`).
        # Shipping it on the list means the client does not fetch the document to
        # find out, and — more to the point — cannot hold a second, stale copy of
        # that list. A PDF is the case that matters: it is a document, it opens,
        # and there is nothing honest to offer a reader who types into it.
        "editable": bool(row.get("doc_type")) and (row.get("doc_type") or "").lower()
        in EDITABLE_DOC_TYPES,
        "shared_with_me": bool(viewer_email and row.get("visibility") == "private"
                               and row.get("owner_email") != viewer_email),
        "shared": bool(row.get("visibility") == "public" or row.get("public_slug")
                       or row.get("has_colleague_shares") or row.get("has_anyone_link")),
        "pinned": bool(row.get("pinned")),
        "accent": row.get("accent") or "",
        # The cover is either bytes we serve ourselves (/api/reports/{slug}/cover)
        # or an external URL the publisher supplied. The card appends ?v=<updated_at>
        # to the self-served form so a re-published cover is not cached stale.
        "has_cover": bool(row.get("has_cover")) or bool(row.get("cover_url")),
        "cover_url": (f"{_base_href()}api/reports/{row['slug']}/cover"
                      if row.get("has_cover") else (row.get("cover_url") or "")),
        "cover_w": row.get("cover_w"),
        "cover_h": row.get("cover_h"),
        "cover_prompt": row.get("cover_prompt") or "",
        "cover_model": row.get("cover_model") or "",
        "langs": [c for c in (row.get("langs") or "").split(",") if c],
        "size_bytes": int(row.get("size_bytes") or 0),
        "views": int(row.get("views") or 0),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


_SELECT_COLS = ("id, slug, title, summary, summary_en, summary_zh, category, tags, author, status, pinned, "
                "accent, size_bytes, views, created_at, updated_at, langs, submitter, cover_mime, "
                "cover_w, cover_h, cover_prompt, cover_model, cover_url, "
                "owner_email, visibility, kind, source_report_id, "
                "project_slug, folder_id, "
                "doc_type, doc_name, doc_pages, "
                "(cover_data IS NOT NULL) AS has_cover, "
                # A boolean, not the schema itself: the wall asks "does this card have a
                # sidebar?", and shipping every filter definition for 200 rows would put
                # megabytes on a page that renders none of them.
                "(filters_json <> '') AS has_filters, "
                "EXISTS (SELECT 1 FROM ai_report_colleague_shares cs "
                "WHERE cs.report_id = ai_reports.id) AS has_colleague_shares, "
                "EXISTS (SELECT 1 FROM ai_report_anyone_links al "
                "WHERE al.report_id = ai_reports.id) AS has_anyone_link")


def _base_href() -> str:
    """`/` — the mount point the SPA is served from.

    Delegates to `core.config.app_base_href()`: the annotation runtime, the Inbox
    deep links and the calendar page all need the same string, and this used to be
    the only place that knew how to compute it.
    """
    return app_base_href()


def _public_report_url(request: Request, slug: str) -> str:
    """
    The report's absolute address. Private reports require the owner's login;
    public snapshots are visible to every signed-in user. It is
    environment-correct because it follows the Host/proto of the publishing request
    (an agent posting to test gets a test link, one posting to prod gets prod).
    """
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "https").split(",")[0].strip()
    if not host:
        return f"{_base_href()}r/{slug}"
    return f"{proto}://{host}{_base_href()}r/{slug}"


def _anyone_token(link_id: str) -> str:
    """A revocable bearer link. Only the random ID is stored in PostgreSQL."""
    digest = hmac.new(settings.SECRET_KEY.encode("utf-8"),
                      ("report-anyone:" + link_id).encode("ascii"), hashlib.sha256).digest()[:16]
    signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return link_id + "." + signature


def _anyone_url(request: Request, link_id: str) -> str:
    token = _anyone_token(link_id)
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "https").split(",")[0].strip()
    path = f"{_base_href()}s/{token}"
    return f"{proto}://{host}{path}" if host else path


def _resolve_anyone_link(token: str, request: Request | None = None) -> dict:
    """Authenticate an anonymous read without granting any other API access."""
    if len(token) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]{20,40}\.[A-Za-z0-9_-]{22}", token):
        raise HTTPException(status_code=404, detail=pick("分享链接不存在或已失效 / shared link not found", request_lang(request) if request else None))
    link_id, _ = token.split(".", 1)
    if not hmac.compare_digest(token, _anyone_token(link_id)):
        raise HTTPException(status_code=404, detail=pick("分享链接不存在或已失效 / shared link not found", request_lang(request) if request else None))
    _ensure_table()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ⚠️ Same omission as `/r/{slug}` above: a guest's document page needs the size
            # and the page count too, or it prints "0 B" to the very person the link was sent to.
            cur.execute("SELECT r.id, r.slug, r.title, r.html, r.state_json, r.kind, r.status, r.filters_json, "
                        "r.doc_type, r.doc_object, r.doc_mime, r.doc_name, r.doc_pages, r.size_bytes, "
                        "r.owner_email, r.visibility "
                        "FROM ai_report_anyone_links l JOIN ai_reports r ON r.id = l.report_id "
                        "WHERE l.link_id = %s", (link_id,))
            row = cur.fetchone()
    if not row or row["status"] != "published":
        raise HTTPException(status_code=404, detail=pick("分享链接不存在或已失效 / shared link not found", request_lang(request) if request else None))
    return dict(row)


def _guest_image_keys(row: dict) -> set[str]:
    """Only image keys literally named by this shared document may be fetched."""
    source = unquote((row.get("html") or "") + "\n" + (row.get("state_json") or ""))
    return {match.group(0).rstrip(".,;}") for match in re.finditer(
        r"(?:product-images|competitor-images)/[^\s\"'<>?&#)\\]+", source, re.I)}


def _rewrite_guest_images(html: str, token: str) -> str:
    """Give embedded OSS images a token-scoped read URL without opening storage."""
    mount = re.escape(_base_href().strip("/"))
    prefix = rf"(?:/{mount}/|/|)" if mount else r"/?"
    return re.sub(rf"(?<![A-Za-z0-9_-]){prefix}api/storage/serve",
                  _base_href() + "s/" + token + "/asset", html, flags=re.I)


def _collision_suffix() -> str:
    """`-YYYYMMDD-HHMM-xw4k2q` — readable "when it landed", plus 6 random chars so two
    reports landing the same minute cannot collide (36^6 ≈ 2×10⁹)."""
    rand = "".join(__import__("random").choices(string.ascii_lowercase + string.digits, k=6))
    return "-" + time.strftime("%Y%m%d-%H%M") + "-" + rand


def _inject_base_tag(html: str, base: str) -> str:
    """
    Insert `<base href="…">` so reports can use relative paths
    (`api/products`, `logo.png`) in every environment.

    Only injected when the document does not already declare one.
    """
    if re.search(r"<base\b", html, re.IGNORECASE):
        return html
    tag = f'<base href="{base}">'
    m = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + tag + html[m.end():]
    m = re.search(r"<html\b[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + "<head>" + tag + "</head>" + html[m.end():]
    # Fragment / headless HTML — a second file wins over the first, exactly like
    # a `<base>` element would; still the least surprising option for a doc with
    # no head at all.
    return tag + html


def _inject_report_context(html: str, slug: str, public_interactive: bool,
                           guest_state_url: str = "") -> str:
    """Bind legacy hard-coded state URLs to this copy's slug before authored JS runs.

    Pulling a report copies its HTML byte-for-byte. Some older reports derive the
    state URL from location; others hard-code the original slug. Rebinding only
    same-origin /api/reports/*/state requests gives both forms an independent
    state document without rewriting arbitrary authored HTML or API calls.
    """
    script = f"""<script data-report-workspace>
(() => {{
  const slug = {json.dumps(slug)};
  const readOnly = {str(public_interactive).lower()};
  const guestStateUrl = {json.dumps(guest_state_url)};
  window.__reportWorkspace = {{slug, readOnly}};
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {{
    try {{
      const source = input instanceof Request ? input.url : String(input);
      const url = new URL(source, document.baseURI);
      if (url.origin === location.origin && /\\/api\\/reports\\/[a-z0-9._-]+\\/state\\/?$/i.test(url.pathname)) {{
        url.pathname = guestStateUrl || url.pathname.replace(/\\/api\\/reports\\/[a-z0-9._-]+\\/state\\/?$/i,
          '/api/reports/' + encodeURIComponent(slug) + '/state');
        input = input instanceof Request ? new Request(url.href, input) : url.href;
      }}
    }} catch (_) {{ /* an invalid URL still reaches the native fetch error */ }}
    return originalFetch(input, init);
  }};
  if (readOnly) document.addEventListener('DOMContentLoaded', () => {{
    const notice = document.createElement('div');
    notice.setAttribute('data-pptx-export', 'omit');
    notice.textContent = guestStateUrl
      ? '共享链接 · 只读。登录 Workspace 后可查看原报告'
      : '公共展示 · 只读。请从 Workspace 公共区拉取到个人工作区后编辑';
    Object.assign(notice.style, {{position:'fixed',top:'10px',right:'10px',zIndex:'99999',
      padding:'6px 10px',borderRadius:'7px',background:'rgba(15,23,42,.86)',
      color:'#fff',font:'12px sans-serif',pointerEvents:'none'}});
    document.body.appendChild(notice);
  }});
}})();
</script>"""
    head = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
    return html[:head.end()] + script + html[head.end():] if head else script + html


# ── Covers ───────────────────────────────────────────────────────────────────
#
# A cover is either bytes we store and serve ourselves (`cover_data`, exposed as
# /api/reports/{slug}/cover) or an external URL the publisher supplied. Covers are
# generated server-side because the publishing agent is external and holds no
# image credentials — see services/report_cover.py.

def _cover_from_body(body, title: str, request: Request | None = None):
    """
    Resolve the cover for a publish/update request.

    Priority: explicit bytes → explicit URL → generate (only when asked). Returns
    None when nothing was supplied, so a re-publish keeps the existing cover
    instead of silently dropping it.

    The decision itself lives in `services/report_cover.py::resolve_cover`, shared with the
    Calendar (an event stores a cover with the same columns and the same request fields);
    only the error mapping is ours: bad input → 400, provider refusal → 502.
    """
    try:
        return report_cover.resolve_cover(body, title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=pick(f"封面生成失败: {exc} / cover generation failed: {exc}", request_lang(request) if request else None)) from exc


def _store_cover(cur, slug: str, cover, clear: bool = False) -> None:
    """Write or clear the cover columns for one report."""
    if clear:
        cur.execute("UPDATE ai_reports SET cover_data = NULL, cover_mime = '', cover_w = NULL, "
                    "cover_h = NULL, cover_prompt = '', cover_model = '', cover_url = '' "
                    "WHERE slug = %s", (slug,))
        return
    if cover is None:
        return
    cur.execute(
        "UPDATE ai_reports SET cover_data = %s, cover_mime = %s, cover_w = %s, cover_h = %s, "
        "cover_prompt = %s, cover_model = %s, cover_url = %s WHERE slug = %s",
        (psycopg2.Binary(cover["data"]) if cover["data"] else None, cover["mime"],
         cover["w"], cover["h"], cover["prompt"], cover.get("model", ""), cover["url"], slug),
    )


# ── Cover field aliases ──────────────────────────────────────────────────────
#
# An external agent published a report that HAD a generated cover but attached
# nothing: it used a field name we don't declare, Pydantic silently dropped it, and
# we answered 201 — so the caller reasonably believed the cover went through. The
# card then showed the "no cover" placeholder and the image was simply lost.
#
# So: accept the plausible spellings here, and (below) refuse a cover-shaped key we
# don't recognise instead of ignoring it.
COVER_ALIASES: dict[str, tuple[str, ...]] = {
    "cover_base64": ("cover_base64", "coverBase64", "cover_image", "coverImage",
                     "image_base64", "imageBase64", "cover"),
    "cover_url": ("cover_url", "coverUrl", "image_url", "imageUrl"),
    "cover_prompt": ("cover_prompt", "coverPrompt"),
    "cover_ratio": ("cover_ratio", "coverRatio", "aspect_ratio", "aspectRatio"),
    "cover_seed": ("cover_seed", "coverSeed", "seed"),
    "cover_extra": ("cover_extra", "coverExtra"),
    "cover_with_title": ("cover_with_title", "coverWithTitle", "with_title"),
    "generate_cover": ("generate_cover", "generateCover", "auto_cover", "autoCover"),
}
COVER_FIELD_NAMES = tuple(COVER_ALIASES)
_COVER_KNOWN_KEYS = {name for names in COVER_ALIASES.values() for name in names}
# Anything matching this that is NOT in _COVER_KNOWN_KEYS is a cover the caller
# meant to send under a name we don't know — that must be an error, not a shrug.
_COVERISH_RE = re.compile(r"cover|image|photo|thumb|picture", re.IGNORECASE)


def _reject_unknown_cover_keys(raw: object, request: Request | None = None) -> None:
    """400 on a cover-shaped field we do not recognise (see COVER_ALIASES)."""
    if not isinstance(raw, dict):
        return
    unknown = sorted(k for k in raw
                     if isinstance(k, str) and _COVERISH_RE.search(k) and k not in _COVER_KNOWN_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=pick("无法识别的封面字段: " + ", ".join(unknown)
                        + "。可接受: " + ", ".join(sorted(COVER_FIELD_NAMES))
                        + " —— 封面必须与 html 在同一次发布调用里提交。"
                        " / unrecognised cover field(s): " + ", ".join(unknown)
                        + ". Accepted: " + ", ".join(sorted(COVER_FIELD_NAMES))
                        + " — a cover must be sent in the SAME publish call as `html`.",
                        request_lang(request) if request else None),
        )


# ── Models ───────────────────────────────────────────────────────────────────

class ReportIn(BaseModel):
    slug: Optional[str] = None
    title: str
    summary: str = ""
    # A bilingual report carries one summary line per language. Send both; `summary` is
    # then optional and defaults to the English line (the card reads `summary` when a
    # report is single-language). See `_bilingual_summaries`.
    summary_en: str = ""
    summary_zh: str = ""
    category: str = "Ad-hoc"
    tags: str = ""
    author: str = "agent"
    submitter: str = Field(default="", validation_alias=AliasChoices(
        "submitter", "submitted_by", "submittedBy", "user_name", "userName", "sender"))
    status: str = "published"
    kind: str = ""
    pinned: bool = False
    accent: str = ""
    html: str
    # Cover: send bytes (cover_base64, data-URI or bare base64), an external
    # cover_url, or ask the server to generate one from the title.
    cover_base64: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_base64"]))
    cover_url: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_url"]))
    cover_prompt: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_prompt"]))
    generate_cover: bool = Field(default=False, validation_alias=AliasChoices(*COVER_ALIASES["generate_cover"]))
    cover_ratio: str = Field(default="16:9", validation_alias=AliasChoices(*COVER_ALIASES["cover_ratio"]))
    cover_with_title: bool = Field(default=False, validation_alias=AliasChoices(*COVER_ALIASES["cover_with_title"]))
    cover_extra: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_extra"]))
    cover_seed: Optional[int] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_seed"]))


class ReportPatch(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    summary_en: Optional[str] = None
    summary_zh: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[str] = None
    author: Optional[str] = None
    submitter: Optional[str] = Field(default=None, validation_alias=AliasChoices(
        "submitter", "submitted_by", "submittedBy", "user_name", "userName", "sender"))
    status: Optional[str] = None
    pinned: Optional[bool] = None
    accent: Optional[str] = None
    html: Optional[str] = None
    kind: Optional[str] = None
    cover_base64: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_base64"]))
    cover_url: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_url"]))  # "" clears
    cover_prompt: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_prompt"]))
    generate_cover: bool = Field(default=False, validation_alias=AliasChoices(*COVER_ALIASES["generate_cover"]))
    cover_ratio: str = Field(default="16:9", validation_alias=AliasChoices(*COVER_ALIASES["cover_ratio"]))
    cover_with_title: bool = Field(default=False, validation_alias=AliasChoices(*COVER_ALIASES["cover_with_title"]))
    cover_extra: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_extra"]))
    cover_seed: Optional[int] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_seed"]))


# ── Read ─────────────────────────────────────────────────────────────────────

# The three areas, as SQL predicates plus their parameter counts. Kept as data (not
# inlined into the handler) so a test can assert the boundaries — in particular that
# `mine` does NOT reach into the colleague-share table. Folding shared rows into
# "My workspace" is exactly the bug this replaced: it put other people's documents on
# a wall whose actions are rendered from ownership.
#
# Parameter order is the order of the `%s` placeholders: all three take the viewer's
# address (once, or twice for the two separate comparisons in `shared`).
_SCOPE_SQL: dict[str, tuple[str, int]] = {
    "mine": ("visibility = 'private' AND owner_email = %s", 1),
    "shared": ("visibility = 'private' AND status = 'published' AND owner_email <> %s "
               "AND EXISTS (SELECT 1 FROM ai_report_colleague_shares cs "
               "WHERE cs.report_id = ai_reports.id AND cs.recipient_email = %s)", 2),
    "public": ("visibility = 'public' AND status = 'published'", 0),
}


def _scope_predicate(scope: str, email: str) -> tuple[str, list[Any]]:
    sql, count = _SCOPE_SQL[scope]
    return sql, [email] * count


@router.get("")
@router.get("/")
@_with_schema
async def list_reports(
    request: Request,
    q: str = Query("", description="Substring search over title / summary / tags / slug"),
    status: str = Query("", description="published (default) | draft | archived | all"),
    scope: str = Query("mine", description="mine | public | shared"),
    category: str = Query("", description="Filter by category"),
    tag: str = Query("", description="Filter by a single tag"),
    project: str = Query("", description="Narrow to one project slug (项目 → 文件夹 → 报告)"),
    folder_id: Optional[int] = Query(None, description="Narrow to one folder id"),
    limit: int = Query(200, ge=1, le=500),
):
    """
    Report cards (metadata only — the HTML body is fetched on demand).

    Three disjoint areas, because they need three different sets of actions:

    * ``mine``   — rows I own. Only these can be managed (rename, share, publish,
      delete).
    * ``shared`` — a colleague's private report that was explicitly shared with my
      address. Read-only; the card offers "Pull to my workspace" instead of manage
      actions.
    * ``public`` — published, immutable snapshots, readable by any signed-in user.

    ⚠️ ``shared`` rows used to be folded into ``mine``. That made "My workspace" a mix
    of documents I own and documents other people own — with manage actions rendered
    from ``can_manage`` rather than from ownership of the list, so the wall advertised
    things the viewer could not actually change. The two are separate scopes now.
    """
    _ensure_table()
    email, _ = _identity(request)
    if scope not in _SCOPE_SQL:
        raise HTTPException(status_code=400, detail=pick("scope 只能是 mine、public 或 shared / scope must be mine, public or shared", request_lang(request) if request else None))
    scope_sql, scope_params = _scope_predicate(scope, email)
    where, params = [scope_sql], list(scope_params)
    want = (status or "").strip().lower()
    if scope == "mine" and want and want != "all":
        where.append("status = %s")
        params.append(_clean_status(want, request))
    if q.strip():
        where.append("(title ILIKE %s OR summary ILIKE %s OR summary_en ILIKE %s"
                     " OR summary_zh ILIKE %s OR tags ILIKE %s OR slug ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like] * 6
    if category.strip():
        where.append("category = %s")
        params.append(category.strip())
    if tag.strip():
        where.append("tags ILIKE %s")
        params.append(f"%{tag.strip()}%")
    # ── placement: 项目 → 文件夹 (2026-10-03) ────────────────────────────────
    #
    # ⚠️ **Both of these translate a container into a NULL test, and both
    # translations are load-bearing.**
    #
    # The unfiled project is a *real* project — it owns a folder, and the client
    # moves reports into it by slug — but an unfiled report carries
    # `project_slug IS NULL`. So `project = <unfiled slug>` has to become
    # `project_slug IS NULL`, and `folder_id = <the unfiled folder>` has to become
    # `folder_id IS NULL`. Filtering on the ids instead matches **nothing**, which
    # would show the wall's own catch-all as permanently blank — the one number a
    # reader is most likely to look at, wrong in the direction that reads as data
    # loss. `ensure_unfiled()` is idempotent and already runs on the wall load, so
    # calling it here costs one indexed SELECT.
    #
    # These are **read-side** filters and deliberately do not widen what is
    # readable: the scope clause above stays in force, so narrowing to a container
    # can never become a way to reach another owner's row.
    unfiled_project, unfiled_folder = report_projects.ensure_unfiled(email)
    if project.strip():
        if project.strip() == unfiled_project:
            where.append("project_slug IS NULL")
        else:
            where.append("project_slug = %s")
            params.append(project.strip())
    if folder_id is not None:
        if folder_id == unfiled_folder:
            where.append("folder_id IS NULL")
            where.append("project_slug IS NULL")
        else:
            folder = report_projects.get_folder(folder_id, email)
            if folder and folder.system:
                where.append("folder_id IS NULL")
                # A system folder means unfiled *within its project*.
                where.append("project_slug = %s")
                params.append(folder.project_slug)
            else:
                where.append("folder_id = %s")
                params.append(int(folder_id))
    sql = f"SELECT {_SELECT_COLS}, (SELECT p.slug FROM ai_reports p " \
          "WHERE p.visibility = 'public' AND p.source_report_id = ai_reports.id " \
          "LIMIT 1) AS public_slug FROM ai_reports"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pinned DESC, updated_at DESC LIMIT %s"
    params.append(limit)

    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            # Built from the same predicate as the list, with its own parameters —
            # not from `where[0]` plus a hand-counted slice of `params`, which
            # silently breaks the moment a scope is added.
            cur.execute("SELECT DISTINCT category FROM ai_reports WHERE " + scope_sql
                        + " AND category <> '' ORDER BY category", scope_params)
            categories = [r["category"] for r in cur.fetchall()]
    return {"reports": [_meta(r, email) for r in rows], "count": len(rows),
            "categories": categories, "scope": scope}


def _load(slug: str, include_html: bool = False, viewer_email: str = "", request: Request | None = None) -> Optional[dict]:
    """
    Load one report as a metadata payload (plus `html` when asked).

    ⚠️ Always call this with a real bool. A route's `include_html: bool =
    Query(False)` default is a `Query` *object* when the function is invoked
    directly from Python — and every object is truthy, so an internal
    `get_report(slug)` would quietly return the whole document.
    """
    _ensure_table()
    cols = _SELECT_COLS + (", html" if include_html else "")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {cols} FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    if viewer_email:
        _require_read(dict(row), viewer_email, request)
    payload = _meta(dict(row), viewer_email)
    if include_html:
        payload["html"] = row["html"]
    return payload


# ── projects and folders (2026-10-03) ───────────────────────────────────────
#
# ⚠️ **This block must stay ABOVE `@router.get("/{slug}")`.** FastAPI matches routes
# in registration order and `{slug}` matches any single path segment, so
# `GET /api/reports/projects` registered below it would be read as a report whose
# slug is the literal string "projects" — a 404 that names the wrong thing entirely,
# and a 404 on the very first request the new page makes. There is no
# "specific beats wildcard" resolution: the order below IS the routing table.
#
# 项目 (a card) → 文件夹 → 报告, with a 未归档 container at each level. Creating a
# project or a folder is an ordinary write on the account's own content, so these
# are open to an agent access code — that is the agent's entry point for "make me a
# folder called 渠道口径, with a cover". Moving a report, renaming and deleting are
# the same. The project cover reuses the report cover fields verbatim, so an agent
# that has learned `POST /api/reports/cover` can send the same body here.


class ProjectIn(BaseModel):
    """A project, and the cover a card wants.

    The cover fields are this module's own names and spellings, so
    `services/report_cover.py::resolve_cover()` takes this object unchanged — the
    same reuse the knowledge page makes (`routers/business_knowledge.py::ProjectIn`).
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
    """Where to file a report.

    ⚠️ **An empty ``project_slug`` means 未归档** — that is how a report is moved
    back out of a project, and it is the same spelling the stored NULL resolves to.
    The alternative (a separate `unfiled: true` flag) would let a body say both at
    once.
    """

    project_slug: str = ""
    folder_id: Optional[int] = None


def _project_not_found(lang: str | None = None) -> HTTPException:
    return HTTPException(status_code=404,
                         detail=pick("项目不存在 / report project not found", lang))


def _folder_not_found(lang: str | None = None) -> HTTPException:
    return HTTPException(status_code=404,
                         detail=pick("文件夹不存在 / report folder not found", lang))


def _container_error(exc: report_projects.ReportProjectError, lang: str | None) -> HTTPException:
    """A refused container write is a 400 with its own bilingual message."""
    return HTTPException(status_code=400, detail=pick(str(exc), lang))


def _cover_from_body(body, title: str, request: Request | None = None):
    """Resolve a submitted cover; 400 for bad input, 502 when the provider refuses.

    The decision itself lives in `report_cover.resolve_cover`, shared with the
    dashboards and the calendar; only the error mapping is ours.
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


def _shape_project(project: report_projects.ReportProject, email: str) -> dict:
    """A project as the card wall reads it.

    `can_manage` is **ownership**, not "the caller may see this" — unlike the
    knowledge page there is no folder grant, so the only projects in the response
    are the caller's own and the flag is constant. It is still sent explicitly
    rather than left for the client to infer, because the same key appears in the
    knowledge payload and a client that reads one shape should read both.
    """
    return {
        "slug": project.slug,
        "title": project.title,
        "summary": project.summary,
        "system": project.system,
        "has_cover": project.has_cover,
        # The same contract as `GET /api/reports/{slug}/cover`: bytes we serve, or a
        # 302 to an external URL. `?v=<updated_at>` is the client's cache-buster.
        "cover_url": (f"{_base_href()}api/reports/projects/{quote(project.slug, safe='')}/cover"
                      if project.has_cover else ""),
        "cover_w": project.cover_w,
        "cover_h": project.cover_h,
        "cover_prompt": project.cover_prompt,
        "folder_count": project.folder_count,
        "report_count": project.report_count,
        "can_manage": project.owner_email == email,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def _shape_folder(folder: report_projects.ReportFolder) -> dict:
    return {
        "id": folder.id,
        "project_slug": folder.project_slug,
        "title": folder.title,
        "system": folder.system,
        "sort_order": folder.sort_order,
        "report_count": folder.report_count,
        "can_manage": folder.can_manage,
    }


@router.get("/projects")
@_with_schema
async def list_projects(request: Request):
    """The project card wall: mine, with the unfiled project always first and present.

    It is always present because it is where an unfiled report is shown, so a wall
    without it would hide those reports entirely.
    """
    try:
        email, _ = _identity(request)
        projects = report_projects.list_projects(email)
    except report_projects.ReportProjectError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), request_lang(request))) from exc
    return {"projects": [_shape_project(p, email) for p in projects], "count": len(projects)}


@router.post("/projects", status_code=201)
@_with_schema
async def create_project(request: Request, payload: ProjectIn):
    """Create a project, its 未归档 folder, and — when asked — a cover from its name.

    **Open to an agent access code.** "Create a folder called X with a cover that
    looks like X" is a write on this account's own content, the same category as
    `POST /api/reports`.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        cover = _cover_from_body(payload, payload.title, request)
        project = report_projects.create_project(email, payload.title,
                                                 summary=payload.summary, cover=cover)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return _shape_project(project, email)


@router.get("/projects/{slug}")
@_with_schema
async def get_project(slug: str, request: Request):
    lang = request_lang(request) if request else None
    email, _ = _identity(request)
    try:
        project = report_projects.get_project(slug, email)
    except report_projects.ReportProjectError as exc:
        raise HTTPException(status_code=503, detail=pick(str(exc), lang)) from exc
    if not project:
        # 404, not 403: a stranger must not learn that a project with this slug exists.
        raise _project_not_found(lang)
    return _shape_project(project, email)


@router.put("/projects/{slug}")
@_with_schema
async def update_project(slug: str, request: Request, payload: ProjectPatch):
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        project = report_projects.update_project(slug, email, title=payload.title,
                                                 summary=payload.summary)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return _shape_project(project, email)


@router.delete("/projects/{slug}")
@_with_schema
async def delete_project(slug: str, request: Request):
    """Delete a project. Its reports fall back to 未归档 — a container is a filing
    decision, the reports are the work, and deleting a folder must never be a way
    to lose a report."""
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        moved = report_projects.delete_project(slug, email)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return {"deleted": slug, "moved_to_unfiled": moved, "count": len(moved)}


# ── folders ──────────────────────────────────────────────────────────────────

@router.get("/projects/{slug}/folders")
@_with_schema
async def list_folders(slug: str, request: Request):
    """The folders in one project the caller owns."""
    lang = request_lang(request) if request else None
    email, _ = _identity(request)
    if not report_projects.get_project(slug, email):
        raise _project_not_found(lang)
    folders = report_projects.list_folders(slug, email)
    return {"folders": [_shape_folder(f) for f in folders], "count": len(folders)}


@router.post("/projects/{slug}/folders", status_code=201)
@_with_schema
async def create_folder(slug: str, request: Request, payload: FolderIn):
    """Create a folder inside a project the caller owns. **Open to an agent.**"""
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        folder = report_projects.create_folder(slug, email, payload.title)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return _shape_folder(folder)


@router.put("/folders/{folder_id}")
@_with_schema
async def rename_folder(folder_id: int, request: Request, payload: FolderIn):
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        folder = report_projects.rename_folder(folder_id, email, payload.title)
    except PermissionError as exc:
        raise _folder_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return _shape_folder(folder)


@router.delete("/folders/{folder_id}")
@_with_schema
async def delete_folder(folder_id: int, request: Request):
    """Delete a folder; its reports fall back to 未归档 **within the same project**,
    and the project itself is kept — "I deleted a folder" must not also mean "I
    moved the file out of the project"."""
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        moved = report_projects.delete_folder(folder_id, email)
    except PermissionError as exc:
        raise _folder_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return {"deleted": folder_id, "moved_to_unfiled": moved, "count": len(moved)}


# ── moving a report ──────────────────────────────────────────────────────────

@router.post("/{slug}/move")
@_with_schema
async def move_report(slug: str, request: Request, payload: MoveIn):
    """File a report under a project + folder, or back into 未归档.

    Ownership is checked on both the report and the target folder, server-side — a
    menu that filtered the target list in the browser would still be a request the
    browser can be made to send.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        moved = report_projects.move_report(slug, email, payload.project_slug, payload.folder_id)
    except PermissionError as exc:
        raise HTTPException(status_code=404,
                            detail=pick("报告不存在 / report not found", lang)) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return moved


# ── the project cover, reusing the Workspace mechanism ───────────────────────

@router.get("/projects/{slug}/cover")
@_with_schema
async def project_cover(slug: str, request: Request):
    """The project's cover: served bytes, a 302 to an external URL, or 404.

    The same contract as `GET /api/reports/{slug}/cover`, so the card wall shares
    the client-side `<img>` logic and the `?v=<updated_at>` cache-buster.
    """
    email, _ = _identity(request)
    try:
        data, mime, external = report_projects.cover_bytes(slug, email)
    except report_projects.ReportProjectError as exc:
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
@_with_schema
async def set_project_cover(slug: str, request: Request, payload: ProjectIn):
    """Generate a project's cover from its name — or store one the owner supplied.

    A dedicated endpoint rather than part of `PUT /projects/{slug}`: the project
    PUT is a whole-row overwrite, so a cover-only save would blank the title.
    """
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        project = report_projects.get_project(slug, email)
        if not project:
            raise _project_not_found(lang)
        cover = _cover_from_body(payload, payload.title or project.title, request)
        project = report_projects.set_cover(slug, email, cover)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return _shape_project(project, email)


@router.delete("/projects/{slug}/cover")
@_with_schema
async def clear_project_cover(slug: str, request: Request):
    lang = request_lang(request) if request else None
    try:
        email, _ = _identity(request)
        project = report_projects.set_cover(slug, email, None, clear=True)
    except PermissionError as exc:
        raise _project_not_found(lang) from exc
    except report_projects.ReportProjectError as exc:
        raise _container_error(exc, lang) from exc
    return _shape_project(project, email)


@router.get("/{slug}")
@_with_schema
async def get_report(slug: str, request: Request, include_html: bool = Query(False)):
    """Report metadata; pass `include_html=true` to also get the raw document."""
    return _load(slug, include_html, _identity(request)[0], request)


@router.get("/{slug}/pptx")
@_with_schema
async def export_report_pptx(slug: str, request: Request, lang: str = ""):
    """Export a fixed-canvas Report Deck as editable PowerPoint objects."""
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, html, state_json, owner_email, visibility, status, kind, filters_json "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row or not _may_read({"id": row[0], "owner_email": row[3], "visibility": row[4], "status": row[5]},
                                _identity(request)[0]):
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    if BeautifulSoup(row[1] or "", "html.parser").select_one(".slide") is None:
        raise HTTPException(status_code=422, detail=pick("只有 16:9 的 Report Deck 才能导出为可编辑 PPTX / Only 16:9 Report Decks can be exported to editable PPTX", request_lang(request) if request else None))
    available = detect_langs(row[1] or "")
    if lang and (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,9}", lang) or (available and lang not in available)):
        raise HTTPException(status_code=400, detail=pick("该报告没有这个语言版本 / language is not available in this report", request_lang(request) if request else None))
    url = "http://127.0.0.1:8000" + _base_href() + "r/" + quote(slug)
    if lang:
        url += "?lang=" + quote(lang)
    try:
        # Render the saved annotation snapshot alongside the HTML. The report's
        # own script reads /state; the isolated exporter serves this exact value
        # to that request so the PPTX reflects what readers currently see.
        snapshots = {}
        data = await report_pptx.render_report_to_pptx(
            url, lang, state_json=row[2] or "{}", data_snapshots=snapshots,
            asset_auth_headers={key: value for key, value in (
                ("cookie", request.headers.get("cookie")),
                ("authorization", request.headers.get("authorization")),
            ) if value},
            html_body=_export_body(
                {"id": row[0], "html": row[1], "kind": row[6], "filters_json": row[7]},
                slug, _identity(request)[0],
                public_interactive=(row[4] == "public" and row[6] == "interactive")),
        )
    except report_pptx.UnsupportedReport as exc:
        raise HTTPException(status_code=422, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    return Response(
        content=data,
        media_type=report_pptx.PPTX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{slug}{("-" + lang) if lang else ""}.pptx"',
                 "Cache-Control": "no-store"},
    )


@router.get("/{slug}/pdf")
@router.get("/{slug}/picture")
@_with_schema
async def export_report_visual(slug: str, request: Request, lang: str = ""):
    """Export browser-painted pages, retaining every colour, font and pixel."""
    kind = "pdf" if request.url.path.rstrip("/").endswith("/pdf") else "picture"
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, html, state_json, owner_email, visibility, status, kind, filters_json, doc_type "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row or not _may_read(row, _identity(request)[0]):
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    if row["doc_type"]:
        # A document card is already a file: export it as itself, not as pictures of a page.
        raise HTTPException(status_code=422, detail=pick("这张卡片是文档 —— 请用它的下载链接 / this card is a document — use its download link", request_lang(request) if request else None))
    available = detect_langs(row["html"] or "")
    if lang and (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,9}", lang)
                 or (available and lang not in available)):
        raise HTTPException(status_code=400, detail=pick("该报告没有这个语言版本 / language is not available in this report", request_lang(request) if request else None))
    url = "http://127.0.0.1:8000" + _base_href() + "r/" + quote(slug)
    if lang:
        url += "?lang=" + quote(lang)
    try:
        snapshots = {}
        data, media_type, extension = await report_visual_export.render_report_visual(
            url, _export_body(
                {"id": row["id"], "html": row["html"], "kind": row["kind"],
                 "filters_json": row["filters_json"]},
                slug, _identity(request)[0],
                public_interactive=(row["visibility"] == "public" and row["kind"] == "interactive")),
            row["state_json"] or "{}", kind, data_snapshots=snapshots,
            asset_auth_headers={key: value for key, value in (
                ("cookie", request.headers.get("cookie")),
                ("authorization", request.headers.get("authorization")),
            ) if value},
        )
    except report_visual_export.VisualExportError as exc:
        raise HTTPException(status_code=422, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    filename = slug + ("-" + lang if lang else "") + "." + extension
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "private, no-store"})


# ── Deck runtime (16:9 paged reports) ────────────────────────────────────────
#
# A report that uses `.deck` / `.slide` becomes a 16:9 paged presentation (see
# knowledge doc `klado-v2:format-report-deck`). The runtime is INLINED into the served
# document rather than fetched from /vendor, for three reasons:
#   * the same document then works on any origin — including the case that bit us
#     before, where the local preview loads the report from test while the
#     surrounding app comes from localhost;
#   * a downloaded .html keeps working offline;
#   * one place to fix, so upgrading the runtime upgrades every report.
# Workspace documents may still link /vendor/report-deck.{css,js} while authoring; those tags
# are stripped and replaced here, and the runtime is idempotent either way.

_DECK_HINT_RE = re.compile(r'class\s*=\s*["\'][^"\']*\b(?:deck|slide)\b'
                                r'|data-lang\s*=', re.IGNORECASE)
_DECK_ASSET_RE = re.compile(
    r'<link[^>]+href\s*=\s*["\'][^"\']*report-deck\.css[^"\']*["\'][^>]*>'
    r'|<script[^>]+src\s*=\s*["\'][^"\']*report-deck\.js[^"\']*["\'][^>]*>\s*</script\s*>',
    re.IGNORECASE,
)
_DECK_INLINE_ASSET_RE = re.compile(
    r'<style\b[^>]*\bdata-report-deck(?=[\s=>])[^>]*>[\s\S]*?</style\s*>'
    r'|<script\b[^>]*\bdata-report-deck(?=[\s=>])[^>]*>[\s\S]*?</script\s*>',
    re.IGNORECASE,
)


# How "is this a deck?" is decided. Deliberately the SAME test as the PPTX export gate
# below (`.slide`), so `format: "deck"` and "exportable to editable PPTX" never disagree.
def _detect_format(html: str) -> str:
    """`deck` when the document pages itself, else `long-form`."""
    if not html:
        return "long-form"
    return "deck" if BeautifulSoup(html, "html.parser").select_one(".slide") is not None else "long-form"


def _stored_html(slug: str) -> str:
    """Just the document body — the PUT response reports its format without echoing 8MB."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT html FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    return (row[0] if row else "") or ""


def _annotate_format(meta: dict, html: Optional[str]) -> dict:
    """
    Say which format was published, and say so loudly when it is not a deck.

    The rule (2026-09-27, user requirement) is that every published report pages itself as
    16:9 — long-form is now the exception, not the default. This is a response field
    rather than a 400 deliberately: agents already installed keep publishing while the
    spec reaches them (their SKILL.md is a static package), and a flagged report is still
    readable. Flip it to a hard rejection once the installed skill is known to be current.
    """
    fmt = _detect_format(html or "")
    meta["format"] = fmt
    if fmt != "deck":
        meta["format_warning"] = (
            "长文报告：项目规范要求所有发布的报告都是 16:9 分页 deck"
            "（`<div class=\"deck\">` + 每页一个 `<section class=\"slide\">`），"
            "见知识库 klado-v2:format-report-html §1.3。请按规范重发；"
            "确需长文时请在交付说明里写明原因。")
    return meta

_deck_asset_cache: dict[str, Optional[str]] = {}


def _deck_asset(filename: str) -> Optional[str]:
    """Read a deck runtime file once per process; None when it is not shipped."""
    if filename not in _deck_asset_cache:
        path = os.path.join(resolve_frontend_dir(), "vendor", filename)
        try:
            with open(path, encoding="utf-8") as handle:
                _deck_asset_cache[filename] = handle.read()
        except OSError as exc:
            _LOG.warning("report deck runtime unavailable (%s): %s", path, exc)
            _deck_asset_cache[filename] = None
    return _deck_asset_cache[filename]


_LANG_RE = re.compile(r'data-lang\s*=\s*["\']([A-Za-z][A-Za-z0-9_-]{0,9})["\']')


def detect_langs(html: str) -> list[str]:
    """
    Languages a report declares, in document order.

    The card needs this to decide whether clicking should ASK for a language —
    asking about a single-language report is pure friction. Base codes only, so
    `zh-CN` and `zh` count as the same language.
    """
    seen: list[str] = []
    for raw in _LANG_RE.findall(html or ""):
        code = raw.strip().lower().split("-")[0].split("_")[0]
        if code and code not in seen:
            seen.append(code)
    return seen


_BILINGUAL_PAIR = ("en", "zh")


def is_bilingual(langs) -> bool:
    """A report that offers both English and Chinese (order irrelevant)."""
    codes = [str(code).strip().lower().split("-")[0] for code in (langs or []) if str(code).strip()]
    return all(code in codes for code in _BILINGUAL_PAIR)


def bilingual_summaries(plain: str, english: str, chinese: str, langs, request: Request | None = None) -> tuple[str, str, str]:
    """Resolve the card's summary from the plain field and the per-language pair.

    ⚠️ **A bilingual report must carry a summary in both of its languages.** The card veil
    shows the two lines; `summary` alone would hand a Chinese reader an English sentence (or
    the reverse) for a document that is otherwise careful to offer both — and nothing would
    look wrong. Returns ``(summary, summary_en, summary_zh)``.

    `summary` still exists and still has to be non-empty for single-language reports: it is
    what existing callers send, and the card falls back to it. For a bilingual report it
    defaults to the English line, so an old caller that sends only `summary` fails loudly
    rather than publishing a half-translated card.
    """
    plain = (plain or "").strip()
    english = (english or "").strip()
    chinese = (chinese or "").strip()
    if is_bilingual(langs) and not (english and chinese):
        missing = [name for name, value in (("summary_en", english), ("summary_zh", chinese)) if not value]
        raise HTTPException(status_code=400, detail=pick("这份报告是双语的（" + ", ".join(langs or []) + "）：必须同时携带两种语言的摘要。请在 html 之外一并提交 summary_en 与 summary_zh（各一行）。缺失: " + ", ".join(missing) + "。对双语报告只有 summary 不够。 / this report is bilingual (" + ", ".join(langs or []) + "): it must carry a summary in both languages. Send `summary_en` and `summary_zh` — one line each — alongside `html`. Missing: " + ", ".join(missing) + ". `summary` alone is not enough for a bilingual document.", request_lang(request) if request else None))
    return (plain or english or chinese), english, chinese


# The rule is shared with the annotation runtime — see `core/htmlkit.py`.
_insert_before_tag = htmlkit.insert_before_tag


def _inline_deck_runtime(html: str) -> str:
    """
    Make a deck report self-contained. No-op for ordinary documents, so a plain
    long-form report is served byte-for-byte as published.
    """
    if not html or not _DECK_HINT_RE.search(html):
        return html
    css = _deck_asset("report-deck.css")
    js = _deck_asset("report-deck.js")
    if not css or not js:
        # Runtime missing: the report still renders, just as an ordinary page.
        return html
    # A downloaded, self-contained report may be published again.  Remove its
    # older inline runtime before installing the current one; otherwise its
    # __reportDeckLoaded guard wins and new model bindings never initialize.
    # Do not match data-report-deck-model (the authored JSON data source).
    html = _DECK_INLINE_ASSET_RE.sub("", _DECK_ASSET_RE.sub("", html))
    html = _insert_before_tag(html, "</head>", f"<style data-report-deck>\n{css}\n</style>")
    return _insert_before_tag(html, "</body>", f"<script data-report-deck>\n{js}\n</script>")


# ── Dynamic reports: the slice runtime ───────────────────────────────────────
#
# A dynamic report states which slice each block belongs to
# (`data-filter-when="period=2026-06&metric=gto"`); the reader picks values in the SPA
# viewer's sidebar, and the runtime here shows the matching blocks. Three properties are
# deliberate and must survive any future edit:
#
#   * **The document gets no control.** The runtime only REACTS. It adds no button, no
#     select, no badge — "用户不可以在页面里面修改" is a product rule, not a styling
#     accident (and it is what keeps `_report_kind` from reading the page as interactive).
#   * **The selection travels with the render, not with the document.** `/r/{slug}` is
#     rendered per reader with THEIR saved selection already in place, so there is no
#     flash of the default view and no race with the frame's load event.
#   * **The runtime is inert data + a hide/show pass.** No network, no storage.

_FILTER_HINT_RE = re.compile(r"data-filter-when\s*=", re.IGNORECASE)
_FILTER_STATE_SCRIPT_RE = re.compile(
    r"""<script\b[^>]*\bdata-report-filters-state\b[^>]*>[\s\S]*?</script\s*>""", re.IGNORECASE)
_FILTER_ASSET_RE = re.compile(
    r"""<style\b[^>]*\bdata-report-filters(?=[\s=>])[^>]*>[\s\S]*?</style\s*>"""
    r"""|<script\b[^>]*\bdata-report-filters(?=[\s=>])[^>]*>[\s\S]*?</script\s*>""", re.IGNORECASE)
_FILTER_LINK_RE = re.compile(
    r'<link[^>]+href\s*=\s*["\'][^"\']*report-filters\.css[^"\']*["\'][^>]*>'
    r'|<script[^>]+src\s*=\s*["\'][^"\']*report-filters\.js[^"\']*["\'][^>]*>\s*</script\s*>',
    re.IGNORECASE)


def _filter_state_for(row: dict, viewer_email: str = "") -> tuple[dict, dict]:
    """`(schema, effective selection)` for one reader of one dynamic report.

    ⚠️ A report with no schema does NOT touch the database: only a dynamic report has a
    per-reader selection, and every static / interactive / document render would otherwise
    pay for a second query that can only return `{}`.
    """
    schema = load_filter_schema(row)
    if not schema.get("filters"):
        return schema, {}
    saved: dict = {}
    if viewer_email and row.get("id"):
        with _db() as conn:
            with conn.cursor() as cur:
                saved, _ = doc_state.saved_selection(
                    cur, "ai_report_filter_selections", row["id"], viewer_email)
    effective, _dropped = effective_selection(schema, saved)
    return schema, effective


def _inline_filter_runtime(html: str, slug: str, schema: dict, selection: dict,
                           read_only: bool = False) -> str:
    """
    Bind a dynamic document to its schema + this reader's selection.

    Injected BEFORE the deck runtime (the deck reads visibility when it counts pages), so
    a hidden slide never appears in the page counter. No-op for a document with neither a
    schema nor a `data-filter-when` hint — an ordinary report is still served unchanged.
    """
    if not html:
        return html
    if not (schema or {}).get("filters") and not _FILTER_HINT_RE.search(html):
        return html
    css = _deck_asset("report-filters.css")
    js = _deck_asset("report-filters.js")
    # A downloaded, self-contained report may be published again: drop the old inline
    # copy (its `__reportFiltersLoaded` guard would otherwise win) and any author-time
    # /vendor reference, exactly as the deck runtime does.
    html = _FILTER_LINK_RE.sub("", _FILTER_ASSET_RE.sub("", _FILTER_STATE_SCRIPT_RE.sub("", html)))
    state = {"slug": slug, "readOnly": bool(read_only),
             "schema": schema or {"version": 1, "filters": []},
             "selection": selection or {}}
    payload = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    state_script = f"<script data-report-filters-state>window.__reportFilters = {payload};</script>"
    if css:
        html = _insert_before_tag(html, "</head>", f"<style data-report-filters>\n{css}\n</style>")
    runtime = f"\n<script data-report-filters>\n{js}\n</script>" if js else ""
    return _insert_before_tag(html, "</body>", state_script + runtime)


def _export_body(row: dict, slug: str, viewer_email: str, *, public_interactive: bool) -> str:
    """
    The HTML an exporter paints: the stored document plus every injected runtime.

    ⚠️ A dynamic report MUST be exported through the slice runtime. The exporter works
    from the stored HTML, not from `/r/{slug}`, so without this every slice would be
    painted on top of the others — an export that looks like a layout bug and is really a
    missing runtime. The requesting reader's saved selection is what they are looking at.
    """
    schema, selection = _filter_state_for(row, viewer_email)
    body = _inject_base_tag(row.get("html") or "", _base_href())
    body = _inject_report_context(body, slug, public_interactive)
    body = _inline_filter_runtime(body, slug, schema, selection)
    return _inline_deck_runtime(body)


# ── Standalone HTML ──────────────────────────────────────────────────────────

def _inline_annotation_layer(html: str, row: dict, *, viewer_email: str = "",
                             read_url: str = "", writable: bool = True) -> str:
    """
    Give a STATIC report its note layer (right-click → a note at the click point).

    Interactive reports are skipped on purpose: they already own the right-click
    gesture for their own cell markup, and two menus on one click is worse than
    either. The `/s/{token}` reader gets the read-only variant, pointed at a
    token-scoped GET, because that document has no session and the `/api/`
    endpoint would answer 401 to it.
    """
    if (row.get("kind") or "static") != "static":
        return html
    return annotations.inline_runtime(html, asset_type="report", slug=row.get("slug") or "",
                                      viewer_email=viewer_email, writable=writable, read_url=read_url)


@router.get("/{slug}/raw", response_class=HTMLResponse)
@_with_schema
async def render_report(slug: str, request: Request, download: bool = Query(False),
                        embed: bool = Query(False)):
    """
    The report as a **standalone page** — this is what the card click loads in
    the viewer iframe (and what a shared permalink opens).

    ⚠️ `embed=1` is passed only by the app's own iframe. It tells a DOCUMENT card to drop
    the page header (title / meta / download) because the Workspace toolbar above the
    frame already shows all three — see `_document_page_html`. A link that leaves the app
    does not carry the flag, so a colleague still gets the full page. It is inert for a
    report: a report's body is the deck, and the deck owns its own on-screen title.

    A `<base href>` is injected so relative asset/API paths resolve inside the
    app mount point in every environment.
    """
    _ensure_table()
    viewer = _identity(request)[0]
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ⚠️ `size_bytes` and `doc_pages` are part of the document reader page (its meta
            # line reads "PowerPoint · file.pptx · 34.0 KB · 7 pages"). Omitting them here did
            # not fail anything — `row.get(...)` is `None`, `_human_size(None)` is "0 B", and
            # every document's reader page quietly claimed a zero-byte file. Measured
            # 2026-10-01 on a real upload.
            # ⚠️⚠️ `slug` itself has to be selected: this query FILTERS by a slug but does not
            # return it, and `_document_page_response` builds the document's own addresses from
            # `row["slug"]`. Without it the reader page emitted `api/reports//document` — an empty
            # slug — so EVERY document preview 404'd into the iframe ("{"detail":"Not Found"}")
            # and every download saved that JSON error body as `document.json` (reported
            # 2026-10-01, found by loading a real card in a browser rather than by trusting the
            # markup). Same shape as the `size_bytes` / `doc_pages` omission below.
            cur.execute("SELECT id, slug, html, title, status, owner_email, visibility, kind, filters_json, "
                        "doc_type, doc_object, doc_mime, doc_name, doc_pages, size_bytes "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
            if row and _may_read(row, viewer):
                cur.execute("UPDATE ai_reports SET views = views + 1 WHERE slug = %s", (slug,))
            else:
                row = None
    if not row:
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))

    # A document card has no HTML to serve: its address is the same stable permalink
    # (`/r/{slug}`), which is what a shared link and the viewer iframe both load.
    if (row.get("doc_type") or ""):
        return _document_page_response(row, request, download=download, embed=embed)

    schema, selection = _filter_state_for(row, viewer)
    body = _inject_base_tag(row["html"] or "", _base_href())
    body = _inject_report_context(
        body, slug, row["kind"] == "interactive" and
        (row["visibility"] == "public" or row["owner_email"] != viewer))
    # Before the deck runtime: the deck counts only VISIBLE pages, so a hidden slice must
    # already be marked when it builds its footers.
    body = _inline_filter_runtime(body, slug, schema, selection)
    body = _inline_deck_runtime(body)
    body = _inline_annotation_layer(body, row, viewer_email=viewer)
    headers = {
        # Workspace documents are re-published under a stable slug, so never let a stale copy
        # linger in the browser or an intermediate cache.
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if download:
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", slug) or "report"
        headers["Content-Disposition"] = f'attachment; filename="{filename}.html"'
    return HTMLResponse(content=body, headers=headers)


async def render_anyone_link(token: str, request: Request):
    """Serve only the report named by this bearer link, with no account session."""
    row = _resolve_anyone_link(token, request)
    if (row.get("doc_type") or ""):
        # A guest gets the bytes through the token-scoped route, never through `/api/`
        # (which would answer 401 to a browser with no session).
        return _document_page_response(row, request, guest_token=token)
    schema = load_filter_schema(row)
    # No account means no saved selection: the schema's defaults are the only honest view.
    selection, _dropped = effective_selection(schema, {})
    body = _inline_filter_runtime(
        _inject_base_tag(_rewrite_guest_images(row["html"] or "", token), _base_href()), row["slug"],
        schema, selection, read_only=True)
    body = _inline_deck_runtime(_inject_report_context(
        body, row["slug"], row["kind"] == "interactive",
        guest_state_url=_base_href() + "s/" + token + "/state"))
    # Read-only: the guest may see the notes already there, but writing one needs
    # an account (the Inbox attributes a note to a person).
    body = _inline_annotation_layer(
        body, row, read_url=_base_href() + "s/" + token + "/annotations", writable=False)
    return HTMLResponse(content=body, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


async def anyone_link_annotations(token: str, request: Request | None = None):
    """
    The notes on a shared document, for a reader with no account.

    GET only, like `/s/{token}/state`: the token is the capability and it
    authenticates nothing else. Guests see what colleagues wrote; they cannot add
    to it, so this route has no POST sibling anywhere in the app.
    """
    row = _resolve_anyone_link(token, request)
    try:
        notes = annotations.list_notes_for_token(row["slug"])
    except annotations.AnnotationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=pick(exc.detail, request_lang(request) if request else None)) from exc
    return Response(content=json.dumps({"notes": notes}, ensure_ascii=False),
                    media_type="application/json",
                    headers={"Cache-Control": "private, no-store",
                             "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer"})


async def anyone_link_state(token: str, request: Request | None = None):
    """
    The saved interactive state, for a reader with no account.

    One of the two dynamic endpoints a `/s/{token}` link can reach (the other is
    `/s/{token}/annotations`); both are GET only. The token authenticates this
    report and nothing else.
    """
    row = _resolve_anyone_link(token, request)
    state = _rewrite_guest_images((row["state_json"] or "").strip() or "{}", token)
    return Response(content=state,
                    media_type="application/json",
                    headers={"Cache-Control": "private, no-store",
                             "X-Content-Type-Options": "nosniff"})


async def anyone_link_asset(token: str, path: str, w: int | None = None, request: Request | None = None):
    """Serve only report-referenced image keys, never general OSS documents."""
    row = _resolve_anyone_link(token, request)
    from routers import storage
    if path not in _guest_image_keys(row) or not storage._is_image_key(path):
        raise HTTPException(status_code=404, detail=pick("分享图片不存在 / shared image not found", request_lang(request) if request else None))
    if w is not None and not 16 <= w <= 2048:
        raise HTTPException(status_code=400, detail=pick("图片宽度参数无效 / invalid image width", request_lang(request) if request else None))
    response = await storage.serve_file(path=path, download=False, w=w, request=request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/{slug}/cover")
@_with_schema
async def report_cover_image(slug: str, request: Request):
    """
    Serve the stored cover image; 302 to the external URL when the publisher
    supplied one instead of bytes.
    """
    _ensure_table()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, cover_data, cover_mime, cover_url, owner_email, visibility, status "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row or not _may_read({"id": row[0], "owner_email": row[4], "visibility": row[5], "status": row[6]},
                                _identity(request)[0]):
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    data, mime, external = row[1:4]
    if not data:
        if external:
            return RedirectResponse(url=external, status_code=302)
        raise HTTPException(status_code=404, detail=pick("该报告没有封面 / report has no cover", request_lang(request) if request else None))
    return Response(
        content=bytes(data),
        media_type=mime or "image/jpeg",
        # Recoverable after a re-publish (the card cache-busts with ?v=<updated_at>).
        headers={"Cache-Control": "private, no-store"},
    )


# ── Per-report annotation state ──────────────────────────────────────────────
#
# An INTERACTIVE report (a grid you right-click to mark up, callout colours, a
# description per cell) keeps that state here instead of in the reader's
# localStorage. The owner's private copy and each pulled copy have independent
# state. Everyone signed in can read a public snapshot, but only the owner of a
# private copy can write its state. The document uses the browser's session cookie.
#
# The state is a BLACK BOX: we never look inside it — no schema, no per-field
# validation, no merging. It is a client-owned JSON document that happens to live on
# our disk. Two consequences worth naming: a save is a whole-document overwrite (two
# readers saving at once = last write wins), and there is no version history.

@router.get("/{slug}/state")
# Trailing-slash alias, like the collection root above: this app answers a sub-route
# with a 404 (not a 307) when a trailing slash is present, and `…/state/` is an easy
# thing to write in a template literal — a silent 404 there reads as "my save vanished".
@router.get("/{slug}/state/", include_in_schema=False)
@_with_schema
async def get_report_state(slug: str, request: Request):
    """
    The stored annotation state of one report, as `application/json`.

    `{}` when nothing has ever been saved — so a reader can always `await r.json()` and
    get an object, with no "is it a 404 or an empty state?" branch in the document.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    _ensure_table()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, state_json, owner_email, visibility, status "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row or not _may_read({"id": row[0], "owner_email": row[2], "visibility": row[3], "status": row[4]},
                                _identity(request)[0]):
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    stored = row[1] or ""
    return Response(
        content=stored.strip() or "{}",
        media_type="application/json",
        # no-store, not no-cache: a stale copy makes freshly-added annotations look
        # like they were lost, which is exactly the failure this feature removes.
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/{slug}/state")
@router.post("/{slug}/state/", include_in_schema=False)   # see the GET alias above
@_with_schema
async def save_report_state(slug: str, request: Request):
    """
    Overwrite one report's annotation state with the request body, verbatim.

    Reads the raw body rather than a Pydantic model: the payload is client-owned and
    must survive the round-trip unchanged (`Any` in a model would still be re-dumped).

    Does NOT touch `updated_at` / the cover / `html` — saving an annotation is not a
    re-publication, and the card wall sorts by `updated_at DESC`.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    email, kind = _identity(request)
    if kind == "agent" and settings.AUTH_ENABLED:
        raise HTTPException(status_code=403, detail=pick("交互编辑需要浏览器会话 / interactive edits require a browser session", request_lang(request) if request else None))
    text = state_text(await request.body(), request)
    _ensure_table()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ai_reports SET state_json = %s, state_updated_at = NOW() "
                "WHERE slug = %s AND owner_email = %s AND visibility = 'private' "
                "RETURNING state_updated_at",
                (text, slug, email),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=403, detail=pick("请先把这份公共报告拉取到自己的工作区再编辑 / pull this public report into your workspace to edit it", request_lang(request) if request else None))
    return {"ok": True, "slug": slug, "updated_at": row[0].isoformat()}


# ── Dynamic reports: the sidebar's two halves ────────────────────────────────
#
#   GET/PUT/DELETE  /api/reports/{slug}/filters            — the agent's schema (one per report)
#   PUT             /api/reports/{slug}/filters/selection  — this reader's view (one per reader)
#
# The schema is written by the AGENT (Feishu 侧) with its normal report credentials, so
# `PUT` is deliberately not browser-only: `/api/reports` is already inside the machine
# caller's write surface. The SELECTION is the opposite — it is one person's view of a
# shared document, so it requires a browser session; otherwise an agent could silently
# move a colleague's quarterly view without ever opening the report.

class FilterSchemaIn(BaseModel):
    version: int = 1
    filters: list[Any] = []


class FilterSelectionIn(BaseModel):
    selection: dict[str, Any] = {}


@router.get("/{slug}/filters")
@_with_schema
async def get_report_filters(slug: str, request: Request):
    """
    The filter sidebar's whole state for THIS reader: schema + their saved selection.

    Readable by anyone who may read the report (owner, a colleague it was shared with, a
    reader of its public snapshot) — the sidebar is part of reading a dynamic report.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    email = _identity(request)[0]
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, kind, title, owner_email, visibility, status, filters_json, "
                        "updated_at FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
            _require_read(row, email, request)
            saved, saved_at = doc_state.saved_selection(
                cur, "ai_report_filter_selections", row["id"], email)
    schema = load_filter_schema(row)
    effective, dropped = effective_selection(schema, saved)
    return {
        "slug": slug,
        "kind": row["kind"],
        "configured": bool(schema.get("filters")),
        "schema": schema,
        # What the reader last chose (pruned against the current schema), and what it
        # resolves to. `dropped` names the filters whose stored value no longer applies —
        # the sidebar can say "this view was adjusted" instead of silently changing it.
        "selection": saved,
        "effective": effective,
        "dropped": dropped,
        # Whether THIS caller may rewrite the schema. The rail never offers it (that is the
        # product rule); it is reported so an agent can tell "I am the owner" from "I am
        # looking at someone else's dynamic report".
        "can_edit": bool(row["owner_email"] == email and row["visibility"] == "private"),
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "selection_updated_at": saved_at,
    }


@router.put("/{slug}/filters")
@_with_schema
async def set_report_filters(slug: str, body: FilterSchemaIn, request: Request):
    """
    Define (or replace) a dynamic report's filters — the agent-facing half.

    Setting a schema on a report that was published as `static` promotes it to `dynamic`,
    because that is what the author meant: the schema and the document's
    `data-filter-when` blocks are two halves of one decision. Sending an empty list
    removes every filter and leaves the card static again.

    The row must be the caller's PRIVATE original — a public snapshot is other people's
    copy, and editing behind their back would silently change what they read.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    email, _kind = _identity(request)
    schema = clean_filter_schema(body.model_dump(), request)
    stored = json.dumps(schema, ensure_ascii=False) if schema["filters"] else ""
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, kind, html, doc_type, owner_email, visibility, filters_json "
                        "FROM ai_reports WHERE slug = %s FOR UPDATE", (slug,))
            row = _require_owner(cur.fetchone(), email, request, private=True)
            if (row.get("doc_type") or ""):
                raise HTTPException(status_code=400, detail=pick("筛选器属于 HTML 报告，不适用于文档卡片 / filters belong to an HTML report, not a document card", request_lang(request) if request else None))
            # Re-derive rather than assume: with an empty schema a report that still carries
            # `data-filter-when` blocks stays dynamic (its slices just all render at once).
            kind = "dynamic" if schema["filters"] else _report_kind(row["html"] or "", request=request)
            cur.execute("UPDATE ai_reports SET filters_json = %s, kind = %s WHERE id = %s",
                        (stored, kind, row["id"]))
    _LOG.info("report filters set slug=%s filters=%d kind=%s", slug, len(schema["filters"]), kind)
    return {"ok": True, "slug": slug, "kind": kind, "configured": bool(schema["filters"]),
            "filter_count": len(schema["filters"]), "schema": schema,
            "url": _public_report_url(request, slug)}


@router.delete("/{slug}/filters")
@_with_schema
async def clear_report_filters(slug: str, request: Request):
    """Remove every filter from a report and re-derive its kind from the document."""
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    email = _identity(request)[0]
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, kind, html, owner_email, visibility "
                        "FROM ai_reports WHERE slug = %s FOR UPDATE", (slug,))
            row = _require_owner(cur.fetchone(), email, request, private=True)
            kind = _report_kind(row["html"] or "", request=request)
            cur.execute("UPDATE ai_reports SET filters_json = '', kind = %s WHERE id = %s", (kind, row["id"]))
    return {"ok": True, "slug": slug, "kind": kind, "configured": False, "filter_count": 0}


@router.put("/{slug}/filters/selection")
@_with_schema
async def save_filter_selection(slug: str, body: FilterSelectionIn, request: Request):
    """
    Save THIS reader's filter values (the sidebar's state, not the document's).

    Browser-only on purpose: a selection is a person's view. Machine callers change a
    report's DEFAULT by editing the schema, which is the honest place for "everyone
    should open on Q2".
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, kind, owner_email, visibility, status, filters_json "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
            _require_read(row, email, request)
            schema = load_filter_schema(row)
            if not schema.get("filters"):
                raise HTTPException(status_code=409, detail=pick("这份报告没有可设置的筛选器 / this report has no filters to set", request_lang(request) if request else None))
            effective, dropped = effective_selection(schema, body.selection)
            cur.execute("""
                INSERT INTO ai_report_filter_selections (report_id, user_email, selection_json, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (report_id, user_email)
                DO UPDATE SET selection_json = EXCLUDED.selection_json, updated_at = NOW()
                RETURNING updated_at
            """, (row["id"], email, json.dumps(effective, ensure_ascii=False)))
            saved_at = cur.fetchone()["updated_at"]
    return {"ok": True, "slug": slug, "selection": effective, "dropped": dropped,
            "updated_at": saved_at.isoformat() if saved_at else None}


class CoverRequest(BaseModel):
    title: str
    ratio: str = "16:9"
    with_title: bool = False
    extra: str = ""
    seed: Optional[int] = None      # same seed ≈ reproducible cover


@router.post("/cover")
async def generate_cover(body: CoverRequest, request: Request):
    """
    Generate one cover from a title and return it as base64 (stateless).

    The publishing agent is external and has no image credentials, so generation
    happens here. It can then either embed the result in a publish call
    (`cover_base64`) or let publish do it in one shot (`generate_cover: true`).

    `with_title=false` (default) draws no text into the image: the card renders the
    title itself. `with_title=true` produces the magazine-cover variant, which is
    for standalone use only.
    """
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail=pick("缺少标题 / title is required", request_lang(request) if request else None))
    try:
        return report_cover.generate_cover(title, body.ratio, body.with_title, body.extra,
                                          body.seed)
    except ValueError as exc:                 # our own input validation → caller's fault
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None)) from exc
    except RuntimeError as exc:               # provider refused / is unreachable
        raise HTTPException(status_code=502, detail=pick(f"封面生成失败: {exc} / cover generation failed: {exc}", request_lang(request) if request else None)) from exc


# ── Write ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
@router.post("/", status_code=201, include_in_schema=False)
@_with_schema
async def publish_report(body: ReportIn, request: Request):
    """
    Publish (or re-publish) a report.

    * `slug` supplied  → idempotent upsert: same slug overwrites, `updated_at`
      refreshes. This is the contract agents should use.
    * `slug` omitted   → the slug derived from the title ALWAYS gets a timestamp +
      6-random-char suffix (`-YYYYMMDD-HHMM-xxxxxx`), so two publications can never
      share an address and the link itself says when it landed (2026-09-27, user
      requirement). ⚠️ The cost is deliberate: a slug-less re-post is now *always* a
      new card. To update an existing report, pass the `slug` that the first POST
      returned — that path keeps the idempotent overwrite semantics.
    """
    _ensure_table()
    owner_email, _ = _identity(request)
    try:
        _reject_unknown_cover_keys(await request.json(), request)
    except HTTPException:
        raise
    except Exception:                                   # malformed body → let FastAPI complain
        pass
    title = (body.title or "").strip()[:MAX_TITLE_LEN]
    if not title:
        raise HTTPException(status_code=400, detail=pick("缺少标题 / title is required", request_lang(request) if request else None))
    status = _clean_status(body.status, request)
    size = _check_html(body.html, request)
    explicit_slug = (body.slug or "").strip().lower()
    slug = _clean_slug(explicit_slug, title, request)
    cover = _cover_from_body(body, title, request)
    langs = detect_langs(body.html)
    summary, summary_en, summary_zh = bilingual_summaries(
        body.summary, body.summary_en, body.summary_zh, langs, request)
    kind = _report_kind(body.html, body.kind, request)
    if explicit_slug:
        # A document card's slug must never be consumed by an HTML publish: the row would
        # keep its stored file and claim to be an HTML report, and the reader would get
        # whichever half the code happened to check first. Two payloads, two slugs.
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT doc_type FROM ai_reports WHERE slug = %s", (slug,))
                clash = cur.fetchone()
        if clash and clash[0]:
            raise HTTPException(status_code=409, detail=pick(f"'{slug}' 是一张文档卡片（.{clash[0]}）。请换一个 slug 发布这份报告，或先删除该文档卡片。 / '{slug}' is a document card (.{clash[0]}). Publish this report under its own slug, or delete the document card first.", request_lang(request) if request else None))

    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not explicit_slug:
                # Slugless → ALWAYS suffixed (2026-09-27). Two publications can then
                # never land on one address, and the address says when it landed. The
                # trade-off is documented above: a slugless POST is always a new card,
                # so updating means passing the slug from the first response.
                base = slug[:60]
                for _ in range(5):
                    candidate = base + _collision_suffix()
                    if _slug_taken(cur, candidate) is None:
                        slug = candidate
                        break
            cur.execute("""
                INSERT INTO ai_reports
                    (slug, title, summary, summary_en, summary_zh, category, tags, author, submitter,
                     status, pinned, accent, html, size_bytes, langs, owner_email, visibility, kind)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'private', %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title      = EXCLUDED.title,
                    summary    = EXCLUDED.summary,
                    summary_en = EXCLUDED.summary_en,
                    summary_zh = EXCLUDED.summary_zh,
                    category   = EXCLUDED.category,
                    tags       = EXCLUDED.tags,
                    author     = EXCLUDED.author,
                    submitter  = EXCLUDED.submitter,
                    status     = EXCLUDED.status,
                    pinned     = EXCLUDED.pinned,
                    accent     = EXCLUDED.accent,
                    html       = EXCLUDED.html,
                    size_bytes = EXCLUDED.size_bytes,
                    langs      = EXCLUDED.langs,
                    kind       = EXCLUDED.kind,
                    updated_at = NOW()
                WHERE ai_reports.owner_email = EXCLUDED.owner_email
                  AND ai_reports.visibility = 'private'
                RETURNING id, created_at, updated_at, slug
            """, (
                slug, title,
                summary[:MAX_SUMMARY_LEN],
                summary_en[:MAX_SUMMARY_LEN],
                summary_zh[:MAX_SUMMARY_LEN],
                (body.category or "Ad-hoc")[:MAX_CATEGORY_LEN],
                (body.tags or "")[:MAX_TAGS_LEN],
                (body.author or "agent")[:MAX_AUTHOR_LEN],
                (body.submitter or "")[:MAX_AUTHOR_LEN],
                status, bool(body.pinned),
                (body.accent or "")[:40],
                body.html, size, ",".join(langs), owner_email, kind,
            ))
            saved = cur.fetchone()
            if not saved:
                raise HTTPException(status_code=409, detail=pick("slug 已被另一份报告占用；请换一个 / slug belongs to another report; choose a new slug", request_lang(request) if request else None))
            row = dict(saved)
            _store_cover(cur, slug, cover)     # keeps the existing cover when none was sent

    meta = _load(slug, viewer_email=owner_email, request=request)
    meta.update({"id": row["id"],
                 "created_at": row["created_at"].isoformat(),
                 "updated_at": row["updated_at"].isoformat(),
                 # The link the caller hands around — public, absolute, environment-
                 # correct. This is the field an agent pastes back into the chat.
                 "url": _public_report_url(request, slug)})
    _annotate_format(meta, body.html)
    _LOG.info("report published slug=%s bytes=%d status=%s format=%s",
              slug, size, status, meta.get("format"))
    return meta


@router.put("/{slug}")
@_with_schema
async def update_report(slug: str, body: ReportPatch, request: Request):
    """Partial update — only the supplied fields change.

    The cover follows the same rule: pass `cover_base64` to replace it, an empty
    `cover_url` to clear it, or nothing at all to keep it.
    """
    _ensure_table()
    email, _ = _identity(request)
    current = _load(slug, viewer_email=email)
    _require_owner(current, email, request, private=True)
    if current.get("doc_type") and (body.html is not None or body.kind is not None):
        # Titles, tags, covers and status are all editable on a document card — its
        # PAYLOAD is not. Sending html/kind here would turn the card into a report that
        # still points at a stored file.
        raise HTTPException(status_code=400, detail=pick(f"'{slug}' 是 .{current['doc_type']} 文档卡片 —— 请把文件重新上传到 POST /api/reports/documents 并带上此 slug 来替换；html/kind 对它不适用 / '{slug}' is a .{current['doc_type']} document card — replace its file by re-uploading to POST /api/reports/documents with this slug; html/kind do not apply to it", request_lang(request) if request else None))
    try:
        _reject_unknown_cover_keys(await request.json(), request)
    except HTTPException:
        raise
    except Exception:
        pass
    # Resolve the cover first: it is the only step that can 400/502, and a bad
    # cover should not leave the text fields half-applied.
    cover = _cover_from_body(body, body.title or current["title"], request)
    clear_cover = (body.cover_url is not None and not body.cover_url.strip()
                   and not body.cover_base64)
    sets, params = [], []

    def _put(column: str, value):
        sets.append(f"{column} = %s")
        params.append(value)

    if body.title is not None:
        title = body.title.strip()[:MAX_TITLE_LEN]
        if not title:
            raise HTTPException(status_code=400, detail=pick("标题不能为空 / title cannot be empty", request_lang(request) if request else None))
        _put("title", title)
    # The bilingual-summary rule fires when the *content or the summary* is being written —
    # a plain rename of an old bilingual report is left alone. Checked against the effective
    # state (payload over stored row), so the next time such a report is republished it has
    # to bring its two summary lines: validation only, the fields are written below exactly
    # as the caller sent them.
    if (body.html is not None or body.summary is not None
            or body.summary_en is not None or body.summary_zh is not None):
        bilingual_summaries(
            body.summary if body.summary is not None else (current.get("summary") or ""),
            body.summary_en if body.summary_en is not None else (current.get("summary_en") or ""),
            body.summary_zh if body.summary_zh is not None else (current.get("summary_zh") or ""),
            detect_langs(body.html) if body.html is not None
            else str(current.get("langs") or "").split(","), request)
    if body.summary is not None:
        _put("summary", body.summary[:MAX_SUMMARY_LEN])
    if body.summary_en is not None:
        _put("summary_en", body.summary_en[:MAX_SUMMARY_LEN])
    if body.summary_zh is not None:
        _put("summary_zh", body.summary_zh[:MAX_SUMMARY_LEN])
    if body.category is not None:
        _put("category", body.category[:MAX_CATEGORY_LEN])
    if body.tags is not None:
        _put("tags", body.tags[:MAX_TAGS_LEN])
    if body.author is not None:
        _put("author", body.author[:MAX_AUTHOR_LEN])
    if body.submitter is not None:
        _put("submitter", body.submitter[:MAX_AUTHOR_LEN])
    if body.status is not None:
        _put("status", _clean_status(body.status, request))
    if body.pinned is not None:
        _put("pinned", bool(body.pinned))
    if body.accent is not None:
        _put("accent", body.accent[:40])
    if body.html is not None:
        _put("html", body.html)
        _put("size_bytes", _check_html(body.html, request))
        _put("langs", ",".join(detect_langs(body.html)))
        _put("kind", _report_kind(body.html, body.kind or "", request))
    elif body.kind is not None:
        _put("kind", _report_kind(_stored_html(slug), body.kind, request))

    if not sets and not cover and not clear_cover:
        raise HTTPException(status_code=400, detail=pick("没有提供任何要更新的字段 / no fields supplied", request_lang(request) if request else None))
    if not sets:
        # cover-only update
        with _db() as conn:
            with conn.cursor() as cur:
                _store_cover(cur, slug, cover, clear=clear_cover)
                if cur.rowcount == 0 and not clear_cover:
                    raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
        meta = _load(slug, viewer_email=email, request=request)
        meta["url"] = _public_report_url(request, slug)
        _annotate_format(meta, _stored_html(slug))
        return meta

    sets.append("updated_at = NOW()")
    params.extend((slug, email))
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE ai_reports SET {', '.join(sets)} "
                        "WHERE slug = %s AND owner_email = %s AND visibility = 'private'", params)
            if cur.rowcount == 0:
                # HTTPException propagates through _db(), which rolls back.
                raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
            _store_cover(cur, slug, cover, clear=clear_cover)
    meta = _load(slug, viewer_email=email, request=request)
    meta["url"] = _public_report_url(request, slug)
    _annotate_format(meta, _stored_html(slug))
    return meta


class ReportTitle(BaseModel):
    title: str


@router.patch("/{slug}/title")
@_with_schema
async def rename_report(slug: str, body: ReportTitle, request: Request):
    """The owner may rename a private original or a public snapshot."""
    email, _ = _identity(request)
    title = body.title.strip()[:MAX_TITLE_LEN]
    if not title:
        raise HTTPException(status_code=400, detail=pick("标题不能为空 / title cannot be empty", request_lang(request) if request else None))
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_reports SET title = %s, updated_at = NOW() "
                        "WHERE slug = %s AND owner_email = %s RETURNING id",
                        (title, slug, email))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    return _load(slug, viewer_email=email, request=request)


class ColleagueShareIn(BaseModel):
    emails: list[str]


def _shareable_report(cur, slug: str, email: str, *, request: Request | None = None, private: bool = False) -> dict:
    cur.execute("SELECT id, slug, status, owner_email, visibility FROM ai_reports "
                "WHERE slug = %s", (slug,))
    row = _require_owner(cur.fetchone(), email, request, private=private)
    if row["status"] != "published":
        raise HTTPException(status_code=409, detail=pick("只有已发布的报告才能共享 / only published reports can be shared", request_lang(request) if request else None))
    return row


@router.get("/{slug}/shares")
@_with_schema
async def get_report_shares(slug: str, request: Request):
    """Show the owner who may read this report and its anonymous link, if any."""
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            report = _shareable_report(cur, slug, email, request=request)
            cur.execute("SELECT recipient_email FROM ai_report_colleague_shares "
                        "WHERE report_id = %s ORDER BY recipient_email", (report["id"],))
            colleagues = [row["recipient_email"] for row in cur.fetchall()]
            cur.execute("SELECT link_id FROM ai_report_anyone_links WHERE report_id = %s",
                        (report["id"],))
            link = cur.fetchone()
    return {"colleagues": colleagues, "colleague_url": _public_report_url(request, slug),
            "anyone_url": _anyone_url(request, link["link_id"]) if link else ""}


@router.post("/{slug}/shares/colleagues")
@_with_schema
async def share_report_with_colleagues(slug: str, body: ColleagueShareIn, request: Request):
    """Grant read-only access to named colleague accounts, including future registrants."""
    email = _require_browser(request)
    recipients = sorted({value.strip().lower() for value in body.emails if value.strip()})
    if not recipients or len(recipients) > 20:
        raise HTTPException(status_code=400,
                            detail=pick("请输入 1–20 个收件人邮箱 / enter 1–20 recipient email addresses", request_lang(request) if request else None))
    if email in recipients:
        raise HTTPException(status_code=400, detail=pick("所有者本来就有访问权限 / the owner already has access", request_lang(request) if request else None))
    # ⚠️ The organization gate, not the old single-domain check. `is_allowed_recipient`
    # (deleted) only asked "does this address end in the configured suffix", which is
    # one global setting and covers two of the nine share paths in the product. This
    # asks the real question — may THIS owner hand it to THAT person — and is the same
    # function every other write path calls.
    allowed, reason, offenders = orgs.may_share_to_many(email, recipients)
    if not allowed:
        status, message = orgs.denial(reason)
        raise HTTPException(status_code=status, detail=pick(message, request_lang(request) if request else None))
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            report = _shareable_report(cur, slug, email, request=request, private=True)
            for recipient in recipients:
                cur.execute("INSERT INTO ai_report_colleague_shares "
                            "(report_id, recipient_email, shared_by) VALUES (%s, %s, %s) "
                            "ON CONFLICT (report_id, recipient_email) DO NOTHING",
                            (report["id"], recipient, email))
    # Outside the transaction: a colleague's share must not fail because the
    # notification row could not be written.
    for recipient in recipients:
        inbox.emit(kind="share", actor_email=email, recipient_email=recipient,
                   target_type="report", target_slug=slug, target_title=report.get("title", ""),
                   target_url=annotations.doc_url("report", slug),
                   summary=f"把报告《{report.get('title') or slug}》分享给了你 / shared the report \"{report.get('title') or slug}\" with you")
    return {"emails": recipients, "url": _public_report_url(request, slug)}


@router.delete("/{slug}/shares/colleagues/{recipient_email}")
@_with_schema
async def revoke_colleague_share(slug: str, recipient_email: str, request: Request):
    email = _require_browser(request)
    recipient = recipient_email.strip().lower()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            report = _shareable_report(cur, slug, email, request=request, private=True)
            cur.execute("DELETE FROM ai_report_colleague_shares "
                        "WHERE report_id = %s AND recipient_email = %s",
                        (report["id"], recipient))
    return {"revoked": recipient}


@router.post("/{slug}/shares/everyone")
@_with_schema
async def create_anyone_link(slug: str, request: Request):
    """Return a stable, revocable, no-login read-only URL for this report."""
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            report = _shareable_report(cur, slug, email, request=request)
            # ⚠️ Checked BEFORE the row is written, and on the *slug* rather than the
            # id: the approval table keys on the document's own identity so the console
            # can show "who asked to publish this" without joining into this module.
            #
            # This path had no gate at all before. It is the sharpest edge in the
            # product — `/s/{token}` is a bearer credential, the table has no owner
            # column, and the token in the URL is the key. An enterprise that says
            # "documents stay inside the company" means it here too.
            allowed, reason = orgs.may_publish(email, orgs.CHANNEL_ANYONE_LINK,
                                               orgs.TARGET_REPORT, slug)
            if not allowed:
                status, message = orgs.denial(reason)
                raise HTTPException(status_code=status,
                                    detail=pick(message, request_lang(request) if request else None))
            cur.execute("SELECT link_id FROM ai_report_anyone_links WHERE report_id = %s",
                        (report["id"],))
            link = cur.fetchone()
            if not link:
                link_id = secrets.token_urlsafe(18)
                cur.execute("INSERT INTO ai_report_anyone_links (report_id, link_id) "
                            "VALUES (%s, %s) RETURNING link_id", (report["id"], link_id))
                link = cur.fetchone()
    return {"url": _anyone_url(request, link["link_id"])}


@router.delete("/{slug}/shares/everyone")
@_with_schema
async def revoke_anyone_link(slug: str, request: Request):
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            report = _shareable_report(cur, slug, email, request=request)
            cur.execute("DELETE FROM ai_report_anyone_links WHERE report_id = %s", (report["id"],))
    return {"revoked": True}


_COPY_COLS = ("title, summary, summary_en, summary_zh, category, tags, author, submitter, status, pinned, accent, "
              "html, size_bytes, langs, cover_data, cover_mime, cover_w, cover_h, "
              "cover_prompt, cover_model, cover_url, state_json, state_updated_at, kind, "
              # A pulled / published copy must keep being the same thing: a dynamic report
              # without its schema has slices nothing can select, and a document card
              # without its OSS key is an empty card pointing at no bytes. `doc_object` is
              # shared on purpose — copies read the same immutable object.
              "filters_json, doc_type, doc_object, doc_mime, doc_name, doc_pages")


@router.post("/{slug}/publish")
@_with_schema
async def publish_to_public(slug: str, request: Request):
    """Create or refresh a read-only public snapshot; keep the private original."""
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, status, owner_email, visibility FROM ai_reports "
                        "WHERE slug = %s FOR UPDATE", (slug,))
            source = _require_owner(cur.fetchone(), email, request, private=True)
            # ⚠️ The public area is a listing any signed-in user can browse, so it is a
            # *different* promise from an anyone-link — and it is checked separately,
            # with its own approval. Had no gate before.
            #
            # Placed after the ownership check on purpose: an approval row is written
            # against `slug`, and we only want a real owner's real document to be able
            # to open one.
            allowed, reason = orgs.may_publish(email, orgs.CHANNEL_PUBLIC,
                                               orgs.TARGET_REPORT, slug)
            if not allowed:
                status, message = orgs.denial(reason)
                raise HTTPException(status_code=status,
                                    detail=pick(message, request_lang(request) if request else None))
            if source["status"] != "published":
                raise HTTPException(status_code=409, detail=pick("请先在工作区发布这份报告 / publish the report in your workspace first", request_lang(request) if request else None))
            cur.execute("SELECT slug FROM ai_reports WHERE source_report_id = %s "
                        "AND visibility = 'public' FOR UPDATE", (source["id"],))
            public = cur.fetchone()
            if public:
                public_slug = public["slug"]
                cur.execute(f"UPDATE ai_reports AS target SET ({_COPY_COLS}) = "
                            f"(SELECT {_COPY_COLS} FROM ai_reports WHERE id = %s), "
                            "updated_at = NOW() WHERE target.slug = %s",
                            (source["id"], public_slug))
            else:
                base = slug[:42] + "-public"
                for _ in range(8):
                    candidate = base + _collision_suffix()
                    if _slug_taken(cur, candidate) is None:
                        public_slug = candidate
                        break
                else:
                    raise HTTPException(status_code=409, detail=pick("无法分配公共链接 / could not allocate a public link", request_lang(request) if request else None))
                cur.execute(f"INSERT INTO ai_reports (slug, {_COPY_COLS}, owner_email, visibility, source_report_id) "
                            f"SELECT %s, {_COPY_COLS}, owner_email, 'public', id "
                            "FROM ai_reports WHERE id = %s", (public_slug, source["id"]))
    meta = _load(public_slug, viewer_email=email, request=request)
    meta["url"] = _public_report_url(request, public_slug)
    inbox.emit(kind="publish", actor_email=email, recipient_email=None,
               target_type="report", target_slug=public_slug, target_title=meta.get("title", ""),
               target_url=annotations.doc_url("report", public_slug),
               summary=f"把报告《{meta.get('title') or public_slug}》发布到了公共区 / published the report \"{meta.get('title') or public_slug}\" to the public area")
    return meta


@router.post("/{slug}/pull", status_code=201)
@_with_schema
async def pull_public_report(slug: str, request: Request):
    """Take an independent private copy of a public or colleague-shared report."""
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, owner_email, visibility, status FROM ai_reports WHERE slug = %s", (slug,))
            source = cur.fetchone()
            if (not source or source["status"] != "published" or source["owner_email"] == email
                    or not _may_read(source, email)):
                raise HTTPException(status_code=404, detail=pick("分享的报告不存在 / shared report not found", request_lang(request) if request else None))
            base = slug[:43] + "-copy"
            for _ in range(8):
                candidate = base + _collision_suffix()
                if _slug_taken(cur, candidate) is None:
                    private_slug = candidate
                    break
            else:
                raise HTTPException(status_code=409, detail=pick("无法分配工作区链接 / could not allocate a workspace link", request_lang(request) if request else None))
            cur.execute(f"INSERT INTO ai_reports (slug, {_COPY_COLS}, owner_email, visibility, source_report_id) "
                        f"SELECT %s, {_COPY_COLS}, %s, 'private', id "
                        "FROM ai_reports WHERE id = %s", (private_slug, email, source["id"]))
    meta = _load(private_slug, viewer_email=email, request=request)
    meta["url"] = _public_report_url(request, private_slug)
    return meta


@router.delete("/{slug}/public")
@_with_schema
async def withdraw_public_report(slug: str, request: Request):
    """Withdraw only the public snapshot; already-pulled private copies remain."""
    email = _require_browser(request)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_reports WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'public'", (slug, email))
            if not cur.rowcount:
                raise HTTPException(status_code=404, detail=pick("公共报告不存在 / public report not found", request_lang(request) if request else None))
    return {"withdrawn": slug}


@router.delete("/{slug}")
@_with_schema
async def delete_report(slug: str, request: Request):
    _ensure_table()
    email, _ = _identity(request)
    object_key = ""
    with _db() as conn:
        with conn.cursor() as cur:
            # Read the stored file's key first: after the DELETE the row — and with it the
            # only pointer to those bytes — is gone.
            cur.execute("SELECT doc_object FROM ai_reports WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'private'", (slug, email))
            found = cur.fetchone()
            object_key = (found[0] if found else "") or ""
            cur.execute("DELETE FROM ai_reports WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'private'", (slug, email))
            deleted = cur.rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail=pick("报告不存在 / report not found", request_lang(request) if request else None))
    # ⚠️ Only when nothing else references them: a public snapshot or a colleague's pulled
    # copy of a document card points at the SAME object, and deleting the original must not
    # blank out the copies.
    if object_key:
        _sweep_document_object(object_key)
    return {"deleted": slug}


# ── Interactive → static snapshot ────────────────────────────────────────────
#
# A static report is "纯 HTML/CSS 快照" (knowledge doc format-interactive-report §0):
# the reader sees a snapshot and cannot edit it. An interactive report keeps its
# content in TWO places — the HTML (the initial render) and the server-side
# `state_json` (every annotation the reader has made since). A snapshot is only
# truthful if the annotations are folded INTO the document, so this endpoint bakes
# the current state in and then strips the authoring layer.

_STATE_EDITABLE_RE = re.compile(
    r"""\scontenteditable\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
# <script> whose body is the initial model. Its content must SURVIVE as inert JSON
# (the PPTX exporter and the deck runtime read it), so it is not blanket-removed.
_MODEL_SCRIPT_RE = re.compile(
    r"""<script\b[^>]*\bdata-report-deck-model\b[^>]*>.*?</script\s*>""",
    re.I | re.S)


def _initial_model(html: str) -> dict:
    """
    The report's authored v1 content model, read the same way the runtime reads it
    (`report-deck.js`: a `script[type="application/json"][data-report-deck-model]`
    whose textContent is JSON). Returns `{}` when absent or malformed — a report
    without a model is legal, its state then simply has nothing to merge onto.
    """
    match = _MODEL_SCRIPT_RE.search(html or "")
    if not match:
        return {}
    body = re.sub(r"^[^>]*>", "", match.group(0), count=1)
    body = re.sub(r"</script\s*>$", "", body, flags=re.I).strip()
    try:
        model = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return model if isinstance(model, dict) else {}


def _model_at(model: object, path: str):
    """Read a dotted path out of the deck model, mirroring the runtime's `modelAt`."""
    node = model
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def _bake_state_into_html(html: str, state: dict, model: dict) -> tuple[str, int]:
    """
    Write the live state into the document, then remove what makes it interactive.

    Returns ``(html, fields_baked)``. Two independent sources have to be folded in:

    * `data-deck-field` / `data-deck-table` nodes are RENDER TARGETS of the model, so
      the current value is read from the model merged with the state — the same pair
      the runtime hands the page (`reportDeckData.setState` then `bind()`).
    * everything else a reader may have edited is tracked by `data-deck-field` too in
      a conforming report; a node with no binding is left exactly as authored rather
      than guessed at, because rewriting an unbound node could silently replace text
      the author never meant to be dynamic.

    ⚠️ Editing a `contenteditable` node does NOT change the model unless the report
    wires its own listener, so for those nodes the DOM text is the freshest truth.
    The caller passes the observed DOM text through `state["__dom__"]` keyed by the
    element's stable id when the page offers one; absent that, the model wins.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    state = state if isinstance(state, dict) else {}
    model = model if isinstance(model, dict) else {}

    def value_for(path: str):
        """
        The current value of one binding, newest source first.

        ⚠️ The state is stored as the report's OWN annotation object, whose shape is
        the author's business — the documented convention is that a note bound to
        `state.note` is stored under `note`, NOT as a nested `{"state": {...}}`
        (knowledge doc format-interactive-report §1: "body 任意 JSON，整份覆盖",
        服务端不解析 schema). So a blind merge of the state into the model would
        REPLACE the whole `state` branch with a sibling key and lose the very
        annotations we came to bake.

        Resolution is therefore by BINDING, not by guessing the state's shape:
          1. an exact dotted path in the state  (`{"state.note": "..."}`)
          2. the leaf key anywhere in the state (`{"note": "..."}`) — the convention
          3. the model's authored value
        Only scalars are taken; a dict/list leaf means a table binding, not text.
        """
        if path in state and not isinstance(state[path], (dict, list)):
            return state[path]
        leaf = path.split(".")[-1]
        for key, value in state.items():
            if key.startswith("__") or isinstance(value, (dict, list)):
                continue
            if key == leaf:
                return value
        return _model_at(model, path)

    baked = 0
    for el in soup.select("[data-deck-field]"):
        path = el.get("data-deck-field")
        value = value_for(path)
        # ⚠️ Strip the binding EVEN when the value could not be resolved. Leaving
        # `data-deck-field` on an unresolved node keeps the report classifiable as
        # interactive (see `_report_kind`), so the snapshot would still be editable
        # — worse than showing the authored text unchanged.
        for attr in ("contenteditable", "data-deck-field", "data-pptx-bind"):
            if el.has_attr(attr):
                del el[attr]
        if value is None or isinstance(value, (dict, list)):
            continue                      # keep the authored text rather than blank it
        el.string = str(value)
        baked += 1

    for table in soup.select("table[data-deck-table]"):
        path = table.get("data-deck-table")
        rows = value_for(path) if path in state else _model_at(model, path)
        header_rows = int(table.get("data-deck-header-rows") or 0)
        if isinstance(rows, list) and all(isinstance(r, list) for r in rows):
            # BeautifulSoup has no DOM `replaceChildren`; clear then build fresh rows.
            # ⚠️ `new_tag` hangs off the SOUP, not off a Tag (`Tag.new_tag` is None).
            table.clear()
            for i, row in enumerate(rows):
                tr = soup.new_tag("tr")
                for value in row:
                    cell = soup.new_tag("th" if i < header_rows else "td")
                    cell.string = "" if value is None else str(value)
                    tr.append(cell)
                table.append(tr)
            baked += 1
        # Unbind either way — an unresolved table must not keep the report editable.
        del table["data-deck-table"]
        table.attrs.pop("data-pptx-table-ref", None)

    # Strip the authoring affordances. The report stays a deck (pagination is not
    # an authoring control — see `_report_kind`), so the runtime is still injected
    # on the way out; only the editable layer goes.
    for el in soup.select('[contenteditable]:not([contenteditable="false"]), '
                          'input, textarea, select'):
        if el.name in ("input", "textarea", "select"):
            el.decompose()
        else:
            for attr in ("contenteditable", "data-deck-field", "data-pptx-bind"):
                if el.has_attr(attr):
                    del el[attr]
    for meta in soup.select('meta[name="report-kind"], [data-report-kind]'):
        if meta.name == "meta":
            meta["content"] = "static"
        else:
            meta["data-report-kind"] = "static"

    out = str(soup)
    # The save-to-state script talks to an endpoint this snapshot will never have.
    out = re.sub(r"<script\b[^>]*>(?:(?!</script\s*>).)*?"
                 r"(?:/state\b|stateEndpoint|saveState)"
                 r"(?:(?!</script\s*>).)*?</script\s*>", "", out, flags=re.I | re.S)
    return out, baked


@router.post("/{slug}/to-static", status_code=201)
@_with_schema
async def report_to_static(slug: str, request: Request):
    """
    Publish the CURRENT content of an interactive report as a static snapshot in
    the owner's own workspace. The original stays interactive and untouched.
    """
    email = _require_browser(request)
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    _ensure_table()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM ai_reports WHERE slug = %s FOR UPDATE", (slug,))
            row = cur.fetchone()
            source = _require_owner(row, email, request, private=True)
            if source["status"] != "published":
                raise HTTPException(status_code=409,
                                    detail=pick("请先在工作区发布这份报告 / publish the report in your workspace first", request_lang(request) if request else None))
            if (source.get("kind") or "static") != "interactive":
                raise HTTPException(status_code=409,
                                    detail=pick("这份报告不是交互式报告 / this report is not interactive", request_lang(request) if request else None))

            try:
                state = json.loads(source.get("state_json") or "{}")
            except (TypeError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            model = _initial_model(source.get("html") or "")
            html, baked = _bake_state_into_html(source.get("html") or "", state, model)
            size = _check_html(html, request)
            if _report_kind(html, request=request) != "static":
                # Belt and braces: never hand back something the classifier still
                # calls interactive (a stray /state reference, a custom editor).
                raise HTTPException(
                    status_code=422,
                    detail=pick("这份报告的交互层无法自动移除；请手动导出 / this report's interactive layer could not be removed automatically; export it by hand instead", request_lang(request) if request else None))

            base = slug[:40] + "-static"
            for _ in range(8):
                candidate = base + _collision_suffix()
                if _slug_taken(cur, candidate) is None:
                    static_slug = candidate
                    break
            else:
                raise HTTPException(status_code=409, detail=pick("无法分配工作区链接 / could not allocate a workspace link", request_lang(request) if request else None))

            title = (source.get("title") or slug)[:MAX_TITLE_LEN]
            langs = detect_langs(html)
            summary, summary_en, summary_zh = bilingual_summaries(
                source.get("summary") or "", source.get("summary_en") or "",
                source.get("summary_zh") or "", langs, request)
            cur.execute(
                "INSERT INTO ai_reports (slug, title, summary, summary_en, summary_zh, "
                "category, tags, author, submitter, status, kind, html, size_bytes, langs, "
                "owner_email, visibility, source_report_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'published','static',%s,%s,%s,%s,'private',%s)",
                (static_slug, title, summary, summary_en, summary_zh,
                 (source.get("category") or "Ad-hoc")[:MAX_CATEGORY_LEN],
                 (source.get("tags") or "")[:MAX_TAGS_LEN],
                 (source.get("author") or "agent")[:MAX_AUTHOR_LEN],
                 source.get("submitter") or "", html, size, langs, email, source["id"]))
    meta = _load(static_slug, viewer_email=email, request=request)
    meta["url"] = _public_report_url(request, static_slug)
    meta["fields_baked"] = baked
    return meta


# ── Document cards: pptx / xlsx / pdf / docx ─────────────────────────────────
#
# A Workspace card has always been "a document an agent published"; until now the only
# document an agent could publish was HTML. These routes let the SAME wall hold the four
# binary deliverables colleagues actually ask for — a deck, a workbook, a PDF, a Word
# file — by reusing everything the wall already does: ownership, the private / public /
# shared scopes, named shares, the `/s/{token}` link, rename, covers, search, delete.
#
# Four decisions worth keeping:
#
#   * **One table.** A document is an `ai_reports` row with `doc_type` set and `html`
#     empty. `kind = 'document'` is what reader-facing branches test — never "html is
#     empty", which a half-finished report also has.
#   * **Immutable objects.** Every upload writes a NEW OSS object (timestamp + random),
#     so re-uploading the original can never change what a public snapshot shows. The
#     previous object is swept only once no row references it.
#   * **Bytes travel as base64**, like a knowledge-page image: the agent holds an API
#     credential, not a storage one, and handing out a presigned OSS URL would grant
#     access the card's own permissions do not cover.
#   * **The card's address stays `/r/{slug}`.** A shared link, the viewer iframe and the
#     permalink all keep working, and the page served there is the reader shell below —
#     so a document and a report are the same thing to everything outside this module.

MAX_DOC_FILENAME_LEN = 160
DOC_LABEL = {"pptx": "PowerPoint", "xlsx": "Excel workbook",
             "pdf": "PDF", "docx": "Word document"}
_DOC_MAGIC = {"pptx": (b"PK",), "xlsx": (b"PK",), "docx": (b"PK",), "pdf": (b"%PDF",)}


class DocumentIn(BaseModel):
    """One uploaded file plus the card metadata. The card behaves like any other report."""

    filename: str = ""
    content_base64: str = Field(default="", validation_alias=AliasChoices(
        "content_base64", "content", "file_base64", "data_base64"))
    media_type: str = Field(default="", validation_alias=AliasChoices(
        "media_type", "mime_type", "content_type"))
    slug: Optional[str] = None
    title: str = ""
    summary: str = ""
    summary_en: str = ""
    summary_zh: str = ""
    category: str = "Documents"
    tags: str = ""
    author: str = "agent"
    submitter: str = Field(default="", validation_alias=AliasChoices(
        "submitter", "submitted_by", "submittedBy", "user_name", "userName", "sender"))
    status: str = "published"
    pinned: bool = False
    accent: str = ""
    cover_base64: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_base64"]))
    cover_url: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_url"]))
    cover_prompt: Optional[str] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_prompt"]))
    generate_cover: bool = Field(default=False, validation_alias=AliasChoices(*COVER_ALIASES["generate_cover"]))
    cover_ratio: str = Field(default="16:9", validation_alias=AliasChoices(*COVER_ALIASES["cover_ratio"]))
    cover_with_title: bool = Field(default=False, validation_alias=AliasChoices(*COVER_ALIASES["cover_with_title"]))
    cover_extra: str = Field(default="", validation_alias=AliasChoices(*COVER_ALIASES["cover_extra"]))
    cover_seed: Optional[int] = Field(default=None, validation_alias=AliasChoices(*COVER_ALIASES["cover_seed"]))


def _absolute_url(request: Request, path: str) -> str:
    """Environment-correct absolute URL, following the Host/proto of THIS request.

    An agent posting to test gets a test link and one posting to prod gets prod — the
    same rule `_public_report_url` uses. Used through `_base_href()` so it also carries
    the mount point.
    """
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "https").split(",")[0].strip()
    return f"{proto}://{host}{path}" if host else path


def _doc_type_of(filename: str, media_type: str = "", request: Request | None = None) -> tuple[str, str]:
    """`(doc_type, safe filename)` for an accepted upload; 400 for anything else."""
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:MAX_DOC_FILENAME_LEN].strip()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in VALID_DOC_TYPES:
        # A caller that named the type properly just forgot the extension (a Feishu agent
        # often has only the mime string). Trust the mime ONLY when the extension is absent
        # or useless — with a real extension, that extension is the answer.
        by_mime = next((kind for kind, mime in DOC_MIME.items()
                        if media_type and mime == media_type.strip().lower()), "")
        if by_mime:
            return by_mime, name or f"document.{by_mime}"
        # The legacy formats are the common mistake (`.ppt` / `.doc` / `.xls`), and the
        # reader cannot preview them, so the message names the fix instead of the problem.
        legacy = {"ppt": "pptx", "doc": "docx", "xls": "xlsx", "csv": "xlsx"}
        hint = legacy.get(ext)
        raise HTTPException(status_code=400, detail=pick(f"'{ext or 'no extension'}' 不是四种支持的类型之一（{', '.join(VALID_DOC_TYPES)}）。" + (f" 请把文件另存为 .{hint} 再上传。" if hint else "") + " / " + f"'{ext or 'no extension'}' is not one of the four accepted types ({', '.join(VALID_DOC_TYPES)})." + (f" Save the file as .{hint} and upload that." if hint else ""), request_lang(request) if request else None))
    if not name:
        name = "document." + ext
    return ext, name


def _decode_document(body: DocumentIn, request: Request | None = None) -> tuple[str, str, bytes]:
    """`(doc_type, filename, bytes)` — validating the name, the size and the container."""
    doc_type, filename = _doc_type_of(body.filename, body.media_type, request)
    raw = (body.content_base64 or "").strip()
    if raw.startswith("data:"):
        # A data URI is accepted, but its declared type is NOT trusted: the extension
        # decides the card type, and the magic bytes below decide whether it is real.
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise HTTPException(status_code=400,
                            detail=pick("content_base64 为空 —— 请把文件字节 base64 编码后发送 / content_base64 is empty — send the file's bytes base64-encoded", request_lang(request) if request else None))
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:                                       # noqa: BLE001 — any decode failure is the same 400
        raise HTTPException(status_code=400, detail=pick("content_base64 不是合法的 base64 / content_base64 is not valid base64", request_lang(request) if request else None)) from None
    if not data:
        raise HTTPException(status_code=400, detail=pick("发送的文件是空的 / the sent file is empty", request_lang(request) if request else None))
    if len(data) > MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail=pick(f"文件为 {len(data)} 字节，上限 {MAX_DOC_BYTES} 字节（{MAX_DOC_BYTES // (1024 * 1024)} MB）。请压缩后重传（压缩图片、拆分 deck）—— 本端点不支持分块上传。 / the file is {len(data)} bytes; the limit is {MAX_DOC_BYTES} bytes ({MAX_DOC_BYTES // (1024 * 1024)} MB). Re-save it smaller (compress the images or split the deck) — there is no chunked upload on this endpoint.", request_lang(request) if request else None))
    magic = _DOC_MAGIC[doc_type]
    if not any(data.startswith(prefix) for prefix in magic):
        want = "PDF (%PDF)" if doc_type == "pdf" else f"an OOXML file (zip, 'PK' — a .{doc_type} saved by Office)"
        raise HTTPException(status_code=400, detail=pick(f"发送的字节不是 {want}。扩展名是 .{doc_type} —— 请确认文件是导出的（而不是改名后发送的）。 / the sent bytes are not {want}. The extension says .{doc_type} — check that the file was exported (not renamed) before sending it.", request_lang(request) if request else None))
    return doc_type, filename, data


def _title_from_filename(filename: str) -> str:
    """`Q3_channel-review.pptx` → `Q3 channel review`. The card always needs a title."""
    stem = re.sub(r"\.[^.]+$", "", filename or "")
    pretty = re.sub(r"[_\s]+", " ", stem).strip()
    return (pretty or "Untitled document")[:MAX_TITLE_LEN]


def _document_pages(doc_type: str, data: bytes) -> Optional[int]:
    """How many pages/sheets a preview will have — a card nicety, never a requirement."""
    try:
        if doc_type == "pdf":
            from services.pdf_io import pdf_page_count
            return pdf_page_count(data)
        if doc_type == "pptx":
            import io
            from pptx import Presentation
            return len(Presentation(io.BytesIO(data)).slides)
        if doc_type == "xlsx":
            import io
            from openpyxl import load_workbook
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            return len([s for s in book.worksheets if s.sheet_state == "visible"])
    except Exception as exc:                                # noqa: BLE001 — metadata only
        _LOG.info("document page count unavailable (%s): %s", doc_type, exc)
    return None


def _sweep_document_object(object_key: str) -> None:
    """Drop bytes that no card references any more. Best effort: never fails a request."""
    if not object_key:
        return
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM ai_reports WHERE doc_object = %s LIMIT 1", (object_key,))
                if cur.fetchone():
                    return          # a copy (public snapshot / pulled copy) still reads it
        oss_storage.remove_object(DOC_BUCKET_PREFIX, object_key)
    except Exception as exc:                                # noqa: BLE001 — an orphan beats a 500
        _LOG.warning("workspace document sweep failed for %s: %s", object_key, exc)


def _drop_object_quietly(object_key: str) -> None:
    """Delete bytes we just wrote and then decided not to use (a failed publish)."""
    if not object_key:
        return
    try:
        oss_storage.remove_object(DOC_BUCKET_PREFIX, object_key)
    except Exception as exc:                                # noqa: BLE001 — the card matters, not the sweep
        _LOG.warning("could not remove unused document object %s: %s", object_key, exc)


def _document_headers(row: dict, *, download: bool) -> dict[str, str]:
    """Content-Disposition for a stored file: a PDF opens inline, everything else downloads."""
    name = row.get("doc_name") or f"{row.get('slug') or 'document'}.{row.get('doc_type') or 'bin'}"
    ascii_name = (name.encode("ascii", "ignore").decode("ascii") or "document").replace('"', "")
    mime = row.get("doc_mime") or DOC_MIME.get(row.get("doc_type") or "", "application/octet-stream")
    inline = (not download) and mime == "application/pdf"
    disposition = "inline" if inline else "attachment"
    return {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
    }


def _document_bytes(row: dict, *, download: bool = False, request: Request | None = None) -> Response:
    """The stored file itself. Raises 502 rather than a 404 when storage is unreachable —
    "your card is fine, storage is not" is a different thing to tell a reader."""
    key = row.get("doc_object") or ""
    if not key:
        raise HTTPException(status_code=404, detail=pick("这张卡片没有存储的文件 / this card has no stored file", request_lang(request) if request else None))
    try:
        data = oss_storage.get_object(DOC_BUCKET_PREFIX, key)
    except Exception as exc:                                # noqa: BLE001 — storage failure is a 502
        _LOG.warning("workspace document read failed slug=%s key=%s: %s", row.get("slug"), key, exc)
        raise HTTPException(status_code=502, detail=pick("存储的文件读取失败 / the stored file could not be read", request_lang(request) if request else None)) from exc
    mime = row.get("doc_mime") or DOC_MIME.get(row.get("doc_type") or "", "application/octet-stream")
    return Response(content=data, media_type=mime, headers=_document_headers(row, download=download))


def _document_row(slug: str, request: Request | None = None) -> dict:
    """One document card by slug (raises 404 when the row is absent or is an HTML report)."""
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, slug, title, kind, status, size_bytes, updated_at, owner_email, "
                        "visibility, doc_type, doc_object, doc_mime, doc_name, doc_pages "
                        "FROM ai_reports WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row or not row.get("doc_type"):
        raise HTTPException(status_code=404, detail=pick("文档不存在 / document not found", request_lang(request) if request else None))
    return dict(row)


def _preview_with_notes(html: str, row: dict, *, pages: str, viewer_email: str = "",
                        writable: bool = True, read_url: str = "") -> str:
    """
    A document card's preview, handed to the reader with its note layer on.

    Two additions, both about the READER rather than about the file:

    * the annotation runtime, anchored per sheet (`pages`) — see the `pages` host mode in
      `vendor/annotations.js`. Without it a colleague can only look at a contract, not ask
      a question about clause 4;
    * remixicon, which that runtime's buttons draw from. The report pages load it already;
      a bare preview document has no theme of its own, so without this the note toggle and
      its menu would be iconless.

    `read_url` is how a `/s/{token}` guest reads notes they may see but cannot write.
    """
    if not html:
        return html
    html = htmlkit.insert_before_tag(
        html, "</head>",
        f'<link rel="stylesheet" href="{_base_href()}remixicon/remixicon.css">')
    return annotations.inline_runtime(
        html, asset_type="report", slug=row.get("slug") or "", viewer_email=viewer_email,
        writable=writable, read_url=read_url, pages=pages)


def _human_size(size: object) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _doc_preview_module(request: Request | None = None):
    """`services.doc_preview`, imported on first use.

    Lazy on purpose: it pulls in python-pptx / python-docx / openpyxl, and a container
    that is missing one of them must still boot and serve every other page (the preview
    routes answer 503 with the reason instead)."""
    try:
        from services import doc_preview
    except Exception as exc:                                # noqa: BLE001 — report, don't crash boot
        _LOG.error("document preview module unavailable: %s", exc)
        raise HTTPException(status_code=503,
                            detail=pick("本部署不支持文档预览 / document preview is unavailable on this deployment", request_lang(request) if request else None)) from exc
    return doc_preview


def _document_page_html(row: dict, *, file_url: str, preview_url: str,
                        guest: bool = False, embed: bool = False) -> str:
    """
    The reader page for a document card (`/r/{slug}` and `/s/{token}`).

    One page for four file types, and the body is always the same thing: an iframe onto the
    HTML preview. That is deliberate. A document card's reading experience is *the file laid
    out the way its own application lays it out* (sheets of paper, slides, an Excel grid),
    the preview route owns that rendering, and the notes a reader leaves live inside it —
    anchored to the slide or the sheet they were left on. See `services/doc_preview.py`.

    Deliberately plain HTML + a tiny inline stylesheet: no JavaScript of the reader page's
    own, nothing to fail. The heavy lifting is the server-rendered preview in the frame.

    ⚠️ **`embed` is the whole point of the second shape, and it is a first-principles
    rule rather than a layout tweak: every piece of chrome has exactly ONE owner.**
    Opened on its own (`/r/{slug}` in a tab, or a colleague's `/s/{token}` link) this page
    IS the interface, so it carries the title, the meta line and the download button. But
    the app shows this very page inside the Workspace viewer's own toolbar, which already
    shows the title, the meta and a download button — so the framed copy used to stack a
    SECOND header on top of the first, and the two copies drifted apart in plain sight:
    the shell said `37 KB`, this one said `37.2 KB`. Two headers is not "belt and braces",
    it is the reader being asked to work out which one is authoritative.

    So when framed, the page renders the document and nothing else, and the shell owns the
    chrome. The note hint stays in both, because right-click-to-annotate is otherwise
    undiscoverable and it is the one thing here nobody can guess.
    """
    doc_type = row.get("doc_type") or ""
    title = row.get("title") or row.get("doc_name") or row.get("slug") or "Document"
    meta_bits = [DOC_LABEL.get(doc_type, doc_type.upper()), row.get("doc_name") or "",
                 _human_size(row.get("size_bytes"))]
    if row.get("doc_pages"):
        # ⚠️ Singular when it is one: a real xlsx came out as "1 sheets" (2026-10-01).
        count = row["doc_pages"]
        noun = "page" if doc_type in ("pdf", "pptx") else "sheet"
        meta_bits.append(f"{count} {noun}" + ("" if count == 1 else "s"))
    meta_line = " · ".join(bit for bit in meta_bits if bit)
    download_url = file_url + ("&" if "?" in file_url else "?") + "download=1"

    main = (f'<iframe class="dp-frame" src="{html_escape(preview_url)}" title="Preview"></iframe>'
            + ('<p class="dp-hint">'
               + html_escape(DOC_LABEL.get(doc_type, doc_type))
               + ' layout, rendered in your browser. Right-click anywhere in it to leave a note.'
               + '</p>' if embed else
               '<p class="dp-hint">Rendered from the file in your browser — '
               + html_escape(DOC_LABEL.get(doc_type, doc_type)) + ' layout and all. '
               'Right-click anywhere in it to leave a note. '
               'Download the original for the full-fidelity file.</p>'))

    guest_note = ('<span class="dp-note">Shared link · read only</span>' if guest else "")
    # The standalone form only. Framed, the app's toolbar is above this and carries all of
    # it — a second title, a second meta line and a second download button is the bug.
    bar = "" if embed else f"""  <header class="dp-bar">
  <div>
    <p class="dp-title">{html_escape(title)}</p>
    <span class="dp-meta">{html_escape(meta_line)}</span>
  </div>
  <span class="dp-spacer"></span>
  {guest_note}
  <a class="dp-dl" href="{html_escape(download_url)}" download>Download</a>
</header>
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(title)}</title>
<style>
  :root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f1f5f9; --blue:#1d6fd6; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",Arial,sans-serif; }}
  .dp-bar {{ position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:14px;
            padding:10px 18px; background:#fff; border-bottom:1px solid var(--line); }}
  .dp-title {{ font-weight:600; font-size:15px; margin:0; }}
  .dp-meta {{ color:var(--muted); font-size:12px; }}
  .dp-note {{ color:var(--muted); font-size:12px; }}
  .dp-spacer {{ flex:1; }}
  .dp-dl {{ display:inline-flex; align-items:center; gap:6px; padding:6px 14px; border-radius:7px;
           background:var(--blue); color:#fff; text-decoration:none; font-weight:600; font-size:13px; }}
  .dp-dl:hover {{ background:#1857a8; }}
  .dp-body {{ padding:14px; }}
  /* Framed: no header above it, so the frame may use the whole height. The standalone
     form has to leave room for the sticky bar plus this paragraph. */
  .dp-frame {{ width:100%; height:{'100vh' if embed else 'calc(100vh - 96px)'};
              border:1px solid var(--line); border-radius:10px; background:#fff; }}
  .dp-hint {{ color:var(--muted); font-size:12px; margin:8px 2px 0; }}
  .dp-empty {{ background:#fff; border:1px solid var(--line); border-radius:10px;
              padding:60px 20px; text-align:center; color:var(--muted); }}
</style>
</head>
<body>
{bar}<div class="dp-body">
{main}
</div>
</body>
</html>"""


def _document_page_response(row: dict, request: Request, *, download: bool = False,
                            guest_token: str = "", embed: bool = False) -> Response:
    """Serve a document card's reader page — the same address a report uses.

    ⚠️ `embed` is what the Workspace viewer's iframe passes. The address itself is
    unchanged, because it has to stay the one stable permalink a colleague can be sent —
    so the difference is a query flag on the same URL, and a link that leaves the app
    (copy-link, a new tab) simply does not carry it and gets the full standalone page.
    """
    slug = row.get("slug") or ""
    if guest_token:
        file_url = f"s/{guest_token}/document"
        preview_url = f"s/{guest_token}/document?preview=html"
    else:
        file_url = f"api/reports/{slug}/document"
        preview_url = f"api/reports/{slug}/document/preview.html"
    if download:
        # `/r/{slug}?download=1` is a download, not a page: hand over the bytes.
        return RedirectResponse(url=file_url + ("&" if "?" in file_url else "?") + "download=1",
                                status_code=302)
    body = _inject_base_tag(
        _document_page_html(row, file_url=file_url, preview_url=preview_url,
                            guest=bool(guest_token), embed=embed), _base_href())
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if guest_token:
        headers["Referrer-Policy"] = "no-referrer"
    return HTMLResponse(content=body, headers=headers)


@router.post("/documents", status_code=201)
@_with_schema
async def publish_document(body: DocumentIn, request: Request):
    """
    Put a pptx / xlsx / pdf / docx into the caller's private workspace as a card.

    Pass `slug` to replace an existing document card (the same idempotent contract as
    `POST /api/reports`); without it a NEW slug is always allocated, so two uploads can
    never land on one address. A slug that belongs to an HTML report is refused — the two
    payloads are not interchangeable, and overwriting one with the other would leave a row
    that claims to be both.

    Returns the card metadata plus `document_url` / `download_url` (absolute, environment
    correct) — those are what the agent hands to the person who asked for the file.
    """
    owner, _kind = _identity(request)
    doc_type, filename, data = _decode_document(body, request)
    title = (body.title or "").strip()[:MAX_TITLE_LEN] or _title_from_filename(filename)
    status = _clean_status(body.status, request)
    explicit_slug = (body.slug or "").strip().lower()
    slug = _clean_slug(explicit_slug, title, request)
    cover = _cover_from_body(body, title, request)
    pages = _document_pages(doc_type, data)

    # Resolve the target BEFORE spending storage on it: a slug that belongs to someone
    # else's card, or to an HTML report, must fail without leaving an orphan object.
    previous_key = ""
    if explicit_slug:
        with _db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, doc_object, doc_type, owner_email, visibility "
                            "FROM ai_reports WHERE slug = %s", (slug,))
                existing = cur.fetchone()
        if existing:
            if existing["owner_email"] != owner or existing["visibility"] != "private":
                raise HTTPException(status_code=409,
                                    detail=pick("slug 已被另一张卡片占用；请换一个 / slug belongs to another card; choose a new slug", request_lang(request) if request else None))
            if not existing["doc_type"]:
                raise HTTPException(status_code=409, detail=pick("该 slug 已被 HTML 报告占用 —— 文档需要自己的 slug（先改名或删除那份报告） / that slug is an HTML report — a document needs its own slug (rename or delete the report first)", request_lang(request) if request else None))
            previous_key = existing["doc_object"] or ""
    else:
        # Slugless → ALWAYS suffixed, exactly like `POST /api/reports`: two uploads can
        # never land on one address, and the address says when it landed.
        with _db() as conn:
            with conn.cursor() as cur:
                base = slug[:60]
                for _ in range(5):
                    candidate = base + _collision_suffix()
                    if _slug_taken(cur, candidate) is None:
                        slug = candidate
                        break
                else:
                    raise HTTPException(status_code=409,
                                        detail=pick("无法分配工作区地址；请重试 / could not allocate a workspace address; try again", request_lang(request) if request else None))

    object_key = (f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.{doc_type}")
    try:
        oss_storage.put_object(DOC_BUCKET_PREFIX, object_key, data, content_type=DOC_MIME[doc_type])
    except Exception as exc:                                # noqa: BLE001 — storage is the whole point
        _LOG.error("document upload failed slug=%s: %s", slug, exc)
        raise HTTPException(status_code=502, detail=pick("文件存储失败 / the file could not be stored", request_lang(request) if request else None)) from exc

    try:
        with _db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO ai_reports
                        (slug, title, summary, summary_en, summary_zh, category, tags, author, submitter,
                         status, pinned, accent, html, size_bytes, langs, owner_email, visibility, kind,
                         doc_type, doc_object, doc_mime, doc_name, doc_pages)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '', %s, '', %s, 'private',
                            'document', %s, %s, %s, %s, %s)
                    ON CONFLICT (slug) DO UPDATE SET
                        title      = EXCLUDED.title,
                        summary    = EXCLUDED.summary,
                        summary_en = EXCLUDED.summary_en,
                        summary_zh = EXCLUDED.summary_zh,
                        category   = EXCLUDED.category,
                        tags       = EXCLUDED.tags,
                        author     = EXCLUDED.author,
                        submitter  = EXCLUDED.submitter,
                        status     = EXCLUDED.status,
                        pinned     = EXCLUDED.pinned,
                        accent     = EXCLUDED.accent,
                        size_bytes = EXCLUDED.size_bytes,
                        kind       = 'document',
                        doc_type   = EXCLUDED.doc_type,
                        doc_object = EXCLUDED.doc_object,
                        doc_mime   = EXCLUDED.doc_mime,
                        doc_name   = EXCLUDED.doc_name,
                        doc_pages  = EXCLUDED.doc_pages,
                        updated_at = NOW()
                    WHERE ai_reports.owner_email = EXCLUDED.owner_email
                      AND ai_reports.visibility = 'private'
                    RETURNING id, created_at, updated_at, slug
                """, (
                    slug, title,
                    (body.summary or "").strip()[:MAX_SUMMARY_LEN],
                    (body.summary_en or "").strip()[:MAX_SUMMARY_LEN],
                    (body.summary_zh or "").strip()[:MAX_SUMMARY_LEN],
                    (body.category or "Documents")[:MAX_CATEGORY_LEN],
                    (body.tags or "")[:MAX_TAGS_LEN],
                    (body.author or "agent")[:MAX_AUTHOR_LEN],
                    (body.submitter or "")[:MAX_AUTHOR_LEN],
                    status, bool(body.pinned), (body.accent or "")[:40],
                    len(data), owner, doc_type, object_key, DOC_MIME[doc_type], filename, pages,
                ))
                saved = cur.fetchone()
                if not saved:
                    raise HTTPException(status_code=409,
                                        detail=pick("slug 已被另一张卡片占用；请换一个 / slug belongs to another card; choose a new slug", request_lang(request) if request else None))
                _store_cover(cur, slug, cover)
    except HTTPException:
        # Nothing referenced the new object, so don't leave it behind.
        _drop_object_quietly(object_key)
        raise
    except Exception as exc:                                # noqa: BLE001 — a DB failure is a 502 here
        _LOG.error("document publish failed slug=%s: %s", slug, exc)
        _drop_object_quietly(object_key)
        raise HTTPException(status_code=502, detail=pick("卡片保存失败 / the card could not be saved", request_lang(request) if request else None)) from exc

    if previous_key and previous_key != object_key:
        # Re-uploaded in place: the old bytes are unreferenced *if* no copy still reads them.
        _sweep_document_object(previous_key)
    meta = _load(slug, viewer_email=owner, request=request)
    meta.update({
        "id": saved["id"],
        "created_at": saved["created_at"].isoformat(),
        "updated_at": saved["updated_at"].isoformat(),
        "format": "document",
        "url": _public_report_url(request, slug),
        "document_url": _absolute_url(request, f"{_base_href()}api/reports/{slug}/document"),
        "download_url": _absolute_url(request, f"{_base_href()}api/reports/{slug}/document?download=1"),
    })
    _LOG.info("document published slug=%s type=%s bytes=%d pages=%s",
              slug, doc_type, len(data), pages)
    return meta


@router.get("/{slug}/document")
@_with_schema
async def get_document(slug: str, request: Request, download: bool = Query(False)):
    """The stored file itself. A PDF opens inline; everything else downloads.

    Readable by anyone who may read the card (owner / named share / public snapshot).
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    row = _document_row(slug, request)
    _require_read(row, _identity(request)[0], request)
    return _document_bytes(row, download=download, request=request)


@router.get("/{slug}/document/preview")
@_with_schema
async def get_document_preview(slug: str, request: Request):
    """
    What a reader should render for this card, as data (the viewer asks once, then obeys):

    * `{"kind":"html",  "url": …}`     — markup the reader's browser renders (all four types)
    * `{"kind":"download", "url": …}`  — nothing can be rendered here; download it

    Every type answers `html` now, PDFs included, because the HTML preview is also where the
    note layer lives — see `doc_preview._pdf_html`. A workbook additionally carries `sheets`,
    for a client that wants the rows rather than the grid.

    A parse failure degrades to `download` with a reason instead of a 500: the file is
    still perfectly good, it is only this server that cannot draw it.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    row = _document_row(slug, request)
    _require_read(row, _identity(request)[0], request)
    doc_type = row["doc_type"]
    file_url = f"{_base_href()}api/reports/{slug}/document"
    download_url = file_url + "?download=1"
    module = _doc_preview_module(request)
    payload: dict[str, Any] = {"slug": slug, "doc_type": doc_type, "title": row["title"],
                               "name": row.get("doc_name") or "", "size_bytes": row.get("size_bytes"),
                               "pages": row.get("doc_pages"),
                               # ⚠️ The server decides this, not the client. A hardcoded
                               # list on the frontend is a second answer to "what can be
                               # edited", and the two drift the first time a format is
                               # added. `EDITABLE_DOC_TYPES` in doc_preview is the one
                               # list, and it is also what `apply_edits` enforces.
                               "editable": doc_type in getattr(module, "EDITABLE_DOC_TYPES", ())}
    try:
        data = oss_storage.get_object(DOC_BUCKET_PREFIX, row["doc_object"])
        html = module.preview_html(data, doc_type, row["title"] or "",
                                   max_slides=MAX_DOC_SLIDES)
        if doc_type == "xlsx":
            payload["sheets"] = module.table_preview(data)
        payload.update({"kind": "html",
                        "url": f"{_base_href()}api/reports/{slug}/document/preview.html",
                        "rendered_bytes": len(html)})
    except HTTPException:
        raise
    except Exception as exc:                                # noqa: BLE001 — degrade, don't fail
        _LOG.info("%s preview unavailable slug=%s: %s", doc_type, slug, exc)
        payload.update({"kind": "download", "url": download_url,
                        "reason": f"this {doc_type} could not be rendered for preview"})
    return payload


class DocumentEditsIn(BaseModel):
    """The reader's edits, keyed by the `data-oid` the preview rendered.

    A list of {oid, text} rather than a dict, so a duplicate oid is visible in
    the request instead of silently collapsing — the server keeps the last one and
    says so in `dropped`, but a caller that sends two edits for one paragraph is
    telling us something is wrong on its side.
    """
    edits: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/{slug}/document/content")
@_with_schema
async def save_document_content(slug: str, body: DocumentEditsIn, request: Request):
    """Write the reader's text edits back into the stored file.

    ## What this changes

    The bytes in object storage. There is no "draft" layer: a successful call
    makes the edited text the document, and the next preview reads it back.

    ## Why the previous file is kept

    Re-uploading a document sweeps the old object (`_sweep_document_object`), which
    is right when the owner hands over a replacement on purpose. An in-place edit
    is not that: it is a few words changed inside a file someone else produced, and
    there is no undo in this product. So the previous object is left where it is
    under a versioned key. Nothing prunes those yet — they are inert bytes, and an
    unanswered "can I get last month's version back" is worth more than the
    storage.

    ## What it will not do

    * PDFs are rejected. Their text is glyphs at coordinates; rewriting a word
      means redrawing the page, and no tool does that without changing the
      document in ways the reader did not ask for.
    * Intra-paragraph MIXED formatting does not survive an edit (bold in the
      middle of a sentence collapses to the first run). Paragraph style, images,
      tables, page breaks and everything not edited are preserved.
    * An oid that no longer resolves is reported in `dropped`, not guessed at.
    """
    lang = request_lang(request)
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", lang))
    row = _document_row(slug, request)
    email, _kind = _identity(request)
    # Ownership, same rule the client is shown as `can_manage`. 404 rather than
    # 403 so a stranger cannot learn that a private document exists.
    if not email or row.get("owner_email") != email:
        raise HTTPException(status_code=404, detail=pick("文档不存在 / document not found", lang))

    doc_type = (row.get("doc_type") or "").lower()
    module = _doc_preview_module(request)
    if doc_type not in getattr(module, "EDITABLE_DOC_TYPES", ()):
        raise HTTPException(
            status_code=409,
            detail=pick(f"这个 {doc_type} 不能就地编辑，请直接下载后修改 / this {doc_type} cannot be edited in place — download it and edit the file", lang))

    # Last write for an oid wins, and the earlier ones are reported: a duplicated
    # paragraph edit means the client lost track, which the caller should see.
    edits: dict[str, str] = {}
    duplicates: list[str] = []
    for item in body.edits or []:
        oid = str(item.get("oid") or "")
        if not oid:
            continue
        if oid in edits:
            duplicates.append(oid)
        edits[oid] = str(item.get("text") or "")
    if not edits:
        raise HTTPException(status_code=400, detail=pick("没有要保存的修改 / there are no edits to save", lang))

    try:
        source = oss_storage.get_object(DOC_BUCKET_PREFIX, row["doc_object"])
    except Exception as exc:                                # noqa: BLE001
        _LOG.error("document read failed slug=%s: %s", slug, exc)
        raise HTTPException(status_code=502, detail=pick("读不到文档原文件 / the stored file could not be read", lang)) from exc

    try:
        data, applied, dropped = module.apply_edits(source, doc_type, edits)
    except Exception as exc:                                # noqa: BLE001
        _LOG.warning("document edit failed slug=%s type=%s: %s", slug, doc_type, exc)
        raise HTTPException(status_code=422, detail=pick(f"无法修改这个 {doc_type}：{exc} / this {doc_type} could not be edited", lang)) from exc

    dropped = list(dropped) + duplicates
    if not applied:
        # Nothing was written, so nothing is stored. A save that reports success
        # while leaving the file alone is the one outcome worth failing loudly for.
        raise HTTPException(
            status_code=409,
            detail=pick("这些修改已经对不上当前文档了，请重新打开后再试 / these edits no longer match the document — reopen it and try again", lang))

    # A NEW key, never an overwrite. See the docstring for why the old bytes stay.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    object_key = f"{row['doc_object']}.v{stamp}-{secrets.token_hex(3)}"
    try:
        oss_storage.put_object(DOC_BUCKET_PREFIX, object_key, data,
                               content_type=DOC_MIME.get(doc_type))
    except Exception as exc:                                # noqa: BLE001
        _LOG.error("document save failed slug=%s: %s", slug, exc)
        raise HTTPException(status_code=502, detail=pick("文件写入失败 / the edited file could not be stored", lang)) from exc

    # ⚠️ Guarded on `doc_object`, not on the slug alone. Two people can have the
    # same document open; if the second save just writes the row, it silently
    # discards the first one's file and the first person watches their "saved"
    # work disappear. Comparing the key we READ is what turns that into a 409 the
    # second person can act on. Same rule as the deploy receiver refusing an event
    # for a commit that is no longer current.
    stale = False
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ai_reports SET doc_object = %s, size_bytes = %s, "
                            "updated_at = NOW() "
                            "WHERE slug = %s AND doc_object = %s RETURNING id",
                            (object_key, len(data), slug, row["doc_object"]))
                stale = cur.fetchone() is None
    except Exception as exc:                                # noqa: BLE001
        _LOG.error("document save failed slug=%s: %s", slug, exc)
        _drop_object_quietly(object_key)
        raise HTTPException(status_code=502, detail=pick("文件保存失败 / the edit could not be recorded", lang)) from exc

    if stale:
        # The bytes we just stored belong to nobody; drop them rather than leave
        # an orphan object that a later sweep would have to guess about.
        _drop_object_quietly(object_key)
        raise HTTPException(
            status_code=409,
            detail=pick("这份文档刚被另一次保存更新过，请重新打开后再改 / this document was just saved elsewhere — reopen it and edit again", lang))

    return {"ok": True, "slug": slug, "doc_type": doc_type,
            "applied": applied, "dropped": dropped,
            "size_bytes": len(data), "pages": row.get("doc_pages")}


@router.get("/{slug}/document/preview.html", response_class=HTMLResponse)
@_with_schema
async def get_document_preview_html(slug: str, request: Request):
    """Any document card's preview as markup, for the READER'S browser to render.

    ⚠️ This is the route the reader page embeds, and the reason is fonts: the PDF version is
    rendered by headless Chromium inside the API image, which ships no CJK font at all, so every
    Chinese character came out as a box ("docx 和 pptx 里面…显示的都是问号问号", 2026-10-01). The
    markup is rendered on the reader's machine instead, which has the fonts the reader reads in.

    All four types, including the ones whose own format is already a rendering (a PDF, a
    workbook): the preview is also where the note layer lives, so a PDF page has to be ours
    rather than a plugin's. See `doc_preview._pdf_html`.

    Nothing is cached server-side: `preview_html` is pure Python over the stored bytes (no
    browser), and the body is generated in milliseconds. `/document/preview.pdf` still exists and
    still works for anything that wants a file — and it deliberately does NOT get the note layer,
    because a printed page cannot be annotated.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    row = _document_row(slug, request)
    email, _ = _identity(request)
    _require_read(row, email, request)
    doc_type = row["doc_type"]
    module = _doc_preview_module(request)
    try:
        source = oss_storage.get_object(DOC_BUCKET_PREFIX, row["doc_object"])
        html = module.preview_html(source, doc_type, row["title"] or "",
                                   max_slides=MAX_DOC_SLIDES)
    except HTTPException:
        raise
    except Exception as exc:                                # noqa: BLE001 — the reader gets a clear 422
        _LOG.warning("document preview html failed slug=%s: %s", slug, exc)
        raise HTTPException(status_code=422,
                            detail=pick(f"这个 {doc_type} 无法渲染预览 —— 请直接下载 / this {doc_type} could not be rendered for preview — download it instead", request_lang(request) if request else None)) from exc
    html = _preview_with_notes(html, row, pages=module.PAGE_SELECTOR, viewer_email=email)
    return HTMLResponse(content=html, headers={"Cache-Control": "private, no-store",
                                               "X-Content-Type-Options": "nosniff"})


@router.get("/{slug}/document/preview.pdf")
@_with_schema
async def get_document_preview_pdf(slug: str, request: Request):
    """A pptx / docx rendered to PDF, cached in storage after the first reader asks.

    The cache key carries `updated_at`, so re-uploading the file cannot serve the old
    rendering; the render itself is Chromium (the same browser the report exports use),
    which is why it is worth caching — a reader should not wait seconds twice.
    """
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=400, detail=pick("无效的报告 slug / invalid report slug", request_lang(request) if request else None))
    row = _document_row(slug, request)
    _require_read(row, _identity(request)[0], request)
    doc_type = row["doc_type"]
    if doc_type not in ("pptx", "docx"):
        raise HTTPException(status_code=400,
                            detail=pick(f"{doc_type} 卡片直接预览；本路由渲染 pptx 和 docx / a {doc_type} card is previewed directly; this route renders pptx and docx", request_lang(request) if request else None))
    stamp = int(row["updated_at"].timestamp()) if row.get("updated_at") else 0
    cache_key = f"preview/{doc_type}-{slug}-{stamp}.pdf"
    try:
        data = oss_storage.get_object(DOC_BUCKET_PREFIX, cache_key)
    except Exception:                                       # noqa: BLE001 — a cache miss is normal
        data = b""
    if not data:
        module = _doc_preview_module(request)
        try:
            source = oss_storage.get_object(DOC_BUCKET_PREFIX, row["doc_object"])
            html = module.preview_html(source, doc_type, row["title"] or "", max_slides=MAX_DOC_SLIDES)
            data = await module.html_to_pdf(html, width=1280, landscape=True, margin_mm=0.0)
        except HTTPException:
            raise
        except Exception as exc:                            # noqa: BLE001 — the reader gets a clear 422
            _LOG.warning("document preview render failed slug=%s: %s", slug, exc)
            raise HTTPException(status_code=422,
                                detail=pick(f"这个 {doc_type} 无法渲染预览 —— 请直接下载 / this {doc_type} could not be rendered for preview — download it instead", request_lang(request) if request else None)) from exc
        try:
            oss_storage.put_object(DOC_BUCKET_PREFIX, cache_key, data, content_type="application/pdf")
        except Exception as exc:                            # noqa: BLE001 — serving beats caching
            _LOG.info("document preview cache write failed slug=%s: %s", slug, exc)
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
               "Content-Disposition": f'inline; filename="{slug}.pdf"'}
    return Response(content=data, media_type="application/pdf", headers=headers)


async def anyone_link_document(token: str, preview: str = "", request: Request | None = None):
    """The stored file, for a reader arriving through a `/s/{token}` link.

    Same assets as the signed-in routes, authenticated by the token instead of a session —
    the token is the capability, exactly as it is for the report body and its images.
    """
    row = _resolve_anyone_link(token, request)
    if not row.get("doc_type"):
        raise HTTPException(status_code=404, detail=pick("这个链接不指向文档 / this link does not point at a document", request_lang(request) if request else None))
    if not preview:
        return _document_bytes(row, download=True, request=request)
    doc_type = row["doc_type"]
    module = _doc_preview_module(request)
    try:
        source = oss_storage.get_object(DOC_BUCKET_PREFIX, row["doc_object"])
        html = module.preview_html(source, doc_type, row.get("title") or "",
                                   max_slides=MAX_DOC_SLIDES)
        # ⚠️ `?preview=html` gives the guest the markup (their browser has the fonts, which is the
        # whole point); anything else truthy keeps the old PDF behaviour.
        if str(preview).strip().lower() in ("html", "markup"):
            # A guest may READ the notes already on this document but never write one: a note is
            # signed by a session (it shows up in a colleague's Inbox under their name), and a
            # share token is not a session. Same split as `/s/{token}/state` and the report body.
            body = _preview_with_notes(
                html, row, pages=module.PAGE_SELECTOR, writable=False,
                read_url=f"{_base_href()}s/{token}/annotations")
            return HTMLResponse(content=body,
                                headers={"Cache-Control": "private, no-store",
                                         "X-Content-Type-Options": "nosniff",
                                         "Referrer-Policy": "no-referrer"})
        if doc_type == "pdf":
            # The file itself, inline: it is already a rendering, and a guest who asked for the
            # PDF wants the PDF.
            return _document_bytes(row, download=False, request=request)
        data = await module.html_to_pdf(html, width=1280, landscape=True, margin_mm=0.0)
    except HTTPException:
        raise
    except Exception as exc:                                # noqa: BLE001 — a guest gets a clear 422
        _LOG.warning("guest document preview failed slug=%s: %s", row.get("slug"), exc)
        raise HTTPException(status_code=422,
                            detail=pick(f"这个 {doc_type} 无法渲染预览 / this {doc_type} could not be rendered for preview", request_lang(request) if request else None)) from exc
    # Not cached: a guest link is revocable at any moment, so nothing derived from it is
    # written to storage (the cache path is the owner's authenticated route).
    return Response(content=data, media_type="application/pdf",
                    headers={"Cache-Control": "private, no-store",
                             "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer",
                             "Content-Disposition": f'inline; filename="{row.get("slug") or "document"}.pdf"'})
