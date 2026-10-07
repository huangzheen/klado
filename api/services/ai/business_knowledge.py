"""Business knowledge base — user-published, permission-isolated markdown documents.

Why this is a separate store
----------------------------
``ai_knowledge_documents`` is the **project manual**: how to use this system
(capabilities, routing, endpoints, presentation rules). It is file-sourced and its
markdown files under ``api/knowledge_docs/`` are the single writer; every signed-in
user reads the same corpus (see ``services/ai/knowledge_docs.py``).

This module is the **business knowledge base**: what the business knows — facts,
definitions, analysis methods. It is written over HTTP, by people and by their
agents, and it is **permission-isolated** per owner. Mixing the two in one table
would destroy the property the manual relies on (``document_id`` is a
user-supplied primary key there, so a published item could overwrite a deployed
rule and then fight the startup reconciler on every restart).

Three tiers, deliberately identical to the Workspace reports
(``routers/reports.py``) so people only learn one mental model:

* ``private``  — owner only (``owner_email``)
* ``shared``   — additionally readable by named colleagues
                 (``ai_knowledge_item_shares``)
* ``public``   — a separate read-only snapshot row (``source_item_id`` points at
                 the private original), readable by every signed-in user

Format
------
**Markdown only.** The body is plain markdown text — no HTML documents, no
uploads. Non-markdown payloads are rejected loudly (``validate_markdown``) rather
than stored and rendered into someone else's page later.

Retrieval
---------
``searchable(email, scope)`` adapts rows into the same ``KnowledgeDocument``
shape the project manual uses, so the agent's knowledge search scores both with
one implementation (``services/ai/knowledge_store.py``). Permission filtering
happens in SQL, before ranking — never after.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import psycopg2.extras

from core.db import connect_main
from services.ai.knowledge_store import KnowledgeDocument

# A business wiki page is markdown, not an archive. 256 KB of text is far more
# than anyone types and small enough that the retriever can keep loading pages
# into memory (see the scaling note in routers/knowledge.py).
MAX_BODY_BYTES = 256 * 1024
MAX_TITLE = 200
MAX_SUMMARY = 500
MAX_TAGS = 400
MAX_SLUG = 80

ITEM_NAMESPACE = "kb"          # document_id prefix used by the knowledge search
VISIBILITIES = ("private", "public")
STATUSES = ("published", "draft", "archived")
# Kept small and open-ended: these are declared by the author and used for
# ranking/filtering only. The project manual's stricter enum does not apply here.
DOC_TYPES = ("note", "definition", "method", "market", "product", "channel", "other")

# ── 未归档, the one container every page lands in when nothing says otherwise ──
#
# Both a project and a folder are needed, so both names have to render in both
# languages. They are pairs in the frontend's sense (one slash, right side starts
# with a letter) because `core/i18n.py::pick()` takes the same shape server-side.
UNFILED_TITLE = "未归档 / Unfiled"

#: 10 hex characters of the account's own address. Deterministic, so the unfiled
#: project needs no lookup to find, and per-account, so two accounts never collide
#: on a globally unique `slug` column. A hash is used rather than a counter because
#: the slug has to be computable on a read path that must not write.
UNFILED_SLUG_PREFIX = "unfiled-"


def unfiled_slug(email: str) -> str:
    """The slug of this account's own 未归档 project. Pure; writes nothing."""
    digest = hashlib.sha256(_text(email).lower().encode("utf-8")).hexdigest()
    return UNFILED_SLUG_PREFIX + digest[:10]


class BusinessKnowledgeError(RuntimeError):
    pass


class BusinessKnowledgeConflict(BusinessKnowledgeError):
    """A page this account may read already carries the same title.

    The wiki is written by people *and* by their agents, so the same subject gets
    written down twice from two accounts. A duplicate is not a technical error — it is
    a business decision — so the write is refused once and the caller has to show the
    owner what it collides with before re-sending ``confirm_conflict``.
    """

    def __init__(self, title: str, conflicts: list[dict]):
        super().__init__(f"a page with this title already exists ({len(conflicts)})")
        self.title = title
        self.conflicts = conflicts


@dataclass(frozen=True)
class KnowledgeItem:
    id: int
    slug: str
    title: str
    summary: str = ""
    # The one-line summary per language. A bilingual page carries both — the page list's
    # hover tooltip shows them, and one line for a two-language page leaves one of the two
    # readers with a summary in the wrong language (see `bilingual_summaries`).
    summary_en: str = ""
    summary_zh: str = ""
    category: str = ""
    tags: str = ""
    document_type: str = "note"
    body: str = ""
    author: str = "agent"
    submitter: str = ""
    version: str = "1.0"
    status: str = "published"
    # The curator's 🌟 switch: lit = the page feeds the agent's knowledge search;
    # dim = out of retrieval in every scope, while the page itself stays readable
    # and listed (semantics fixed with the user 2026-09-30).
    enabled: bool = True
    owner_email: str = ""
    visibility: str = "private"
    source_item_id: int | None = None
    size_bytes: int = 0
    # ── placement: project → folder → page (2026-10-03) ──────────────────────
    #
    # ⚠️ **NULL means "未归档", and it is not a bug to backfill.** Both columns were
    # added to a table that was already full of pages, so NULL is the honest
    # description of "this page was written before projects existed". The read path
    # resolves NULL to the owner's unfiled project on the way out
    # (`services/ai/knowledge_projects.py::resolve_placement`), and `move_item()`
    # is the only writer — so the moment a page is filed the value is real. A
    # backfill migration would have had to invent a project row per owner anyway.
    project_slug: str = ""
    folder_id: int | None = None
    created_at: Any = None
    updated_at: Any = None
    shared_emails: list[str] = field(default_factory=list)

    def document_id(self) -> str:
        return f"{ITEM_NAMESPACE}:{self.slug}"

    def tag_list(self) -> list[str]:
        return [tag.strip() for tag in (self.tags or "").split(",") if tag.strip()]


# ── schema ───────────────────────────────────────────────────────────────────

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_tables() -> None:
    """Create/upgrade the business knowledge tables. Idempotent, safe on startup."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        try:
            conn = connect_main()
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_knowledge_items (
                        id            SERIAL PRIMARY KEY,
                        slug          TEXT UNIQUE NOT NULL,
                        title         TEXT NOT NULL,
                        summary       TEXT NOT NULL DEFAULT '',
                        summary_en    TEXT NOT NULL DEFAULT '',
                        summary_zh    TEXT NOT NULL DEFAULT '',
                        category      TEXT NOT NULL DEFAULT '',
                        tags          TEXT NOT NULL DEFAULT '',
                        document_type TEXT NOT NULL DEFAULT 'note',
                        body          TEXT NOT NULL DEFAULT '',
                        author        TEXT NOT NULL DEFAULT 'agent',
                        submitter     TEXT NOT NULL DEFAULT '',
                        version       TEXT NOT NULL DEFAULT '1.0',
                        status        TEXT NOT NULL DEFAULT 'published',
                        owner_email   TEXT,
                        visibility    TEXT,
                        source_item_id INTEGER,
                        size_bytes    INTEGER NOT NULL DEFAULT 0,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                # Existing deployments: CREATE TABLE IF NOT EXISTS never adds columns.
                for statement in (
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS summary_en TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS summary_zh TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS tags TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS document_type TEXT NOT NULL DEFAULT 'note'",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS submitter TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT '1.0'",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'published'",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS source_item_id INTEGER",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS size_bytes INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS owner_email TEXT",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS visibility TEXT",
                    # The 🌟 curator switch (2026-09-30). Existing pages stay lit.
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE",
                    # ── placement (2026-10-03): project → folder → page ───────────
                    # Nullable **on purpose**: see KnowledgeItem.project_slug. NULL is
                    # "未归档", resolved on read, so no data migration is needed for
                    # the pages that already exist.
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS project_slug TEXT",
                    "ALTER TABLE ai_knowledge_items ADD COLUMN IF NOT EXISTS folder_id INTEGER",
                ):
                    cur.execute(statement)
                cur.execute("UPDATE ai_knowledge_items SET owner_email = '' WHERE owner_email IS NULL")
                cur.execute("UPDATE ai_knowledge_items SET visibility = 'private' WHERE visibility IS NULL")
                cur.execute("ALTER TABLE ai_knowledge_items ALTER COLUMN owner_email SET NOT NULL")
                cur.execute("ALTER TABLE ai_knowledge_items ALTER COLUMN visibility SET NOT NULL")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_items_owner_idx "
                            "ON ai_knowledge_items (owner_email, visibility, updated_at DESC)")
                # One public snapshot per private original — the reports model, so a
                # re-publish refreshes instead of accumulating duplicates.
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ai_knowledge_items_public_source_idx "
                            "ON ai_knowledge_items (source_item_id) "
                            "WHERE visibility = 'public' AND source_item_id IS NOT NULL")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_knowledge_item_shares (
                        item_id         INTEGER NOT NULL REFERENCES ai_knowledge_items(id) ON DELETE CASCADE,
                        recipient_email TEXT NOT NULL,
                        shared_by       TEXT NOT NULL,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (item_id, recipient_email)
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_item_shares_recipient_idx "
                            "ON ai_knowledge_item_shares (recipient_email, item_id)")
                # ── projects and folders (2026-10-03) ────────────────────────────
                #
                # A project is the top-level container the user thinks of as a "main
                # folder" and sees as a card; a folder is the one level below it. Every
                # project gets exactly one `system` folder — 未归档 — and every account
                # gets exactly one `system` project, its own catch-all. Both are
                # created lazily by `knowledge_projects.ensure_unfiled()`.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_knowledge_projects (
                        id           SERIAL PRIMARY KEY,
                        slug         TEXT UNIQUE NOT NULL,
                        title        TEXT NOT NULL,
                        summary      TEXT NOT NULL DEFAULT '',
                        system       BOOLEAN NOT NULL DEFAULT FALSE,
                        -- The cover, **the same seven columns `ai_reports` keeps**, so
                        -- `services/report_cover.py::resolve_cover` serves projects,
                        -- reports, dashboards and calendar events from one function
                        -- instead of a second copy drifting (see the note in
                        -- `services/dashboard_store.py`).
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
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_projects_owner_idx "
                            "ON ai_knowledge_projects (owner_email, updated_at DESC)")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_knowledge_folders (
                        id           SERIAL PRIMARY KEY,
                        project_slug TEXT NOT NULL
                                   REFERENCES ai_knowledge_projects(slug) ON DELETE CASCADE,
                        title        TEXT NOT NULL,
                        system       BOOLEAN NOT NULL DEFAULT FALSE,
                        sort_order   INTEGER NOT NULL DEFAULT 0,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_folders_project_idx "
                            "ON ai_knowledge_folders (project_slug, sort_order, id)")
                # One 未归档 per project, enforced by the index rather than by code:
                # the folder every unfiled page resolves to has to be findable by a
                # unique key, because two of them would split the pages in half.
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ai_knowledge_folders_system_idx "
                            "ON ai_knowledge_folders (project_slug) WHERE system")
                # Sharing a FOLDER, not the pages in it: a row here is read by the
                # permission predicate, so a page filed into a shared folder later
                # inherits the grant with no second write.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_knowledge_folder_shares (
                        folder_id       INTEGER NOT NULL
                                        REFERENCES ai_knowledge_folders(id) ON DELETE CASCADE,
                        recipient_email TEXT NOT NULL,
                        shared_by       TEXT NOT NULL,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (folder_id, recipient_email)
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_folder_shares_recipient_idx "
                            "ON ai_knowledge_folder_shares (recipient_email, folder_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_items_project_idx "
                            "ON ai_knowledge_items (project_slug) WHERE project_slug IS NOT NULL")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_knowledge_items_folder_idx "
                            "ON ai_knowledge_items (folder_id) WHERE folder_id IS NOT NULL")
                conn.commit()
            finally:
                cur.close()
                conn.close()
        except Exception as exc:  # pragma: no cover - surfaced as a 503
            raise BusinessKnowledgeError(f"业务知识库表初始化失败: {exc} / could not initialise the business knowledge tables: {exc}") from exc
        _schema_ready = True


# ── validation (markdown only) ───────────────────────────────────────────────

_HTML_DOCUMENT_RE = re.compile(r"^\s*(?:<!DOCTYPE\s+html|<!doctype\s+html|<html[\s>])", re.I)
# ⚠️ CJK is deliberately KEPT in slugs. This product's titles are overwhelmingly
# Chinese, and the old `[^a-z0-9._-]+` stripped every one of them to nothing, so
# `clean_slug` fell through to the literal string "untitled" — every Chinese page
# then collided on the same slug, and the collision retry gave up after 6 tries
# with "could not allocate a unique slug". So the *second* Chinese page of a kind
# could not be created at all. CJK is URL-safe (percent-encoded on the wire),
# readable in the address bar, and what a reader would actually type to find the
# page again. What must still go: whitespace, punctuation, path separators.
_SLUG_RE = re.compile(r"[^\w.\-一-鿿]+", re.UNICODE)


def _text(value: Any, *, limit: int | None = None) -> str:
    out = str(value if value is not None else "").strip()
    return out[:limit] if limit else out


def validate_markdown(body: Any) -> str:
    """Return the storable markdown body, or explain why the payload is rejected.

    **Markdown only** is enforced here rather than trusted from the caller: an
    HTML document pasted into the body would be stored verbatim and rendered in
    someone else's page (see the sanitising renderer in the frontend), and a
    binary blob would poison the knowledge search for everyone.
    """
    if body is None:
        raise BusinessKnowledgeError("缺少 body（markdown） / body is required (markdown)")
    if not isinstance(body, str):
        raise BusinessKnowledgeError("body 必须是 markdown 字符串 / body must be a markdown string")
    text = body.strip()
    if not text:
        raise BusinessKnowledgeError("body 为空 / body is empty")
    if _HTML_DOCUMENT_RE.match(text):
        raise BusinessKnowledgeError(
            "本知识库只接受 markdown，不接受 HTML 文档 —— 请把内容整理成 markdown"
            "（报告发布走 /api/reports）"
            " / this knowledge base takes markdown, not an HTML document — send the "
            "content as markdown (the Workspace report endpoint is /api/reports)"
        )
    size = len(text.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise BusinessKnowledgeError(
            f"body 为 {size} 字节，markdown 上限 {MAX_BODY_BYTES} 字节"
            f" / body is {size} bytes; the markdown limit is {MAX_BODY_BYTES} bytes"
        )
    control = sum(1 for ch in text[:4000] if ord(ch) < 9 or 13 < ord(ch) < 32)
    if control > max(4, len(text[:4000]) // 100):
        raise BusinessKnowledgeError("body 不像文本（已拒绝二进制内容) / body does not look like text (binary content rejected)")
    return text


def clean_slug(raw: Any, fallback_title: str) -> str:
    """Lowercase slug for the URL and for the retrieval document_id."""
    candidate = _text(raw).lower().replace(" ", "-")
    candidate = _SLUG_RE.sub("-", candidate).strip("-.")
    if not candidate:
        candidate = _SLUG_RE.sub("-", _text(fallback_title).lower().replace(" ", "-")).strip("-.")
    if not candidate:
        candidate = "untitled"
    return candidate[:MAX_SLUG]


def _clean_choice(raw: Any, allowed: tuple[str, ...], default: str) -> str:
    value = _text(raw).lower()
    return value if value in allowed else default


# ── permissions ──────────────────────────────────────────────────────────────

def may_read(row: dict, email: str) -> bool:
    """The three-tier read rule. Same shape as ``routers/reports.py::_may_read``."""
    if row.get("owner_email") == email:
        return True
    if row.get("visibility") == "public" and row.get("status") == "published":
        return True
    if row.get("visibility") != "private" or row.get("status") != "published":
        return False
    if _share_exists(row.get("id"), email):
        return True
    # The folder grant — see the note in READABLE_PREDICATE. Without this the
    # Python rule and the SQL rule would disagree, and a page filed into a shared
    # folder would be readable here and invisible to every list and search.
    return bool(row.get("folder_id")) and _folder_share_exists(row.get("folder_id"), email)


# The same rule as SQL, for the retrieval path: filtering must happen in the
# database *before* ranking, never by fetching everything and dropping rows in
# Python — that is how a correct-looking `if` still leaks through an excerpt.
READABLE_PREDICATE = (
    "(owner_email = %s"
    " OR (visibility = 'public' AND status = 'published')"
    " OR (visibility = 'private' AND status = 'published' AND EXISTS ("
    "SELECT 1 FROM ai_knowledge_item_shares s"
    " WHERE s.item_id = ai_knowledge_items.id AND s.recipient_email = %s))"
    # A folder share is a share of **every page filed in that folder**, so this is a
    # second grant rather than a third tier: it has the same visibility/status
    # conditions as the item share above. It takes a THIRD %s — see the call sites
    # (`_title_conflicts_with`, `searchable`) which pass one email per branch.
    " OR (visibility = 'private' AND status = 'published' AND EXISTS ("
    "SELECT 1 FROM ai_knowledge_folder_shares fs"
    " WHERE fs.folder_id = ai_knowledge_items.folder_id AND fs.recipient_email = %s)))"
)


def _may_read_with(cur, row: dict, email: str) -> bool:
    """Same decision as ``may_read`` but reusing an open cursor (no second connection)."""
    if row.get("owner_email") == email:
        return True
    if row.get("visibility") == "public" and row.get("status") == "published":
        return True
    if row.get("visibility") != "private" or row.get("status") != "published":
        return False
    cur.execute("SELECT 1 FROM ai_knowledge_item_shares WHERE item_id = %s AND recipient_email = %s",
                (row.get("id"), email))
    if cur.fetchone() is not None:
        return True
    # The folder grant. A page that is not filed in a shared folder cannot be read by
    # this reader — including one in the reader's *own* unfiled folder, because that
    # folder is per-account and nobody else's share can reach into it.
    if not row.get("folder_id"):
        return False
    cur.execute("SELECT 1 FROM ai_knowledge_folder_shares WHERE folder_id = %s AND recipient_email = %s",
                (row.get("folder_id"), email))
    return cur.fetchone() is not None


def _share_exists(item_id: Any, email: str) -> bool:
    if not item_id:
        return False
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ai_knowledge_item_shares WHERE item_id = %s AND recipient_email = %s",
                        (item_id, email))
            return cur.fetchone() is not None


def _folder_share_exists(folder_id: Any, email: str) -> bool:
    if not folder_id:
        return False
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ai_knowledge_folder_shares "
                        "WHERE folder_id = %s AND recipient_email = %s", (folder_id, email))
            return cur.fetchone() is not None


# ── connection helper ────────────────────────────────────────────────────────

class _db:
    """Minimal context manager around the shared main connection."""

    def __enter__(self):
        self._conn = connect_main()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


_SELECT_COLS = ("id, slug, title, summary, summary_en, summary_zh, category, tags, document_type, body, author, "
                "submitter, version, status, owner_email, visibility, source_item_id, "
                "size_bytes, project_slug, folder_id, created_at, updated_at, enabled")


def _row_to_item(row: dict, shared: list[str] | None = None) -> KnowledgeItem:
    return KnowledgeItem(
        id=row["id"], slug=row["slug"], title=row["title"], summary=row.get("summary") or "",
        summary_en=row.get("summary_en") or "", summary_zh=row.get("summary_zh") or "",
        category=row.get("category") or "", tags=row.get("tags") or "",
        document_type=row.get("document_type") or "note", body=row.get("body") or "",
        author=row.get("author") or "agent", submitter=row.get("submitter") or "",
        version=row.get("version") or "1.0", status=row.get("status") or "published",
        owner_email=row.get("owner_email") or "", visibility=row.get("visibility") or "private",
        source_item_id=row.get("source_item_id"), size_bytes=row.get("size_bytes") or 0,
        project_slug=row.get("project_slug") or "", folder_id=row.get("folder_id"),
        created_at=row.get("created_at"), updated_at=row.get("updated_at"),
        enabled=bool(row.get("enabled", True)),
        shared_emails=shared or [],
    )


def _shared_emails(cur, item_id: int) -> list[str]:
    cur.execute("SELECT recipient_email FROM ai_knowledge_item_shares WHERE item_id = %s "
                "ORDER BY recipient_email", (item_id,))
    return [row["recipient_email"] for row in cur.fetchall()]


# ── CRUD ─────────────────────────────────────────────────────────────────────

# ── bilingual pages, and the summary that has to match ───────────────────────
#
# A page may carry its content twice, once per language, separated by a marker line.
# `routers/business_knowledge.py` writes what the reader's page renders; this is the
# server's own reading of the same convention, used only to decide what a summary owes.

_PAGE_LANG_MARKER = re.compile(
    r"^[ \t]*<!--[ \t]*lang[ \t]*:[ \t]*([A-Za-z][A-Za-z-]*)[ \t]*-->[ \t]*$", re.M)


def body_langs(body: str) -> list[str]:
    """The languages a page declares, in document order. Only zh / en are languages here."""
    seen: list[str] = []
    for raw in _PAGE_LANG_MARKER.findall(body or ""):
        code = raw.strip().lower().split("-")[0]
        if code in ("en", "zh") and code not in seen:
            seen.append(code)
    return seen


def is_bilingual_body(body: str) -> bool:
    return len(body_langs(body)) > 1


def bilingual_summaries(plain: Any, english: Any, chinese: Any, body: str) -> tuple[str, str, str]:
    """Resolve a page's summary from the plain field and the per-language pair.

    ⚠️ **A bilingual page must carry a summary in both of its languages.** The page list's
    hover tooltip shows the lines, and one line for a two-language page leaves one of the two
    readers with a summary in the wrong language — nothing about the page would look wrong.
    Returns ``(summary, summary_en, summary_zh)``.

    `summary` stays: it is what a single-language page uses and what the client falls back to,
    and on a bilingual page it defaults to the English line. A page that is not bilingual is
    never asked for the pair, so writing in one language stays a first-class option.
    """
    plain = _text(plain, limit=MAX_SUMMARY)
    english = _text(english, limit=MAX_SUMMARY)
    chinese = _text(chinese, limit=MAX_SUMMARY)
    if is_bilingual_body(body) and not (english and chinese):
        missing = [name for name, value in (("summary_en", english), ("summary_zh", chinese)) if not value]
        raise BusinessKnowledgeError(
            "这个页面是双语的（" + ", ".join(body_langs(body)) + "）：必须同时携带两种语言的摘要。"
            "请一并提交 summary_en 与 summary_zh（各一行）。缺失: " + ", ".join(missing)
            + "。对双语页面只有 summary 不够。"
            " / this page is bilingual (" + ", ".join(body_langs(body)) + "): it must carry a summary "
            "in both languages. Send `summary_en` and `summary_zh` — one line each. Missing: "
            + ", ".join(missing) + ". `summary` alone is not enough for a bilingual page.")
    return (plain or english or chinese), english, chinese


def _normalise_title(title: Any) -> str:
    return " ".join(str(title or "").split()).lower()


# Same title, readable by this account, different page. Compared on the *title* and
# not the slug: two accounts writing about the same subject pick different slugs and so
# never collide on the unique index — which is exactly how the duplicate stays invisible.
_CONFLICT_SQL = (
    "SELECT slug, title, owner_email, visibility, status, updated_at"
    " FROM ai_knowledge_items"
    " WHERE slug <> %s"
    "   AND lower(btrim(regexp_replace(title, '\\s+', ' ', 'g'))) = %s"
    "   AND " + READABLE_PREDICATE +
    " ORDER BY updated_at DESC LIMIT 10"
)


def find_title_conflicts(title: str, email: str, *, exclude_slug: str = "") -> list[dict]:
    """Pages this account may read that already use this title."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return _title_conflicts_with(cur, title, email, exclude_slug=exclude_slug)


def _title_conflicts_with(cur, title: str, email: str, *, exclude_slug: str = "") -> list[dict]:
    # One email per branch of READABLE_PREDICATE — there are three now (own,
    # item share, folder share), and a count that is one short fails at the
    # database, not at the assertion that was supposed to catch it.
    cur.execute(_CONFLICT_SQL, (exclude_slug, _normalise_title(title), email, email, email))
    return [
        {
            "slug": row["slug"],
            "title": row["title"],
            "owner_email": row["owner_email"],
            "visibility": row["visibility"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
        }
        for row in cur.fetchall()
    ]


def upsert_item(payload: dict, email: str, *, slug: str | None = None) -> KnowledgeItem:
    """Create or update one markdown item. Updating requires owning the slug."""
    ensure_tables()
    body = validate_markdown(payload.get("body"))
    title = _text(payload.get("title"), limit=MAX_TITLE)
    if not title:
        raise BusinessKnowledgeError("缺少标题 / title is required")
    existing_slug = _text(slug or payload.get("slug"))
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = None
            if existing_slug:
                cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s FOR UPDATE",
                            (_text(existing_slug),))
                row = cur.fetchone()
                if row and row["owner_email"] != email:
                    raise PermissionError("this knowledge item belongs to another account")
            target_slug = row["slug"] if row else clean_slug(existing_slug, title)
            summary, summary_en, summary_zh = bilingual_summaries(
                payload.get("summary"), payload.get("summary_en"), payload.get("summary_zh"), body)
            values = {
                "summary": summary,
                "summary_en": summary_en,
                "summary_zh": summary_zh,
                "category": _text(payload.get("category")),
                "tags": _text(payload.get("tags"), limit=MAX_TAGS),
                "document_type": _clean_choice(payload.get("document_type"), DOC_TYPES, "note"),
                "author": _text(payload.get("author")) or "agent",
                "submitter": _text(payload.get("submitter")),
                "version": _text(payload.get("version")) or "1.0",
                "status": _clean_choice(payload.get("status"), STATUSES, "published"),
            }
            if row and row["visibility"] == "public":
                # Editing a public snapshot only happens by re-publishing the original.
                raise PermissionError("this slug is the public snapshot; edit the private original instead")
            if row:
                cur.execute(
                    """
                    UPDATE ai_knowledge_items SET title = %s, summary = %s, summary_en = %s, summary_zh = %s,
                        category = %s, tags = %s,
                        document_type = %s, body = %s, author = %s, submitter = %s, version = %s,
                        status = %s, size_bytes = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (title, values["summary"], values["summary_en"], values["summary_zh"],
                     values["category"], values["tags"], values["document_type"],
                     body, values["author"], values["submitter"], values["version"], values["status"],
                     len(body.encode("utf-8")), row["id"]),
                )
                item_id = row["id"]
            else:
                # A brand-new page is the moment this knowledge gets *published*, so it is
                # where a duplicate has to be surfaced: writing a second page about a
                # subject this account can already read is refused once, and only goes
                # through when the caller says the owner has seen what it collides with.
                if not payload.get("confirm_conflict"):
                    conflicts = _title_conflicts_with(cur, title, email)
                    if conflicts:
                        raise BusinessKnowledgeConflict(title, conflicts)
                collide = 0
                while True:
                    # A SAVEPOINT, not a full rollback. The old `conn.rollback()` in the
                    # UniqueViolation handler discarded the whole transaction, so one
                    # colliding insert threw away every page written earlier in the same
                    # batch — a bulk import silently lost rows and reported only the last
                    # error. psycopg2 has no rollback-to-savepoint helper, so both halves
                    # are issued as SQL on the same cursor.
                    cur.execute("SAVEPOINT knowledge_insert")
                    try:
                        cur.execute(
                            """
                            INSERT INTO ai_knowledge_items
                                (slug, title, summary, summary_en, summary_zh, category, tags,
                                 document_type, body, author,
                                 submitter, version, status, owner_email, visibility, size_bytes)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'private', %s)
                            RETURNING id
                            """,
                            (target_slug, title, values["summary"], values["summary_en"],
                             values["summary_zh"], values["category"], values["tags"],
                             values["document_type"], body, values["author"], values["submitter"],
                             values["version"], values["status"], email, len(body.encode("utf-8"))),
                        )
                        item_id = cur.fetchone()["id"]
                        cur.execute("RELEASE SAVEPOINT knowledge_insert")
                        break
                    except psycopg2.errors.UniqueViolation:
                        # Slug is a URL; a collision must never overwrite someone else's page.
                        # Undo just this statement and try the next candidate.
                        cur.execute("ROLLBACK TO SAVEPOINT knowledge_insert")
                        collide += 1
                        if collide > 6:
                            cur.execute("ROLLBACK TO SAVEPOINT knowledge_insert")
                            raise BusinessKnowledgeError("无法分配唯一 slug / could not allocate a unique slug")
                        target_slug = f"{clean_slug(existing_slug or title, title)[:60]}-{collide + 1}"
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE id = %s", (item_id,))
            saved = cur.fetchone()
            shared = _shared_emails(cur, item_id)
    return _row_to_item(dict(saved), shared)


def list_items(email: str, *, scope: str = "mine", q: str = "", limit: int = 200,
               project: str = "", folder_id: int | None = None,
               unfiled_folder: int | None = None) -> list[KnowledgeItem]:
    """Cards for one view. ``mine`` never leaks another owner's rows.

    ``project`` / ``folder_id`` narrow to one container. They are **read-side**
    filters and deliberately do not change what is readable: the permission clause
    stays in force, so narrowing to a shared folder cannot widen it.

    ⚠️ ``unfiled_folder`` is this account's 未归档 folder id, supplied by the caller
    because the store cannot look it up without importing the containers module
    (which imports this one). It exists for the same reason ``project == unfiled_slug``
    is translated below: a request for 未归档 has to match the pages that carry
    `project_slug IS NULL`, and `folder_id = <the unfiled folder>` matches **nothing**,
    because those rows have `folder_id IS NULL` too. Without it the unfiled folder
    would always report itself empty — the wall's own catch-all, permanently blank.
    """
    ensure_tables()
    scope = scope if scope in {"mine", "public", "shared"} else "mine"
    where, params = [], []
    if scope == "mine":
        where.append("owner_email = %s AND visibility = 'private'")
        params.append(email)
    elif scope == "public":
        where.append("visibility = 'public' AND status = 'published'")
    else:  # shared with me
        # ⚠️ Both grants, not just the item one. A page filed into a folder somebody
        # shared with me is "shared with me" even though nothing was written to
        # `ai_knowledge_item_shares` for it — leaving the second EXISTS out would
        # make the shared tab silently omit exactly the pages the folder feature
        # exists to deliver.
        where.append("visibility = 'private' AND status = 'published' AND owner_email <> %s "
                     "AND (EXISTS (SELECT 1 FROM ai_knowledge_item_shares s "
                     "WHERE s.item_id = ai_knowledge_items.id AND s.recipient_email = %s)"
                     " OR EXISTS (SELECT 1 FROM ai_knowledge_folder_shares fs "
                     "WHERE fs.folder_id = ai_knowledge_items.folder_id "
                     "AND fs.recipient_email = %s))")
        params.extend((email, email, email))
    if q.strip():
        where.append("(title ILIKE %s OR summary ILIKE %s OR summary_en ILIKE %s"
                         " OR summary_zh ILIKE %s OR tags ILIKE %s OR slug ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like] * 6
    if project:
        # ⚠️ The unfiled project is a **real** project (it owns a folder, and the
        # client moves pages into it by slug) but unfiled pages carry
        # `project_slug IS NULL`. Translating here is deliberate: a caller that
        # passed the unfiled slug and got nothing back would be a silent data bug,
        # and a second boolean flag is exactly the kind of thing one call site
        # forgets. See KnowledgeItem.project_slug.
        if project == unfiled_slug(email):
            where.append("project_slug IS NULL")
        else:
            where.append("project_slug = %s")
            params.append(project)
    if folder_id is not None:
        if unfiled_folder is not None and folder_id == unfiled_folder:
            where.append("folder_id IS NULL")
        else:
            where.append("folder_id = %s")
            params.append(folder_id)
    if q.strip():
        where.append("(title ILIKE %s OR summary ILIKE %s OR summary_en ILIKE %s"
                         " OR summary_zh ILIKE %s OR tags ILIKE %s OR slug ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like] * 6
    sql = (f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE " + " AND ".join(where)
           + " ORDER BY updated_at DESC LIMIT %s")
    params.append(max(1, min(limit, 500)))
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            for row in rows:
                row["shared_emails"] = _shared_emails(cur, row["id"])
    return [_row_to_item(row, row["shared_emails"]) for row in rows]


def get_item(slug: str, email: str) -> KnowledgeItem | None:
    """One item, or None when it does not exist **or is not readable** by ``email``.

    Returning None rather than 403 is deliberate: "exists but forbidden" and "does
    not exist" must be indistinguishable, or the error code itself leaks the fact
    that another account has a page with that slug.
    """
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s", (_text(slug),))
            row = cur.fetchone()
            if not row or not _may_read_with(cur, dict(row), email):
                return None
            shared = _shared_emails(cur, row["id"])
    return _row_to_item(dict(row), shared)


def title_of(slug: str) -> str:
    """
    A page's title, with no permission check.

    Only ever used to label an Inbox line for a share the caller has *already*
    been authorised to make (the router calls it after the grant is committed).
    `get_item` cannot serve that purpose: it filters by the reader, and the page
    being shared is private to the sharer.
    """
    ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM ai_knowledge_items WHERE slug = %s", (_text(slug),))
            row = cur.fetchone()
    return str(row[0]) if row else ""


def delete_item(slug: str, email: str) -> bool:
    """Delete a private item the caller owns. Public snapshots are withdrawn separately."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_knowledge_items WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'private'", (_text(slug), email))
            return cur.rowcount > 0


# ── placement: moving a page between folders (2026-10-03) ────────────────────

def move_item(slug: str, email: str, project_slug: str, folder_id: int | None) -> KnowledgeItem:
    """File a private page under ``project_slug`` / ``folder_id``.

    **The only writer of the two placement columns.** Everything else — the item
    PUT, publish, pull — leaves them alone, so "where does this page live" has one
    answer on the page and one code path on the server.

    The page's public snapshot follows, for the same reason ``set_enabled`` mirrors
    the 🌟: a reader who opens the public copy should find it in the folder the
    author filed it in, not back in 未归档.

    Ownership is checked on **both** the page and the target folder. Checking only
    the page would let a caller file their own page into a stranger's folder, which
    then silently grants that folder's shares read access to it.
    """
    ensure_tables()
    project_slug = _text(project_slug)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s FOR UPDATE",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email or row["visibility"] != "private":
                # 404 rather than 403, everywhere else in this module.
                raise PermissionError("knowledge item not found")
            if not project_slug:
                # Moving to 未归档 writes NULLs rather than the unfiled project's slug.
                # Both spell the same place (`list_items` translates), and NULL is the
                # one a page written before this feature already has.
                cur.execute("UPDATE ai_knowledge_items SET project_slug = NULL, folder_id = NULL, "
                            "updated_at = NOW() WHERE id = %s", (row["id"],))
                cur.execute("UPDATE ai_knowledge_items SET project_slug = NULL, folder_id = NULL "
                            "WHERE source_item_id = %s AND visibility = 'public'", (row["id"],))
            else:
                cur.execute(
                    """
                    SELECT f.id FROM ai_knowledge_folders f
                    JOIN ai_knowledge_projects p ON p.slug = f.project_slug
                    WHERE f.id = %s AND f.project_slug = %s AND p.owner_email = %s
                    """,
                    (folder_id, project_slug, email),
                )
                if cur.fetchone() is None:
                    raise BusinessKnowledgeError(
                        "目标文件夹不存在，或不属于你 / the target folder does not exist, or it is not yours")
                cur.execute("UPDATE ai_knowledge_items SET project_slug = %s, folder_id = %s, "
                            "updated_at = NOW() WHERE id = %s", (project_slug, folder_id, row["id"]))
                cur.execute("UPDATE ai_knowledge_items SET project_slug = %s, folder_id = %s "
                            "WHERE source_item_id = %s AND visibility = 'public'",
                            (project_slug, folder_id, row["id"]))
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE id = %s", (row["id"],))
            saved = cur.fetchone()
            shared = _shared_emails(cur, row["id"])
    return _row_to_item(dict(saved), shared)


# ── sharing / publishing (the owner's controls) ──────────────────────────────

def publish_to_public(slug: str, email: str) -> KnowledgeItem:
    """Create or refresh a read-only public snapshot; the private original stays."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s FOR UPDATE",
                        (_text(slug),))
            source = cur.fetchone()
            if not source or source["owner_email"] != email or source["visibility"] != "private":
                raise PermissionError("knowledge item not found")
            if source["status"] != "published":
                raise BusinessKnowledgeError("请先在工作区发布该页面 / publish the item in your workspace first")
            cur.execute("SELECT slug FROM ai_knowledge_items WHERE source_item_id = %s "
                        "AND visibility = 'public' FOR UPDATE", (source["id"],))
            public = cur.fetchone()
            public_slug = public["slug"] if public else f"{source['slug'][:48]}-public"
            # The snapshot carries the **same placement** as the original, so a public
            # reader sees the page filed where its author filed it. Copying the two
            # columns is also what keeps a folder share of the original meaningful for
            # the snapshot's readers — they are different rows, so nothing would
            # otherwise carry over.
            if public:
                cur.execute(
                    """
                    UPDATE ai_knowledge_items SET title = %s, summary = %s, summary_en = %s, summary_zh = %s,
                        category = %s, tags = %s,
                        document_type = %s, body = %s, version = %s, status = %s, size_bytes = %s,
                        enabled = %s, project_slug = %s, folder_id = %s, updated_at = NOW()
                    WHERE slug = %s
                    """,
                    (source["title"], source["summary"], source.get("summary_en") or "",
                     source.get("summary_zh") or "", source["category"], source["tags"],
                     source["document_type"], source["body"], source["version"], source["status"],
                     source["size_bytes"], bool(source.get("enabled", True)),
                     source.get("project_slug"), source.get("folder_id"), public_slug),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO ai_knowledge_items
                        (slug, title, summary, summary_en, summary_zh, category, tags,
                         document_type, body, author, submitter,
                         version, status, owner_email, visibility, source_item_id, size_bytes, enabled,
                         project_slug, folder_id)
                    SELECT %s, title, summary, summary_en, summary_zh, category, tags,
                           document_type, body, author, submitter,
                           version, status, owner_email, 'public', id, size_bytes, enabled,
                           project_slug, folder_id
                    FROM ai_knowledge_items WHERE id = %s
                    """,
                    (public_slug, source["id"]),
                )
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s", (public_slug,))
            saved = cur.fetchone()
    return _row_to_item(dict(saved))


def withdraw_public(slug: str, email: str) -> bool:
    """Withdraw only the public snapshot; pulled private copies remain."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_knowledge_items WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'public'", (_text(slug), email))
            return cur.rowcount > 0


def set_enabled(slug: str, email: str, enabled: bool) -> KnowledgeItem:
    """Toggle the curator's 🌟 switch on one owned page.

    Semantics fixed with the user (2026-09-30): **dim = out of the agent's knowledge
    search in every scope, the owner's included** — this path is what the agent
    answers from — **while the page itself stays readable** (``may_read`` is
    untouched), listed in the wiki, and openable by direct link. Toggling is
    metadata, not content, so ``updated_at`` — the wiki's ordering — does not move.
    The public snapshot mirrors the original, so one toggle retires the knowledge
    from retrieval everywhere at once.
    """
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s FOR UPDATE",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email or row["visibility"] != "private":
                # Same 404 semantics as everywhere else: a stranger cannot even learn
                # that a page exists under this slug.
                raise PermissionError("knowledge item not found")
            cur.execute("UPDATE ai_knowledge_items SET enabled = %s WHERE id = %s",
                        (bool(enabled), row["id"]))
            cur.execute("UPDATE ai_knowledge_items SET enabled = %s "
                        "WHERE source_item_id = %s AND visibility = 'public'",
                        (bool(enabled), row["id"]))
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE id = %s", (row["id"],))
            saved = cur.fetchone()
    return _row_to_item(dict(saved))


def share_with(slug: str, email: str, recipients: list[str], shared_by: str) -> list[str]:
    """Grant named colleagues read access to a private item the caller owns."""
    ensure_tables()
    cleaned = sorted({_text(r).lower() for r in recipients if _text(r)})
    if not cleaned:
        raise BusinessKnowledgeError("至少需要一个收件人邮箱 / at least one recipient email is required")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, owner_email, visibility FROM ai_knowledge_items WHERE slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email or row["visibility"] != "private":
                raise PermissionError("knowledge item not found")
            for recipient in cleaned:
                cur.execute(
                    "INSERT INTO ai_knowledge_item_shares (item_id, recipient_email, shared_by) "
                    "VALUES (%s, %s, %s) ON CONFLICT (item_id, recipient_email) DO NOTHING",
                    (row["id"], recipient, shared_by),
                )
            return _shared_emails(cur, row["id"])


def unshare(slug: str, email: str, recipient: str) -> list[str]:
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, owner_email FROM ai_knowledge_items WHERE slug = %s", (_text(slug),))
            row = cur.fetchone()
            if not row or row["owner_email"] != email:
                raise PermissionError("knowledge item not found")
            cur.execute("DELETE FROM ai_knowledge_item_shares WHERE item_id = %s AND recipient_email = %s",
                        (row["id"], _text(recipient).lower()))
            return _shared_emails(cur, row["id"])


def pull_copy(slug: str, email: str) -> KnowledgeItem:
    """Take an independent private copy of a public or colleague-shared item."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE slug = %s", (_text(slug),))
            source = cur.fetchone()
            if (not source or source["owner_email"] == email
                    or not _may_read_with(cur, dict(source), email)):
                raise PermissionError("knowledge item not found")
            base = source["slug"][:56] + "-copy"
            new_slug, attempt = base, 0
            while True:
                cur.execute("SELECT 1 FROM ai_knowledge_items WHERE slug = %s", (new_slug,))
                if cur.fetchone() is None:
                    break
                attempt += 1
                if attempt > 6:
                    raise BusinessKnowledgeError("无法分配副本 slug / could not allocate a copy slug")
                new_slug = f"{base}-{attempt + 1}"
            # ⚠️ `enabled` rides along, and it is not cosmetic: the column defaults to TRUE, so
            # omitting it here silently RE-LIT a page the curator had switched out of retrieval
            # — a copy of a dimmed page came back into the agent's search, which is the one thing
            # the 🌟 is for (found 2026-10-01 by auditing the star's isolation; the INSERT listed
            # seventeen columns and this was the eighteenth).
            #
            # ⚠️ `project_slug` / `folder_id` are the other two that are **deliberately
            # absent**: a pulled copy lands in 未归档. It is the reader's own copy, filed
            # by nobody, and inheriting the original's folder would put it in a project
            # the reader does not own — a row they can neither see nor file again.
            cur.execute(
                """
                INSERT INTO ai_knowledge_items
                    (slug, title, summary, summary_en, summary_zh, category, tags, document_type,
                     body, author, submitter,
                     version, status, owner_email, visibility, source_item_id, size_bytes, enabled)
                SELECT %s, title, summary, summary_en, summary_zh, category, tags, document_type,
                       body, author, submitter,
                       version, status, %s, 'private', id, size_bytes, enabled
                FROM ai_knowledge_items WHERE id = %s
                """,
                (new_slug, email, source["id"]),
            )
            cur.execute("SELECT id FROM ai_knowledge_items WHERE slug = %s", (new_slug,))
            item_id = cur.fetchone()["id"]
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_knowledge_items WHERE id = %s", (item_id,))
            saved = cur.fetchone()
    return _row_to_item(dict(saved))


# ── retrieval adapter ────────────────────────────────────────────────────────

def searchable(email: str, scope: str) -> list[KnowledgeDocument]:
    """Business items this account may read, shaped like the manual's documents.

    ``scope`` follows the knowledge-search contract:
      ``mine``     — my private items (+ the public snapshots I own)
      ``public``   — every published public snapshot
      ``shared``   — items a colleague shared with me
      ``business`` — all of the above (the default for business knowledge)
      ``all``      — identical to ``business`` here; the caller merges the manual
    """
    ensure_tables()
    scope = scope if scope in {"mine", "public", "shared", "business", "all"} else "business"
    where, params = [], []
    if scope == "mine":
        where.append("owner_email = %s")
        params.append(email)
    elif scope == "public":
        where.append("visibility = 'public' AND status = 'published'")
    elif scope == "shared":
        where.append("visibility = 'private' AND status = 'published' AND owner_email <> %s "
                     "AND EXISTS (SELECT 1 FROM ai_knowledge_item_shares s "
                     "WHERE s.item_id = ai_knowledge_items.id AND s.recipient_email = %s)")
        params.extend((email, email))
    else:
        where.append(READABLE_PREDICATE)
        params.extend((email, email, email))   # three branches — see _title_conflicts_with
    # `AND enabled` is the curator's 🌟 switch: a dimmed page leaves retrieval in
    # every scope — the owner's included, because this path *is* the knowledge the
    # agent answers from. The page itself stays readable (may_read is untouched) and
    # the wiki list keeps showing it, so nothing can be lost by toggling.
    sql = ("SELECT slug, title, body, document_type, version, tags, owner_email, visibility, "
           "source_item_id, category FROM ai_knowledge_items WHERE " + " AND ".join(where)
           + " AND status = 'published' AND enabled")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    documents: list[KnowledgeDocument] = []
    for row in rows:
        documents.append(KnowledgeDocument(
            document_id=f"{ITEM_NAMESPACE}:{row['slug']}",
            title=row["title"] or row["slug"],
            content=row["body"] or "",
            document_type=row["document_type"] or "note",
            source="business-knowledge",
            version=row["version"] or "1.0",
            metadata={
                "tags": [t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
                "owner_email": row["owner_email"],
                "visibility": row["visibility"],
                "category": row["category"] or "",
                # Marked so the retriever can rank curated rules above user notes.
                "knowledge_tier": "business",
            },
        ))
    return documents