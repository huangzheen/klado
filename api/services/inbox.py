"""
The Inbox — a reverse-chronological log of what colleagues and the data layer did.

One table, `inbox_events`, and one rule for who sees a row:

* `recipient_email` set   → that person only ("X shared a document with you",
  "Y left a note in your document").
* `recipient_email` NULL  → a broadcast every signed-in user sees ("X published a
  report to the public area", "a dataset was re-imported").

That split is the whole visibility model. It is deliberately a column and not a
rule per `kind`, because "who may see this" is exactly the question that gets
re-asked every time a new emitter is added, and a missing row is invisible: a new
event type that forgets to choose a recipient silently becomes a broadcast.

Everything a reader DOES to a row is per reader and lives in
`inbox_reader_state`, never in `inbox_events`:

* `read_at` on the row is what "mark as read" writes. It is one column for the
  whole row, which is why a broadcast's read state is shared — marking one read
  for me marks it read for everyone. That is the long-standing behaviour of
  "Mark all read" and it is left exactly as it was.
* "mark as unread" cannot use it: it is a per-reader control, and writing the
  shared column would put a colleague's message back in bold in THEIR inbox too.
  So it writes an override row, which the effective read state prefers.
* "delete" writes `dismissed_at` on the same override row. It hides the row from
  one reader and leaves the log — and every other reader — alone.

`emit()` is best-effort. An Inbox is an observation of work that already
succeeded, so it must never be the reason that work fails — a colleague's share
is not rolled back because the notification table was briefly unavailable. The
one thing we do insist on is that failures are LOUD: `_LOG` does not reach the
server's stdout (see core/logring.py), so the warning is also printed with
flush=True.
"""
from __future__ import annotations

import logging
import threading

import psycopg2
import psycopg2.extras
from core.db import connect_main

_LOG = logging.getLogger(__name__)

# Emitters pass one of these. Kept as a set (not free text) so a typo in a new
# emitter is a 500 at review time rather than a class of rows the UI cannot
# colour or group.
KINDS = (
    "note",           # someone annotated a document you can see
    "note_resolved",  # the owner closed a note you left
    "share",          # a colleague granted you access to a document
    "publish",        # a document became a public-area snapshot
    "dataset",        # a Data Center dataset was re-imported / refreshed
)

# "Rows this reader may see, minus the ones they hid", in one place. The feed and
# the unread count MUST ask it identically: they are the same question asked twice,
# and two copies of the rule is how a badge and a list end up disagreeing.
# Parameter order is (reader_email, reader_email) — the LEFT JOIN, then the recipient.
_VISIBLE = (
    "LEFT JOIN inbox_reader_state s ON s.event_id = inbox_events.id "
    "AND s.reader_email = %s "
    "WHERE (inbox_events.recipient_email IS NULL OR inbox_events.recipient_email = %s) "
    "AND (s.reader_email IS NULL OR s.dismissed_at IS NULL)"
)

# The read state this reader sees: their own override when they have one, the row's
# own `read_at` otherwise. `s.reader_email IS NULL` is "no override row" — the two
# are NOT interchangeable with a NULL read_at, which is the whole point of the
# override existing.
_EFFECTIVE_READ = (
    "CASE WHEN s.reader_email IS NULL THEN inbox_events.read_at ELSE s.read_at END"
)

_schema_ready = False
_schema_lock = threading.Lock()


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
                    CREATE TABLE IF NOT EXISTS inbox_events (
                        id              SERIAL PRIMARY KEY,
                        kind            TEXT NOT NULL,
                        actor_email     TEXT NOT NULL DEFAULT '',
                        recipient_email TEXT,          -- NULL = broadcast to every signed-in user
                        target_type     TEXT NOT NULL DEFAULT '',   -- report|knowledge|event|dataset
                        target_slug     TEXT NOT NULL DEFAULT '',
                        target_title    TEXT NOT NULL DEFAULT '',
                        target_url      TEXT NOT NULL DEFAULT '',
                        summary         TEXT NOT NULL DEFAULT '',
                        note_id         INTEGER,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        read_at         TIMESTAMPTZ
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS inbox_events_created_idx "
                            "ON inbox_events (created_at DESC)")
                # The feed always filters on the reader, and NULL recipients must
                # stay usable in that index — a partial index would hide broadcasts.
                cur.execute("CREATE INDEX IF NOT EXISTS inbox_events_recipient_idx "
                            "ON inbox_events (recipient_email, created_at DESC)")
                # Per-reader state. Two columns that can each be NULL, and the NULL means
                # different things in each — read_at NULL is "this reader marked it
                # unread", dismissed_at NULL is "still in this reader's Inbox". Both are
                # read through the row's EXISTENCE (see `_VISIBLE`), never through COALESCE,
                # because an explicit NULL is exactly the value that has to win here.
                #
                # ⚠️ Why a table and not a DELETE on inbox_events: a broadcast is one row
                # that every signed-in user sees, so deleting it for me would delete the
                # record that a colleague published something, for everybody. This Inbox
                # observes work that already succeeded (see the module docstring); hiding
                # a row is a decision about MY screen, never about what happened.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS inbox_reader_state (
                        reader_email TEXT NOT NULL,
                        event_id     INTEGER NOT NULL REFERENCES inbox_events(id) ON DELETE CASCADE,
                        read_at      TIMESTAMPTZ,
                        dismissed_at TIMESTAMPTZ,
                        PRIMARY KEY (reader_email, event_id)
                    )
                """)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _schema_ready = True


def emit(*, kind: str, summary: str, actor_email: str = "", recipient_email: str | None = None,
         target_type: str = "", target_slug: str = "", target_title: str = "",
         target_url: str = "", note_id: int | None = None) -> None:
    """Record one Inbox row. Never raises — see the module docstring."""
    if kind not in KINDS:
        # A wrong `kind` is a programming error, not a runtime condition. Make it
        # visible on the server's stdout rather than storing a row the UI cannot group.
        print(f"WARNING inbox.emit: unknown kind {kind!r} for {target_type}:{target_slug}",
              flush=True)
        return
    try:
        ensure_tables()
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO inbox_events (kind, actor_email, recipient_email, target_type, "
                    "target_slug, target_title, target_url, summary, note_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (kind, (actor_email or "").strip().lower(), recipient_email,
                     target_type, target_slug, target_title, target_url, summary, note_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:            # noqa: BLE001 — see module docstring
        _LOG.warning("inbox emit failed (%s %s): %s", kind, target_slug, exc)
        print(f"WARNING inbox emit failed ({kind} {target_slug}): {exc}", flush=True)


def _display_names(emails: list[str]) -> dict[str, str]:
    """actor_email → app_users.display_name, best-effort.

    The feed must never fail because of this decoration (an auth table that does
    not exist yet, a migration mid-flight), so any problem degrades to '' and the
    UI falls back to the email local-part.
    """
    wanted = sorted({e for e in emails if e})
    if not wanted:
        return {}
    try:
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT email, display_name FROM app_users WHERE email = ANY(%s)",
                            (wanted,))
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:            # noqa: BLE001 — decoration only, see above
        _LOG.warning("inbox display-name lookup failed: %s", exc)
        return {}
    return {email: (name or "").strip() for email, name in rows if (name or "").strip()}


def feed(email: str, *, limit: int = 60, before_id: int | None = None) -> list[dict]:
    """This reader's rows, newest first: their own plus every broadcast.

    Rows this reader has hidden are gone, and `read_at` is the state THEY see —
    the list's bold rows and the badge's number are computed from the same
    expression for exactly that reason.
    """
    ensure_tables()
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = ("SELECT id, kind, actor_email, recipient_email, target_type, target_slug, "
                   "target_title, target_url, summary, note_id, created_at, "
                   + _EFFECTIVE_READ + " AS read_at "
                   "FROM inbox_events " + _VISIBLE + " ")
            params: list = [email, email]
            if before_id:
                sql += "AND inbox_events.id < %s "
                params.append(before_id)
            sql += "ORDER BY id DESC LIMIT %s"
            params.append(max(1, min(limit, 200)))
            cur.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # The UI writes "Zhen Huang published …", so the actor's display name rides
    # along; the email alone reads like a machine log.
    names = _display_names([r.get("actor_email", "") for r in rows])
    for r in rows:
        r["actor_name"] = names.get(r.get("actor_email", ""), "")
    return rows


def unread_count(email: str) -> int:
    ensure_tables()
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM inbox_events " + _VISIBLE
                        + " AND " + _EFFECTIVE_READ + " IS NULL",
                        (email, email))
            return int(cur.fetchone()[0])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_read(email: str, *, ids: list[int] | None = None) -> int:
    """Mark rows read. A NULL `ids` means "everything I can see"."""
    ensure_tables()
    conn = _db()
    try:
        with conn.cursor() as cur:
            if ids:
                cur.execute("UPDATE inbox_events SET read_at = NOW() "
                            "WHERE read_at IS NULL AND id = ANY(%s) "
                            "AND (recipient_email IS NULL OR recipient_email = %s)",
                            (list(ids), email))
            else:
                cur.execute("UPDATE inbox_events SET read_at = NOW() WHERE read_at IS NULL "
                            "AND (recipient_email IS NULL OR recipient_email = %s)", (email,))
            changed = cur.rowcount
            # ⚠️ Clear this reader's own "unread again" overrides, or the row stays bold
            # in their list after they click it: the override is exactly what the
            # effective read state reads first. `dismissed_at IS NULL` keeps a hidden
            # row hidden — dropping the whole row here would resurrect it.
            if ids:
                cur.execute("DELETE FROM inbox_reader_state "
                            "WHERE reader_email = %s AND dismissed_at IS NULL "
                            "AND event_id = ANY(%s)", (email, list(ids)))
            else:
                cur.execute("DELETE FROM inbox_reader_state "
                            "WHERE reader_email = %s AND dismissed_at IS NULL "
                            "AND read_at IS NULL", (email,))
        conn.commit()
        return int(changed or 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_unread(email: str, *, ids: list[int] | None = None) -> int:
    """Put rows back to unread **for this reader only**.

    The override, not `inbox_events.read_at`: a broadcast's `read_at` is one column
    shared by every user, so writing it here would turn a colleague's message bold in
    THEIR inbox as well — a per-reader control with a team-wide effect.
    """
    return _set_state(email, ids=ids, read_at="NULL")


def dismiss(email: str, *, ids: list[int] | None = None) -> int:
    """Hide rows from this reader's Inbox. A NULL `ids` means "everything I can see".

    The row survives for everyone else, and nothing is removed from the log itself —
    see the `inbox_reader_state` note in `ensure_tables`.
    """
    return _set_state(email, ids=ids, dismissed_at="NOW()")


def _set_state(email: str, *, ids: list[int] | None, read_at: str | None = None,
               dismissed_at: str | None = None) -> int:
    """Upsert one reader's state on the rows they can see.

    The candidate ids come from a SELECT against `inbox_events`, so the visibility
    rule is enforced by the database: there is no path here by which a reader can
    touch a row addressed to somebody else, whatever ids the caller sends.
    """
    ensure_tables()
    # ⚠️ The two interpolated values are THIS function's own literals ("read_at" /
    # "dismissed_at", "NULL" / "NOW()"), never anything a caller can reach — the ids
    # and the reader's email stay in `params`. The only caller-supplied value is
    # `ids`, and it is a bound parameter.
    column = "read_at" if read_at is not None else "dismissed_at"
    value = read_at if read_at is not None else dismissed_at
    sql = ("INSERT INTO inbox_reader_state (reader_email, event_id, %s) "
           "SELECT %%s, inbox_events.id, %s FROM inbox_events "
           "WHERE (inbox_events.recipient_email IS NULL OR inbox_events.recipient_email = %%s)"
           % (column, value))
    params: list = [email, email]
    if ids:
        sql += " AND inbox_events.id = ANY(%s)"
        params.append(list(ids))
    sql += (" ON CONFLICT (reader_email, event_id) DO UPDATE SET %s = EXCLUDED.%s"
            % (column, column))
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            changed = cur.rowcount
        conn.commit()
        return int(changed or 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def forget_note(note_id: int) -> None:
    """Drop the Inbox rows that point at a deleted note (best-effort)."""
    try:
        ensure_tables()
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM inbox_events WHERE note_id = %s", (note_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:            # noqa: BLE001
        _LOG.warning("inbox forget_note(%s) failed: %s", note_id, exc)
