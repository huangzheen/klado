"""Sharing one dataset with a named colleague.

A dataset is the thing people actually want to hand over — a colleague should be
able to build a dashboard on your numbers without getting a copy of the file you
uploaded. So the grant is on the dataset, and deliberately **not** on the source
file: the grantee can read and query rows, and cannot walk away with the original
upload, its object key, or anything else in your file library.

Read-only by construction. `may_write_dataset` is not a parameter here and no
write path consults this table: a grantee's access is exactly "this table appears
in my list and answers SELECT". That is the whole contract, which is why there is
no revoke-race to reason about — removing the row removes the access.

Visibility therefore has two sources and the SQL says so once, in
`data_center_db._ROW_VISIBLE`, so every list and every named lookup agrees.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from core.db import connect_main

TABLE = "public.dataset_shares"
_schema_ready = False

# Dataset names are identifiers we generate from an upload, but this table is
# also reachable from an HTTP body, so the name is validated before it is ever
# interpolated into SQL.
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# The one address shape a share can be sent to. Rejecting early keeps a typo from
# becoming a share nobody can ever revoke because nobody can find it.
_SAFE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ShareError(RuntimeError):
    """A share could not be created or removed. Carries a bilingual message."""


@contextmanager
def _conn():
    """Own connection lifecycle, like the sibling `account_modules` service.

    Not the Data Center pool: `data_center_db.delete_pg_table` calls back into
    this module to revoke, and borrowing that pool here would nest two borrows
    of the same 8-slot pool on one request for no benefit.
    """
    conn = connect_main()
    try:
        yield conn
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    id            SERIAL PRIMARY KEY,
                    table_name    TEXT NOT NULL,
                    grantee_email TEXT NOT NULL,
                    shared_by     TEXT NOT NULL,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (table_name, grantee_email)
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS dataset_shares_grantee_idx "
                        f"ON {TABLE}(grantee_email)")
        conn.commit()
    _schema_ready = True


def _check_name(table_name: str) -> str:
    name = (table_name or "").strip()
    if not _SAFE_NAME.match(name):
        raise ShareError(
            f"数据集名不合法 / not a valid dataset name: {name!r}")
    return name


def _check_email(email: str) -> str:
    addr = (email or "").strip().lower()
    if not _SAFE_EMAIL.match(addr):
        raise ShareError(f"邮箱地址无效 / invalid email address: {email!r}")
    return addr


def ensure_schema() -> None:
    """Public so the app's startup path can create it before the first request."""
    _ensure_schema()


def share(table_name: str, grantee_email: str, owner_email: str) -> dict:
    """Grant `grantee_email` read access to `table_name`. Idempotent.

    Raises `ShareError` when the dataset does not exist, when the owner is not its
    owner, or when the grantee *is* the owner (which would otherwise leave a row
    that grants nothing and reads like a mistake in the sharing UI).
    """
    name = _check_name(table_name)
    grantee = _check_email(grantee_email)
    owner = (owner_email or "").strip().lower()
    if grantee == owner:
        raise ShareError("不能把数据集分享给自己 / a dataset cannot be shared with its own owner")
    _ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT owner_email FROM public._import_registry WHERE table_name=%s", (name,))
            row = cur.fetchone()
            if not row:
                raise ShareError(f"数据集不存在 / no such dataset: {name}")
            if not owner or row["owner_email"] != owner:
                raise ShareError(
                    f"你不是这个数据集的所有者 / you do not own this dataset: {name}")
            cur.execute(f"""
                INSERT INTO {TABLE}(table_name, grantee_email, shared_by, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (table_name, grantee_email)
                DO UPDATE SET shared_by = EXCLUDED.shared_by, created_at = NOW()
                RETURNING table_name, grantee_email, shared_by, created_at
            """, (name, grantee, owner))
            out = dict(cur.fetchone())
        conn.commit()
    out["created_at"] = out["created_at"].isoformat() if out.get("created_at") else None
    return out


def revoke(table_name: str, grantee_email: str, owner_email: str) -> bool:
    """Remove a grant. True when a row was actually removed."""
    name = _check_name(table_name)
    grantee = _check_email(grantee_email)
    _ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT owner_email FROM public._import_registry WHERE table_name=%s", (name,))
            row = cur.fetchone()
            if not row:
                raise ShareError(f"数据集不存在 / no such dataset: {name}")
            if not owner_email or row[0] != owner_email.strip().lower():
                raise ShareError(
                    f"你不是这个数据集的所有者 / you do not own this dataset: {name}")
            cur.execute(
                f"DELETE FROM {TABLE} WHERE table_name=%s AND grantee_email=%s",
                (name, grantee))
            removed = cur.rowcount
        conn.commit()
    return bool(removed)


def shares_of(table_name: str) -> list[dict]:
    """Everyone `table_name` is shared with — the owner's view of its own data."""
    name = _check_name(table_name)
    _ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""SELECT grantee_email, shared_by, created_at
                            FROM {TABLE} WHERE table_name=%s ORDER BY created_at""", (name,))
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
                rows.append(d)
        return rows


def shared_with_me(email: str) -> list[str]:
    """Dataset names `email` was granted, for the one place that needs a list."""
    return [r["table_name"] for r in shared_with_me_rows(email)]


def shared_with_me_rows(email: str) -> list[dict]:
    """Grants received by `email`, with the dataset's display fields.

    Joined against `_import_registry` so the receiving page can label each entry
    without a second round trip — and so a grant whose dataset has since vanished
    (dropped outside `delete_pg_table`) simply disappears instead of rendering a
    card that 404s.
    """
    addr = (email or "").strip().lower()
    if not addr:
        return []
    _ensure_schema()
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT s.table_name, s.shared_by, s.created_at,
                       r.display_name, r.description, r.row_count
                FROM {TABLE} s
                JOIN public._import_registry r ON r.table_name = s.table_name
                WHERE s.grantee_email = %s AND s.shared_by = r.owner_email
                  AND to_regclass('public.' || r.table_name) IS NOT NULL
                ORDER BY s.created_at
            """, (addr,))
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
                rows.append(d)
        return rows


def revoke_all_for(table_name: str) -> None:
    """Drop every grant on a dataset. Called when the dataset itself goes away.

    Without this a dropped table would leave grants behind, and if a later upload
    happened to reuse the name the previous grantees would silently regain access
    to somebody else's rows.
    """
    name = (table_name or "").strip()
    if not name:
        return
    _ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE table_name=%s", (name,))
        conn.commit()


def revoke_all_for_in_tx(cur, table_names) -> int:
    """The same thing on the CALLER's cursor, committing nothing.

    ⚠️ This exists for the delete-a-dataset path, which drops several tables and
    deletes the container **in one transaction**. `revoke_all_for` opens its own
    connection and commits, which is right for one table and wrong here in both
    directions: run it first and a rolled-back group delete would have already
    revoked the grants of tables that still exist; run it last and a failure in it
    would leave grants behind on tables that no longer do — which is the exact
    hazard this module's docstring is about.

    Returns the number of grants removed, for the caller's return value.
    """
    names = [(n or "").strip() for n in (table_names or [])]
    names = [n for n in names if n]
    if not names:
        return 0
    cur.execute(f"DELETE FROM {TABLE} WHERE table_name = ANY(%s)", (names,))
    return cur.rowcount or 0
