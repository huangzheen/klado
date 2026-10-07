"""Per-account module entitlements.

The deployment says which modules exist and are on sale (`KLADO_MODULES`, see
`core/modules.py`); this says which of them *this* account has. Kept apart from
`app_users` deliberately: an account's identity and its subscription are different
things with different lifetimes, and one will eventually come from a billing
system rather than a deployment file.

The rule that keeps the two from fighting: an account row can only **narrow** the
deployment. `core.modules.resolve()` intersects, so a module switched off in the
deployment stays off no matter what any row says — otherwise turning a module off
for an edition would be defeated by a stale entitlement.
"""
from __future__ import annotations

import logging

import psycopg2

from core.db import connect_main

logger = logging.getLogger(__name__)

_TABLE = "public.account_modules"
_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    user_id    INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                    module_key TEXT    NOT NULL,
                    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, module_key)
                )
            """)
            cur.execute(f"CREATE INDEX IF NOT EXISTS account_modules_key_idx "
                        f"ON {_TABLE}(module_key)")
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    _schema_ready = True


def ensure_schema() -> None:
    """Public so startup can create the table before the first request.

    Otherwise a failed `CREATE` would surface as the first person who opens
    Settings getting an error, with a deployment that looks healthy.
    """
    _ensure_schema()


def overrides_for(user_id: int | None) -> dict[str, bool]:
    """`{module_key: enabled}` for one account. Empty when it has no rows.

    An absent account (local single-user mode, `AUTH_ENABLED=0`) returns empty,
    so it simply follows the deployment default.
    """
    if not user_id:
        return {}
    _ensure_schema()
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT module_key, enabled FROM {_TABLE} WHERE user_id = %s",
                        (user_id,))
            return {str(k): bool(v) for k, v in cur.fetchall()}
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_module(user_id: int, module_key: str, enabled: bool) -> None:
    """Grant or withhold one module for one account."""
    _ensure_schema()
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {_TABLE}(user_id, module_key, enabled, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, module_key)
                DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = NOW()
            """, (user_id, module_key, enabled))
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_for(user_id: int) -> None:
    """Drop every override, returning the account to the deployment default."""
    _ensure_schema()
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE user_id = %s", (user_id,))
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_one(user_id: int, module_key: str) -> None:
    """Drop ONE override, returning that single module to the deployment default.

    ⚠️ Distinct from `clear_for`, and the distinction is load-bearing. "Send this module
    back to following the deployment" and "send this account back to following the
    deployment" are different acts: the first leaves the account's other narrowings
    alone, the second discards decisions somebody made on purpose. A `DELETE` route
    that reads `…/modules/{key}` and calls `clear_for` silently does the second while
    its own docstring promises the first — which is how a person loses four module
    settings by clicking "reset" on the fifth.

    A key that has no row is a no-op, and that is the success case: the caller asked for
    "nobody has said anything about this module", and that is now true.
    """
    _ensure_schema()
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE user_id = %s AND module_key = %s",
                        (user_id, module_key))
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_all() -> list[dict]:
    """Every override in the deployment — the admin view.

    Accounts with no row are absent on purpose: the overwhelming majority of
    accounts follow the deployment default, and listing them would say nothing.
    """
    _ensure_schema()
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT am.user_id, u.email, am.module_key, am.enabled, am.updated_at
                FROM {_TABLE} am
                JOIN app_users u ON u.id = am.user_id
                ORDER BY u.email, am.module_key
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def entitled(user: dict | None) -> tuple[str, ...]:
    """The modules one account may use, narrowed by its company's licence.

    ⚠️ This is the ONE place the two narrowings are applied together, and it exists
    because they were being applied in four places and would drift. `resolve()` knows
    about the deployment and the per-account rows; it cannot know which company the
    caller is in without a database lookup, so the licence is passed IN. Doing that
    lookup here — once, next to the `account_modules` read that was already on this path
    — is what keeps the middleware gate, `/api/health` and `/api/auth/me` from
    answering three different questions.

    ⚠️ Both reads fail SOFT, and that is a deliberate asymmetry worth naming. An
    unreadable `account_modules` table must not lock everybody out of a deployment that
    is otherwise healthy, so the override read falls back to "no opinion". The licence
    read fails the same way, and for the same reason — but note what it means: during a
    database blip a company is briefly **less** restricted, not more. The write path
    (`/api/org/members/{id}/modules/{key}`) is where the ceiling is enforced as a hard
    refusal, so a blip can never be *used* to grant something; it can only delay a
    withdrawal that nobody is asking for at that moment.
    """
    from core import modules as core_modules
    from klado_shared import orgs

    user = user or {}
    overrides = {}
    if user.get("id"):
        try:
            overrides = overrides_for(user["id"])
        except Exception:  # noqa: BLE001 — a missing table must not lock anybody out
            logger.warning("account_modules unreadable; falling back to the deployment "
                           "default", exc_info=True)
    licence = None
    if user.get("email"):
        try:
            licence = orgs.module_licence_of(user["email"])
        except Exception:  # noqa: BLE001 — see the note above
            logger.warning("organization module licence unreadable; falling back to the "
                           "deployment default", exc_info=True)
    return core_modules.resolve(overrides, licence=licence)

