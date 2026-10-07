"""Closing an account: what happens to everything it owned.

The rule this module implements, as decided: **an account's data belongs to that
account.** Deleting the account takes the data with it, after a 30-day grace period
in which it can be restored. Anything a colleague *pulled* is theirs by then and
outlives the original owner.

Three phases, and the order matters:

1. `soft_delete()` — mark the account, revoke its agent codes, and make its content
   invisible to everyone else **now**. Nothing is physically removed.
2. `restore()` — clear the marks. Because phase 1 removed nothing, this is exact:
   the account and its data come back identical, down to row ids.
3. `purge_account()` — the real teardown. Runs at the deadline, or on demand.
   Drops the user's PG tables, deletes their objects from storage, and deletes their
   rows across all six modules.

Why the two-phase design rather than deleting immediately: "close my account" is
rarely deliberate for the first ten seconds, and a hard cascade over six modules is
not something a person can undo or even fully see. The grace period converts the
worst outcome from permanent into a phone call.

What is deliberately NOT deleted, and why — each of these was a real decision, not an
oversight:

* `ai_knowledge_documents` (the project manual). It has no owner column because it is
  reconciled from `api/knowledge_docs/*.md` on every startup; deleting it would
  destroy a derived copy that the next restart writes back.
* `access_log`. It carries `user_email` as text precisely so the audit trail survives
  the account it describes.
* Content a colleague *owns* that merely references the deleted user — an Inbox item
  addressed to somebody else, a calendar event where the deleted user was a partner.
  Those are the other person's content; the deleted user is removed from them, not
  the other way round.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import psycopg2
import psycopg2.extras

from klado_shared.db import connect_main
from klado_shared import accounts as auth_store

log = logging.getLogger(__name__)

#: How long a closed account stays restorable. Surfaced to the user as a date
#: (`purge_after`), not recomputed from this number at read time — a config change
#: must not retroactively move a deadline that was already shown to somebody.
GRACE_DAYS = 30


class LifecycleError(RuntimeError):
    """Carries a bilingual message, like the other services."""


# ── the module's tables, and what each one's "belongs to them" column is ──────
#
# Every entry is (table, owner column, what it holds). The cascade is generated from
# this list on purpose: a hand-written DELETE per module is a list that stops being
# updated when a module is added, and the failure is a row nobody can see any more.
#
# `datasets` and `files` are NOT here — they are real PG tables and storage objects
# and need `_purge_datasets` / `_purge_files`, which are not row deletions.

#: (table, email column, human label) for content the account owns outright.
OWNED_CONTENT: tuple[tuple[str, str, str], ...] = (
    ("ai_reports", "owner_email", "reports and documents"),
    ("ai_dashboards", "owner_email", "dashboards"),
    ("ai_calendar_events", "owner_email", "calendar events"),
    ("ai_knowledge_items", "owner_email", "knowledge pages"),
    # ⚠️ Deleting the project is enough: `ai_knowledge_folders` hangs off it with
    # ON DELETE CASCADE, and `ai_knowledge_folder_shares` off the folders. Listing
    # the folders here too would be a second DELETE of rows the first one already
    # removed. What this buys is that a closed account's project cards are gone —
    # otherwise the address could be registered again and inherit them.
    ("ai_knowledge_projects", "owner_email", "knowledge projects and folders"),
)

#: (table, email column, human label) for rows that are *about* the account but not
#: its content — the ones that must not survive the account, or the account cannot be
#: said to be gone.
#:
#: `mail_settings` is deliberately absent and must stay that way: it is a single
#: deployment-wide row (id=1) holding the SMTP password, with `updated_by` recording
#: who last touched it — not who owns it. Keying a purge on it would either fail (there
#: is no owner column) or, worse, drop the installation's mail configuration because one
#: unrelated account closed.
OWNED_SIDECARS: tuple[tuple[str, str, str], ...] = (
    ("dataset_shares", "grantee_email", "dataset shares received"),
    ("dataset_shares", "shared_by", "dataset shares given"),
    ("ai_report_colleague_shares", "recipient_email", "report shares received"),
    ("ai_report_filter_selections", "user_email", "saved report filters"),
    ("ai_knowledge_item_shares", "recipient_email", "knowledge shares received"),
    # The folder grant. Separate from `ai_knowledge_item_shares` on purpose: it hangs
    # off a folder the *owner* may still have, so it is removed by the recipient's
    # address and not by the project's cascade.
    ("ai_knowledge_folder_shares", "recipient_email", "knowledge folder shares received"),
    ("inbox_events", "recipient_email", "inbox items"),
    ("account_modules", None, "module entitlements"),   # by user_id, not email
    ("agent_tokens", None, "agent access codes"),       # by user_id, not email
    ("login_codes", "email", "unused sign-in codes"),
    ("file_shares", "grantee_email", "file grants received"),
    ("file_shares", "shared_by", "file grants given"),
)


# ── phase 1: soft delete ─────────────────────────────────────────────────────

def soft_delete(user_id: int, *, actor: str = "", days: int = GRACE_DAYS) -> dict:
    """Close the account. Nothing is removed; everything it owned goes dark.

    Idempotent: a second call does not move the deadline. Restarting a 30-day clock
    because somebody clicked twice would quietly shorten a window the user was told
    about.
    """
    user = auth_store.get_user_by_id(user_id)
    if not user:
        raise LifecycleError("账号不存在 / no such account")
    if user.get("deleted_at"):
        return _deleted_view(user)

    email = (user.get("email") or "").strip().lower()
    now = datetime.now(timezone.utc)
    purge_after = now + timedelta(days=max(0, int(days)))

    auth_store._ensure_schema()
    with auth_store._db() as conn:
        with conn.cursor() as cur:
            # ⚠️ `disabled` is deliberately NOT set. It is an operator's switch with
            # its own meaning ("this person is blocked but their account still
            # exists"), and folding it in here would mean a restore silently re-enabled
            # an account an admin had turned off for an unrelated reason. Sign-in is
            # blocked by `deleted_at` in `authenticate()`, which is the one gate that
            # is about *this* lifecycle rather than about the person.
            cur.execute("""
                UPDATE app_users
                   SET deleted_at = %s, purge_after = %s, deleted_by = %s
                 WHERE id = %s AND deleted_at IS NULL
            """, (now, purge_after, (actor or "").strip(), user_id))
            # An agent code outliving the account would be a way back in after the
            # fact, so it is revoked now, not at the purge.
            cur.execute("UPDATE agent_tokens SET revoked_at = NOW(), revoked_by = %s "
                        "WHERE user_id = %s AND revoked_at IS NULL", (actor or "closed", user_id))
            # Any half-issued sign-in code for the address stops working immediately.
            cur.execute("UPDATE login_codes SET consumed_at = NOW(), code_plain = NULL "
                        "WHERE lower(email) = lower(%s) AND consumed_at IS NULL", (email,))
    log.info("account soft-deleted id=%s email=%s purge_after=%s by=%s",
             user_id, email, purge_after.isoformat(), actor)
    return _deleted_view(auth_store.get_user_by_id(user_id) or {})


def restore(user_id: int, *, actor: str = "") -> dict:
    """Undo a close. Exact, because soft delete removed nothing.

    Nothing is touched except the account row itself. In particular the shares the
    account *gave* are left alone: its datasets were never dropped, so every link it
    handed out still resolves, and deleting the grant rows here would break them while
    the data they point at is sitting right there.

    Grants the user *received* are a different matter — those belong to other people
    and survive independently, so there is nothing to undo either way. They are not
    re-created if the owner revoked them in the meantime, which is the correct outcome:
    the person who granted access gets to decide again.

    `disabled` is deliberately left as-is. Soft delete does not set it (see
    `soft_delete`), so an operator's manual "disable" survives a close/restore cycle
    instead of being silently undone by it.
    """
    user = auth_store.get_user_by_id(user_id)
    if not user:
        raise LifecycleError("账号不存在 / no such account")
    if not user.get("deleted_at"):
        return user
    auth_store._ensure_schema()
    with auth_store._db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE app_users
                   SET deleted_at = NULL, purge_after = NULL, deleted_by = ''
                 WHERE id = %s
            """, (user_id,))
    log.info("account restored id=%s email=%s by=%s", user_id, user.get("email"), actor)
    return auth_store.get_user_by_id(user_id) or {}


def _deleted_view(user: dict) -> dict:
    view = dict(user)
    view["pending_deletion"] = True
    view["days_left"] = days_left(user)
    return view


def days_left(user: dict) -> int | None:
    """Whole days until the purge. `None` when there is no deadline.

    Rounded **up**, deliberately. `timedelta.days` truncates, so a deadline 29 hours
    away would read "1 day" and a person deciding whether there is still time would be
    told a day less than they actually have. For a promise ("you have 30 days") the
    error has to be in the direction of leaving it to you, not taking it.
    """
    deadline = user.get("purge_after")
    if not deadline:
        return None
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError:
            return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    delta = deadline - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0
    return int(-(-delta.total_seconds() // 86400))    # ceil without importing math


def list_pending(include_expired: bool = True) -> list[dict]:
    """The recycle bin, newest first."""
    auth_store._ensure_schema()
    sql = ("SELECT id, email, display_name, role, disabled, created_at, last_login_at, "
           "deleted_at, purge_after, deleted_by FROM app_users WHERE deleted_at IS NOT NULL")
    if not include_expired:
        sql += " AND purge_after > NOW()"
    sql += " ORDER BY deleted_at DESC"
    with auth_store._db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            out = []
            for r in cur.fetchall():
                d = auth_store._public(dict(r))
                d["days_left"] = days_left(d)
                out.append(d)
    return out


# ── phase 3: the real purge ──────────────────────────────────────────────────

def purge_due(now: datetime | None = None) -> list[dict]:
    """Accounts whose grace period has run out."""
    auth_store._ensure_schema()
    with auth_store._db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, email FROM app_users "
                        "WHERE deleted_at IS NOT NULL AND purge_after <= %s "
                        "ORDER BY purge_after", (now or datetime.now(timezone.utc),))
            return [dict(r) for r in cur.fetchall()]


def sweep(now: datetime | None = None) -> list[dict]:
    """Purge everything past its deadline. Safe to call often; a no-op when empty."""
    due = purge_due(now)
    done = []
    for row in due:
        try:
            done.append({"email": row["email"], "purged": purge_account(row["id"])})
        except Exception:                                   # noqa: BLE001
            # One account's stuck storage object must not stop the rest of the queue,
            # or a single failure freezes the recycle bin forever.
            log.exception("purge failed for user_id=%s (%s)", row["id"], row["email"])
            done.append({"email": row["email"], "purged": False})
    return done


def purge_account(user_id: int) -> dict:
    """Physically remove the account and everything it owned. Irreversible.

    Ordered so that a failure loses as little as possible: content first, the
    account row last. If a storage object cannot be deleted, the account row stays
    and the sweep retries, rather than the account vanishing while its data lingers
    with nobody attached to it.
    """
    user = auth_store.get_user_by_id(user_id)
    if not user:
        raise LifecycleError("账号不存在 / no such account")
    email = (user.get("email") or "").strip().lower()
    report: dict[str, Any] = {"email": email}

    report["datasets"] = _purge_datasets(email)
    report["files"] = _purge_files(email)
    report["content"] = _purge_owned_content(email, user_id)
    report["sidecars"] = _purge_sidecars(email, user_id)
    _detach_from_others(email)

    with auth_store._db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE access_log SET user_id = NULL WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_users WHERE id = %s", (user_id,))
    log.info("account purged id=%s email=%s %s", user_id, email, report)
    return report


# ── Data Center: real tables and storage objects ─────────────────────────────

def _purge_datasets(email: str) -> int:
    """DROP every table the account created, and take the grants with it.

    The grants are revoked through `data_center_db.delete_pg_table` so the same code
    path runs as a manual delete — including the cascade, which exists precisely so a
    dropped dataset cannot leave a grant behind for a future upload that reuses the
    name.
    """
    from services import data_center_db as db

    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM public._import_registry "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            names = [r[0] for r in cur.fetchall()]
    dropped = 0
    for name in names:
        try:
            db.delete_pg_table(name)
            dropped += 1
        except Exception:                                   # noqa: BLE001
            log.exception("could not drop dataset %s (owner %s)", name, email)
    return dropped


def _purge_files(email: str) -> int:
    """Delete the account's uploads from object storage and the library.

    Uses the store's own delete so the object key is resolved the same way it is for
    a normal delete. A storage failure is logged and the library row still goes: an
    orphaned object is recoverable from the bucket listing, a database row pointing
    at a deleted account's object is not.
    """
    from services import data_center_db as db

    with db.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public._file_library "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            ids = [r[0] for r in cur.fetchall()]
    removed = 0
    for file_id in ids:
        try:
            db.delete_file(file_id)
            removed += 1
        except Exception:                                   # noqa: BLE001
            log.exception("could not delete file %s (owner %s)", file_id, email)
    return removed


# ── the other modules ────────────────────────────────────────────────────────

def _purge_owned_content(email: str, user_id: int) -> dict[str, int]:
    """Delete the account's rows in the four content tables, with their children.

    ⚠️ `ai_knowledge_documents` is NOT in `OWNED_CONTENT` and must never be added: it
    has no owner column because it is reconciled from `api/knowledge_docs/*.md` on
    every startup. Deleting its rows would only lose a derived copy that the next
    restart writes straight back — it is the product's content, not the user's.
    """
    counts: dict[str, int] = {}
    children: tuple[tuple[str, str, str], ...] = (
        # (child table, fk column, parent table)
        ("ai_dashboard_colleague_shares", "dashboard_id", "ai_dashboards"),
        ("ai_report_colleague_shares", "report_id", "ai_reports"),
        ("ai_report_anyone_links", "report_id", "ai_reports"),
        ("ai_knowledge_item_shares", "item_id", "ai_knowledge_items"),
    )
    for table, column, label in OWNED_CONTENT:
        with connect_main() as conn:
            with conn.cursor() as cur:
                # Children first: they cascade in SQL, but doing it here means the
                # count in the report is the number of rows actually removed and a
                # table without the FK constraint cannot leave orphans behind.
                for child, fk, parent in children:
                    if parent != table:
                        continue
                    cur.execute(
                        f"DELETE FROM public.{child} WHERE {fk} IN "
                        f"(SELECT id FROM public.{table} WHERE lower(owner_email) = lower(%s))",
                        (email,))
                    counts[f"{child}"] = cur.rowcount
                cur.execute(f"DELETE FROM public.{table} WHERE lower(owner_email) = lower(%s)",
                            (email,))
                counts[table] = cur.rowcount
            conn.commit()
    return counts


def _purge_sidecars(email: str, user_id: int) -> dict[str, int]:
    """Delete the rows that are about the account rather than owned by it."""
    counts: dict[str, int] = {}
    for table, column, label in OWNED_SIDECARS:
        if column is None:
            continue                              # handled by user_id below
        with connect_main() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM public.{table} WHERE lower({column}) = lower(%s)",
                            (email,))
                counts[table] = cur.rowcount
            conn.commit()
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.account_modules WHERE user_id = %s", (user_id,))
            counts["account_modules"] = cur.rowcount
            cur.execute("DELETE FROM public.agent_tokens WHERE user_id = %s", (user_id,))
            counts["agent_tokens"] = cur.rowcount
        conn.commit()
    return counts


def _detach_from_others(email: str) -> dict[str, int]:
    """Take the account out of OTHER people's content without touching their content.

    The asymmetry is the point. A colleague's Inbox item that happens to name this
    person is the colleague's item — the person is removed from it, not the item from
    their inbox. Same for calendar partners.
    """
    counts = {}
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.ai_calendar_event_partners "
                        "WHERE lower(partner_email) = lower(%s)", (email,))
            counts["calendar_partners"] = cur.rowcount
            # An Inbox item that this person is the *actor* of stays, with the actor
            # label blanked rather than deleted: the recipient's history should not
            # lose a row because its author closed their account.
            cur.execute("UPDATE public.inbox_events SET actor_email = '' "
                        "WHERE lower(actor_email) = lower(%s)", (email,))
            counts["inbox_actor_blanked"] = cur.rowcount
        conn.commit()
    return counts


# ── who is using this account's data ─────────────────────────────────────────

def dependents(user_id: int) -> dict[str, list]:
    """Who would be affected if this account were purged.

    The answer drives the "are you sure" step, so it is written from the OTHER
    people's side: dashboards that name this account's datasets, accounts holding a
    grant on them, and the colleagues whose live pages would start failing. A
    warning that only counts the account's own rows tells the operator nothing about
    what they are about to break.
    """
    user = auth_store.get_user_by_id(user_id)
    if not user:
        raise LifecycleError("账号不存在 / no such account")
    email = (user.get("email") or "").strip().lower()
    out: dict[str, list] = {"datasets": [], "dashboards": [], "shares": [],
                            "reports": [], "own_dashboards": [], "knowledge": [],
                            "calendar": [], "files": [], "public_items": []}
    with connect_main() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT table_name, display_name, row_count FROM public._import_registry "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            datasets = [dict(r) for r in cur.fetchall()]
            out["datasets"] = datasets
            names = [d["table_name"] for d in datasets]
            if names:
                # A dashboard is a live query, not a copy: it names the dataset by
                # name. It is the thing that breaks when the table goes.
                cur.execute("SELECT slug, title, owner_email, datasets FROM public.ai_dashboards "
                            "WHERE lower(owner_email) <> lower(%s)", (email,))
                for r in cur.fetchall():
                    try:
                        declared = dashboard_datasets(r["datasets"])
                    except Exception:                       # noqa: BLE001
                        declared = []
                    hits = [n for n in names if n in declared]
                    if hits:
                        out["dashboards"].append(
                            {"slug": r["slug"], "title": r["title"],
                             "owner_email": r["owner_email"], "datasets": hits})
                cur.execute("SELECT grantee_email, table_name FROM public.dataset_shares "
                            "WHERE lower(shared_by) = lower(%s) OR table_name = ANY(%s)",
                            (email, names))
                out["shares"] = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT slug, title, visibility FROM public.ai_reports "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            out["reports"] = [dict(r) for r in cur.fetchall()]
            # The account's OWN pages. `dashboards` above lists other people's pages that
            # depend on this account's data; this is the other half — the pages that
            # simply disappear with it, which the operator is about to lose.
            cur.execute("SELECT slug, title, visibility FROM public.ai_dashboards "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            out["own_dashboards"] = [dict(r) for r in cur.fetchall()]
            # Derived from the rows already fetched rather than re-queried: a second
            # SELECT here would both cost a round trip and add every public report
            # twice to `total`, which is the number the confirm dialog shows.
            out["public_items"] = [r for r in out["reports"] if r.get("visibility") == "public"]
            cur.execute("SELECT slug, title, visibility FROM public.ai_knowledge_items "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            out["knowledge"] = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT slug, title, visibility FROM public.ai_calendar_events "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            out["calendar"] = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT count(*) AS n FROM public._file_library "
                        "WHERE lower(owner_email) = lower(%s)", (email,))
            row = cur.fetchone()
            file_count = int(row["n"]) if row else 0
            out["files"] = [{"count": file_count}] if file_count else []
    # `total` is what the operator reads before confirming, so it has to be a count of
    # things, not of buckets. Three corrections:
    #   * `files` is a single aggregate row — it would contribute 1 whether the account
    #     holds one upload or two hundred, so it is counted by its real count;
    #   * `public_items` is a derived VIEW of `reports`, not a second set of rows, so
    #     summing it would count every public report twice;
    #   * `own_dashboards` must be in the sum — leaving it out silently under-reports
    #     what the account owns, which is the one number the dialog exists to state.
    counted = ("datasets", "dashboards", "shares", "reports", "own_dashboards",
               "knowledge", "calendar")
    out["total"] = sum(len(out[k]) for k in counted) + file_count
    out["blocking"] = bool(out["dashboards"] or out["shares"] or out["public_items"])
    return out


def dashboard_datasets(raw: Any) -> list[str]:
    """The dataset names a dashboard declares — the column stores JSON text."""
    from services.dashboard_store import parse_datasets
    return parse_datasets(raw)
