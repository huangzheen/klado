"""
Klado API — FastAPI 主入口
产品营销中心后端服务
"""
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import asyncio
import base64
import traceback
import httpx
import os
import re
import subprocess
import sys

from core.access import (ADMIN_REQUIRED_DETAIL, is_admin_identity, may_write_app_setting,
                          requires_admin)
from core import i18n as core_i18n
from core import modules as core_modules
from core.config import settings, resolve_frontend_dir
from core import logring
from core.third_auth import AuthError, access_token_from_request, authenticate_token

# 尽早安装环形日志缓冲（先于 DB bootstrap / 路由注册，捕获全部启动阶段日志）
logring.install()
logger = logging.getLogger("klado.main")

from core.bootstrap import ensure_database_initialized
from core.database import init_db
from services import auth_store
from services.ai.knowledge_store import KnowledgeStoreError, ensure_schema as ensure_knowledge_store
from services.ai.knowledge_docs import startup_apply
from routers import auth, knowledge, data_center, storage, export
from routers import town as town_router
from routers import org_admin as org_admin_router
from routers import reports as reports_router
from routers import settings as settings_router
from routers import business_knowledge as business_knowledge_router
from routers import calendar as calendar_router
from routers import annotations as annotations_router
from routers import inbox as inbox_router
from routers import dashboard as dashboard_router
from routers import rail as rail_router


_pdf_proc: subprocess.Popen | None = None


def _normalize_app_base_path(path: str) -> str:
    path = (path or "").strip()
    if not path or path == "/":
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/")


def _log_db_startup_diagnostics():
    """
    启动时打印数据库诊断信息到控制台，方便在服务日志里排查启动失败。
    输出三块：
      1. 容器内实际注入的 env vars（原始值；密码只显示长度）
      2. 应用实际使用的"解析后"主库值（按 connect_main 的优先级链）
      3. TCP 可达性快速测试（不验证凭据，只测网络）
    """
    import os
    import socket

    def _first_set(names, default):
        """返回 (value, source_name) —— 模拟 _pg_kwargs 的优先级链 + 默认值"""
        for n in names:
            v = os.environ.get(n)
            if v is not None and v != "":
                return v, n
        return default, (names[0] if names else "?") + " [default]"

    def _console(message=""):
        print(message, flush=True)

    BANNER = "=" * 60
    _console(BANNER)
    _console("Klado DB startup diagnostics")
    _console(BANNER)

    _console("[1] env vars as injected into container")
    env_keys = (
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
        "PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASS",
    )
    for k in env_keys:
        v = os.environ.get(k)
        if v is None:
            _console(f"    {k:<32}  (not set)")
        elif any(t in k for t in ("PASSWORD", "PASS", "SECRET")):
            _console(f"    {k:<32}  <set, len={len(v)}>")
        else:
            _console(f"    {k:<32}  = {v!r}")

    _console()
    _console("[2] resolved values (what app will actually use)")
    main_pw,   _ = _first_set(["POSTGRES_PASSWORD", "PG_PASS"],                default=None)
    # Same default as core/db.pg_connection_kwargs(): this runs on one machine, so a
    # missing host should read as "nothing is listening here", not as an old service name.
    main_host, _ = _first_set(["POSTGRES_HOST",      "PG_HOST"],                default="localhost")
    main_port, _ = _first_set(["POSTGRES_PORT",      "PG_PORT"],                default="5432")
    main_db,   _ = _first_set(["POSTGRES_DB",        "PG_DB"],                  default="klado")
    main_user, _ = _first_set(["POSTGRES_USER",      "PG_USER"],                default="klado")
    _console(f"    main     host={main_host}  port={main_port}  dbname={main_db}  user={main_user}")
    _console(f"    main     password={'<set>' if main_pw else '<MISSING -> connect_main() will raise>'}")
    if not main_host:
        _console("    main     host   <MISSING -> connect_main() will fail>")

    _console()
    _console("[3] tcp reachability (no auth, just network)")
    if not main_pw:
        _console(f"    main     skipped (no password set)")
    elif not main_host:
        _console(f"    main     skipped (no host)")
    else:
        try:
            port_int = int(main_port) if main_port else 5432
            with socket.create_connection((main_host, port_int), timeout=3):
                _console(f"    main     {main_host}:{port_int:<5}  OK (tcp reachable)")
        except Exception as e:
            _console(f"    main     {main_host}:{main_port}  FAIL  ->  {type(e).__name__}: {e}")

    _console(BANNER)
    _console("Klado DB startup diagnostics end")
    _console(BANNER)


def _bootstrap_first_admin() -> None:
    """
    Seed the first admin account from configuration.

    Why this exists: the first account cannot be created by registering, because
    registering needs a mailed code — and a fresh install with no mail server
    configured has no way to read that code. So the account is seeded at startup
    from `AUTH_BOOTSTRAP_ADMIN_EMAIL` + `AUTH_BOOTSTRAP_ADMIN_HASH`, with a
    **pre-computed hash** rather than a plaintext password, so a repository reader
    still cannot log in.

    Idempotent and defensive: runs only when the config holds a plausible hash, and
    never overwrites an existing account.
    """
    email = (settings.AUTH_BOOTSTRAP_ADMIN_EMAIL or "").strip().lower()
    stored = (settings.AUTH_BOOTSTRAP_ADMIN_HASH or "").strip()
    if not (email and stored):
        return
    # A stray `${...}` placeholder (a variable that was never filled in) must not
    # become a broken account.
    if not stored.startswith("pbkdf2_sha256$") or stored.count("$") != 3:
        print(f"WARNING: AUTH_BOOTSTRAP_ADMIN_HASH is not a pbkdf2_sha256 hash; "
              f"skipping the admin bootstrap for {email}", flush=True)
        return
    try:
        from services import auth_store
        made = auth_store.create_user_with_hash(
            email, stored, display_name=email.split("@")[0], role="admin")
        print(f"first-admin bootstrap: {email} -> "
              f"{'created' if made else 'already existed (password left untouched)'}", flush=True)
    except Exception as exc:  # noqa: BLE001 — must not stop the app from starting
        print(f"WARNING: first-admin bootstrap failed for {email}: {exc}", flush=True)
        return
    # ⚠️ The seeded admin is the one account that never passes through `register/complete`,
    # so it never got the agent code registration mints for everybody else. Mint it here, at
    # creation, rather than leaving the page to offer a button for it — a code is not
    # something an operator should have to remember to ask for on the account that holds the
    # Agent Skill. Idempotent, and `_backfill_agent_codes` below is the belt to this braces.
    try:
        from services import auth_store as _auth
        user = _auth.get_user_by_email(email)
        if user and _auth.ensure_initial_agent_code(
                user["id"], "bootstrap", created_by=email):
            print(f"first-admin bootstrap: agent code issued for {email}", flush=True)
    except Exception as exc:  # noqa: BLE001 — same rule: never block startup
        print(f"WARNING: could not issue the first-admin agent code for {email}: {exc}",
              flush=True)


def _backfill_agent_codes() -> None:
    """
    Re-establish "every active account has an agent code" on startup.

    Cheap, idempotent, and the reason the accounts page can simply *show* a code instead of
    offering an action to create one: any account created before the invariant existed, or
    by a path that does not mint one (an invitation, a seeded admin), is repaired on the next
    start rather than waiting for somebody to notice a missing value in a table.

    ⚠️ Runs in its own try/except and after `_bootstrap_first_admin`, and neither failure
    stops the app: an account without a code is a missing convenience, not a broken install.
    """
    try:
        from services import auth_store
        issued = auth_store.backfill_missing_agent_codes()
        if issued:
            print(f"agent codes: issued {issued} for account(s) that had none", flush=True)
    except Exception as exc:  # noqa: BLE001 — must not stop the app from starting
        print(f"WARNING: the agent-code backfill did not complete: {exc}", flush=True)


async def lifespan(app: FastAPI):
    """应用启动 / 关闭生命周期"""
    global _pdf_proc
    _log_db_startup_diagnostics()
    try:
        from services import oss_storage
        await asyncio.to_thread(oss_storage.log_startup_diagnostics)
    except Exception:
        # OSS diagnostics are intentionally non-fatal; detailed causes are
        # emitted by services.oss_storage into the diagnostic log ring.
        print("WARNING: Klado OSS startup diagnostics could not run", flush=True)
    await init_db()
    try:
        ensure_database_initialized()
    except Exception:
        print("WARNING: Klado DB bootstrap failed; continuing application startup", flush=True)
        traceback.print_exc()
        if settings.DB_BOOTSTRAP_STRICT:
            raise
    # First admin, seeded from configuration (see the function's docstring for why).
    await asyncio.to_thread(_bootstrap_first_admin)
    await asyncio.to_thread(_backfill_agent_codes)
    try:
        # The AI knowledge store intentionally uses the same connect_main()
        # connection as the Yunxiao-injected business database.
        # There is exactly ONE knowledge store. The second, Custom-GPT-only copy
        # (`public.chatgpt_knowledge_documents`) was retired on 2026-09-23: it was
        # seeded once and never updated, so it had silently drifted into a stale
        # 25-document duplicate of the live store while every published contract
        # pointed at /api/ai/knowledge/*.
        ensure_knowledge_store()
        # The files under api/knowledge_docs/ are the single source of truth; the
        # database is a derived copy reconciled here. Hash-skipped, so a normal
        # restart writes nothing. This is the ONLY writer of knowledge documents —
        # the HTTP write routes were removed, which is what makes read access safe
        # to share.
        print(f"Klado knowledge docs: {startup_apply()}", flush=True)
    except KnowledgeStoreError:
        print("WARNING: Klado knowledge-store bootstrap failed; continuing application startup", flush=True)
        traceback.print_exc()
        if settings.DB_BOOTSTRAP_STRICT:
            raise
    # 业务知识库（wiki）：建表。刻意在这里做而不是等首次请求 —— 否则建表失败
    # 会变成"第一个打开 wiki 的人拿到 503"，而部署本身看着是绿的。幂等，无数据写入。
    try:
        from services.ai.business_knowledge import ensure_tables as ensure_business_knowledge
        from services.ai.calendar_events import ensure_tables as ensure_calendar_events
        await asyncio.to_thread(ensure_business_knowledge)
        await asyncio.to_thread(ensure_calendar_events)
        print("Klado business knowledge: tables ensured", flush=True)
    except Exception:
        print("WARNING: Klado business-knowledge bootstrap failed; the page will retry on first request",
              flush=True)
        traceback.print_exc()
    # 模块授权与数据集分享：同样在启动时建表。这两张表被中间件（每一个请求都读
    # account_modules）和 Data Center 的分享 UI 依赖，留在"首次调用再建"的话，
    # 建表失败会变成随机用户的一个 403 或 500，而部署日志是绿的。
    try:
        from services import account_modules
        from services import dataset_shares
        from services import dashboard_store, file_shares, data_center_db
        from services import dataset_groups
        await asyncio.to_thread(data_center_db.init_file_library)
        await asyncio.to_thread(file_shares.ensure_schema)
        await asyncio.to_thread(account_modules.ensure_schema)
        await asyncio.to_thread(dataset_shares.ensure_schema)
        await asyncio.to_thread(dataset_groups.ensure_schema)
        await asyncio.to_thread(dashboard_store.ensure_schema)
        print("Klado modules, dataset shares & dashboards: tables ensured", flush=True)
    except Exception:
        print("WARNING: Klado module/share/dashboard bootstrap failed; requests will retry on demand",
              flush=True)
        traceback.print_exc()
    # Closed accounts are erased once their grace period runs out. On a single-host
    # install the only trigger is this startup plus the admin button, which is
    # correct: the deadline is a promise to a human ("30 days"), not a precision
    # timer, and an account nobody restarts the server for keeps its 30 days.
    try:
        from services import account_lifecycle
        auth_store._ensure_schema()
        swept = await asyncio.to_thread(account_lifecycle.sweep)
        if swept:
            logger.info("recycle bin sweep on startup: %s", swept)
        print(f"Recycle bin: {len(swept)} account(s) purged", flush=True)
    except Exception:
        # A purge failure must never stop the app from starting — the accounts stay
        # in the bin and the next sweep retries them.
        print("WARNING: recycle-bin sweep failed; accounts remain pending", flush=True)
        traceback.print_exc()
    # Organizations (`klado_shared/orgs.py`). Placed AFTER the block above on purpose:
    # `org_members.user_id` is a foreign key onto `app_users`, and that table is created
    # by `auth_store._ensure_schema()`. Running this first would fail on a fresh install
    # with "relation app_users does not exist".
    try:
        from klado_shared import orgs as shared_orgs
        # ⚠️ Hook, not import: `account_modules` writes a table this process creates,
        # and the shared layer is not allowed to import the main app. Wiring it here
        # means `/api/org/members/{id}/modules/*` works in this process and reports a
        # clear error in a console started against a database the main app never
        # initialised — which is the truth, rather than an ImportError at boot.
        from services import account_modules as _am
        # ⚠️ The three hooks are NOT interchangeable, and the middle one is the trap:
        # the shared layer calls the "clear" hook as `clear(user_id, module_key)`,
        # which is `clear_one`. Binding `clear_for` (one argument, drops the whole row)
        # raises TypeError at the first click, so "send this module back to following
        # the deployment" was a 500 in every build that had this line.
        shared_orgs.bind_account_modules(_am.set_module, _am.clear_one, _am.clear_for)
        await asyncio.to_thread(shared_orgs.ensure_schema)
        backfilled = await asyncio.to_thread(shared_orgs.backfill_personal_orgs)
        print(f"Klado organizations: tables ensured, backfilled {backfilled}", flush=True)
    except Exception:
        # ⚠️ Non-fatal, but NOT quietly so: without these tables the share gate has
        # nothing to read and every share falls back to the permissive default. That is
        # the correct failure direction (an installation that just upgraded should keep
        # working), which is exactly why it needs a line in the log.
        print("WARNING: Klado organization bootstrap failed; sharing stays unrestricted "
              "until it succeeds", flush=True)
        traceback.print_exc()
    # Start embedded pdf-service when running in merged mode (no external container)
    pdf_script = os.path.join(os.path.dirname(__file__), "pdf_service.py")
    if os.getenv("PDF_SERVICE_URL", "") == "" and os.path.isfile(pdf_script):
        _pdf_proc = subprocess.Popen([sys.executable, pdf_script],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Saved external-database connections and the timer that refreshes their local
    # copies. Started here, not on first request, for the same reason as the tables
    # above: a pull that cannot run must be visible in the log at boot rather than
    # discovered later as a copy that silently stopped updating.
    try:
        from services import external_sync
        await asyncio.to_thread(external_sync.ensure_schema)
        external_sync.start_scheduler()
        print("Klado external database sync: tables ensured, scheduler started", flush=True)
    except Exception:
        print("WARNING: Klado external database sync bootstrap failed; scheduled pulls "
              "will not run (one-off import still works)", flush=True)
        traceback.print_exc()
    yield
    # ⚠️ Stopped BEFORE the pdf process and not in a finally: a scheduler left running
    # past shutdown keeps a thread and an open pool connection alive, and APScheduler
    # has no join here, so the app would exit with a live background thread.
    try:
        from services import external_sync
        external_sync.stop_scheduler()
    except Exception:
        pass
    if _pdf_proc and _pdf_proc.poll() is None:
        _pdf_proc.terminate()


_APP_BASE_PATH = _normalize_app_base_path(settings.APP_BASE_PATH)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="产品营销中心 · 全业务智能运营平台",
    lifespan=lifespan,
    root_path=_APP_BASE_PATH,
)

# ─── Middleware ───────────────────────────────────────────────────
if _APP_BASE_PATH:
    @app.middleware("http")
    async def strip_app_base_path(request: Request, call_next):
        path = request.scope.get("path", "")
        if path == _APP_BASE_PATH:
            # A document URL without the trailing slash resolves relative
            # resources (for example vendor/bootstrap.min.css) from the host
            # root instead of from the mounted application directory.
            if request.method in {"GET", "HEAD"}:
                query = request.scope.get("query_string", b"").decode("latin-1")
                target = _APP_BASE_PATH + "/"
                if query:
                    target += "?" + query
                return RedirectResponse(url=target, status_code=307)
            request.scope["path"] = "/"
            request.scope["root_path"] = _APP_BASE_PATH
        elif path.startswith(_APP_BASE_PATH + "/"):
            request.scope["path"] = path[len(_APP_BASE_PATH):] or "/"
            request.scope["root_path"] = _APP_BASE_PATH
        return await call_next(request)

app.add_middleware(GZipMiddleware, minimum_size=1024)


def _request_path(request: Request) -> str:
    path = request.scope.get("path", "")
    if _APP_BASE_PATH and (path == _APP_BASE_PATH or path.startswith(_APP_BASE_PATH + "/")):
        path = path[len(_APP_BASE_PATH):] or "/"
    return path


_AUTH_PUBLIC_PATHS = {
    "/api/health",
    "/openapi.json", "/docs", "/redoc",
    # Reachable before anyone is signed in — otherwise nobody could ever register
    # or log in. Everything else under /api/ requires an identity when AUTH_ENABLED.
    "/api/auth/health",
    "/api/auth/register/start",
    "/api/auth/register/complete",
    "/api/auth/login",
}

def _is_public_path(path: str) -> bool:
    """True when the request path skips authentication entirely."""
    return path in _AUTH_PUBLIC_PATHS

# ── What a machine caller MAY WRITE ─────────────────────────────────────────
# A request carrying `Authorization: Basic` or a `Bearer` token is reported as
# kind='agent' (see _local_identity). A browser logs in with the session cookie
# and its fetch bridge only attaches a Bearer token for the local-preview bridge
# or the platform's iframe integration — so this list does NOT affect humans.
#
# Reads are unrestricted on purpose: an agent's job is "read the knowledge base,
# publish a report", and the local preview is also an 'agent' caller that must
# keep reading the business endpoints. What we take away is the write surface —
# without this, ANY signed-in identity could call e.g.
# `DELETE /api/data-center/datasets/{table}` (drop a table).
#
# Workspace cards belong to the agent's account (publish / update / delete);
# business knowledge pages likewise (the owner's wiki is edited by the owner's
# agent); login/logout let it obtain and drop a token. Everything else is denied —
# in particular "publish to public" and "pull a copy" stay browser-only inside the
# router, so this prefix grants only the owner's own pages.
_AGENT_WRITE_ALLOWED_PREFIXES = ("/api/reports", "/api/knowledge/items",
                                 # Images for a knowledge page. Same trust story as the
                                 # page itself: the agent already writes this account's
                                 # knowledge, and a page with a picture needs one.
                                 "/api/knowledge/assets",
                                 # Calendar events. An agent creating an event from Feishu
                                 # has to be able to name the colleagues who are on it —
                                 # that is the point of the @-mention. Publishing to the
                                 # public area stays browser-only (see routers/calendar.py).
                                 "/api/calendar/events",
                                 # Dashboard pages. Same trust story as a report: the
                                 # agent already writes this account's Workspace, and a
                                 # dashboard is an HTML document it authored. Without
                                 # this the module exists but the agent that is supposed
                                 # to fill it can only read it — the whole point of the
                                 # module. Sharing stays owner-side: a grantee is a
                                 # person, and `routers/dashboard.py` answers 404 for
                                 # anything the caller does not own.
                                 "/api/dashboard",
                                 "/api/auth/login", "/api/auth/logout")
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Endpoints that take only a read action but happen to use POST. A machine caller may use
# them (they cannot mutate), so they are exempt from the write rule.
#
# ⚠️ Kept as a hand-maintained list, and that is the hazard: the SPA no longer holds
# a mirror of it (the `localPreviewReadPostRoutes` array the old comment pointed at is
# gone), so this file is now the ONLY place that answers "may this POST be treated as
# a read?". A second read-only POST path added to a new module will 403 for an agent
# until it is listed here — which reads like a permission bug, not a missing entry.
# `tests/verify_agent_dashboard_http.py` pins the whole agent surface for the module.
# Each pattern below was checked for write operations (INSERT/UPDATE/DELETE/commit):
# `/api/export/normalise` transforms an uploaded workbook in memory and touches no table;
# `/api/data-center/query` and `/api/dashboard/query` are the same read-only SELECT
# gateway reached through two module paths — the second is NOT covered by the first
# pattern, and a read-only POST blocked by a write rule is a 403 that reads like a
# permission problem rather than a missing allow-list entry.
_AGENT_POST_READONLY = tuple(re.compile(p) for p in (
    r"^/api/data-center/query/?$",
    r"^/api/dashboard/query/?$",
    r"^/api/export/(?:xlsx|normalise|pdf)/?$",
))


def _agent_may_write(path: str) -> bool:
    """True when a machine (agent) caller may use a non-read method on `path`."""
    # Both routes are strictly caller-owned; neither accepts another account id.
    if path.rstrip("/") in {"/api/auth/office-work", "/api/auth/me/avatar",
                            "/api/auth/agent-heartbeat", "/api/auth/agent-messages"}:
        return True
    if re.fullmatch(r"/api/auth/(?:agent-messages|office-agents)/[0-9]+/?",path):
        return True
    if path.startswith(_AGENT_WRITE_ALLOWED_PREFIXES):
        return True
    return any(rx.match(path or "") for rx in _AGENT_POST_READONLY)


def _local_mode() -> bool:
    """Platform SSO unset → authenticate against the local user store instead."""
    return not (settings.AUTH_USER_INFO_URL and settings.AUTH_TOOL_CODE)


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    """`Authorization: Basic base64(email:password)` — how agents authenticate."""
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — malformed header = no credentials
        return None
    email, _, password = decoded.partition(":")
    return (email.strip().lower(), password)


AGENT_TOKEN_HEADER = "X-Klado-Agent-Token"


def _presented_agent_token(request: Request) -> str:
    """
    Read an agent access code (授权码).

    ⚠️ Deliberately NOT read from cookies: a code must never be usable as a browser
    session, which is the whole reason it exists next to Basic/Bearer. Accepting the
    dedicated header as well keeps it easy to tell apart from a session token in logs.
    """
    dedicated = (request.headers.get(AGENT_TOKEN_HEADER) or "").strip()
    if dedicated.startswith(auth_store.AGENT_TOKEN_PREFIX):
        return dedicated
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        value = value.strip()
        if value.startswith(auth_store.AGENT_TOKEN_PREFIX):
            return value
    return ""


def _local_identity(request: Request) -> tuple[dict | None, str]:
    """
    Resolve the caller from local credentials. Returns (user, kind).

    kind is 'agent' for machine presentations (Basic, or a Bearer session token)
    and 'browser' for the cookie — the admin page reports the two separately, and
    that is the whole reason the distinction is tracked.
    """
    presented = _presented_agent_token(request)
    if presented:
        return (auth_store.resolve_agent_token(presented), "agent")
    basic = _basic_credentials(request)
    if basic:
        user, _reason = auth_store.authenticate(*basic)
        return (user, "agent")
    # Our own session cookie first: access_token_from_request() only knows the
    # PLATFORM cookie (aihub-${AUTH_TOOL_CODE}), which does not exist in local mode —
    # reading only that made every browser request 401.
    cookie_token = request.cookies.get(auth_router.SESSION_COOKIE)
    token = cookie_token or access_token_from_request(request)
    if token:
        # ⚠️ `allow_legacy=True` is REQUIRED here, not a nicety. Sessions minted before
        # the audience tag was added carry an untagged signature, and the default is
        # False — so omitting this logs out every signed-in browser on the deployment
        # that introduced the admin console. Only the app audience is permitted to
        # accept them (see `klado_shared.session.LEGACY_TOLERANT_AUDIENCES`); the
        # console passes nothing and so refuses an untagged token by construction.
        # Retire this flag once real sessions have cycled through their 30-day TTL.
        user_id = auth_store.session_user_id(token, allow_legacy=True)
        if user_id:
            user = auth_store.get_user_by_id(user_id)
            # ⚠️ `deleted_at` must be checked here, not just in `authenticate()`.
            # Closing an account does not clear its cookies: a session issued before
            # the close is still a valid signature, so without this check the account
            # stays fully usable until the cookie happens to expire — which is the
            # one thing "closed" is supposed to mean.
            if user and not user.get("disabled") and not user.get("deleted_at"):
                return (user, "browser" if cookie_token else "agent")
    return (None, "")


# Paths that are served **to a signed-in viewer** but are not under /api/: the standalone
# documents a viewer loads in an iframe. The middleware is what puts the caller on
# `request.state`, and these handlers call `_identity()` — so a path missing from here
# answers **401 to a browser that IS signed in**, because an iframe cannot send an
# Authorization header and has only the cookie to offer.
#
# ⚠️ `/e/` (the calendar event page) was missing, and that is exactly what happened:
# the event overlay rendered this 401 JSON in place of the event. All three prefixes are
# required; adding a standalone document means adding it here — `/d/` (the dashboard
# reader) would fail the same way, a signed-in browser getting a 401 because an iframe
# can only offer the cookie.
_SESSION_DOCUMENT_PREFIXES = ("/r/", "/e/", "/d/")


def needs_identity(path: str) -> bool:
    """Must this path resolve a caller before its handler runs?

    ⚠️ `/s/{token}` is absent **on purpose**: the token in it *is* the credential (the only
    login-free entry point in the app). `test_report_state.py` pins both directions.
    """
    return path.startswith("/api/") or path.startswith(_SESSION_DOCUMENT_PREFIXES)


# ── module gate ──────────────────────────────────────────────────────────────
# One choke point for "this account does not have that module". It sits next to
# the admin check on purpose: both are policy about *who*, decided before any
# handler runs, so a route cannot forget to ask.
#
# A path that no module claims is refused rather than allowed. That is the whole
# point of `core.modules`: a new route must declare the module it belongs to, and
# a route added without doing so fails closed instead of quietly opening a hole.
def _entitled_keys(user: dict | None) -> set[str]:
    # ⚠️ One call, not "read the table here and intersect the company licence here".
    # `account_modules.entitled()` is the single place both narrowings are applied, and
    # this function used to do only the first — so the middleware gate and the payload
    # `/api/health` hands the SPA would disagree about a company whose operator licence
    # is narrower than the deployment, and the nav would draw a tab whose every request
    # 403s.
    from services import account_modules
    return set(account_modules.entitled(user))


def _module_gate_response(path: str, user: dict, lang: str | None) -> JSONResponse | None:
    """403 when the caller lacks the module that owns `path`; None when fine."""
    module = core_modules.module_for_path(path)
    if module is None:
        if not path.startswith("/api/") and not path.startswith(_SESSION_DOCUMENT_PREFIXES):
            return None                              # shell, assets, public pages
        return JSONResponse({
            "detail": "该接口未声明所属模块，已按无权限处理 / this endpoint declares no "
                      "module and is treated as not permitted — it is almost certainly a "
                      "route added without registering it in core/modules.py",
            "unclaimed_path": path,
        }, status_code=403)
    if module.key in _entitled_keys(user):
        return None
    return JSONResponse({
        "detail": core_i18n.pick(
            f"你的账号没有「{module.label_zh} / {module.label_en}」模块 / "
            f"your account does not include the {module.label_en} module",
            lang),
        "module": module.key,
    }, status_code=403)


async def _attach_identity_if_possible(request: Request) -> None:
    """Best-effort identity for a public path. Never raises, never 401s.

    Used only where the answer has to be correct *for whoever is asking*: a public
    path that also carries per-account state. Anything that goes wrong leaves
    `current_user` unset, and the handler falls back to the deployment-wide answer,
    which is exactly the right answer for "nobody".
    """
    if getattr(request.state, "current_user", None):
        return
    try:
        if _local_mode():
            local_user, kind = _local_identity(request)
            if local_user:
                request.state.current_user = local_user
                request.state.auth_kind = kind
            return
        token = access_token_from_request(request)
        if token:
            request.state.current_user = await authenticate_token(token)
    except Exception:
        # A public endpoint must stay public: an expired cookie or an unreachable
        # identity provider is not a reason to turn /api/health into a 500.
        request.state.current_user = None


@app.middleware("http")
async def unified_authentication(request: Request, call_next):
    """Require upstream authentication for human-facing API routes when enabled."""
    # Resolve the caller's language once, for every handler and error path:
    # browsers always send Accept-Language, so people get one language; API
    # clients that send none keep the historical bilingual messages.
    request.state.lang = core_i18n.parse_accept_language(
        request.headers.get("accept-language", ""))
    path = _request_path(request)
    if (not settings.AUTH_ENABLED or request.method == "OPTIONS"
            or not needs_identity(path)):
        return await call_next(request)
    # Human-facing API routes: resolve the caller before the handler runs.
    if _is_public_path(path):
        # Public means "reachable without an identity", not "identity-blind".
        # `/api/health` is the one public path that reports account-scoped state (the
        # caller's module entitlements, which the SPA needs before it can draw a tab).
        # Short-circuiting without resolving meant `request.state.current_user` was
        # never set, so the handler silently served the *deployment* default to every
        # signed-in caller — a per-account module switch never reached the browser.
        # Best effort: a caller with no usable credential still gets its 200, just
        # with the deployment default, which is the honest answer for "nobody".
        await _attach_identity_if_possible(request)
        return await call_next(request)
    if _local_mode():
        local_user, kind = _local_identity(request)
        if not local_user:
            message = core_i18n.pick(
                "未登录 —— 请带会话 cookie、Bearer token，或 Authorization: Basic "
                "<base64(email:password)> / not authenticated — send the session cookie, "
                "a Bearer token, or Authorization: Basic <base64(email:password)>",
                request.state.lang,
            )
            return JSONResponse({"detail": message}, status_code=401)
        request.state.current_user = local_user
        request.state.auth_kind = kind
        gated = _module_gate_response(path, local_user, request.state.lang)
        if gated is not None:
            auth_store.log_access(local_user, kind, request.method, path, 403,
                                  (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                                   or (request.client.host if request.client else "")),
                                  request.headers.get("user-agent", ""))
            return gated
        if requires_admin(path, request.method) and not is_admin_identity(local_user):
            # An operator-only request. Enforced here (not per route) so that a route
            # added later cannot quietly reopen the hole this closed: previously any
            # signed-in identity could drop a table or replace the database.
            auth_store.log_access(local_user, kind, request.method, path, 403,
                                  (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                                   or (request.client.host if request.client else "")),
                                  request.headers.get("user-agent", ""))
            if path.startswith("/api/settings/") and not path.startswith("/api/settings/app/"):
                detail = (ADMIN_REQUIRED_DETAIL)
            elif path.startswith("/api/settings/app/"):
                detail = ("该配置项只对管理员开放 / this setting is operator-only — "
                          "只有形如 `<page>_settings` 的界面偏好可以由普通用户写入。")
            else:
                detail = (ADMIN_REQUIRED_DETAIL)
            return JSONResponse({"detail": detail, "requires_admin": True}, status_code=403)
        if kind == "agent" and request.method not in _READ_METHODS and not _agent_may_write(path):
            auth_store.log_access(local_user, kind, request.method, path, 403,
                                  (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                                   or (request.client.host if request.client else "")),
                                  request.headers.get("user-agent", ""))
            return JSONResponse(
                {"detail": "agent credentials are read-only except for Workspace items — "
                           f"{request.method} {path} is not allowed. Read the knowledge base "
                           "or publish with POST /api/reports.",
                 "allowed_writes": list(_AGENT_WRITE_ALLOWED_PREFIXES)},
                status_code=403,
            )
        response = await call_next(request)
        # Recorded after the fact so the status code is the real one. Aggregated per
        # (user, kind, path, minute) so the SPA's request volume cannot bloat the table.
        auth_store.log_access(local_user, kind or "browser", request.method, path, response.status_code,
                              (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                               or (request.client.host if request.client else "")),
                              request.headers.get("user-agent", ""))
        return response

    # The host platform supplies aihub-${AUTH_TOOL_CODE} as a same-site Cookie.
    token = access_token_from_request(request)
    if not token:
        return JSONResponse({"detail": "Missing authentication cookie or Bearer access token"}, status_code=401)
    try:
        request.state.current_user = await authenticate_token(token)
    except AuthError as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    gated = _module_gate_response(path, request.state.current_user, request.state.lang)
    if gated is not None:
        return gated
    if requires_admin(path, request.method) and not is_admin_identity(request.state.current_user):
        # Same policy in the platform-SSO branch (which has no `role`, so
        # is_admin_identity falls back to AUTH_ADMIN_EMAILS there).
        return JSONResponse({"detail": ADMIN_REQUIRED_DETAIL, "requires_admin": True}, status_code=403)
    return await call_next(request)

# ⚠️ 顺序很重要，别往上挪：`add_middleware` 是 insert(0)，**后注册的在外层**。
# CORS 必须在 `unified_authentication`（上面那个 @app.middleware）**之后**注册，
# 否则鉴权中间件会先返回 401/403，而那些响应不经过 CORSMiddleware、不带 CORS 头 ——
# 跨域调用方（本地预览、iframe 集成）只会看到 "blocked by CORS policy"，
# 真正的"未登录"被掩盖成网络错误（2026-09-26 实测踩到）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 路由注册 ──────────────────────────────────────────────
app.include_router(auth.router,       prefix="/api/auth",      tags=["认证"])
auth_router = auth  # the middleware needs SESSION_COOKIE from it
app.include_router(knowledge.router,  prefix="/api/ai",        tags=["知识库"])
app.include_router(data_center.router, prefix="/api/data-center", tags=["数据中心"])
app.include_router(settings_router.router, prefix="/api/settings",   tags=["系统设置"])
# ⚠️ `/api/org/*` (enterprise administration) is registered here but is NOT the
# operator surface. Its gate is its own `_scoped_org()`, derived from the
# caller's membership row — deliberately NOT `requires_admin`, which is the
# installation-level policy that every destructive route depends on. See
# `routers/org_admin.py` for why the two must not share a predicate.
app.include_router(org_admin_router.router)
app.include_router(town_router.router)
app.include_router(storage.router,         prefix="/api/storage",    tags=["文件存储"])
app.include_router(export.router,          prefix="/api/export",     tags=["导出"])
app.include_router(reports_router.router, prefix="/api/reports", tags=["Workspace"])
# The business knowledge base (the wiki). Separate router on purpose: the project
# manual at /api/ai/knowledge stays GET-only so sharing read access can never leak
# write access (asserted in tests/test_knowledge_router.py).
app.include_router(business_knowledge_router.router, prefix="/api/knowledge", tags=["Knowledge"])
app.include_router(calendar_router.router, prefix="/api/calendar", tags=["Calendar"])
# Notes on a document, and the Inbox that reports them. Registered last: both are
# cross-cutting over the three asset families above, not a fourth family.
app.include_router(annotations_router.router, prefix="/api/annotations", tags=["Annotations"])
app.include_router(inbox_router.router, prefix="/api/inbox", tags=["Inbox"])
# Dashboard is two routers on purpose. `include_router(prefix=…)` would rewrite the
# absolute reader path `/d/{slug}` into `/api/dashboard/d/{slug}`, and the reader has to
# keep the same standalone shape as `/r/{slug}` and `/e/{slug}` — a document the shell
# can drop into an iframe without knowing which module it came from.
app.include_router(dashboard_router.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(dashboard_router.standalone_router)
# The left rail's folders and short module labels. Cross-cutting chrome, so it is
# registered on the `core` module (see klado_shared/modules.py) — the middleware
# answers 403 for a path no module claims, and an unregistered prefix here would make
# the whole rail unreachable in the running app while every unit test still passed.
app.include_router(rail_router.router, prefix="/api/rail", tags=["Left rail"])


@app.get("/api/health")
async def health(request: Request):
    """Liveness, plus the caller's module entitlements.

    The SPA needs the module list before it can decide what to draw, and this is the
    one endpoint it already calls on every load. `/api/auth/me` cannot carry it in a
    single-user deployment: with `AUTH_ENABLED=0` it deliberately answers
    `{"enabled": false}` and never reaches the branch that attaches `modules`, so the
    nav would have nothing to render from.
    """
    payload = {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}
    try:
        from core import modules as core_modules
        from services import account_modules
        user = getattr(request.state, "current_user", None) or {}
        payload["modules"] = core_modules.for_client(account_modules.entitled(user))
    except Exception:
        # Health must not fail because a module table is unreadable: the app is up.
        logger.warning("health: could not resolve module entitlements", exc_info=True)
        payload["modules"] = None
    return payload


# ─── PDF export proxy ────────────────────────────────────────────────────────
# In merged mode (no separate pdf-service container) the subprocess above
# listens on localhost:19010.  Set PDF_SERVICE_URL env var to override.
_PDF_SERVICE_URL = os.getenv("PDF_SERVICE_URL", "http://localhost:19010/pdf")

@app.post("/api/export/pdf")
async def proxy_pdf(request: Request):
    body = await request.body()
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            _PDF_SERVICE_URL,
            content=body,
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/pdf"),
    )


# ─── 静态前端文件（必须最后挂载）───────────────────────────────────
def root_document_for(*, has_query: bool, auth_enabled: bool,
                      authenticated: bool) -> str:
    """Which FILE `/` means for THIS caller. Pure, so it can be unit-tested.

    ⚠️ Module level on purpose: this used to live inside
    `if os.path.isdir(_FRONTEND_DIR):` next to the routes that call it, which
    meant the one piece of logic actually worth testing was only importable on a
    machine that happened to have the frontend built.

    Returns the document's FILENAME rather than a short name for the caller to
    map, so the return value can be asserted against the two files that exist —
    a typo here would otherwise be invisible to every test.

    The three inputs, and why each one is here:

    * `has_query` — a request carrying a query string is somebody who came for a
      SPECIFIC thing, and the app addresses those as deep links
      (`?report=<slug>`, `?kb=<slug>`). Serving them the marketing page drops them
      at the wrong destination; the link still works once they are through, but the
      first click on a link someone was sent is where you lose them. `?welcome=1`
      is the landing page's own escape hatch and is handled before this is called.
    * `auth_enabled` — a deployment with auth off has no sessions at all, so
      "not authenticated" is everybody and the gate would be permanent.
    * `authenticated` — the actual question. Signed in → the application; not
      signed in → the landing page, every time, including on a refresh.
    """
    if has_query or not auth_enabled:
        return "index.html"
    return "index.html" if authenticated else "welcome.html"


_FRONTEND_DIR = resolve_frontend_dir()
print(
    f"Klado static UI: APP_BASE_PATH={_APP_BASE_PATH or '/'} FRONTEND_DIR={_FRONTEND_DIR} "
    f"exists={os.path.isdir(_FRONTEND_DIR)}",
    flush=True,
)
if os.path.isdir(_FRONTEND_DIR):
    import pathlib
    from fastapi.responses import FileResponse

    def _frontend_file_response(filename: str):
        path = pathlib.Path(_FRONTEND_DIR) / filename
        if filename.endswith(".html") and _APP_BASE_PATH:
            html = path.read_text(encoding="utf-8")
            # The redirect above canonicalises new visits.  The base element
            # also keeps all relative assets correct for clients which retain
            # a bare-path document during a rolling deployment.
            html = html.replace(
                "<head>",
                f'<head>\n<base href="{_APP_BASE_PATH}/">',
                1,
            )
            patch = f"""
<script>
(function() {{
  const basePath = {(_APP_BASE_PATH)!r};
  const withBasePath = function(url) {{
    if (typeof url !== 'string') return url;
    if (url === '/api' || url.startsWith('/api/')) return basePath + url;
    return url;
  }};
  const rawFetch = window.fetch;
  window.fetch = function(input, init) {{
    if (typeof input === 'string') return rawFetch.call(this, withBasePath(input), init);
    if (input instanceof Request) {{
      const url = withBasePath(input.url.replace(window.location.origin, ''));
      if (url !== input.url && url.startsWith(basePath)) return rawFetch.call(this, new Request(url, input), init);
    }}
    return rawFetch.call(this, input, init);
  }};
  const rawOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {{
    arguments[1] = withBasePath(url);
    return rawOpen.apply(this, arguments);
  }};
}})();
</script>
"""
            html = html.replace("</head>", patch + "\n</head>", 1)
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    # ── The landing page, and the gate that decides who sees it ─────────────
    #
    # ⚠️ What decides this was WRONG for a long time, and the symptom was only
    # visible to the person standing outside: a browser that had ever clicked the
    # landing page's call to action carried `klado-welcome-seen=1` for a YEAR, and
    # `/` served it the application from then on — even with no session, even after
    # signing out, even on a refresh. The landing page became a once-per-browser
    # page instead of a page for people who are NOT logged in, which is the only
    # audience it was ever written for.
    #
    # It showed up as "I clicked Sign in and now I can never get back". Back does
    # not help, and the reason is worth keeping: the landing page is served AT `/`
    # (a rewrite, not a redirect), so the previous history entry is `/` itself —
    # which now answers with the application, because of the cookie. The gate was
    # a PROXY for "has this browser been onboarded", and the honest signal for that
    # is the one the server already has: is there a session.
    #
    # So the marker cookie is gone (nothing read it but this function, and a
    # write-only cookie is worse than none — it outlives the reason it existed).
    # What replaced it is the caller's identity, resolved best-effort exactly the
    # way a public endpoint already does it (`_attach_identity_if_possible`), so an
    # expired cookie or an unreachable identity provider still lands somebody on
    # the landing page instead of turning `/` into a 500.
    #
    # ⚠️ `/` must stay OUT of `_SESSION_DOCUMENT_PREFIXES`. It is the one document
    # path whose entire audience is not signed in yet; adding it there 401s the
    # landing page itself.
    async def _root_document(request: Request):
        """`root_document_for`, with the identity resolved off the request."""
        await _attach_identity_if_possible(request)
        return _frontend_file_response(root_document_for(
            has_query=bool(request.query_params),
            auth_enabled=settings.AUTH_ENABLED,
            authenticated=bool(getattr(request.state, "current_user", None)),
        ))

    @app.get("/", include_in_schema=False)
    async def serve_index(request: Request):
        # `?welcome=1` is what the landing page's call to action links to, and it
        # is the ONLY way to reach the application without a session — without it
        # the gate below would send the person who clicked "Sign in" straight back
        # to the page they just read, forever.
        if request.query_params.get("welcome") == "1":
            return _frontend_file_response("index.html")
        return await _root_document(request)

    if _APP_BASE_PATH:
        @app.get(_APP_BASE_PATH, include_in_schema=False)
        @app.get(_APP_BASE_PATH + "/", include_in_schema=False)
        async def serve_prefixed_index(request: Request):
            if request.query_params.get("welcome") == "1":
                return _frontend_file_response("index.html")
            return await _root_document(request)

        @app.get(_APP_BASE_PATH + "/index.html", include_in_schema=False)
        async def serve_prefixed_index_html():
            return _frontend_file_response("index.html")

    # ── A report's own address: {base}/r/<slug> ─────────────────────────────
    # Clean and short, serves the report DOCUMENT itself (no app chrome), so it is
    # what you hand out when sharing. Delegates to the same handler as
    # /api/reports/{slug}/raw, so the injected <base> plus the deck/language runtime
    # arrive unchanged. The mount point is stripped by strip_app_base_path(), so
    # /r/<slug> lands here.
    @app.get("/r/{slug}", include_in_schema=False)
    async def serve_report_standalone(slug: str, request: Request, download: bool = False):
        return await reports_router.render_report(slug, request, download)

    # A random, revocable report capability. Unlike /r/<slug>, these GETs
    # are deliberately reachable without a session; they expose only this report,
    # saved annotation state and explicitly referenced images.
    @app.get("/s/{token}", include_in_schema=False)
    async def serve_report_anyone_link(token: str, request: Request):
        return await reports_router.render_anyone_link(token, request)

    @app.get("/s/{token}/state", include_in_schema=False)
    async def serve_report_anyone_state(token: str):
        return await reports_router.anyone_link_state(token)

    # The colleagues' notes on a shared report. GET only, like /state: a guest may
    # read what was written, and writing one requires an account.
    @app.get("/s/{token}/annotations", include_in_schema=False)
    async def serve_report_anyone_annotations(token: str):
        return await reports_router.anyone_link_annotations(token)

    @app.get("/s/{token}/asset", include_in_schema=False)
    async def serve_report_anyone_asset(token: str, path: str, w: int | None = None):
        return await reports_router.anyone_link_asset(token, path, w)

    # A document card's own bytes, reached through the same token (an iframe inside
    # /s/{token} cannot send an Authorization header, and `/api/...` would answer 401).
    # `preview=1` asks for the rendered PDF of a pptx/docx instead of the original file.
    @app.get("/s/{token}/document", include_in_schema=False)
    async def serve_report_anyone_document(token: str, preview: str = ""):
        return await reports_router.anyone_link_document(token, preview)

    # ── An event's own address: {base}/e/<slug> ─────────────────────────────
    # Same shape as a report's /r/<slug>: the event DOCUMENT itself, no app chrome, which
    # is what the detail viewer's iframe loads. It needs a session — the event is private
    # unless its owner published a snapshot — so it is not a link to hand to strangers.
    @app.get("/e/{slug}", include_in_schema=False)
    async def serve_event_standalone(slug: str, request: Request):
        return await calendar_router.render_event(slug, request)

    @app.get("/privacy", include_in_schema=False)
    async def serve_privacy():
        return _frontend_file_response("privacy.html")

    # ── The product landing page: {base}/welcome ──────────────────────────
    # Same shape as `/privacy`, and deliberately so: it is a standalone document
    # with its own address, no app chrome, and no identity.
    #
    # ⚠️ It must stay OUT of `_SESSION_DOCUMENT_PREFIXES`. That list is the one
    # place that decides "does this path resolve a caller", and a marketing page
    # has no caller to resolve — adding it there would hand the page a 401 for
    # everyone who is not signed in yet, which is exactly the audience it exists
    # for. `_module_gate_response` already exempts non-`/api/`, non-document paths
    # as "shell, assets, public pages", so no module has to claim it.
    @app.get("/welcome", include_in_schema=False)
    async def serve_welcome():
        return _frontend_file_response("welcome.html")

    if _APP_BASE_PATH:
        @app.get(_APP_BASE_PATH + "/privacy", include_in_schema=False)
        async def serve_prefixed_privacy():
            return _frontend_file_response("privacy.html")

        @app.get(_APP_BASE_PATH + "/welcome", include_in_schema=False)
        async def serve_prefixed_welcome():
            return _frontend_file_response("welcome.html")

    class NoCacheStaticFiles(StaticFiles):
        """StaticFiles with explicit revalidation headers.

        index.html must never be cached by CDNs: deploys swap it wholesale and
        a stale cached shell hides every newly-deployed page from users.
        hashed vendor assets still get long freshness via ETag revalidation.
        """
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            path = getattr(resp, "path", "") or str(getattr(getattr(resp, "background", None), "path", ""))
            if path.endswith("index.html") or not path:
                resp.headers["Cache-Control"] = "no-cache"
            else:
                resp.headers.setdefault("Cache-Control", "public, max-age=3600")
            return resp

    if _APP_BASE_PATH:
        app.mount(_APP_BASE_PATH, NoCacheStaticFiles(directory=_FRONTEND_DIR, html=True), name="static-prefixed")
    app.mount("/", NoCacheStaticFiles(directory=_FRONTEND_DIR, html=True), name="static")
else:
    print(f"WARNING: FRONTEND_DIR not found, static UI disabled: {_FRONTEND_DIR}", flush=True)
