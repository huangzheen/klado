"""
Reader notes pinned to a spot inside a document.

Three asset families share one table (`doc_annotations`): a Workspace report, a
wiki page, a calendar event. What they do NOT share is the permission question —
each family already owns a three-tier read rule (owner → partner/sharee → public
snapshot) and those rules are not interchangeable. So this module never decides
access on its own: it resolves the target row and hands it to that family's own
`may_read`. A note can therefore never widen access to the document it sits on,
and the four copies of the read rule stay where their owners keep them.

Positions are stored as percentages of the anchor box plus a page index, not as
pixels: a 1920×1080 deck is shown at 0.42 scale on a laptop and 1.0 on a second
monitor, and a note must land on the same sentence in both.

Scope of who may write (a decision, not a technicality):

* **Read** — anyone who can read the document, *including an agent*. That is the
  point of the feature: an agent is told to revise a document and must see what
  the humans wrote about it. `GET` is never gated on the auth kind.
* **Write / edit / delete** — a signed-in browser session, because a note is a
  person's comment and the Inbox attributes it to a person. An agent credential
  (Basic/Bearer) is refused exactly like it is on the interactive-report state
  endpoint. Anyone who can open the document may write; the author may delete
  their own note and the document's owner may delete any note on it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from urllib.parse import quote

import psycopg2
import psycopg2.extras
from core import htmlkit
from core.config import app_base_href, resolve_frontend_dir
from core.db import connect_main
from services import inbox

_LOG = logging.getLogger(__name__)

# asset_type -> (table, module path, may_read attribute, human label)
TARGETS = {
    "report": ("ai_reports", "routers.reports", "_may_read", "report"),
    # ⚠️ Added 2026-10-05, when HTML moved from Workspace to Dashboard. A dashboard is
    # now allowed to be a plain snapshot page, so it can carry notes exactly as a
    # `static` report can — and the rules below are the report's rules, which is the
    # point: they are the same code, not a similar-looking second copy.
    "dashboard": ("ai_dashboards", "services.dashboard_store", "may_read", "dashboard"),
    "knowledge": ("ai_knowledge_items", "services.ai.business_knowledge", "may_read", "knowledge page"),
    "event": ("ai_calendar_events", "services.ai.calendar_events", "may_read", "calendar event"),
}

MAX_BODY_LEN = 2000          # a note is a comment, not a document
MAX_NOTES_PER_ASSET = 500     # a runaway client must not turn a page into a wall

_schema_ready = False
_schema_lock = threading.Lock()


class AnnotationError(RuntimeError):
    """A refusal the caller can act on. `status_code` is the HTTP code to answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status_code = status
        self.detail = detail


def _db():
    conn = connect_main()
    conn.autocommit = False
    return conn


def ensure_tables() -> None:
    """Create the table on first use (idempotent, memoised per process)."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS doc_annotations (
                        id           SERIAL PRIMARY KEY,
                        asset_type   TEXT NOT NULL,     -- report | knowledge | event
                        asset_slug   TEXT NOT NULL,
                        asset_id     INTEGER,           -- the row's id at write time
                        body         TEXT NOT NULL,
                        x_pct        REAL NOT NULL DEFAULT 0,
                        y_pct        REAL NOT NULL DEFAULT 0,
                        page         INTEGER NOT NULL DEFAULT 0,  -- deck slide; 0 = long-form
                        author_email TEXT NOT NULL,
                        resolved_at  TIMESTAMPTZ,
                        resolved_by  TEXT,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS doc_annotations_asset_idx "
                            "ON doc_annotations (asset_type, asset_slug, page, created_at)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _schema_ready = True


def _with_schema(fn):
    """
    Recreate the table and retry once if it is found missing.

    Mirrors `routers/reports.py::_with_schema`: the Data Center UI can drop public
    tables, so a memoised "ready" flag must not be the reason every note endpoint
    500s until the server restarts.
    """
    import functools

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        ensure_tables()
        try:
            return fn(*args, **kwargs)
        except psycopg2.errors.UndefinedTable:
            global _schema_ready
            _LOG.warning("doc_annotations vanished at runtime — recreating and retrying")
            _schema_ready = False
            ensure_tables()
            return fn(*args, **kwargs)

    return guarded


# ── Targets ──────────────────────────────────────────────────────────────────

def _may_read_fn(asset_type: str):
    """That family's own read rule — imported lazily to keep the import graph flat."""
    if asset_type == "report":
        from routers.reports import _may_read
        return _may_read
    if asset_type == "dashboard":
        from services import dashboard_store
        # `may_read` takes the same `(row, email)` shape as the report rule, and the row
        # `resolve_target` selected carries exactly the columns it needs.
        return dashboard_store.may_read
    if asset_type == "knowledge":
        from services.ai import business_knowledge
        return business_knowledge.may_read
    if asset_type == "event":
        from services.ai import calendar_events
        return calendar_events.may_read
    raise AnnotationError(400, f"未知的资源类型 {asset_type!r} / unknown asset type {asset_type!r}")


def _asset_type(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in TARGETS:
        # ⚠️ The message is built from the table rather than written out, so adding a
        # fourth family cannot leave this sentence claiming there are three. It was
        # wrong the moment `dashboard` was added and nothing had failed yet.
        _names = {"report": "report", "knowledge": "knowledge", "event": "event",
                  "dashboard": "dashboard"}
        raise AnnotationError(
            400,
            "asset_type 只能是 %s / asset_type must be one of %s"
            % ("、".join(_names[k] for k in TARGETS),
               ", ".join(_names[k] for k in TARGETS)))
    return value


def resolve_target(asset_type: str, slug: str, email: str, *, conn=None) -> dict:
    """The document row this note hangs on, or a 404-shaped refusal.

    404 (not 403) for "not yours", because a 403 would confirm that somebody else
    has a document under this slug — the same reasoning the other three modules use.
    """
    asset_type = _asset_type(asset_type)     # an unknown type is a 400, never a KeyError
    table = TARGETS[asset_type][0]
    # ⚠️ `kind` is selected for BOTH families that have one. It was `report`-only for a
    # year, and the day `dashboard` joined, a rule three lines below started reading a
    # missing key — which reads as `static`, so notes would have been offered on an
    # interactive page whose right-click belongs to its own runtime. Derived from the
    # table instead of listed, so the next family cannot repeat it.
    _has_kind = asset_type in ("report", "dashboard")
    sql = (f"SELECT id, slug, title, owner_email, visibility, status{', kind' if _has_kind else ''} "
           f"FROM {table} WHERE slug = %s")
    owned = conn is None
    conn = conn or _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (slug,))
            row = cur.fetchone()
        if owned:
            conn.commit()
    except Exception:
        if owned:
            conn.rollback()
        raise
    finally:
        if owned:
            conn.close()
    row = dict(row) if row else None
    if not row or not _may_read_fn(asset_type)(row, email):
        raise AnnotationError(404, f"{asset_type} 不存在 / {asset_type} not found")
    # Interactive reports keep their own right-click markup in `state_json`, and a dynamic
    # report's right-click belongs to its filter runtime; this feature is deliberately not
    # offered on top of either (two competing right-click menus in one document is worse
    # than neither). A DOCUMENT card is the opposite case and is allowed here: it is a
    # read-only Office file with no markup of its own, which is exactly what this layer is
    # for. Reader asked for it 2026-10-01 ("这些类型的文档也需要可以加notes").
    # ⚠️ The SAME rule, and the same reason, for a dashboard: a page whose right-click
    # belongs to its own markup or its filter runtime must not grow a second competing
    # menu. Written once for both families because the two cases are indistinguishable
    # once you have the row, and a rule that only mentions the first family is a rule
    # the second family quietly does not follow.
    if asset_type in ("report", "dashboard") and \
            (row.get("kind") or "static") not in ("static", "document"):
        raise AnnotationError(409, "交互式报告的标注保存在它自己的 state 里 / interactive reports keep their own annotation state")
    return row


def doc_url(asset_type: str, slug: str) -> str:
    """
    Where a reader lands when they follow an Inbox item.

    Root-relative WITH the mount point, because a link the browser follows does
    not go through the SPA's fetch patch: `/api/…` at the host root is answered by
    the platform gateway with a 200 and 49 bytes of JSON (the Data Center
    download bug), while `/…` is us.
    """
    base = app_base_href()
    if asset_type == "report":
        return f"{base}?report={quote(slug, safe='')}"
    # A dashboard opens at `/d/{slug}`, which is its own document page — the query form
    # is how a REPORT is opened, and there is no `?dashboard=` deep link in the SPA.
    if asset_type == "dashboard":
        return f"{base}d/{quote(slug, safe='')}"
    if asset_type == "knowledge":
        return f"{base}?kb={quote(slug, safe='')}"
    if asset_type == "event":
        return f"{base}?event={quote(slug, safe='')}"
    if asset_type == "dataset":
        return f"{base}?dc={quote(slug, safe='')}"
    return base


# ── Notes ────────────────────────────────────────────────────────────────────

def _row_to_note(row: dict) -> dict:
    return {
        "id": row["id"],
        "asset_type": row["asset_type"],
        "slug": row["asset_slug"],
        "body": row["body"],
        "x": round(float(row["x_pct"]), 3),
        "y": round(float(row["y_pct"]), 3),
        "page": int(row["page"] or 0),
        "author": row["author_email"],
        "resolved": row["resolved_at"] is not None,
        "resolved_by": row["resolved_by"] or "",
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@_with_schema
def list_notes(asset_type: str, slug: str, email: str) -> list[dict]:
    """Every note on one document. The reader's own access is the only gate."""
    resolve_target(asset_type, slug, email)
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM doc_annotations WHERE asset_type = %s AND asset_slug = %s "
                        "ORDER BY page, created_at, id", (asset_type, slug))
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return [_row_to_note(r) for r in rows]


@_with_schema
def list_notes_for_token(slug: str) -> list[dict]:
    """
    Notes on one report, with no account in the picture.

    Deliberately permission-free: the caller has already authenticated the
    `/s/{token}` capability, and the token names exactly one report. Report-only
    by construction — no token family exists for the wiki or the calendar, so
    there is nothing here to widen.
    """
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM doc_annotations WHERE asset_type = 'report' AND asset_slug = %s "
                        "ORDER BY page, created_at, id", (slug,))
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return [_row_to_note(r) for r in rows]


@_with_schema
def create_note(*, asset_type: str, slug: str, email: str, body: str,
                x: float, y: float, page: int = 0) -> dict:
    text = (body or "").strip()
    if not text:
        raise AnnotationError(400, "备注内容不能为空 / note text is required")
    if len(text) > MAX_BODY_LEN:
        raise AnnotationError(400, f"备注为 {len(text)} 字符，上限 {MAX_BODY_LEN} 字符 / note is {len(text)} characters; the limit is {MAX_BODY_LEN}")
    target = resolve_target(asset_type, slug, email)
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS note_count FROM doc_annotations "
                        "WHERE asset_type = %s AND asset_slug = %s", (asset_type, slug))
            if int(cur.fetchone()["note_count"]) >= MAX_NOTES_PER_ASSET:
                raise AnnotationError(409, f"这份文档已有 {MAX_NOTES_PER_ASSET} 条备注 / this document already has {MAX_NOTES_PER_ASSET} notes")
            cur.execute(
                "INSERT INTO doc_annotations (asset_type, asset_slug, asset_id, body, x_pct, y_pct, "
                "page, author_email) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (asset_type, slug, target.get("id"), text, _pct(x), _pct(y), max(0, int(page or 0)), email),
            )
            row = dict(cur.fetchone())
        conn.commit()
    except AnnotationError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    note = _row_to_note(row)
    _notify_owner(target, asset_type, note)
    return note


def _pct(value: object) -> float:
    """Clamp a coordinate into 0–100; a note off the page is not a note."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:            # NaN
        return 0.0
    return round(min(100.0, max(0.0, number)), 3)


def _load_note(note_id: int, email: str) -> tuple[dict, dict]:
    """The note plus the document it belongs to, with the reader's access checked."""
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM doc_annotations WHERE id = %s", (note_id,))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not row:
        raise AnnotationError(404, "备注不存在 / note not found")
    row = dict(row)
    target = resolve_target(row["asset_type"], row["asset_slug"], email)
    return row, target


@_with_schema
def update_note(note_id: int, email: str, *, body: str | None = None,
                resolved: bool | None = None) -> dict:
    """
    Edit the text and/or flip the resolved flag.

    The author may edit their own note; the document's owner may edit any note on
    their document. Resolving is triage, not authorship — anyone who can read the
    document may set it, and anyone may set it back, because "I handled this" and
    "actually I did not" are both things a reader needs to be able to say.
    """
    row, target = _load_note(note_id, email)
    is_owner = target.get("owner_email") == email
    if body is None and resolved is None:
        raise AnnotationError(400, "没有要修改的内容 / nothing to change")
    if body is not None:
        if not (row["author_email"] == email or is_owner):
            raise AnnotationError(403, "只有备注作者或文档所有者能编辑 / only the note's author or the document's owner can edit it")
        text = body.strip()
        if not text:
            raise AnnotationError(400, "备注内容不能为空 / note text is required")
        if len(text) > MAX_BODY_LEN:
            raise AnnotationError(400, f"备注为 {len(text)} 字符，上限 {MAX_BODY_LEN} 字符 / note is {len(text)} characters; the limit is {MAX_BODY_LEN}")
    conn = _db()
    try:
        with conn.cursor() as cur:
            if body is not None:
                cur.execute("UPDATE doc_annotations SET body = %s, updated_at = NOW() WHERE id = %s",
                            (text, note_id))
            if resolved is not None:
                # One statement, so "resolved" and "resolved_by" can never disagree.
                cur.execute("UPDATE doc_annotations SET "
                            "resolved_at = CASE WHEN %s THEN NOW() ELSE NULL END, "
                            "resolved_by = CASE WHEN %s THEN %s ELSE NULL END, "
                            "updated_at = NOW() WHERE id = %s",
                            (bool(resolved), bool(resolved), email, note_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if resolved is not None:
        _notify_resolution(row, target, bool(resolved), email)
    return get_note(note_id, email)


@_with_schema
def delete_note(note_id: int, email: str) -> None:
    """The author may delete their own note; the document's owner may delete any."""
    row, target = _load_note(note_id, email)
    if not (row["author_email"] == email or target.get("owner_email") == email):
        raise AnnotationError(403, "只有备注作者或文档所有者能删除 / only the note's author or the document's owner can delete it")
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doc_annotations WHERE id = %s", (note_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # The Inbox row points at a note that no longer exists; leaving it would make
    # the feed link to something the reader cannot open.
    inbox.forget_note(note_id)


@_with_schema
def get_note(note_id: int, email: str) -> dict:
    row, _ = _load_note(note_id, email)
    return _row_to_note(row)


# ── Inbox notifications ──────────────────────────────────────────────────────

def _notify_owner(target: dict, asset_type: str, note: dict) -> None:
    """Tell the document's owner that somebody annotated their document."""
    owner = (target.get("owner_email") or "").strip().lower()
    if not owner or owner == note["author"]:
        return
    label = TARGETS[asset_type][3]
    excerpt = note["body"] if len(note["body"]) <= 80 else note["body"][:77] + "…"
    # ⚠️ `page` is already 1-based — the runtime stores the number the reader can SEE ("Page 2"
    # in the context menu is stored as 2). Adding one here made the Inbox say "第 3 页" about a
    # note the reader left on page 2 (measured 2026-10-01 in verify_office_notes_ui.py).
    where = f"第 {note['page']} 页" if note["page"] else ""
    inbox.emit(
        kind="note", actor_email=note["author"], recipient_email=owner,
        target_type=asset_type, target_slug=target.get("slug", ""),
        target_title=target.get("title", ""), target_url=doc_url(asset_type, target.get("slug", "")),
        summary=f"在你的{label}《{target.get('title') or target.get('slug')}》里留了一条备注{where}：{excerpt}",
        note_id=note["id"],
    )


def _notify_resolution(row: dict, target: dict, resolved: bool, email: str) -> None:
    """Tell the note's author that their note was closed — or reopened."""
    author = (row["author_email"] or "").strip().lower()
    if not author or author == email:
        return
    label = TARGETS[row["asset_type"]][3]
    inbox.emit(
        kind="note_resolved", actor_email=email, recipient_email=author,
        target_type=row["asset_type"], target_slug=row["asset_slug"],
        target_title=target.get("title", ""), target_url=doc_url(row["asset_type"], row["asset_slug"]),
        summary=(f"《{target.get('title') or row['asset_slug']}》里你的备注被标记为已处理"
                 if resolved else
                 f"《{target.get('title') or row['asset_slug']}》里你的备注被重新打开"),
        note_id=row["id"],
    )


# ── Client runtime ───────────────────────────────────────────────────────────
#
# The same file is used three ways, which is why it is a vendor asset and not a
# snippet in a route:
#   * inlined by this module into a served report / event document;
#   * loaded by the SPA with a <script src> to annotate the wiki reading pane;
#   * inlined into a downloaded, self-contained document.
# One implementation, three hosts — the alternative is three right-click menus that
# drift, and this file is what the reader actually experiences.

_ASSET_CACHE: dict[str, str] = {}


def _asset(filename: str) -> str:
    cached = _ASSET_CACHE.get(filename)
    if cached is not None:
        return cached
    path = os.path.join(resolve_frontend_dir(), "vendor", filename)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        _LOG.warning("annotation runtime asset missing (%s): %s", filename, exc)
        text = ""
    _ASSET_CACHE[filename] = text
    return text


def inline_runtime(html: str, *, asset_type: str, slug: str, viewer_email: str = "",
                   writable: bool = True, read_url: str = "", page_hint: int = 0,
                   pages: str = "") -> str:
    """
    Give a served document its note layer.

    `read_url` is for the no-login `/s/{token}` reader: that document may show the
    notes it can already see, but writing needs a session, so it points at a
    token-scoped GET instead of the `/api/` endpoint. Empty `read_url` with
    `writable=false` means "no note layer at all".

    `pages` is a CSS selector for the per-page elements of a paginated preview (a document
    card's sheets of paper / slides / grid). With it the runtime anchors each note to the
    sheet the reader clicked rather than to a percentage of one long scroll — see the
    `pages` host mode in `vendor/annotations.js`.
    """
    css = _asset("annotations.css")
    js = _asset("annotations.js")
    if not css or not js:
        # Runtime missing: the document still renders, it just cannot be annotated.
        return html
    config = {
        "type": asset_type,
        "slug": slug,
        "base": app_base_href(),
        "api": (read_url or f"{app_base_href()}api/annotations"),
        "writable": bool(writable),
        "viewer": viewer_email,
        "page": int(page_hint or 0),
    }
    if pages:
        config["pages"] = pages
    boot = (f"<script data-doc-annotations-boot>\n"
            f"document.addEventListener('DOMContentLoaded',function(){{"
            f"window.DocAnnotations && window.DocAnnotations.mount({json.dumps(config)}); }});\n"
            f"</script>")
    html = htmlkit.insert_before_tag(html, "</head>", f"<style data-doc-annotations>\n{css}\n</style>")
    return htmlkit.insert_before_tag(
        html, "</body>", f"<script data-doc-annotations>\n{js}\n</script>\n{boot}")


def note_to_agent_line(note: dict) -> str:
    """One note as a single line of plain text, for an agent reading a document.

    Carries who wrote it AND when: an agent asked to revise a document has to be able to say
    "this was raised three days ago and has not been answered", and a line without the time
    turns every note into an undated demand. `note['created_at']` is an ISO string (see
    `_row_to_note`), so an agent gets the same instant the Inbox shows a human.

    ⚠️ The page number is printed as stored (1-based, the number the reader saw), never
    incremented — see `_notify_owner`.
    """
    where = f"page {note['page']}" if note.get("page") else "document"
    state = " [resolved]" if note.get("resolved") else ""
    when = str(note.get("created_at") or "")[:16].replace("T", " ")
    return (f"- ({note.get('x', 0):.0f}%, {note.get('y', 0):.0f}% of {where}) "
            f"{note.get('author', '?')} at {when}{state}: {note.get('body', '')}")
