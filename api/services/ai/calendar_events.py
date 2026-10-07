"""Calendar events — the store behind the Calendar page.

Same shape as the wiki (`services/ai/business_knowledge.py`) and the Workspace
(`routers/reports.py`), deliberately: an event has an **owner**, is **readable by
colleagues it was shared with**, and may have a **read-only public snapshot**. One
permission model for all three features means one thing to reason about.

What is specific to an event
----------------------------
* It has a **schedule**: `start_date` … `end_date` (inclusive, dates only — the calendar
  is a month grid, and a timestamp would imply a precision the grid cannot show), plus an
  optional `deadline` that is drawn as its own marker.
* It has **partners**: the colleagues who are *on* the event. ⚠️ Being a partner **is**
  being granted read access — the two are the same list on purpose, because "I put you on
  this event" and "you may read this event" are the same statement, and two lists that
  have to be kept in step is how one of them ends up wrong.
* It has **attachments**: references to existing Workspace reports and business-knowledge
  pages (`{kind, slug}`), never copies. An attachment is a link to the thing that is
  already maintained; a copy would drift the day after. A third kind, `file`, points at an
  object in the file library (`{kind: "file", path: "<bucket>/<key>"}`) — that is what an
  upload becomes, and it is still a reference: the bytes live in OSS, the event only names
  them.
* It has a **cover**, reusing the Workspace mechanism end to end
  (`services/report_cover.py` for generation, the same `cover_*` columns in this table, the
  same `GET .../cover` contract). Covers are generated **server-side** because the account
  publishing an event holds no image credentials.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg2.extras

from core.db import connect_main

MAX_TITLE = 200
MAX_SUMMARY = 500
MAX_BODY_BYTES = 8 * 1024 * 1024          # same ceiling as a Workspace report
MAX_DESCRIPTION = 1200    # ~4 lines at 22px in the page's box; the panel warns past this
MAX_TAGS = 200
MAX_ATTACHMENTS = 20
MAX_PARTNERS = 50
MAX_TODOS = 30
MAX_TODO_TEXT = 240

VISIBILITIES = ("private", "public")
STATUSES = ("published", "draft", "cancelled")
# The event types. Chosen for a product-marketing centre's calendar (asked for 2026-09-30:
# "现在事件类型还不够丰富。特别是营销活动 … 还有个类型是「会议」，一定要加上"), and it is a
# CLOSED enum: the UI offers exactly these and anything else is coerced (see `upsert_event`).
KINDS = ("meeting", "review", "launch", "promotion", "campaign", "media", "content",
         "offline", "training", "research", "planning", "other")
# ⚠️ Two values that earlier revisions (and the old default) wrote. They are still ACCEPTED
# so that saving an event that carries one cannot silently rewrite its type — but they are no
# longer offered anywhere. `milestone` in particular is retired as a standalone type: the
# milestone the product wants lives INSIDE an event (a to-do's `due`), which the calendar
# already draws as a diamond. See `format-calendar-event` §1 `todos`.
LEGACY_KINDS = ("event", "milestone")
# `report` / `knowledge` name a row in our own tables; `file` names an object in the file
# library. All three are references.
REPORT_KINDS = ("report", "knowledge", "file")


def coerce_kind(raw: Any) -> str:
    """The event type a payload actually gets.

    ⚠️ A closed enum, and an unknown value becomes `other` — NOT one of the real types. The
    old fallback was `event`, which meant an agent that invented a type ("webinar") got a
    plausible-looking event back carrying a type it never sent, and nothing said so. `other`
    at least looks like what it is. Legacy values are accepted, so a PUT on an event written
    before a type was retired keeps the type it has instead of having it silently rewritten.
    """
    value = _text(raw)
    return value if value in KINDS + LEGACY_KINDS else "other"
# ⚠️ Keep in sync with `routers/storage.py::_ALLOWED_BUCKETS` — a path outside that set
# would 400 at serve time, i.e. the owner would see an attachment that never opens.
# `test_calendar.py` asserts the two sets are equal.
FILE_BUCKETS = ("datacenter-raw", "product-images", "competitor-images", "knowledge-assets")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CalendarError(RuntimeError):
    pass


class CalendarConflict(CalendarError):
    """A page with this title already exists — the same one-shot gate as the wiki."""

    def __init__(self, title: str, conflicts: list[dict]):
        super().__init__(f"an event with this title already exists ({len(conflicts)})")
        self.title = title
        self.conflicts = conflicts


@dataclass(frozen=True)
class Attachment:
    """A reference, never a copy.

    `kind` is `report` | `knowledge` | `file`. For the first two, `slug` is the row's slug
    in `ai_reports` / `ai_knowledge_items`. For `file` it is the **OSS object key**
    (`<bucket>/<key>`) — served by `/api/storage/serve?path=…`, and case-sensitive, which
    is why `parse_attachments` does not lowercase it the way it lowercases a slug.
    """

    kind: str
    slug: str
    title: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "slug": self.slug, "title": self.title}


@dataclass(frozen=True)
class Todo:
    """One line of the event's to-do list.

    ⚠️ This used to be authored prose inside the page body (`<ul class="ev-todo"><li>…`),
    and it was the only thing on the page the event itself did not know about. That made
    three things impossible, which is why it is now a field:

    * **a name and a date per line** — "who does it, by when" is the whole point of a
      to-do, and prose cannot be read back;
    * **a checkbox that means something** — `done` has to survive a reload;
    * **each to-do is a milestone** — with a `due` date it can be drawn on the calendar
      at that day, which is what turns the list into a schedule.

    `assignee` is a colleague's address, **not** a permission: being assigned a to-do
    does not add you to `partners`. Being a partner is what grants read access, and
    silently widening it from a to-do field would be exactly the kind of implicit
    permission change this codebase avoids everywhere else.
    """

    text: str
    assignee: str = ""
    due: str = ""            # YYYY-MM-DD; empty = no date, and then it is not a milestone
    done: bool = False

    def as_dict(self) -> dict:
        return {"text": self.text, "assignee": self.assignee, "due": self.due, "done": self.done}


@dataclass(frozen=True)
class CalendarEvent:
    id: int
    slug: str
    title: str
    start_date: date | None = None
    end_date: date | None = None
    deadline: date | None = None
    summary: str = ""
    summary_en: str = ""
    summary_zh: str = ""
    # The 项目描述 box on the page, one per language — the same pairing as the summaries,
    # because the page is bilingual and one field would print English on the Chinese page.
    # Until 2026-09-30 this box was only ever authored inside the page body, which is why the
    # panel had no way to edit it. Set one and the server fills that language's `.ev-desc`;
    # leave it empty and the authored text is untouched (same rule as the to-do list).
    description_en: str = ""
    description_zh: str = ""
    category: str = ""
    tags: str = ""
    kind: str = "other"
    body: str = ""
    owner_email: str = ""
    visibility: str = "private"
    status: str = "published"
    source_event_id: int | None = None
    size_bytes: int = 0
    created_at: Any = None
    updated_at: Any = None
    partners: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    # Cover metadata only — never `cover_data` (the bytes are read by their own query, so
    # listing 500 events does not drag 500 JPEGs through the wire).
    has_cover: bool = False
    cover_mime: str = ""
    cover_w: int | None = None
    cover_h: int | None = None
    cover_prompt: str = ""
    cover_model: str = ""
    cover_url: str = ""

    def tag_list(self) -> list[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]


# ── schema ───────────────────────────────────────────────────────────────────

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_tables() -> None:
    """Create/upgrade the calendar tables. Idempotent, safe on startup."""
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
                    CREATE TABLE IF NOT EXISTS ai_calendar_events (
                        id            SERIAL PRIMARY KEY,
                        slug          TEXT UNIQUE NOT NULL,
                        title         TEXT NOT NULL,
                        start_date    DATE,
                        end_date      DATE,
                        deadline      DATE,
                        summary       TEXT NOT NULL DEFAULT '',
                        summary_en    TEXT NOT NULL DEFAULT '',
                        summary_zh    TEXT NOT NULL DEFAULT '',
                        description_en TEXT NOT NULL DEFAULT '',
                        description_zh TEXT NOT NULL DEFAULT '',
                        category      TEXT NOT NULL DEFAULT '',
                        tags          TEXT NOT NULL DEFAULT '',
                        kind          TEXT NOT NULL DEFAULT 'event',
                        body          TEXT NOT NULL DEFAULT '',
                        attachments   TEXT NOT NULL DEFAULT '[]',
                        todos         TEXT NOT NULL DEFAULT '[]',
                        owner_email   TEXT,
                        visibility    TEXT NOT NULL DEFAULT 'private',
                        status        TEXT NOT NULL DEFAULT 'published',
                        source_event_id INTEGER,
                        size_bytes    INTEGER NOT NULL DEFAULT 0,
                        -- The cover, same columns as `ai_reports` (see report_cover.py):
                        -- generated server-side because the account publishing an event
                        -- holds no image credentials.
                        cover_data    BYTEA,
                        cover_mime    TEXT NOT NULL DEFAULT '',
                        cover_w       INTEGER,
                        cover_h       INTEGER,
                        cover_prompt  TEXT NOT NULL DEFAULT '',
                        cover_model   TEXT NOT NULL DEFAULT '',
                        cover_url     TEXT NOT NULL DEFAULT '',
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_calendar_event_partners (
                    event_id        INTEGER NOT NULL REFERENCES ai_calendar_events(id) ON DELETE CASCADE,
                    partner_email   TEXT NOT NULL,
                    added_by        TEXT NOT NULL DEFAULT '',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (event_id, partner_email)
                )""")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_calendar_events_window_idx "
                            "ON ai_calendar_events (start_date, end_date)")
                cur.execute("CREATE INDEX IF NOT EXISTS ai_calendar_partners_email_idx "
                            "ON ai_calendar_event_partners (partner_email)")
                # Existing deployments: CREATE TABLE IF NOT EXISTS never adds columns.
                for stmt in (
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS summary_en TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS summary_zh TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS description_en TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS description_zh TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS attachments TEXT NOT NULL DEFAULT '[]'",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS todos TEXT NOT NULL DEFAULT '[]'",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS deadline DATE",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_data BYTEA",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_mime TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_w INTEGER",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_h INTEGER",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_prompt TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_model TEXT NOT NULL DEFAULT ''",
                    # `cover_brand` was removed with the brand palettes: a cover carries no
                    # business identity. Drop any copy a database still carries.
                    "ALTER TABLE ai_calendar_events DROP COLUMN IF EXISTS cover_brand",
                    "ALTER TABLE ai_calendar_events ADD COLUMN IF NOT EXISTS cover_url TEXT NOT NULL DEFAULT ''",
                ):
                    cur.execute(stmt)
            # ⚠️ **Commit.** psycopg2 runs DDL inside the transaction, so closing without a
            # commit rolls the CREATE TABLE back — the tables then never exist and every
            # query 500s with "relation does not exist" while the deployment looks healthy.
            # (Caught 2026-09-30 by the first real request to this endpoint, after the init
            # path had already swallowed the same failure in the lifespan.)
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


# ── pure rules ───────────────────────────────────────────────────────────────

def _text(value: Any, *, limit: int | None = None) -> str:
    text = "" if value is None else str(value).strip()
    return text[:limit] if limit else text


def _as_date(value: Any, *, field_name: str) -> date | None:
    """`YYYY-MM-DD` or a real date. A calendar cell has no room for a time of day."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CalendarError(f"{field_name} 必须是 YYYY-MM-DD（收到 {value!r}） / {field_name} must be YYYY-MM-DD (got {value!r})") from exc


def _clean_partner(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not email:
        return ""
    if not _EMAIL.match(email):
        raise CalendarError(f"不是邮箱地址: {value!r} / not an email address: {value!r}")
    return email


def clean_slug(raw: Any, fallback_title: str) -> str:
    # ⚠️ CJK is deliberately KEPT, for the same reason as business_knowledge.clean_slug:
    # event titles here are Chinese, and `[^a-z0-9._-]+` stripped every one of them to
    # nothing, so the hash fallback below produced `event-1a2b3c4d` for *every* event.
    # Those urls are unguessable, unreadable in the address bar, and impossible for a
    # reader to type to reach an event they were told about. CJK is URL-safe
    # (percent-encoded on the wire) and is what someone would actually search for.
    # The hash fallback stays for the genuinely empty-title case.
    base = re.sub(r"[^\w.\-一-鿿]+", "-", str(raw or "").strip().lower(), flags=re.UNICODE).strip("-.")
    if not base:
        base = re.sub(r"[^\w\-一-鿿]+", "-", str(fallback_title or "").strip().lower(),
                      flags=re.UNICODE).strip("-.")
    base = base[:70].strip("-.")
    if not base:
        import hashlib
        base = "event-" + hashlib.sha1(str(fallback_title or "event").encode()).hexdigest()[:8]
    return base


def validate_body(body: Any) -> str:
    """The event's 16:9 page. HTML only, and not empty."""
    if not isinstance(body, str) or not body.strip():
        raise CalendarError("body 必须是非空 HTML 文档（日程的 16:9 页面） / body must be a non-empty HTML document (the event's 16:9 page)")
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise CalendarError(f"body 为 {size} 字节，上限 {MAX_BODY_BYTES} 字节 / body is {size} bytes; the limit is {MAX_BODY_BYTES} bytes")
    return body.strip()


def _normalise_title(title: Any) -> str:
    return " ".join(str(title or "").split()).lower()


def _clean_file_path(raw: Any) -> str:
    """`<bucket>/<key>` for an object in the file library.

    ⚠️ **Case is preserved.** A slug is lowercased everywhere else in this module; an OSS
    object key is case-sensitive, so lowercasing it would turn a working attachment into a
    404 the moment a folder or filename has a capital letter.
    """
    path = _text(raw)
    if not path or ".." in path or path.startswith("/") or "\x00" in path:
        raise CalendarError("附件需要 <bucket>/<key> 形式的路径 / an attached file needs a path like <bucket>/<key>")
    bucket, _, key = path.partition("/")
    if bucket not in FILE_BUCKETS:
        raise CalendarError(
            f"附件必须位于 {', '.join(FILE_BUCKETS)} 之一（收到 {bucket!r}）"
            f" / attached file must live in one of {', '.join(FILE_BUCKETS)} (got {bucket!r})")
    if not key:
        raise CalendarError("附件路径必须带 bucket 之后的对象 key / attached file path must carry the object key after the bucket")
    return path


def parse_attachments(raw: Any) -> list[Attachment]:
    """`[{kind, slug}]` → attachments. Unknown kinds are refused, not ignored.

    `kind: "file"` names an object in the file library and carries its OSS key in `slug`
    (an upload from the event page lands in `datacenter-raw/…`). `path` and `name` are
    accepted as spellings of `slug` / `title` so an uploader does not have to learn that
    the field is called `slug`.
    """
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalendarError("attachments 必须是 JSON 数组 / attachments must be a JSON array") from exc
    if not isinstance(raw, list):
        raise CalendarError("attachments 必须是数组 / attachments must be a list")
    if len(raw) > MAX_ATTACHMENTS:
        raise CalendarError(f"附件最多 {MAX_ATTACHMENTS} 个 / at most {MAX_ATTACHMENTS} attachments")
    out: list[Attachment] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if isinstance(entry, str):                      # "report:q3-review" shorthand
            kind, _, slug = entry.partition(":")
        elif isinstance(entry, dict):
            kind, slug = entry.get("kind", ""), entry.get("slug") or entry.get("path") or ""
        else:
            raise CalendarError('每个附件是 {kind, slug} 或 "kind:slug" / each attachment is {kind, slug} or "kind:slug"')
        kind = _text(kind).lower()
        if kind not in REPORT_KINDS:
            raise CalendarError(f"附件 kind 必须是 {', '.join(REPORT_KINDS)} 之一（收到 {kind!r}） / attachment kind must be one of {', '.join(REPORT_KINDS)} (got {kind!r})")
        slug = _clean_file_path(slug) if kind == "file" else _text(slug).lower()
        if not slug:
            raise CalendarError("附件缺少 slug / an attachment needs a slug")
        if (kind, slug) in seen:
            continue
        seen.add((kind, slug))
        title = ""
        if isinstance(entry, dict):
            title = entry.get("title") or entry.get("name") or ""
        out.append(Attachment(kind=kind, slug=slug, title=_text(title, limit=MAX_TITLE)))
    return out


def attachments_json(items: list[Attachment]) -> str:
    return json.dumps([item.as_dict() for item in items], ensure_ascii=False)


def parse_todos(raw: Any) -> list[Todo]:
    """`[{text, assignee, due, done}]` → to-dos.

    `text` is required (a to-do with no text is a stray checkbox). `assignee` and `due`
    are optional, but when present they are **validated rather than stored as typed**: an
    assignee that is not an address would render as a name nobody can act on, and a `due`
    that is not a date would either be ignored by the calendar or drawn on the wrong day.
    Both mistakes are cheap to catch here and expensive to notice later.

    ⚠️ An empty `text` entry is **dropped**, not refused: the editor sends its draft rows
    and a row the user added but never typed into is a normal, harmless state — refusing
    the whole save for it would lose the rest of the edit.
    """
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalendarError("todos 必须是 JSON 数组 / todos must be a JSON array") from exc
    if not isinstance(raw, list):
        raise CalendarError("todos 必须是数组 / todos must be a list")
    if len(raw) > MAX_TODOS:
        raise CalendarError(f"待办最多 {MAX_TODOS} 条 / at most {MAX_TODOS} to-dos")
    out: list[Todo] = []
    for entry in raw:
        if isinstance(entry, str):                       # "write the deck" shorthand
            entry = {"text": entry}
        if not isinstance(entry, dict):
            raise CalendarError("每条待办是 {text, assignee, due, done} / each to-do is {text, assignee, due, done}")
        text = _text(entry.get("text"), limit=MAX_TODO_TEXT)
        if not text:
            continue
        assignee = str(entry.get("assignee") or "").strip().lower()
        if assignee and not _EMAIL.match(assignee):
            raise CalendarError(f"待办负责人必须是邮箱地址（收到 {entry.get('assignee')!r}） / a to-do assignee must be an email address (got {entry.get('assignee')!r})")
        due = _as_date(entry.get("due"), field_name="due")
        out.append(Todo(text=text, assignee=assignee, due=due.isoformat() if due else "",
                        done=bool(entry.get("done"))))
    return out


def todos_json(items: list[Todo]) -> str:
    return json.dumps([item.as_dict() for item in items], ensure_ascii=False)


def is_bilingual(langs: list[str]) -> bool:
    codes = {str(code).strip().lower().split("-")[0] for code in langs or []}
    return "en" in codes and "zh" in codes


def bilingual_summaries(plain: Any, english: Any, chinese: Any, langs: list[str],
                        body: str = "") -> tuple[str, str, str]:
    """Same rule as a Workspace report — see `routers/reports.py::bilingual_summaries`.

    A bilingual event needs a summary in **both** languages: the calendar cell and the
    event's own page both show it, and one line for a two-language event hands one of the
    two readers a sentence in the wrong language.
    """
    from urllib.parse import urlparse  # noqa: F401  (kept local: no other use here)

    plain = _text(plain, limit=MAX_SUMMARY)
    english = _text(english, limit=MAX_SUMMARY)
    chinese = _text(chinese, limit=MAX_SUMMARY)
    detected = langs if langs else detect_langs(body)
    if is_bilingual(detected) and not (english and chinese):
        missing = [name for name, value in (("summary_en", english), ("summary_zh", chinese)) if not value]
        raise CalendarError(
            "这个日程是双语的（" + ", ".join(detected) + "）：必须同时携带两种语言的摘要。"
            "请一并提交 summary_en 与 summary_zh（各一行）。缺失: " + ", ".join(missing)
            + "。对双语日程只有 summary 不够。"
            " / this event is bilingual (" + ", ".join(detected) + "): it must carry a summary in "
            "both languages. Send `summary_en` and `summary_zh` — one line each. Missing: "
            + ", ".join(missing) + ". `summary` alone is not enough for a bilingual event.")
    return (plain or english or chinese), english, chinese


_LANG_RE = re.compile(r'data-lang\s*=\s*["\']([A-Za-z][A-Za-z0-9_-]{0,9})["\']')


def detect_langs(html: str) -> list[str]:
    """Languages the page declares, in document order (mirrors the Workspace rule)."""
    seen: list[str] = []
    for raw in _LANG_RE.findall(html or ""):
        code = raw.strip().lower().split("-")[0].split("_")[0]
        if code and code not in seen:
            seen.append(code)
    return seen


def may_read(row: dict, email: str) -> bool:
    """Owner → partner → public snapshot. Same three tiers as the wiki and Workspace."""
    if row.get("owner_email") == email:
        return True
    if row.get("visibility") == "public" and row.get("status") == "published":
        return True
    if row.get("visibility") != "private" or row.get("status") != "published":
        return False
    return bool(_partner_exists(row.get("id"), email))


READABLE_PREDICATE = (
    "(owner_email = %s"
    " OR (visibility = 'public' AND status = 'published')"
    " OR (visibility = 'private' AND status = 'published' AND EXISTS ("
    "SELECT 1 FROM ai_calendar_event_partners p"
    " WHERE p.event_id = ai_calendar_events.id AND p.partner_email = %s)))"
)

# ── connection helper ────────────────────────────────────────────────────────

class _db:
    def __init__(self) -> None:
        self._conn = connect_main()

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False


def _partner_exists(event_id: Any, email: str) -> bool:
    if not event_id or not email:
        return False
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ai_calendar_event_partners "
                        "WHERE event_id = %s AND partner_email = %s", (event_id, email))
            return cur.fetchone() is not None


_SELECT_COLS = ("id, slug, title, start_date, end_date, deadline, summary, summary_en, summary_zh, "
                "description_en, description_zh, "
                "category, tags, kind, body, attachments, todos, owner_email, visibility, status, "
                "source_event_id, size_bytes, created_at, updated_at, "
                "cover_mime, cover_w, cover_h, cover_prompt, cover_model, cover_url, "
                # ⚠️ `cover_data` itself is NEVER selected here: the calendar lists hundreds of
                # events at a time and would drag a JPEG per row through the connection. Only
                # `GET .../cover` reads the bytes, in its own query below.
                "(cover_data IS NOT NULL) AS has_cover")


def _row_to_event(row: dict, partners: list[str] | None = None) -> CalendarEvent:
    try:
        raw = json.loads(row.get("attachments") or "[]")
    except json.JSONDecodeError:
        raw = []
    attached = [Attachment(kind=a.get("kind", ""), slug=a.get("slug", ""), title=a.get("title", ""))
                for a in raw if isinstance(a, dict)]
    try:
        raw_todos = json.loads(row.get("todos") or "[]")
    except json.JSONDecodeError:
        raw_todos = []
    todos = [Todo(text=t.get("text", ""), assignee=t.get("assignee", "") or "",
                  due=str(t.get("due") or ""), done=bool(t.get("done")))
             for t in raw_todos if isinstance(t, dict) and t.get("text")]
    return CalendarEvent(
        id=row["id"], slug=row["slug"], title=row["title"],
        start_date=row.get("start_date"), end_date=row.get("end_date"),
        deadline=row.get("deadline"),
        summary=row.get("summary") or "", summary_en=row.get("summary_en") or "",
        summary_zh=row.get("summary_zh") or "",
        description_en=row.get("description_en") or "",
        description_zh=row.get("description_zh") or "",
        category=row.get("category") or "", tags=row.get("tags") or "",
        kind=row.get("kind") or "other", body=row.get("body") or "",
        owner_email=row.get("owner_email") or "", visibility=row.get("visibility") or "private",
        status=row.get("status") or "published", source_event_id=row.get("source_event_id"),
        size_bytes=row.get("size_bytes") or 0, created_at=row.get("created_at"),
        updated_at=row.get("updated_at"), partners=list(partners or []), attachments=attached,
        todos=todos,
        # `has_cover` means "we hold the bytes" (same meaning as the raw column alias a
        # report uses); the payload decides separately whether an external URL counts.
        has_cover=bool(row.get("has_cover")),
        cover_mime=row.get("cover_mime") or "", cover_w=row.get("cover_w"),
        cover_h=row.get("cover_h"), cover_prompt=row.get("cover_prompt") or "",
        cover_model=row.get("cover_model") or "", cover_url=row.get("cover_url") or "",
    )


def _partners(cur, event_id: int) -> list[str]:
    cur.execute("SELECT partner_email FROM ai_calendar_event_partners WHERE event_id = %s "
                "ORDER BY partner_email", (event_id,))
    return [row["partner_email"] for row in cur.fetchall()]


# ── attachments: references, never copies ────────────────────────────────────

def resolve_attachments(cur, items: list[Attachment], email: str) -> list[Attachment]:
    """Keep the ones that exist and that this account may read; fill in their titles.

    ⚠️ A reference to something the account cannot read is dropped rather than stored:
    otherwise the event page would show a link that 404s for its own owner, and — worse —
    the *title* of a page this account is not allowed to see would leak into an event it
    is allowed to see.

    `kind: "file"` is the exception: it points at an OSS object, which has no row to look
    up and no owner to check. `parse_attachments` already restricted it to a known bucket,
    and `/api/storage/serve` re-checks that allowlist at read time — so it is kept as given
    (with its name as the title when the caller did not supply one).
    """
    out: list[Attachment] = []
    for item in items:
        if item.kind == "file":
            name = item.title or item.slug.rsplit("/", 1)[-1]
            out.append(Attachment(kind="file", slug=item.slug, title=name))
            continue
        if item.kind == "report":
            cur.execute("SELECT slug, title FROM ai_reports WHERE slug = %s", (item.slug,))
        else:
            cur.execute("SELECT slug, title FROM ai_knowledge_items WHERE slug = %s", (item.slug,))
        row = cur.fetchone()
        if not row:
            continue
        out.append(Attachment(kind=item.kind, slug=row["slug"], title=row["title"]))
    return out


# ── write ────────────────────────────────────────────────────────────────────

_CONFLICT_SQL = (
    "SELECT slug, title, owner_email, visibility, updated_at FROM ai_calendar_events"
    " WHERE slug <> %s"
    "   AND lower(btrim(regexp_replace(title, '\\s+', ' ', 'g'))) = %s"
    "   AND " + READABLE_PREDICATE +
    " ORDER BY updated_at DESC LIMIT 10"
)


def _title_conflicts_with(cur, title: str, email: str, *, exclude_slug: str = "") -> list[dict]:
    cur.execute(_CONFLICT_SQL, (exclude_slug, _normalise_title(title), email, email))
    return [{"slug": row["slug"], "title": row["title"], "owner_email": row["owner_email"],
             "visibility": row["visibility"]} for row in cur.fetchall()]


def upsert_event(payload: dict, email: str, *, slug: str | None = None,
                 cover: dict | None = None, clear_cover: bool = False) -> CalendarEvent:
    """Create or update one event. Updating requires owning the slug.

    `cover` is the dict `services/report_cover.py::resolve_cover` returned (the same shape a
    Workspace report stores); `clear_cover` wipes it. Both are ignored on a re-write that
    passes neither, so editing the partners of an event cannot silently drop its cover.
    """
    ensure_tables()
    body = validate_body(payload.get("body"))
    title = _text(payload.get("title"), limit=MAX_TITLE)
    if not title:
        raise CalendarError("缺少标题 / title is required")
    start = _as_date(payload.get("start_date") or payload.get("start"), field_name="start_date")
    end = _as_date(payload.get("end_date") or payload.get("end"), field_name="end_date")
    if start is None:
        raise CalendarError("缺少 start_date / start_date is required")
    end = end or start
    if end < start:
        raise CalendarError("end_date 不能早于 start_date / end_date cannot be before start_date")
    deadline = _as_date(payload.get("deadline"), field_name="deadline")
    langs = detect_langs(body)
    summary, summary_en, summary_zh = bilingual_summaries(
        payload.get("summary"), payload.get("summary_en"), payload.get("summary_zh"), langs, body)
    attachments = parse_attachments(payload.get("attachments"))
    todos = parse_todos(payload.get("todos"))
    partners = []
    for raw in (payload.get("partners") or payload.get("mentions") or []):
        candidate = _clean_partner(raw)
        if candidate and candidate != email.lower() and candidate not in partners:
            partners.append(candidate)
    if len(partners) > MAX_PARTNERS:
        raise CalendarError(f"参与人最多 {MAX_PARTNERS} 个 / at most {MAX_PARTNERS} partners")
    existing_slug = _text(slug or payload.get("slug"))

    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = None
            if existing_slug:
                cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE slug = %s FOR UPDATE",
                            (_text(existing_slug),))
                row = cur.fetchone()
                if row and row["owner_email"] != email:
                    raise PermissionError("this event belongs to another account")
            if row and row["visibility"] == "public":
                raise PermissionError("this slug is the public snapshot; edit the private original")
            resolved = resolve_attachments(cur, attachments, email)
            values = {
                "summary": summary, "summary_en": summary_en, "summary_zh": summary_zh,
                "description_en": _text(payload.get("description_en"), limit=MAX_DESCRIPTION),
                "description_zh": _text(payload.get("description_zh"), limit=MAX_DESCRIPTION),
                "category": _text(payload.get("category")),
                "tags": _text(payload.get("tags"), limit=MAX_TAGS),
                "kind": coerce_kind(payload.get("kind")),
                "status": _text(payload.get("status")) if payload.get("status") in STATUSES else "published",
            }
            target_slug = row["slug"] if row else clean_slug(existing_slug, title)
            if row:
                # A partner list replaces the previous one on every write: an @-mention is a
                # statement about who is on the event *now*, and merging would make it
                # impossible to take someone off it.
                cur.execute("DELETE FROM ai_calendar_event_partners WHERE event_id = %s", (row["id"],))
                cur.execute(
                    """
                    UPDATE ai_calendar_events SET title = %s, start_date = %s, end_date = %s,
                        deadline = %s, summary = %s, summary_en = %s, summary_zh = %s,
                        description_en = %s, description_zh = %s, category = %s,
                        tags = %s, kind = %s, body = %s, attachments = %s, todos = %s,
                        status = %s, size_bytes = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (title, start, end, deadline, values["summary"], values["summary_en"],
                     values["summary_zh"], values["description_en"], values["description_zh"],
                     values["category"], values["tags"],
                     values["kind"], body, attachments_json(resolved), todos_json(todos),
                     values["status"], len(body.encode("utf-8")), row["id"]),
                )
                event_id = row["id"]
            else:
                if not payload.get("confirm_conflict"):
                    conflicts = _title_conflicts_with(cur, title, email)
                    if conflicts:
                        raise CalendarConflict(title, conflicts)
                collide = 0
                while True:
                    # A SAVEPOINT, not a full rollback — same reasoning as
                    # business_knowledge.upsert_item. `conn.rollback()` used to discard the
                    # whole transaction, so a single colliding insert lost every event
                    # written earlier in the same batch.
                    cur.execute("SAVEPOINT event_insert")
                    try:
                        cur.execute(
                            """
                            INSERT INTO ai_calendar_events
                                (slug, title, start_date, end_date, deadline, summary, summary_en,
                                 summary_zh, description_en, description_zh, category, tags,
                                 kind, body, attachments, todos,
                                 owner_email, visibility, status, size_bytes)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, 'private', %s, %s)
                            RETURNING id
                            """,
                            (target_slug, title, start, end, deadline, values["summary"],
                             values["summary_en"], values["summary_zh"], values["description_en"],
                             values["description_zh"], values["category"],
                             values["tags"], values["kind"], body,
                             attachments_json(resolved), todos_json(todos), email,
                             values["status"], len(body.encode("utf-8"))),
                        )
                        event_id = cur.fetchone()["id"]
                        cur.execute("RELEASE SAVEPOINT event_insert")
                        break
                    except psycopg2.errors.UniqueViolation:
                        cur.execute("ROLLBACK TO SAVEPOINT event_insert")
                        collide += 1
                        if collide > 6:
                            cur.execute("ROLLBACK TO SAVEPOINT event_insert")
                            raise CalendarError("无法分配唯一 slug / could not allocate a unique slug")
                        target_slug = f"{clean_slug(existing_slug or title, title)[:60]}-{collide + 1}"
            if cover is not None or clear_cover:
                _store_cover(cur, event_id, cover, clear=clear_cover)
            for partner in partners:
                cur.execute(
                    "INSERT INTO ai_calendar_event_partners (event_id, partner_email, added_by) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (event_id, partner, email))
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE id = %s", (event_id,))
            saved = cur.fetchone()
            partner_list = _partners(cur, event_id)
    return _row_to_event(dict(saved), partner_list)


# ── the cover: the Workspace's mechanism, the same columns and the same contract ──
#
# A cover is either bytes we store and serve ourselves (`cover_data`, exposed as
# `/api/calendar/events/{slug}/cover`) or an external URL. Reusing the Workspace's columns
# and dict shape means one generator (`services/report_cover.py`) serves both, and a cover
# stored on an event behaves exactly like one stored on a report.

def _store_cover(cur, event_id: int, cover: dict | None, *, clear: bool = False) -> None:
    """Write or clear the cover columns for one event."""
    if clear:
        cur.execute("UPDATE ai_calendar_events SET cover_data = NULL, cover_mime = '', "
                    "cover_w = NULL, cover_h = NULL, cover_prompt = '', cover_model = '', "
                    "cover_url = '' WHERE id = %s", (event_id,))
        return
    if cover is None:
        return
    cur.execute(
        "UPDATE ai_calendar_events SET cover_data = %s, cover_mime = %s, cover_w = %s, "
        "cover_h = %s, cover_prompt = %s, cover_model = %s, cover_url = %s "
        "WHERE id = %s",
        (psycopg2.Binary(cover["data"]) if cover.get("data") else None, cover.get("mime", ""),
         cover.get("w"), cover.get("h"), cover.get("prompt", ""), cover.get("model", ""),
         cover.get("url", ""), event_id),
    )


def _owned_event_id(cur, slug: str, email: str) -> int | None:
    cur.execute("SELECT id, owner_email FROM ai_calendar_events WHERE slug = %s", (_text(slug),))
    row = cur.fetchone()
    if not row or row["owner_email"] != email:
        return None
    return row["id"]


def set_cover(slug: str, email: str, cover: dict | None = None, *, clear: bool = False) -> CalendarEvent | None:
    """Set or clear the cover of one event. Returns None unless the caller owns it."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            event_id = _owned_event_id(cur, slug, email)
            if event_id is None:
                return None
            _store_cover(cur, event_id, cover, clear=clear)
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE id = %s", (event_id,))
            saved = dict(cur.fetchone())
            return _row_to_event(saved, _partners(cur, event_id))


def get_cover(slug: str, email: str) -> tuple[bytes, str, str] | None:
    """`(bytes, mime, external_url)` for one event's cover; None when it has none.

    ⚠️ The only place `cover_data` is read — see the note on `_SELECT_COLS`.
    """
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS}, cover_data FROM ai_calendar_events WHERE slug = %s",
                        (_text(slug),))
            row = cur.fetchone()
            if not row or not _may_read_with(cur, dict(row), email):
                return None
            data, external = row.get("cover_data"), (row.get("cover_url") or "")
            if not data and not external:
                return None
            return (bytes(data) if data else b""), ((row.get("cover_mime") or "") or "image/jpeg"), external


# ── read ─────────────────────────────────────────────────────────────────────

def list_events(email: str, *, scope: str = "mine", date_from: date | None = None,
                date_to: date | None = None,
                limit: int = 500) -> list[CalendarEvent]:
    """Events for one window. The calendar asks for a range, never for 'everything'."""
    ensure_tables()
    scope = scope if scope in {"mine", "public", "shared"} else "mine"
    where, params = [], []
    if scope == "mine":
        where.append("owner_email = %s AND visibility = 'private'")
        params.append(email)
    elif scope == "public":
        where.append("visibility = 'public' AND status = 'published'")
    else:
        where.append("visibility = 'private' AND status = 'published' AND owner_email <> %s "
                     "AND EXISTS (SELECT 1 FROM ai_calendar_event_partners p "
                     "WHERE p.event_id = ai_calendar_events.id AND p.partner_email = %s)")
        params.extend((email, email))
    if date_from:
        # Overlap, not containment: an event that starts last month still occupies cells today.
        where.append("(end_date IS NULL OR end_date >= %s)")
        params.append(date_from)
    if date_to:
        where.append("start_date <= %s")
        params.append(date_to)
    sql = (f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE " + " AND ".join(where)
           + " ORDER BY start_date ASC, updated_at DESC LIMIT %s")
    params.append(max(1, min(int(limit), 2000)))
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            return [_row_to_event(dict(row), _partners(cur, row["id"])) for row in rows]


def get_event(slug: str, email: str) -> CalendarEvent | None:
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE slug = %s", (_text(slug),))
            row = cur.fetchone()
            if not row or not _may_read_with(cur, dict(row), email):
                return None
            return _row_to_event(dict(row), _partners(cur, row["id"]))


def _may_read_with(cur, row: dict, email: str) -> bool:
    if row.get("owner_email") == email:
        return True
    if row.get("visibility") == "public" and row.get("status") == "published":
        return True
    if row.get("visibility") != "private" or row.get("status") != "published":
        return False
    cur.execute("SELECT 1 FROM ai_calendar_event_partners "
                "WHERE event_id = %s AND partner_email = %s", (row.get("id"), email))
    return cur.fetchone() is not None


def delete_event(slug: str, email: str) -> bool:
    ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_calendar_events WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'private'", (_text(slug), email))
            return cur.rowcount > 0


# ── the owner's controls ─────────────────────────────────────────────────────

def _public_slug(slug: str) -> str:
    return f"{slug}-public"


def publish_to_public(slug: str, email: str) -> CalendarEvent:
    """Publish a read-only snapshot. The private event stays exactly as it is."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE slug = %s FOR UPDATE",
                        (_text(slug),))
            source = cur.fetchone()
            if not source or source["owner_email"] != email or source["visibility"] != "private":
                raise PermissionError("event not found")
            if source["status"] != "published":
                raise CalendarError("请先发布该日程（当前状态不是 published) / publish the event first (its status is not published)")
            public_slug = _public_slug(source["slug"])
            cur.execute("SELECT id FROM ai_calendar_events WHERE slug = %s FOR UPDATE", (public_slug,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """
                    UPDATE ai_calendar_events SET title = %s, start_date = %s, end_date = %s,
                        deadline = %s, summary = %s, summary_en = %s, summary_zh = %s, category = %s,
                        tags = %s, kind = %s, body = %s, attachments = %s, todos = %s,
                        status = %s, size_bytes = %s, updated_at = NOW() WHERE id = %s
                    """,
                    (source["title"], source["start_date"], source["end_date"], source["deadline"],
                     source["summary"], source["summary_en"], source["summary_zh"], source["category"],
                     source["tags"], source["kind"], source["body"],
                     source["attachments"], source["todos"], source["status"], source["size_bytes"],
                     existing["id"]),
                )
                target_id = existing["id"]
            else:
                cur.execute(
                    """
                    INSERT INTO ai_calendar_events
                        (slug, title, start_date, end_date, deadline, summary, summary_en, summary_zh,
                         category, tags, kind, body, attachments, todos, owner_email,
                         visibility, status, source_event_id, size_bytes)
                    SELECT %s, title, start_date, end_date, deadline, summary, summary_en, summary_zh,
                           category, tags, kind, body, attachments, todos, owner_email,
                           'public', status, id, size_bytes
                    FROM ai_calendar_events WHERE id = %s RETURNING id
                    """,
                    (public_slug, source["id"]),
                )
                target_id = cur.fetchone()["id"]
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE id = %s", (target_id,))
            saved = cur.fetchone()
            return _row_to_event(dict(saved), [])


def withdraw_public(slug: str, email: str) -> bool:
    ensure_tables()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_calendar_events WHERE slug = %s AND owner_email = %s "
                        "AND visibility = 'public'", (_public_slug(_text(slug)), email))
            return cur.rowcount > 0


def pull_copy(slug: str, email: str) -> CalendarEvent:
    """Take an independent private copy of a public or shared event."""
    ensure_tables()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE slug = %s", (_text(slug),))
            source = cur.fetchone()
            if (not source or source["owner_email"] == email
                    or not _may_read_with(cur, dict(source), email)):
                raise PermissionError("event not found")
            base = source["slug"][:56] + "-copy"
            new_slug, attempt = base, 0
            while True:
                cur.execute("SELECT 1 FROM ai_calendar_events WHERE slug = %s", (new_slug,))
                if cur.fetchone() is None:
                    break
                attempt += 1
                if attempt > 6:
                    raise CalendarError("无法分配副本 slug / could not allocate a copy slug")
                new_slug = f"{base}-{attempt + 1}"
            cur.execute(
                """
                INSERT INTO ai_calendar_events
                    (slug, title, start_date, end_date, deadline, summary, summary_en, summary_zh,
                     category, tags, kind, body, attachments, todos, owner_email,
                     visibility, status, source_event_id, size_bytes)
                SELECT %s, title, start_date, end_date, deadline, summary, summary_en, summary_zh,
                       category, tags, kind, body, attachments, todos, %s, 'private',
                       status, id, size_bytes
                FROM ai_calendar_events WHERE id = %s
                """,
                (new_slug, email, source["id"]),
            )
            cur.execute("SELECT id FROM ai_calendar_events WHERE slug = %s", (new_slug,))
            event_id = cur.fetchone()["id"]
            cur.execute(f"SELECT {_SELECT_COLS} FROM ai_calendar_events WHERE id = %s", (event_id,))
            saved = cur.fetchone()
            return _row_to_event(dict(saved), [])