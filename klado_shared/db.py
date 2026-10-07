"""The single psycopg2 connection resolver.

Every caller that builds its own connection must go through `pg_connection_kwargs()`.
Nine call sites used to read `PG_HOST`/`PG_PORT`/`PG_DB`/`PG_USER`/`PG_PASS` on their own
and default the host to `postgres` — the service name of the retired compose stack. That
only ever worked because the cluster injected both spellings; against a `.env` written
from `.env.example` (which documents `POSTGRES_*`) those routes answered 500 with
"could not translate host name postgres".

Shared because the admin console opens its own connections to the same database. It must
resolve the same host, the same password and the same statement timeout as the main app —
a console that silently connected somewhere else would be the worst kind of bug: it
would work in development and read the wrong rows in production.
"""
from __future__ import annotations

import os

import psycopg2

#: The environment variable that caps every statement. Exposed as a module constant
#: because the admin console's long-running reports need the same 10 minutes, and a
#: second hard-coded copy of the number would drift.
STATEMENT_TIMEOUT_ENV = "PG_STATEMENT_TIMEOUT_MS"
_DEFAULT_STATEMENT_TIMEOUT_MS = 600000


def pg_connection_kwargs(default_host: str = "localhost") -> dict:
    """Resolve the main database's five connection fields.

    Precedence is POSTGRES_* > PG_*. The default host is `localhost`, not the old compose
    service name: this project runs on one machine, and a wrong default should fail as
    "nothing is listening" rather than as an unresolvable host. Does not raise — the
    caller decides whether a missing password is fatal.
    """
    def pick(primary: str, secondary: str, default: str) -> str:
        return os.environ.get(primary) or os.environ.get(secondary) or default

    return dict(
        host=pick("POSTGRES_HOST", "PG_HOST", default_host),
        port=int(pick("POSTGRES_PORT", "PG_PORT", "5432")),
        dbname=pick("POSTGRES_DB", "PG_DB", "klado"),
        user=pick("POSTGRES_USER", "PG_USER", "klado"),
        password=os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PG_PASS") or "",
    )


def _pg_kwargs() -> dict:
    kwargs = pg_connection_kwargs()
    if not kwargs["password"]:
        raise RuntimeError(
            "POSTGRES_PASSWORD (or PG_PASS) environment variable is not set"
        )
    kwargs.update(
        # Network resilience: the app -> database path has been observed silently
        # dropping connections mid-COPY (TCP half-open: server stuck in ClientRead for
        # 30+ min while holding table locks). psycopg2 defaults to no timeout, so such a
        # connection freezes its worker forever.
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        # Hard cap per statement. The largest table COPY stays well below this; the
        # point is to convert "hung forever" into a retryable error.
        options=f"-c statement_timeout={os.environ.get(STATEMENT_TIMEOUT_ENV, str(_DEFAULT_STATEMENT_TIMEOUT_MS))}",
    )
    return kwargs


def connect_main() -> "psycopg2.extensions.connection":
    """Return a new psycopg2 connection to the main Klado PostgreSQL database."""
    return psycopg2.connect(**_pg_kwargs())
