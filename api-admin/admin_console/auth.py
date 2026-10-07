"""The console's own login: `POST /api/admin-console/login` and nothing else.

⚠️ **This is not `api/routers/auth.py`.** It looks like a smaller copy and it is
deliberately not a shared implementation, for one reason: the two logins have different
answers to "what happens next", and a shared half would have to be configured per caller —
which is how the two processes end up disagreeing about who may hold a console session.

The three things that differ from the main app, each load-bearing:

* **Audience.** The token is signed for `ADMIN_AUDIENCE`, so it verifies in this process
  and *not* in the main app. A console session replayed against `:8000` must be worthless
  — that is the whole reason the audience exists.
* **Cookie name.** `klado_admin_session`, not `klado_session`. Cookies are matched on
  host and ignore the port, so a shared name would have the two processes overwriting
  each other's login on every visit.
* **Who may hold one.** `role='admin'` only. An enterprise administrator is a real role
  and is explicitly NOT an operator: they administer one company, through
  `api/routers/org_admin.py` in the *main* app. Letting them in here would hand the
  console's whole-installation view to somebody whose entire remit is one company.

The rejection message is deliberately the same string for "no such account" and "wrong
password" — see `authenticate()` in the shared store. A different message for each is an
account-existence oracle on the most valuable credential in the installation.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from admin_console import access
from klado_shared import accounts, deployment
from klado_shared.config import settings
from klado_shared.i18n import pick, request_lang
from klado_shared.identity import is_admin_identity
from klado_shared.session import ADMIN_AUDIENCE, issue_session

router = APIRouter(prefix="/api/admin-console", tags=["控制台登录"])
log = logging.getLogger(__name__)

MIN_PASSWORD_LEN = 8


class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    lang = request_lang(request) if request else None
    if not deployment.console_login_enabled():
        # Refused before the credentials are even checked: with the switch off there is
        # no console login to attempt, and saying so is more useful than a 401 that
        # invites six more guesses.
        raise HTTPException(status_code=403, detail=pick(
            "管理后台登录已被关闭 / console login is switched off", lang))
    email = (body.email or "").strip().lower()
    access.throttle_login(email, request)
    # ⚠️ `authenticate()` already refuses a disabled or closed account, and it already
    # touches `last_login_at`. Repeating either here would be a second copy of a rule that
    # has to agree with the main app's login — and the `deleted` branch in particular is
    # ordered AFTER the password check on purpose, so that a closed account cannot be used
    # to discover that an address once existed.
    user, reason = accounts.authenticate(email, body.password)
    if not user:
        log.info("console login refused for %s: %s", email, reason)
        raise HTTPException(status_code=401, detail=pick(
            "邮箱或密码不正确 / that email and password do not match", lang))
    if not is_admin_identity(user):
        # 403 rather than 401: the credentials were right, and saying so is what makes
        # "I can log into the app but not the console" answerable without a support call.
        # Same predicate as the request gate — see core/access.py.
        raise HTTPException(status_code=403, detail=pick(
            "只有超级管理员可以登录管理后台 / only a super administrator can sign in to "
            "the admin console", lang))
    access.clear_login_throttle(email, access.client_ip(request))
    access.set_session_cookie(response, issue_session(user, ADMIN_AUDIENCE), request)
    return {"ok": True, "user": {k: v for k, v in user.items()
                                 if k not in ("password_hash", "password_hash_enc")}}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(access.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    """Who the console thinks you are, for the SPA's gate.

    401 when there is no operator session — the console shows its own login screen rather
    than an empty shell. It deliberately does NOT return the main app's `/api/auth/me`
    shape, because the console's SPA is a separate program (P5) and a shared response
    contract between two SPAs is a contract that will drift.
    """
    user = access.current_operator(request)
    if not user:
        raise HTTPException(status_code=401, detail=pick(
            "未登录 / not authenticated", request_lang(request) if request else None))
    from klado_shared.identity import is_admin_identity
    if not is_admin_identity(user):
        raise HTTPException(status_code=403, detail=pick(
            "需要管理员权限 / admin only", request_lang(request) if request else None))
    return {
        "ok": True,
        "user": {k: v for k, v in user.items()
                 if k not in ("password_hash", "password_hash_enc")},
        "read_only": not deployment.console_login_enabled(),
        "app_base_path": settings.APP_BASE_PATH or "",
    }


@router.get("/health")
async def health():
    """Unauthenticated liveness, for the port check in `scripts/start_admin.sh`.

    ⚠️ Reports only what a port check needs. The `read_only` flag is here rather than in
    `/me` because a deploy script has no session, and a script that cannot tell "the
    console is up but refusing writes" from "the console is down" will report the wrong
    thing during exactly the incident it was written for.
    """
    return {"ok": True, "service": "klado-admin-console",
            "read_only": not deployment.console_login_enabled()}
