"""Saved connections to an external PostgreSQL, and the schedule that keeps a copy fresh.

⚠️ **Isolation here is a product decision, not a default.** The rule the product owner
set is: *a connection is private to the person operating that database; the local COPY
it produces may be shared, and the source credentials may never be shared.*

So this module is shaped so that rule is hard to get wrong rather than merely documented:

* every read and write filters on `owner_email`. There is no `admin=True` escape on
  these functions — an operator can administer ACCOUNTS, and being an operator is not
  a reason to hold somebody's database password;
* the credential is Fernet-encrypted under `SECRET_KEY` and is **never returned by any
  function here**, not even to the owner. `public_view` reports `has_password` and
  nothing else, so "edit this connection" in the UI starts blank instead of handing a
  password back to a page that may be shared or screenshotted;
* a scheduled pull runs **as the owner** and writes into the owner's own local copy.
  Somebody the copy is shared with sees the refreshed ROWS — the point of the feature —
  and never the connection, the schedule, or the wire.

⚠️ There is deliberately **no plaintext fallback** for the credential, unlike
`klado_shared/accounts.py::_encrypt_code`, which degrades to `plain:` when the crypto
import is missing. That is right for a display code and wrong for a password: a "the
dependency is missing, here it is in the clear" path means a database dump contains a
usable credential. Here the save is refused instead, and the caller is told why.

The import itself is NOT reimplemented. A scheduled pull calls
`dataset_groups.import_external_postgres`, so the read-only session, the identifier
checks, the column guard and the name-collision guard are the same code a person runs
by hand from the dialog.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import timedelta
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from psycopg2.extras import RealDictCursor

from core.config import settings
from services import data_center_db as db
from services import dataset_groups

_LOG = logging.getLogger(__name__)

CONN_TABLE = "public.external_db_connections"
JOB_TABLE = "public.external_db_sync_jobs"

# A different salt from `klado_shared/accounts.py::AGENT_CODE_SALT` and from
# `mail_config`, so the three stored secrets are unrelated keys: one being rotated
# (or one being cracked) says nothing about the others.
CREDENTIAL_SALT = "external-db-credentials"

# Interval choices the UI offers, in minutes. The scheduler itself accepts any
# positive integer; this is only the set of shortcuts.
INTERVAL_CHOICES = (15, 60, 360, 1440, 10080)

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 60 * 24 * 90

# One tick a minute. Which jobs are due is decided in SQL from `last_run_at`, not by
# how often APScheduler happens to fire, so a restart cannot shorten or skip a period.
TICK_MINUTES = 1

_SCHEDULER = None


class SyncError(Exception):
    """Refused, with a message a person can act on. Never a credential."""


# ── schema ────────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {CONN_TABLE} (
                    id           SERIAL PRIMARY KEY,
                    owner_email  TEXT NOT NULL,
                    label        TEXT NOT NULL DEFAULT '',
                    host         TEXT NOT NULL,
                    port         INTEGER NOT NULL DEFAULT 5432,
                    db_name      TEXT NOT NULL,
                    db_user      TEXT NOT NULL DEFAULT '',
                    db_schema    TEXT NOT NULL DEFAULT 'public',
                    password_enc TEXT NOT NULL DEFAULT '',
                    created_at   TIMESTAMP DEFAULT NOW(),
                    updated_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS external_db_conn_owner "
                        f"ON {CONN_TABLE} (owner_email)")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {JOB_TABLE} (
                    id           SERIAL PRIMARY KEY,
                    owner_email  TEXT NOT NULL,
                    connection_id INTEGER NOT NULL
                                   REFERENCES {CONN_TABLE}(id) ON DELETE CASCADE,
                    slug         TEXT NOT NULL,
                    target_table TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    mode         TEXT NOT NULL DEFAULT 'overwrite',
                    interval_minutes INTEGER NOT NULL DEFAULT 1440,
                    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
                    last_run_at  TIMESTAMP,
                    last_status  TEXT NOT NULL DEFAULT '',
                    last_error   TEXT NOT NULL DEFAULT '',
                    last_rows    INTEGER,
                    created_at   TIMESTAMP DEFAULT NOW(),
                    updated_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS external_db_job_owner "
                        f"ON {JOB_TABLE} (owner_email)")
        conn.commit()


# ── credential ────────────────────────────────────────────────────────────────

def _fernet() -> "Fernet | None":
    try:
        material = hashlib.sha256(
            f"{CREDENTIAL_SALT}:{settings.SECRET_KEY}".encode()).digest()
        return Fernet(base64.urlsafe_b64encode(material))
    except Exception:  # noqa: BLE001 — a missing/broken crypto import must not crash
        _LOG.warning("external db credentials unavailable: crypto import failed")
        return None


def encrypt_credential(plain: str) -> str:
    """`enc:…`, or refuse. There is no `plain:` form — see the module docstring."""
    if not plain:
        return ""
    f = _fernet()
    if f is None:
        raise SyncError(
            "口令无法安全保存：本机缺少加密依赖，因此拒绝保存明文。"
            " / The password cannot be stored safely: the encryption dependency is "
            "missing, so saving it in the clear is refused.")
    return "enc:" + f.encrypt(plain.encode("utf-8")).decode()


def decrypt_credential(stored: str) -> str:
    """The password, or `""` when it cannot be recovered (key rotated, row corrupt)."""
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        # A value that is not in our envelope is not silently treated as a password —
        # that is how a base64 blob becomes a login attempt. Refuse instead.
        _LOG.warning("external db credential is not in the expected envelope")
        return ""
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(stored[len("enc:"):].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        # The SECRET_KEY was rotated, or the row was tampered with. The pull then
        # fails with "cannot connect" and the owner re-enters the password — which
        # is the honest outcome. Returning "" here would authenticate as an empty
        # password against somebody's production database.
        _LOG.warning("external db credential could not be decrypted")
        return ""


# ── views ─────────────────────────────────────────────────────────────────────

def public_view(row: dict) -> dict:
    """What the API is allowed to hand out.

    ⚠️ The password is not here in any form — not masked, not prefixed, not a
    length. `has_password` exists so the form can say 「已保存口令，留空则不改」,
    which is the only reason a UI needs to know anything about it.
    """
    return {
        "id": row["id"],
        "label": row.get("label") or "",
        "host": row.get("host") or "",
        "port": int(row.get("port") or 5432),
        "database": row.get("db_name") or "",
        "user": row.get("db_user") or "",
        "schema": row.get("db_schema") or "public",
        "has_password": bool(row.get("password_enc")),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
    }


def _next_run_at(row: dict) -> str | None:
    """When this schedule fires next, derived — never stored.

    ⚠️ Derived here and not in the browser on purpose: `due_jobs()` decides what is
    due by ``last_run_at <= NOW() - interval`` (or ``created_at`` for a schedule that
    has never run), so a UI that recomputed it from `last_run_at` alone would show
    "in N minutes" for a job created five minutes ago that is actually due now.
    One rule, one place. A paused schedule has no next run, which is the honest answer
    rather than a time it will never keep.
    """
    if not row.get("enabled", True):
        return None
    anchor = row.get("last_run_at") or row.get("created_at")
    if anchor is None:
        return None
    return (anchor + timedelta(minutes=int(row["interval_minutes"]))).isoformat()


def job_view(row: dict) -> dict:
    """A schedule. It names a connection by id, never by credential."""
    return {
        "id": row["id"],
        "connection_id": row["connection_id"],
        "connection_label": row.get("connection_label") or "",
        "slug": row["slug"],
        "target_table": row["target_table"],
        "source_table": row["source_table"],
        "mode": row["mode"],
        "interval_minutes": int(row["interval_minutes"]),
        "enabled": bool(row["enabled"]),
        "last_run_at": row["last_run_at"].isoformat() if row.get("last_run_at") else None,
        "next_run_at": _next_run_at(row),
        "last_status": row.get("last_status") or "",
        "last_error": row.get("last_error") or "",
        "last_rows": row.get("last_rows"),
    }


# ── connections ───────────────────────────────────────────────────────────────

def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def save_connection(owner_email: str, spec: dict, connection_id: int | None = None) -> dict:
    if not owner_email:
        raise SyncError("需要登录 / sign in first")
    host = _clean(spec.get("host"), 255)
    db_name = _clean(spec.get("database"), 255)
    if not host or not db_name:
        raise SyncError("主机和数据库都要填 / host and database are both required")
    try:
        port = int(spec.get("port") or 5432)
    except (TypeError, ValueError):
        raise SyncError("端口不是数字 / the port is not a number") from None

    password = spec.get("password") or ""
    # An edit that leaves the password blank KEEPS the stored one; it does not clear
    # it. Clearing has to be explicit, or saving an unrelated field would silently
    # break the next scheduled pull.
    enc = ""
    if password:
        enc = encrypt_credential(str(password))
    elif not connection_id:
        enc = ""

    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if connection_id:
                cur.execute(f"SELECT id FROM {CONN_TABLE} WHERE id=%s AND owner_email=%s",
                            (int(connection_id), owner_email))
                if cur.fetchone() is None:
                    raise SyncError("连接不存在或不是你的 / no such connection, or it is not yours")
                # An edit that leaves the password blank KEEPS the stored one; it does
                # not clear it. Clearing has to be explicit, or saving an unrelated
                # field would silently break the next scheduled pull.
                sets = ["host=%s", "port=%s", "db_name=%s", "db_user=%s", "db_schema=%s",
                        "label=%s", "updated_at=NOW()"]
                args: list[Any] = [host, port, db_name, _clean(spec.get("user"), 255),
                                   _clean(spec.get("schema") or "public", 63) or "public",
                                   _clean(spec.get("label"), 80)]
                if enc:
                    sets.insert(0, "password_enc=%s")
                    args.insert(0, enc)
                args += [int(connection_id), owner_email]
                cur.execute(f"UPDATE {CONN_TABLE} SET {', '.join(sets)} "
                            f"WHERE id=%s AND owner_email=%s RETURNING *", args)
            else:
                cur.execute(
                    f"INSERT INTO {CONN_TABLE} "
                    f"(owner_email, label, host, port, db_name, db_user, db_schema, password_enc) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (owner_email, _clean(spec.get("label"), 80), host, port, db_name,
                     _clean(spec.get("user"), 255),
                     _clean(spec.get("schema") or "public", 63) or "public", enc))
            row = cur.fetchone()
        conn.commit()
    return public_view(dict(row))


def list_connections(owner_email: str) -> list[dict]:
    if not owner_email:
        return []
    ensure_schema()
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {CONN_TABLE} WHERE owner_email=%s ORDER BY id", (owner_email,))
            return [public_view(dict(r)) for r in cur.fetchall()]


def delete_connection(owner_email: str, connection_id: int) -> None:
    """Cascades to that person's schedules. Somebody else's is simply not there."""
    ensure_schema()
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"DELETE FROM {CONN_TABLE} WHERE id=%s AND owner_email=%s",
                        (int(connection_id), owner_email))
            if cur.rowcount == 0:
                raise SyncError("连接不存在或不是你的 / no such connection, or it is not yours")
        conn.commit()


def connection_spec(connection_id: int, owner_email: str) -> dict:
    """The full spec, credential included — the ONE place that returns a password.

    Only the scheduler and the import path call it, both of which immediately hand the
    dict to psycopg2. Nothing that renders a response may call this.
    """
    ensure_schema()
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM {CONN_TABLE} WHERE id=%s AND owner_email=%s",
                        (int(connection_id), owner_email))
            row = cur.fetchone()
    if row is None:
        raise SyncError("连接不存在或不是你的 / no such connection, or it is not yours")
    row = dict(row)
    return {
        "host": row["host"], "port": int(row["port"] or 5432), "database": row["db_name"],
        "user": row["db_user"], "schema": row["db_schema"] or "public",
        "password": decrypt_credential(row.get("password_enc") or ""),
    }


# ── schedules ─────────────────────────────────────────────────────────────────

def _clamp_interval(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise SyncError("周期必须是分钟数 / the interval must be a number of minutes") from None
    if minutes < MIN_INTERVAL_MINUTES or minutes > MAX_INTERVAL_MINUTES:
        raise SyncError(
            f"周期要在 {MIN_INTERVAL_MINUTES} 分钟到 {MAX_INTERVAL_MINUTES} 分钟之间 / "
            f"the interval must be between {MIN_INTERVAL_MINUTES} and "
            f"{MAX_INTERVAL_MINUTES} minutes")
    return minutes


def create_job(owner_email: str, body: dict) -> dict:
    if not owner_email:
        raise SyncError("需要登录 / sign in first")
    ensure_schema()
    connection_id = body.get("connection_id")
    if not connection_id:
        raise SyncError("先选一个连接 / pick a connection first")
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT id FROM {CONN_TABLE} WHERE id=%s AND owner_email=%s",
                        (int(connection_id), owner_email))
            if cur.fetchone() is None:
                raise SyncError("连接不存在或不是你的 / no such connection, or it is not yours")
            source = _clean(body.get("source_table"), 128)
            target = _clean(body.get("target_table"), 128)
            if not source:
                raise SyncError("要拉哪张表 / which source table?")
            mode = "append" if body.get("mode") == "append" else "overwrite"
            cur.execute(
                f"INSERT INTO {JOB_TABLE} (owner_email, connection_id, slug, target_table, "
                f"source_table, mode, interval_minutes, enabled) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (owner_email, int(connection_id), _clean(body.get("slug"), 80),
                 target or source, source, mode,
                 _clamp_interval(body.get("interval_minutes") or 1440),
                 bool(body.get("enabled", True))))
            job_id = cur.fetchone()["id"]
        conn.commit()
    return get_job(owner_email, job_id)


def _job_row(owner_email: str, job_id: int) -> dict:
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT j.*, c.label AS connection_label FROM {JOB_TABLE} j "
                f"LEFT JOIN {CONN_TABLE} c ON c.id = j.connection_id "
                f"WHERE j.id=%s AND j.owner_email=%s", (int(job_id), owner_email))
            row = cur.fetchone()
    if row is None:
        raise SyncError("定时拉取不存在或不是你的 / no such schedule, or it is not yours")
    return dict(row)


def get_job(owner_email: str, job_id: int) -> dict:
    return job_view(_job_row(owner_email, job_id))


def list_jobs(owner_email: str) -> list[dict]:
    if not owner_email:
        return []
    ensure_schema()
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT j.*, c.label AS connection_label FROM {JOB_TABLE} j "
                f"LEFT JOIN {CONN_TABLE} c ON c.id = j.connection_id "
                f"WHERE j.owner_email=%s ORDER BY j.id", (owner_email,))
            return [job_view(dict(r)) for r in cur.fetchall()]


def update_job(owner_email: str, job_id: int, body: dict) -> dict:
    ensure_schema()
    _job_row(owner_email, job_id)          # ownership first, before any field is read
    sets, args = [], []
    if "enabled" in body:
        sets.append("enabled=%s"); args.append(bool(body["enabled"]))
    if "interval_minutes" in body:
        sets.append("interval_minutes=%s"); args.append(_clamp_interval(body["interval_minutes"]))
    if body.get("mode") in ("overwrite", "append"):
        sets.append("mode=%s"); args.append(body["mode"])
    if sets:
        sets.append("updated_at=NOW()")
        args += [int(job_id), owner_email]
        with db.get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"UPDATE {JOB_TABLE} SET {', '.join(sets)} "
                            f"WHERE id=%s AND owner_email=%s", args)
            conn.commit()
    return get_job(owner_email, job_id)


def delete_job(owner_email: str, job_id: int) -> None:
    ensure_schema()
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"DELETE FROM {JOB_TABLE} WHERE id=%s AND owner_email=%s",
                        (int(job_id), owner_email))
            if cur.rowcount == 0:
                raise SyncError("定时拉取不存在或不是你的 / no such schedule, or it is not yours")
        conn.commit()


# ── running ───────────────────────────────────────────────────────────────────

def run_job(owner_email: str, job_id: int) -> dict:
    """Run one schedule now, as its owner, and record what happened."""
    job = _job_row(owner_email, job_id)
    return _execute(dict(job))


def _execute(job: dict) -> dict:
    owner = job["owner_email"]
    spec = connection_spec(int(job["connection_id"]), owner)
    try:
        result = dataset_groups.import_external_postgres(
            spec, job["slug"], job["target_table"], job["source_table"],
            mode=job["mode"], owner_email=owner)
        status, error, rows = "ok", "", int(result.get("rows") or 0)
    except Exception as exc:  # noqa: BLE001 — a failing remote must not kill the tick
        # ⚠️ The message is truncated and the exception TYPE is kept, because a
        # psycopg2 error can quote the server's error text and this string is
        # displayed in the UI. Nothing here ever touches the spec.
        status = "failed"
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        rows = None
        _LOG.warning("scheduled pull of %s failed: %s", job.get("target_table"), type(exc).__name__)
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # ⚠️ `owner_email` in the WHERE, not just in the row we happened to fetch.
            # The invariant guard in tests/test_external_db_sync.py flagged this
            # statement as the one UPDATE in the module that named only `id` — the
            # row came from an owner-checked read, so it was safe in practice, and
            # "safe in practice" is exactly what stops the next reader from trusting
            # it.
            cur.execute(f"UPDATE {JOB_TABLE} SET last_run_at=NOW(), last_status=%s, "
                        f"last_error=%s, last_rows=%s, updated_at=NOW() "
                        f"WHERE id=%s AND owner_email=%s",
                        (status, error, rows, int(job["id"]), owner))
        conn.commit()
    return {"id": int(job["id"]), "status": status, "error": error, "rows": rows}


def due_jobs(now=None) -> list[dict]:
    """Enabled schedules whose period has elapsed.

    ⚠️ `last_run_at IS NULL` counts as due, so a schedule that has never run is not
    stranded — but it is also how a schedule created while the app was down runs
    exactly once rather than once per missed tick.
    """
    ensure_schema()
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT j.*, c.label AS connection_label FROM {JOB_TABLE} j "
                f"LEFT JOIN {CONN_TABLE} c ON c.id = j.connection_id "
                f"WHERE j.enabled AND ("
                f"  j.last_run_at IS NULL AND j.created_at <= NOW() - (j.interval_minutes || ' minutes')::interval "
                f"  OR j.last_run_at <= NOW() - (j.interval_minutes || ' minutes')::interval)")
            return [dict(r) for r in cur.fetchall()]


_ADVISORY_LOCK_KEY = 802197341   # arbitrary but fixed; see run_due_jobs


def run_due_jobs(now=None) -> list[dict]:
    """Every due schedule, one at a time, under a database-level lock.

    ⚠️ The lock is what makes a second replica harmless. Klado runs one uvicorn
    process today, but a pull that ran twice would `TRUNCATE` and rewrite the copy
    while somebody reads it, and "we only run one" is a property of the compose file
    rather than of the code. `pg_try_advisory_lock` returns false instead of waiting,
    so the loser of the race does nothing rather than queueing a second copy.
    """
    results: list[dict] = []
    with db.get_pg_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
            locked = bool(cur.fetchone()[0])
        if not locked:
            return results
        try:
            jobs = due_jobs(now)
            for job in jobs:
                results.append(_execute(job))
        finally:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    return results


def _tick() -> None:
    """What the scheduler actually calls.

    ⚠️ Wrapped whole: `get_pg_conn()` borrows from a pool that RAISES when exhausted
    rather than waiting, and the import path opens its own connections while this one
    is held. An unhandled error here would be raised on every tick forever; the pull
    is a convenience, and a pull that cannot run must stay quiet until it can.
    """
    try:
        run_due_jobs()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("scheduled pull tick failed: %s: %s", type(exc).__name__, str(exc)[:200])


def start_scheduler() -> None:
    """Start the tick. Idempotent — a second call is not a second scheduler."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:  # noqa: BLE001 — no scheduler is a degraded feature, not a crash
        _LOG.warning("APScheduler is unavailable; scheduled pulls will not run on a timer")
        return
    ensure_schema()
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_tick, "interval", minutes=TICK_MINUTES, id="external_db_sync",
                  max_instances=1, coalesce=True, replace_existing=True)
    sched.start()
    _SCHEDULER = sched
    _LOG.info("external database sync scheduler started (tick %s min)", TICK_MINUTES)


def stop_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is None:
        return
    try:
        _SCHEDULER.shutdown(wait=False)
    finally:
        _SCHEDULER = None
