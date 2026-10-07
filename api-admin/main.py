"""The admin console — a second process, a second port, the same installation.

Run it:

    cd api-admin && set -a && . ../.env && set +a && uvicorn main:app --port 8787

or `bash scripts/start_admin.sh`, which does that and then checks the port came up.

## What is deliberately NOT here
--------------------------------
`api/main.py`'s routers. Not one. This process mounts no main-app route, and
`api/tests/test_shared_layer.py` fails the build if any file under `api-admin/` writes
`import api.*` or `from api.…`. The constraint is enforced rather than documented, because
an import that is merely discouraged is an import that eventually happens at 2am.

The reason is not tidiness. Importing `api.services.oss_storage` would drag in the main
app's whole import chain — settings bootstrap, database bootstrap, every service module —
and the console would become the main app wearing a second hat. The two would then have to
be restarted together, share a fate neither asked for, and a crash in either would take
out the other. Two processes means two failure domains, and this boundary is the only
thing that makes it true.

## What IS shared
----------------
`klado_shared/`. The same `SECRET_KEY`, the same PostgreSQL, the same session signing
code, the same organization rules, the same mail templates. Those are not "shared
utilities" — they are *facts about this installation*, and two copies of any of them
would be two answers. Session tokens carry an audience, so a console session is worthless
against `:8000` and an app session is worthless here; see `klado_shared/session.py` for
why that is a difference in the signature rather than a check in a handler.

## The read-only switch
----------------------
`auth.admin_audience_enabled` turns the console read-only. It is enforced in
`core/access.py::require_write` and NOT in `require_operator`, because an operator who
suspects the console has been walked into needs to be able to look at it. That module's
docstring has the full reasoning, and for why exactly one route may bypass the switch.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import _shared_path  # noqa: F401  — side effect: repository root on sys.path

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from klado_shared import deployment, orgs  # noqa: E402
from klado_shared.config import settings  # noqa: E402
from klado_shared.db import connect_main  # noqa: E402
from klado_shared.i18n import pick, request_lang  # noqa: E402
from admin_console import (agent_skill, auth, knowledge, orgs as orgs_router,  # noqa: E402
                          overview, personal, recycle_bin, system)

log = logging.getLogger("klado.admin")

# The console's own static directory. ⚠️ Deliberately NOT the main app's `frontend/out`:
# serving the SPA from here is what makes the console a separate program rather than a
# second tab in the same one, and the main app's 15,600-line `index.html` carries none of
# the console's pages, its routes or its audience.
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

DESCRIPTION = ("Klado operator console — organizations, personal users, module policy, "
               "system mail, the recycle bin and the audit trail.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Bring up the console's half of the schema, then report what it found.

    ⚠️ The main app owns `app_users` and `account_modules`, and this process does not
    create them or try to. What it does create is the `orgs` tables (shared, idempotent:
    `ensure_schema()` is `CREATE ... IF NOT EXISTS` plus two idempotent `ALTER`s) and
    `app_settings`.

    The console is allowed to do that much because on a fresh database somebody has to
    start *something*, and if that is the console it must not die on a table it could
    perfectly well create. The tables it does *not* create give it a clear error at the
    first query instead, which is the truth: a console pointed at a database the main app
    has never touched has no accounts to administer.
    """
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.app_users')")
            app_users_exists = cur.fetchone()[0] is not None
        conn.commit()
    orgs.ensure_schema()
    deployment.ensure_table()
    # Prune approvals whose document is gone. Safe from either process, on every start:
    # the queue is a work list, and a row pointing at a purged document is one nobody can
    # ever act on — it just sits in the count forever.
    pruned = orgs.prune_orphan_approvals()
    # ⚠️ print(), not `log.info`. Uvicorn configures its own loggers and leaves the root
    # one alone, so a `logging.getLogger("klado.admin").info(...)` has no handler above it
    # and is dropped on the floor — which is the same trap `core/logring.py` sets for the
    # main app (see AGENTS.md). The first version of this file used `log.info` and the
    # console started silently, including the case that matters most: no `app_users`
    # table, where every subsequent request is a 401 with no explanation anywhere.
    print(f"Klado admin console: app_users="
          f"{'present' if app_users_exists else 'ABSENT — start the main app first'}, "
          f"pruned {pruned} orphan approval(s), read_only="
          f"{not deployment.console_login_enabled()}", flush=True)
    if not app_users_exists:
        print("WARNING: no app_users table — every console request will be refused until "
              "the main app has created it", flush=True)
    yield


app = FastAPI(
    title="Klado admin console",
    description=DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/admin-console/docs",
    openapi_url="/api/admin-console/openapi.json",
    # ⚠️ No CORS middleware, and that is a decision rather than an omission. The console's
    # frontend and its API are both on this port, so they are same-origin and there is
    # nothing to preflight. Permissive CORS here would hand every site in the operator's
    # browser a credentialed route into every account in the installation.
)


@app.middleware("http")
async def attach_identity(request: Request, call_next):
    """Resolve the caller once, so handlers ask instead of parsing cookies.

    ⚠️ Failure is soft on purpose. `connect_main()` raises when PostgreSQL is unreachable,
    and turning that into a 500 on *every* request — including the login page an operator
    needs in order to fix anything — is the least useful possible answer. A request with
    no resolvable identity simply gets no `current_user`, and
    `core/access.require_operator` replies 401, which is both true and actionable.
    """
    from admin_console import access
    try:
        user = access.current_operator(request)
        if user:
            request.state.current_user = user
    except Exception:  # noqa: BLE001 — the gate re-checks; this is only a shortcut
        log.debug("could not resolve the console identity", exc_info=True)
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """One shape for anything the console did not anticipate.

    ⚠️ The message goes through `pick()`, so a `中文 / English` string from a service layer
    reads in the console's own language, and an *un*translated one is left whole — which is
    the documented fallback rather than a bug (see `klado_shared/i18n.py`).

    The traceback goes to the log and never to the response. A console error page is a
    place an operator will paste into a ticket, and a stack trace there hands internal
    module paths to whoever reads the ticket.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": pick(
        "服务器内部错误，请查看日志 / internal server error; see the log",
        request_lang(request))})


app.include_router(auth.router)
app.include_router(overview.router)
app.include_router(orgs_router.router)
app.include_router(personal.router)
app.include_router(knowledge.router)
app.include_router(agent_skill.router)
app.include_router(recycle_bin.router)
app.include_router(system.router)


if os.path.isdir(FRONTEND_DIR):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="console-ui")
else:  # pragma: no cover — only before P5 writes the SPA
    @app.get("/", include_in_schema=False)
    async def _no_ui():
        return JSONResponse(status_code=503, content={"detail": pick(
            "管理后台页面尚未构建 / the console UI has not been built yet", "en")})
