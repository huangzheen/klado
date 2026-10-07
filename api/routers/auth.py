"""
Authentication endpoints — temporary local accounts (see services/auth_store.py).

The host platform's SSO is not available, so this module owns the whole flow:

    POST /api/auth/register/start     {email}                      → emails a code
    POST /api/auth/register/complete  {email, code, password}      → creates the user
    POST /api/auth/login              {email, password}            → session cookie
    POST /api/auth/logout
    GET  /api/auth/me
    PATCH /api/auth/me/avatar         {animal, cloth}             → own office avatar
    GET  /api/auth/office-seats                                  → the landing-page room

Agents do not need any of those calls: they authenticate on every request with
`Authorization: Basic base64(email:password)` (handled in the middleware). Logging
in is offered as well, because a script may prefer a session token.

Admin-only (role == 'admin'; the first AUTH_ADMIN_EMAILS entry, if any):

    GET    /api/auth/admin/users?q=
    PATCH  /api/auth/admin/users/{id}      {disabled?, role?, display_name?}
    GET    /api/auth/admin/users/{id}/impact   who this closing would break
    DELETE /api/auth/admin/users/{id}?confirm=<email>   → recycle bin, restorable
    POST   /api/auth/admin/users/{id}/restore          undo the close
    DELETE /api/auth/admin/users/{id}/purge?confirm=<email>   erase for good
    GET    /api/auth/admin/recycle-bin?include_expired=
    POST   /api/auth/admin/recycle-bin/sweep          purge due accounts now
    GET    /api/auth/admin/activity?user_id=&kind=&limit=
    GET    /api/auth/admin/codes           live (unused) verification codes + values

⚠️ Closing an account is NOT `DELETE FROM app_users`. It marks the row and revokes its
credentials; the data stays for `GRACE_DAYS` and the purge happens afterwards
(`services/account_lifecycle.py`). Erasing it immediately is a separate, explicit act.

⚠️ The address suffix is a format gate, not proof of identity — which domains are
accepted is set by `AUTH_ALLOWED_EMAIL_DOMAIN` (empty accepts any well-formed address).
What makes an account real is that the code was delivered to a mailbox the person controls.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

from klado_shared import town as town_store
from routers.town import call as town_call
from core.config import settings
from core.i18n import pick, request_lang
from core.third_auth import AuthError, authenticate_token
from services import account_lifecycle, auth_store, mail_config, mailer

router = APIRouter()

_LOG = logging.getLogger(__name__)

SESSION_COOKIE = "klado_session"
MIN_PASSWORD_LEN = 8
# `label` on the code every new account gets at sign-up. It is the 用途 column in the
# admin page, so it has to read as "where did this come from" rather than a device name.
SIGNUP_AGENT_LABEL = "signup"

# ── crude in-process rate limiting ───────────────────────────────────────────
# Single replica, so a dict is enough; the point is to make password guessing and
# code spamming unattractive, not to be a WAF.
_WINDOW_SECONDS = 300
_MAX_PER_WINDOW = 12
_hits: dict[str, list[float]] = {}


def _rate_limit(request: Request, key: str) -> None:
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < _WINDOW_SECONDS]
    if len(recent) >= _MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail=pick(
            "尝试次数过多，请稍后再试 / too many attempts, try again later", request_lang(request) if request else None))
    recent.append(now)
    _hits[key] = recent


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    secure = forwarded_proto == "https" or request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(settings.AUTH_SESSION_TTL_SECONDS or 30 * 24 * 3600),
        httponly=True, samesite="lax", secure=secure, path="/",
    )


def _local_mode() -> bool:
    """True when this deployment authenticates locally (no platform SSO configured)."""
    return not (settings.AUTH_USER_INFO_URL and settings.AUTH_TOOL_CODE)


def _asset_base_url(request: Request) -> str:
    """
    Base URL for assets embedded in an email (the banners and the app link), taken
    from the request so the message points back at whoever sent it. Returns "" when
    the request carries no host — the templates then simply omit those links instead
    of embedding an address that belongs to somebody else's installation.
    """
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return ""
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "https").split(",")[0].strip()
    path = (settings.APP_BASE_PATH or "").rstrip("/")
    return f"{proto}://{host}{path}"


def _welcome_agent_code(user: dict, request: Request) -> str:
    """
    Mint the agent access code (授权码) for a freshly created account and email it.

    A new colleague should be able to hand the code to their assistant right away instead
    of asking somebody to issue one. The code reuses the account's own identity but with
    the narrower agent permissions, so this grants nothing the person did not already
    have — it is the *robot's* credential, not a second account.

    ⚠️ This must never fail the registration: the account exists and is signed in by the
    time it runs, so an SMTP hiccup would otherwise strand the user in a half-done
    sign-up. Two things keep that safe — the code is stored server-side (`code_enc`, so
    the admin page can show it again and hand it over), and the plaintext is deliberately
    **not** returned here: the SPA reloads the moment registration succeeds, so a code in
    this response body would be dropped on the floor before anyone could read it.

    Returns the mailer's own status ('sent' / 'unconfigured' / 'failed' / 'error').
    """
    email = user.get("email") or ""
    try:
        code, _row = auth_store.create_agent_token(
            user["id"], SIGNUP_AGENT_LABEL, created_by=email)
    except Exception as exc:  # noqa: BLE001 — a welcome code must not block sign-up
        print(f"WARNING: could not issue the signup agent code for {email}: {exc}", flush=True)
        return "error"
    try:
        mail = mailer.send_agent_code(email, code, SIGNUP_AGENT_LABEL,
                                      settings.AGENT_SKILL_URL,
                                      asset_base=_asset_base_url(request))
    except Exception as exc:  # noqa: BLE001 — same rule as above
        print(f"WARNING: could not email the signup agent code to {email}: {exc}", flush=True)
        return "error"
    if mail != "sent":
        # print(), not _LOG — core/logring.py swallows app-level logging (see the note in
        # register_start). The code stays readable on the admin page, so this is a
        # "hand it over by hand" situation, not a lost credential.
        print(f"WARNING: the signup agent code for {email} was not emailed ({mail}); "
              f"it is stored and copyable from the admin page", flush=True)
    return mail


def _require_user(request: Request) -> dict:
    """Any signed-in identity (browser session or machine credential)."""
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail=pick("未登录 / not authenticated", request_lang(request) if request else None))
    return user


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail=pick("未登录 / not authenticated", request_lang(request) if request else None))
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail=pick("需要管理员权限 / admin only", request_lang(request) if request else None))
    return user


# ── models ───────────────────────────────────────────────────────────────────

class RegisterStart(BaseModel):
    email: str


class RegisterComplete(BaseModel):
    email: str
    code: str
    password: str
    display_name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


class UserPatch(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    display_name: str | None = None
    # Admin reset. Until this existed there was no way to recover a forgotten
    # password at all — not for a user and not for the admin.
    password: str | None = None


class AvatarPatch(BaseModel):
    """The reader's own office avatar: which beast, which shirt.

    ⚠️ No user id, deliberately. Every other patch in this file that can name an
    account is admin-gated; this one is the reader choosing their own face, so the
    router fills the id in from the session and the body cannot even express
    "change somebody else's". A body that could carry an id is a body that will
    eventually carry someone else's.
    """
    model_config = ConfigDict(extra="forbid")
    animal: str
    cloth: str


# ── public ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def auth_health():
    return {
        "ok": True,
        "enabled": settings.AUTH_ENABLED,
        "mode": "local" if _local_mode() else "platform",
        "mail": "configured" if mailer.configured() else "unconfigured",
        "allowed_domain": auth_store.allowed_domain(),
        # Whether a first-admin bootstrap is configured (never the hash itself).
        # Public on purpose: it is how a deploy is verified from outside, since
        # neither the operator nor the agent may have cluster access here.
        # "on" only when it would actually run — a mis-quoted pipeline variable
        # must not look armed.
        "bootstrap": "on" if (settings.AUTH_BOOTSTRAP_ADMIN_EMAIL.strip()
                              and settings.AUTH_BOOTSTRAP_ADMIN_HASH.strip()
                              .startswith("pbkdf2_sha256$")) else "off",
    }


def _modules_for(user: dict) -> dict:
    """This account's module entitlements, for the SPA to render from.

    Resolved here rather than in the middleware so the SPA and the gate read the
    same answer: one registry, one resolution order, no second opinion.
    """
    from core import modules as core_modules
    from services import account_modules
    # ⚠️ `entitled()`, not a hand-rolled read here. This endpoint and the middleware gate
    # must answer the SAME question — including the per-company licence the platform
    # operator sets in the console — and two copies of the narrowing is two copies to
    # drift. It also fails soft, which is what this branch was doing by hand.
    return core_modules.for_client(account_modules.entitled(user))


@router.get("/me")
async def current_user(request: Request):
    """Who the caller is. 401 when unauthenticated so the SPA can show the gate."""
    if not settings.AUTH_ENABLED:
        # Auth off: keep the old shape so nothing that probed this breaks.
        return {"enabled": False, "authenticated": False, "user": None}
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail=pick("未登录 / not authenticated", request_lang(request) if request else None))
    return {"enabled": True, "authenticated": True, "mode": "local" if _local_mode() else "platform",
            "user": {k: v for k, v in user.items() if k != "password_hash"},
            "modules": _modules_for(user)}


@router.get("/colleagues")
async def search_colleagues(request: Request, q: str = Query(""), limit: int = Query(8, ge=1, le=25)):
    """Registered colleagues matching ``q`` — the share pickers' autocomplete.

    Not admin-only on purpose: this is the same set of addresses a colleague already types
    into the share box, and the response carries only email / display name / role. The
    account metadata the admin table shows (id, created_at, last_login, disabled) is not
    part of it, and deactivated accounts are left out — you cannot share with someone who
    can no longer sign in.

    Queries shorter than two characters get an empty list rather than the whole company.
    """
    _require_user(request)
    return {"colleagues": auth_store.search_users(q, limit), "domain": auth_store.allowed_domain()}


@router.get("/office-seats")
async def office_seats(request: Request, limit: int = Query(120, ge=1, le=500)):
    """Compatibility URL; all desks now belong to agents in the caller's office."""
    user=_require_user(request)
    return town_call(town_store.office_agents,int(user['id']))



class OfficeWorkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["working", "waiting", "done", "error", "idle"]
    task: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=1200)

    @field_validator("task")
    @classmethod
    def nonempty_task(cls, value):
        if not value.strip():
            raise ValueError("任务不能为空 / task must not be blank")
        return value.strip()


@router.post("/office-work", status_code=201)
async def report_office_work(body: OfficeWorkUpdate, request: Request):
    """Share a task update with the caller's office. One desk per agent code.

    Send working when starting, waiting when human input is needed, done with a
    summary on completion, error on failure, or idle when available. Only include
    information intended for the office; private document contents stay private.
    """
    user = _require_user(request)
    return town_call(town_store.report,user,body.state,body.task,body.summary)


@router.patch("/me/avatar")
async def set_my_avatar(body: AvatarPatch, request: Request):
    """Choose the beast and the shirt this account's agent wears in the office.

    ⚠️ Non-admin on purpose, and scoped to the CALLER: this is a reader picking
    their own face, not an operator editing accounts, so the id comes from the
    session and never from the body. An unknown animal or shirt is a 400 rather
    than a silent fallback — see `set_avatar`.

    The response is the user dict, so the SPA can repaint the menu chip from what
    the SERVER stored rather than from what it optimistically set, which is the
    only way a write that half-failed is visible.
    """
    user = _require_user(request)
    updated = auth_store.set_avatar(int(user["id"]), body.animal, body.cloth)
    if not updated:
        raise HTTPException(
            status_code=400,
            detail=pick(
                f"未知的形象或衣服颜色（可选 {', '.join(auth_store.AVATAR_ANIMALS)} × "
                f"{', '.join(auth_store.AVATAR_CLOTHS)}） / unknown avatar",
                request_lang(request) if request else None,
            ),
        )
    return {k: v for k, v in updated.items() if k != "password_hash"}


@router.post("/register/start")
async def register_start(body: RegisterStart, request: Request):
    """Validate the address, then email a single-use code."""
    if not _local_mode():
        raise HTTPException(status_code=400, detail=pick("本部署使用平台登录，无需注册 / handled by the platform", request_lang(request) if request else None))
    email = (body.email or "").strip().lower()
    _rate_limit(request, f"start:{email}:{_client_ip(request)}")
    domain = auth_store.allowed_domain()
    if "@" not in email or (domain and not email.endswith("@" + domain)):
        detail = (f"只能用 @{domain} 企业邮箱注册 / only @{domain} addresses can register"
                  if domain else "邮箱地址无效 / invalid email address")
        raise HTTPException(status_code=400, detail=pick(detail, request_lang(request) if request else None))
    if len(email.split("@")[0]) < 2:
        raise HTTPException(status_code=400, detail=pick("邮箱地址无效 / invalid email address", request_lang(request) if request else None))
    if auth_store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail=pick("该邮箱已注册，请直接登录 / already registered, please log in", request_lang(request) if request else None))

    code = auth_store.create_code(email)
    delivery = mailer.send_verification_code(
        email, code, asset_base=_asset_base_url(request))
    if delivery != "sent":
        # Mail is not configured (or the server refused). The code is live and valid,
        # so the request succeeds — but then it MUST be readable somewhere, or the
        # flow dead-ends: there is no "resend to another mailbox" fallback, and the
        # admin page (which does list live codes) needs an admin to already exist.
        #
        # ⚠️ It cannot go through logging. `core/logring.install()` attaches a
        # ring-buffer handler to the ROOT logger, and a root handler suppresses
        # Python's last-resort handler — so app-level `_LOG.warning` never reaches
        # stdout and therefore never reaches the server's own log (it is only
        # readable via /api/debug/logs). print() is what actually lands there — the
        # same convention the startup diagnostics in main.py use.
        print(f"WARNING: registration code for {email} is {code} "
              f"(mail delivery: {delivery}) — the admin page lists live codes too",
              flush=True)
        auth_store.log_access(None, "browser", "REGISTER", "/api/auth/register/start", 200,
                              _client_ip(request), request.headers.get("user-agent", ""))
    return {
        "ok": True,
        "email": email,
        "expires_in": int(settings.AUTH_CODE_TTL_SECONDS or 600),
        "mail": delivery,
        "hint": pick(("验证码已发送，请查收邮件（也可能在垃圾箱）。 / The code has been emailed — check your inbox (and spam)."
                      if delivery == "sent" else
                      "邮件通道尚未配置或发送失败 —— 请联系管理员在「管理员页面」查看实时验证码。"
                      " / Mail is not configured or delivery failed — ask an admin to read the live code on the admin page."),
                     request_lang(request) if request else None),
    }


@router.post("/register/complete")
async def register_complete(body: RegisterComplete, request: Request, response: Response):
    """Consume the code, create the account, and sign the browser in."""
    if not _local_mode():
        raise HTTPException(status_code=400, detail=pick("本部署使用平台登录 / handled by the platform", request_lang(request) if request else None))
    email = (body.email or "").strip().lower()
    _rate_limit(request, f"complete:{email}:{_client_ip(request)}")
    password = body.password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=pick(f"密码至少 {MIN_PASSWORD_LEN} 位 / password too short", request_lang(request) if request else None))
    if auth_store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail=pick("该邮箱已注册，请直接登录 / already registered", request_lang(request) if request else None))

    ok, reason = auth_store.verify_code(email, body.code)
    if not ok:
        messages = {
            "no_code": "请先获取验证码 / request a code first",
            "expired": "验证码已过期，请重新获取 / code expired",
            "too_many_attempts": "验证码尝试次数过多，请重新获取 / too many attempts",
            "wrong_code": "验证码不正确 / wrong code",
        }
        raise HTTPException(status_code=400, detail=pick(messages.get(reason, "验证码校验失败 / code check failed"), request_lang(request) if request else None))

    try:
        user = auth_store.create_user(email, password, body.display_name)
    except Exception as exc:  # noqa: BLE001 — unique violation on a race
        raise HTTPException(status_code=409, detail=pick("该邮箱已注册 / already registered", request_lang(request) if request else None)) from exc

    token = auth_store.issue_session(user)
    _set_session_cookie(response, token, request)
    auth_store.log_access(user, "browser", "REGISTER", "/api/auth/register/complete", 200,
                          _client_ip(request), request.headers.get("user-agent", ""))
    # The welcome mail with the Doubao Work access code. Runs last and cannot fail the
    # request — see the docstring for why the plaintext is not part of this response.
    agent_mail = _welcome_agent_code(user, request)
    return {"ok": True, "user": auth_store._public(user), "token": token,
            "agent_mail": agent_mail}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    email = (body.email or "").strip().lower()
    _rate_limit(request, f"login:{email}:{_client_ip(request)}")
    user, reason = auth_store.authenticate(email, body.password or "")
    if not user:
        status = 403 if reason == "disabled" else 401
        detail = "账号已被禁用，请联系管理员 / account disabled" if reason == "disabled" else "邮箱或密码不正确 / wrong email or password"
        auth_store.log_access(auth_store.get_user_by_email(email), "browser", "LOGIN",
                              "/api/auth/login", status, _client_ip(request),
                              request.headers.get("user-agent", ""))
        raise HTTPException(status_code=status, detail=pick(detail, request_lang(request) if request else None))
    token = auth_store.issue_session(user)
    _set_session_cookie(response, token, request)
    auth_store.log_access(user, "browser", "LOGIN", "/api/auth/login", 200,
                          _client_ip(request), request.headers.get("user-agent", ""))
    return {"ok": True, "user": auth_store._public(user), "token": token}


class PasswordChange(BaseModel):
    current_password: str = ""
    new_password: str = ""


@router.post("/password")
async def change_password(body: PasswordChange, request: Request):
    """
    Self-service password change. Worth having right after a bootstrap: the seeded
    password was chosen by whoever set up the installation, so the account holder
    should be able to replace it without an admin and without a restart.
    """
    me = getattr(request.state, "current_user", None)
    if not me:
        raise HTTPException(status_code=401, detail=pick("未登录 / not authenticated", request_lang(request) if request else None))
    if len(body.new_password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400,
                            detail=pick(f"新密码至少 {MIN_PASSWORD_LEN} 位 / new password too short",
                                        request_lang(request) if request else None))
    # A session cookie proves the session, not the person at the keyboard: ask for
    # the current password so a borrowed laptop cannot silently take the account.
    user, reason = auth_store.authenticate(me["email"], body.current_password or "")
    if not user:
        raise HTTPException(status_code=403,
                            detail=pick("当前密码不正确 / current password is incorrect", request_lang(request) if request else None))
    auth_store.set_password(me["id"], body.new_password)
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    # ⚠️ Signing out makes the next visitor a first-time one again, and that is the
    # point: the landing page is what `/` serves to anybody without a session, so
    # after this endpoint runs, `/` will hand it back to whoever opens the domain
    # next — which is exactly the "not dropped into somebody else's app" property.
    # No second cookie is involved any more: `klado-welcome-seen` used to have to be
    # cleared here, and it no longer exists (see `root_document_for` in main.py —
    # a browser that had "been here" used to be routed to the application forever,
    # session or not, which is what made the landing page impossible to get back to).
    return {"ok": True}


# ── admin ────────────────────────────────────────────────────────────────────

@router.get("/admin/users")
async def admin_users(request: Request, q: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
    _require_admin(request)
    return {"users": auth_store.list_users(q, limit)}


@router.patch("/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: UserPatch, request: Request):
    me = _require_admin(request)
    target = auth_store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", request_lang(request) if request else None))
    if target["id"] == me["id"] and (body.disabled or (body.role and body.role != "admin")):
        raise HTTPException(status_code=400, detail=pick("不能禁用或降级自己 / cannot disable or demote yourself", request_lang(request) if request else None))
    if body.password is not None:
        if len(body.password) < MIN_PASSWORD_LEN:
            raise HTTPException(status_code=400, detail=pick(f"密码至少 {MIN_PASSWORD_LEN} 位 / password too short", request_lang(request) if request else None))
        auth_store.set_password(user_id, body.password)
    updated = auth_store.update_user(user_id, body.disabled, body.role, body.display_name)
    return {"ok": True, "user": auth_store._public(updated)}


@router.get("/admin/users/{user_id}/impact")
async def admin_user_impact(user_id: int, request: Request):
    """What closing this account would take with it, and who it would break.

    Called before the confirm step. Read-only and never mutating: an operator has to be
    able to look at the blast radius, close the tab, and come back without having
    started anything.
    """
    _require_admin(request)
    target = auth_store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", request_lang(request) if request else None))
    try:
        return account_lifecycle.dependents(user_id)
    except account_lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), request_lang(request) if request else None))


@router.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request, confirm: str = Query("")):
    """Close an account — into the recycle bin, not into the void.

    ⚠️ This used to `DELETE FROM app_users` outright, which removed the login while
    leaving every report, dataset and dashboard behind as rows nobody could reach.
    Closing now goes through `account_lifecycle.soft_delete`, and the data stays
    exactly where it is for the grace period.

    `confirm` must carry the account's email address. An operator deleting a person is
    about to destroy their work; making them type the address is the cheapest thing
    that reliably catches a mis-click, and it doubles as the client's proof that the
    impact dialog they were shown is the one being acted on.
    """
    me = _require_admin(request)
    lang = request_lang(request) if request else None
    if user_id == me["id"]:
        raise HTTPException(status_code=400, detail=pick("不能删除自己 / cannot delete yourself", lang))
    target = auth_store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if not target.get("deleted_at"):
        if (confirm or "").strip().lower() != (target["email"] or "").strip().lower():
            raise HTTPException(
                status_code=400,
                detail=pick(f"请输入该账号邮箱以确认 / type the account email to confirm ({target['email']})", lang))
    try:
        view = account_lifecycle.soft_delete(user_id, actor=me.get("email", ""))
    except account_lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), lang))
    return {"ok": True, "user": view}


@router.post("/admin/users/{user_id}/restore")
async def admin_restore_user(user_id: int, request: Request):
    """Undo a close. Only works inside the grace period."""
    me = _require_admin(request)
    lang = request_lang(request) if request else None
    target = auth_store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if not target.get("deleted_at"):
        raise HTTPException(status_code=400, detail=pick("该账号未被删除 / this account is not pending deletion", lang))
    try:
        return {"ok": True, "user": account_lifecycle.restore(user_id, actor=me.get("email", ""))}
    except account_lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), lang))


@router.delete("/admin/users/{user_id}/purge")
async def admin_purge_user(user_id: int, request: Request, confirm: str = Query("")):
    """Erase a closed account for good. Irreversible; same email confirmation.

    Separated from the close so "remove this person" and "destroy everything they ever
    made" are two different acts with two different buttons. A client should only
    offer this to a row that is already in the recycle bin.
    """
    me = _require_admin(request)
    lang = request_lang(request) if request else None
    if user_id == me["id"]:
        raise HTTPException(status_code=400, detail=pick("不能删除自己 / cannot delete yourself", lang))
    target = auth_store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if (confirm or "").strip().lower() != (target["email"] or "").strip().lower():
        raise HTTPException(
            status_code=400,
            detail=pick(f"请输入该账号邮箱以确认 / type the account email to confirm ({target['email']})", lang))
    try:
        return {"ok": True, "report": account_lifecycle.purge_account(user_id)}
    except account_lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), lang))


@router.get("/admin/recycle-bin")
async def admin_recycle_bin(request: Request, include_expired: bool = Query(True)):
    """Accounts waiting to be purged, with the day each one has left."""
    _require_admin(request)
    return {"accounts": account_lifecycle.list_pending(include_expired=include_expired)}


@router.post("/admin/recycle-bin/sweep")
async def admin_sweep(request: Request):
    """Purge everything past its deadline now, instead of waiting for the nightly sweep."""
    _require_admin(request)
    return {"results": account_lifecycle.sweep()}


@router.get("/admin/activity")
async def admin_activity(request: Request, user_id: int | None = Query(None),
                         kind: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
    _require_admin(request)
    return {"activity": auth_store.activity(user_id, kind, limit)}


@router.get("/admin/codes")
async def admin_codes(request: Request, limit: int = Query(50, ge=1, le=200)):
    """
    Live verification codes, value included. The mail path is best-effort (this server
    talking to the mailbox provider, and the account needs a client-specific
    password that the provider can revoke), so a code that nobody can read is
    simply a registration that cannot be finished — see
    `auth_store.pending_codes()` for how long the value survives.
    """
    _require_admin(request)
    return {"codes": auth_store.pending_codes(limit)}


@router.delete("/admin/codes")
async def admin_clear_codes(request: Request, email: str = Query(..., min_length=3)):
    """
    Retract the live invitation/registration code for one address.

    The counterpart of the "重新生成并发送" button: an admin who invited the wrong
    address (or wants to cut somebody off mid-invitation) can make the code dead
    immediately rather than waiting for its TTL. Idempotent — clearing nothing is fine.
    """
    _require_admin(request)
    cleaned = auth_store.clear_codes(email)
    return {"ok": True, "email": email.strip().lower(), "cleared": cleaned}


@router.post("/admin/invite")
async def admin_invite(body: InviteBody, request: Request):
    """
    Invite a colleague who has no account yet: mint a long-lived code and mail an
    invitation that welcomes them, says what Klado is for, and tells them how to
    finish signing up.

    Why the code is minted here rather than pointing at the normal sign-up: that flow
    mails a *second* code which lives ten minutes, so an invitation would need two
    emails and the second one would likely be read after it had expired. One mail, one
    code, one step.

    Falls back to returning the code **once** when mail is not configured — the same
    rule the verification and agent mails follow, so an invite cannot dead-end on a
    missing mailbox (and the accounts table shows live codes anyway).
    """
    admin = _require_admin(request)
    email = (body.email or "").strip().lower()
    domain = auth_store.allowed_domain()
    if "@" not in email or (domain and not email.endswith("@" + domain)):
        detail = (f"只能邀请 @{domain} 企业邮箱 / only @{domain} addresses can be invited"
                  if domain else "邮箱地址无效 / invalid email address")
        raise HTTPException(status_code=400, detail=pick(detail, request_lang(request) if request else None))
    if len(email.split("@")[0]) < 2:
        raise HTTPException(status_code=400, detail=pick("邮箱地址无效 / invalid email address", request_lang(request) if request else None))
    if auth_store.get_user_by_email(email):
        raise HTTPException(status_code=409,
                            detail=pick("该邮箱已经注册过了，直接让他登录即可 / already registered", request_lang(request) if request else None))

    ttl = int(settings.AUTH_INVITE_TTL_SECONDS or 1209600)
    code = auth_store.create_code(email, ttl_seconds=ttl)
    invited_by = (admin.get("display_name") or "").strip() or (admin.get("email") or "")
    base = _asset_base_url(request)
    mail = mailer.send_invite(email, code, max(1, ttl // 86400), base + "/", invited_by,
                              asset_base=base)
    out = {"ok": True, "email": email, "mail": mail, "expires_in": ttl,
           "invited_by": invited_by}
    if mail != "sent":
        # print(), not _LOG — services/mailer.py explains why app logs miss the server log
        print(f"WARNING: the invitation for {email} was not emailed ({mail}); the live "
              f"code is readable on the admin page", flush=True)
        out["code"] = code
        out["hint"] = pick("邮件通道未配置或发送失败 —— 这枚邀请码也会出现在管理员页这一行的 "
                           "Codes 列里，复制转达即可。"
                           " / Mail is not configured or delivery failed — the invite code is also listed "
                           "in the Codes column of the admin page; copy it from there.",
                           request_lang(request) if request else None)
    return out

# ── SMTP configuration (admin page) ──────────────────────────────────────────
# Why these are not environment variables: env vars are fixed when the server
# starts, so changing one means editing a file and restarting — and it puts a
# mailbox password into that file. They live in the `mail_settings` table instead
# (see services/mail_config.py), so the admin pastes them into the admin page and
# they take effect on the next send. The password is write-only: accepted here,
# never returned by any endpoint. The SMTP_* environment
# variables still work as a per-field fallback.

class MailSettingsBody(BaseModel):
    host: str | None = None
    port: int | None = None
    security: str | None = None
    username: str | None = None
    sender: str | None = None
    # omitted/None → keep the stored password · "" → clear it · a string → store it
    password: str | None = None


class MailTestBody(BaseModel):
    to: str | None = None


class AgentTokenBody(BaseModel):
    label: str = ""
    # Admins may issue a code for somebody else (a colleague who cannot self-serve).
    email: str | None = None


class InviteBody(BaseModel):
    email: str


@router.post("/agent-tokens")
async def create_agent_token(body: AgentTokenBody, request: Request):
    """
    Issue an agent access code (授权码) for the caller — or, for an admin, for `email`.

    ⚠️ Reachable with a **browser session only**: an agent cannot mint itself another
    code (and cannot extend its own access) because machine credentials are refused on
    every write outside /api/reports.

    The code is emailed to the account it belongs to *and* returned here, so the page
    that issued it can offer a copy button that hands out something usable — it used to
    return only the 12-character prefix, which is not a credential. When mail is not
    configured the email step is simply skipped, so the feature never dead-ends on a
    missing mailbox — same rule as the registration code.
    """
    me = getattr(request.state, "current_user", None)
    if not me:
        raise HTTPException(status_code=401, detail=pick("未登录 / not authenticated", request_lang(request) if request else None))
    target = me
    wanted = (body.email or "").strip().lower()
    if wanted and wanted != (me.get("email") or "").lower():
        if me.get("role") != "admin":
            raise HTTPException(status_code=403, detail=pick("只能为自己生成授权码 / admin required to issue for others", request_lang(request) if request else None))
        target = auth_store.get_user_by_email(wanted)
        if not target:
            raise HTTPException(status_code=404, detail=pick("该账号不存在 / no such account", request_lang(request) if request else None))
        if target.get("disabled"):
            raise HTTPException(status_code=400, detail=pick("该账号已被禁用 / account is disabled", request_lang(request) if request else None))
    token, row = auth_store.create_agent_token(
        target["id"], body.label, created_by=me.get("email") or "")
    skill_url = settings.AGENT_SKILL_URL
    mail = mailer.send_agent_code(target["email"], token, body.label, skill_url,
                                  asset_base=_asset_base_url(request))
    out = {"ok": True, "token": row, "mail": mail, "to": target["email"],
           "skill_url": skill_url, "code": token}
    if mail != "sent":
        # The mailbox is not a delivery route here, so the page is the only one — say so
        # rather than implying the recipient has mail waiting.
        out["hint"] = pick("邮件通道未配置或发送失败 —— 请从页面上直接复制这串码转达；"
                           "管理员页的「Mail delivery」配好后就不再需要人工转达。"
                           " / Mail is not configured or delivery failed — copy the code from this page; "
                           "once Mail delivery is configured on the admin page this is no longer needed.",
                           request_lang(request) if request else None)
    return out


@router.get("/agent-tokens")
async def list_my_agent_tokens(request: Request):
    me = _require_user(request)
    if getattr(request.state, "auth_kind", "") == "agent":
        raise HTTPException(403, detail="Agent credentials cannot read other access codes")
    return {"tokens": auth_store.list_agent_tokens(me["id"])}


@router.delete("/agent-tokens/{token_id}")
async def revoke_my_agent_token(token_id: int, request: Request):
    me = _require_user(request)
    if not auth_store.revoke_agent_token(token_id, me.get("email") or "", user_id=me["id"]):
        raise HTTPException(status_code=404, detail=pick("授权码不存在或已吊销 / not found or already revoked", request_lang(request) if request else None))
    return {"ok": True, "revoked": token_id}


@router.delete("/agent-tokens/{token_id}/purge")
async def purge_my_agent_token(token_id: int, request: Request):
    """
    Delete one of *my* revoked codes for good.

    404 (not 403) when the code is still active: from this endpoint's point of view a live
    code is simply not eligible, and the message says which step is missing.
    """
    me = _require_user(request)
    if not auth_store.purge_agent_token(token_id, user_id=me["id"]):
        raise HTTPException(status_code=404,
                            detail=pick("只有已吊销的授权码才能删除（先吊销） / revoke it first",
                                        request_lang(request) if request else None))
    return {"ok": True, "purged": token_id}


@router.get("/admin/agent-tokens")
async def admin_agent_tokens(request: Request):
    """Every issued code, with its owner — the audit/damage-control view."""
    _require_admin(request)
    if getattr(request.state, "auth_kind", "") == "agent":
        raise HTTPException(403, detail="Use a browser session to manage access codes")
    return {"tokens": auth_store.list_agent_tokens()}


@router.delete("/admin/agent-tokens/{token_id}/purge")
async def admin_purge_agent_token(token_id: int, request: Request):
    """Delete a revoked code for good (admin view). Same rule: 吊销 first."""
    _require_admin(request)
    if not auth_store.purge_agent_token(token_id):
        raise HTTPException(status_code=404,
                            detail=pick("只有已吊销的授权码才能删除（先吊销） / revoke it first",
                                        request_lang(request) if request else None))
    return {"ok": True, "purged": token_id}


@router.delete("/admin/agent-tokens/{token_id}")
async def admin_revoke_agent_token(token_id: int, request: Request):
    admin = _require_admin(request)
    if not auth_store.revoke_agent_token(token_id, admin.get("email") or ""):
        raise HTTPException(status_code=404, detail=pick("授权码不存在或已吊销 / not found or already revoked", request_lang(request) if request else None))
    return {"ok": True, "revoked": token_id}


@router.post("/admin/agent-tokens/{token_id}/mail")
async def admin_mail_agent_token(token_id: int, request: Request):
    """
    Send the agent-access (授权码) email again for a code that already exists — no rotation.

    This is the manual counterpart of the welcome mail every new account gets: same
    template, same code. So a message that landed in spam, or that the recipient deleted
    before reading it, can be handed over again without invalidating the copy they may
    already have. Replacing the code is what 「重新生成并发送」 does, and that is the right
    answer only when the old one must stop working.

    ⚠️ The code has to be *recoverable*: this reads `code_enc` back and decrypts it, the
    same value the page's copy button shows. A code issued before that column existed (or
    after SECRET_KEY was rotated) has nothing to send, so this reports that instead of
    mailing a 12-character prefix that a robot would reject with 401.
    """
    _require_admin(request)
    row = auth_store.get_agent_token(token_id)
    if not row:
        raise HTTPException(status_code=404, detail=pick("授权码不存在 / no such agent code", request_lang(request) if request else None))
    if row.get("revoked_at"):
        raise HTTPException(status_code=400,
                            detail=pick("这枚授权码已吊销，不能再发 / this code is revoked", request_lang(request) if request else None))
    if not (row.get("user_email") or "").strip():
        raise HTTPException(status_code=400,
                            detail=pick("这枚授权码所属的账号已不存在 / the owning account is gone", request_lang(request) if request else None))
    code = row.get("code") or ""
    if not code:
        raise HTTPException(
            status_code=400,
            detail=pick("这枚码的完整值读不出来（生成于本功能上线前，或 SECRET_KEY 已轮换）—— "
                        "请点「重新生成并发送」换一枚可发送的 / the full code cannot be read back",
                        request_lang(request) if request else None))
    mail = mailer.send_agent_code(row["user_email"], code, row.get("label") or "",
                                  settings.AGENT_SKILL_URL,
                                  asset_base=_asset_base_url(request))
    out = {"ok": True, "mail": mail, "to": row["user_email"], "token_id": token_id}
    if mail != "sent":
        # Same degrade rule as everywhere else: the page can hand the code over by hand.
        out["hint"] = pick("邮件通道未配置或发送失败 —— 这枚码仍在这一行的 Details 里，"
                           "复制转达即可。"
                           " / Mail is not configured or delivery failed — the code is still in this "
                           "row's Details; copy it from there.",
                           request_lang(request) if request else None)
    return out


@router.get("/admin/mail")
async def admin_mail(request: Request):
    """Effective SMTP settings — the password itself is never included."""
    _require_admin(request)
    return mail_config.describe()


@router.put("/admin/mail")
async def admin_mail_save(body: MailSettingsBody, request: Request):
    admin = _require_admin(request)
    if body.security is not None and body.security.strip().lower() not in ("ssl", "starttls", "plain"):
        raise HTTPException(status_code=400, detail=pick("security 只能是 ssl、starttls 或 plain / security must be ssl, starttls or plain", request_lang(request) if request else None))
    if body.port is not None and not 1 <= body.port <= 65535:
        raise HTTPException(status_code=400, detail=pick("端口不合法 / invalid port", request_lang(request) if request else None))
    if body.sender is not None and body.sender.strip() and "@" not in body.sender:
        raise HTTPException(status_code=400, detail=pick("发信地址需要是完整邮箱地址 / sender must be a full email address", request_lang(request) if request else None))
    return mail_config.save(
        host=(body.host or None), port=body.port,
        security=(body.security.strip().lower() if body.security else None),
        username=(body.username or None), sender=(body.sender or None),
        password=(mail_config.KEEP if body.password is None else body.password),
        actor=admin.get("email", ""),
    )


@router.delete("/admin/mail")
async def admin_mail_clear(request: Request):
    """Forget the stored settings and fall back to the environment."""
    _require_admin(request)
    return mail_config.clear()


@router.post("/admin/mail/test")
async def admin_mail_test(request: Request, body: MailTestBody | None = None):
    """
    Send the real email templates (with sample codes) and hand back the SMTP
    server's own error text.

    This exists because "failed" is useless to whoever has to fix it: a mailbox
    that refuses us says `526 Authentication failure`, and that code decides
    whether the fix is in the mail console (the third-party-client blacklist) or in
    the password. It sends the templates themselves, because the operator's real
    question is "what will the recipient see" — answerable only in a real inbox.
    Defaults to the admin's own address; pass another one to preview elsewhere.
    """
    admin = _require_admin(request)
    to = ((body.to if body else None) or admin.get("email") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail=pick("没有收件地址 / no recipient address", request_lang(request) if request else None))
    status, detail, samples = mailer.send_test(
        to, settings.AGENT_SKILL_URL, asset_base=_asset_base_url(request),
        invited_by=((admin.get("display_name") or "").strip() or (admin.get("email") or "")),
        lang=request_lang(request))
    if status != "sent":
        # print(), not _LOG — see services/mailer.py for why app logs miss the server log
        print(f"WARNING: admin mail test to {to} failed: {detail}", flush=True)
    return {"status": status, "detail": detail, "to": to, "samples": samples}
