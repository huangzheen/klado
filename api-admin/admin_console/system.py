"""The module allowlist, and the accounts page the console took over from `settings/admin`.

Three groups of endpoints, and the reason they are in this process rather than the main
app's admin page is the same for all of them: they are **platform-level** concerns. An
enterprise administrator may narrow a *member's* modules; only an operator may change what
this installation offers at all.

**`/modules`** — the deployment default. `KLADO_MODULES` in `.env` seeds it on a fresh
install; once written here, the table is the answer and the environment variable is only
the initial value. That is a real behavioural difference from every other setting in this
project, so it is spelled out in the response: `source` tells the page which one is in
force, because an operator who edits the list and sees no change has almost certainly
edited the wrong one.

⚠️ Narrowing the deployment list does **not** revoke anything already handed out, and it
does not close a module for an account that has an explicit `account_modules` row saying
otherwise — well, it does not: `klado_shared/modules.py::resolve()` intersects the two, so
an account can only ever be narrowed *below* the deployment default. Lowering the default
therefore *does* close modules for everybody without an override, and that is a real
consequence worth saying out loud before somebody does it at 5pm on a Friday.

**`/accounts`** — the operator's view of every account, plus the destructive acts
(close / restore / purge). These are the same calls the main app made; they are here
because the console is the whole-installation view, and leaving "close any account" in
`settings/admin` next to a per-company member list is how somebody ends up purging a
colleague while trying to remove them from a roster.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from admin_console import access
from klado_shared import accounts, deployment, lifecycle, mail_config, modules
from klado_shared.config import settings
from klado_shared.i18n import pick, request_lang

router = APIRouter(prefix="/api/admin-console", tags=["模块与账号"])

MIN_PASSWORD_LEN = 8


# ── the console's own switches ───────────────────────────────────────────────

@router.get("/system")
async def system_state(request: Request):
    """Everything about this process that an operator would otherwise have to guess.

    ⚠️ `read_only` is here as well as on `/me` and `/health` because it is the answer to
    "why did my button do nothing", and the three endpoints serve three different
    questions: a session, a port check, and a person about to press something. All three
    need it, and a fourth copy in the response body of whichever write failed would be the
    worst place for it.
    """
    user = access.require_operator(request)
    return {
        "version": settings.APP_VERSION,
        "admin_port": settings.KLADO_ADMIN_PORT,
        "read_only": not deployment.console_login_enabled(),
        "actor": {"email": user.get("email"), "role": user.get("role"),
                  "id": user.get("id")},
        "settings_keys": {
            "blocked_email_domains": len(deployment.blocked_email_domains()),
            "console_login": deployment.console_login_enabled(),
            "modules_source": ("table" if deployment.read(
                modules.DEPLOYMENT_DEFAULT_KEY) is not None
                else ("env" if (settings.KLADO_MODULES or "").strip() else "registry")),
        },
    }


class LoginSwitch(BaseModel):
    enabled: bool
    #: Required only to turn it back ON. Turning the console read-only is the action
    #: somebody takes *because* they are worried; requiring a typed address to undo it
    #: means the undo takes a deliberate second, which is the point — and it does not
    #: stop a determined attacker, which is not what it is for. Turning it OFF is never
    #: gated, because that is the action that must always be available.
    confirm_email: Optional[str] = None


@router.put("/system/console-login")
async def set_console_login(body: LoginSwitch, request: Request):
    """The read-only switch. **The one route allowed to run while it is off.**

    ⚠️ `exempt_switch=True` is the whole reason this module's docstring has a paragraph
    about it. Without the exemption, flipping the switch off would be a one-way door: the
    operator who suspects a breach turns it off, and then cannot turn it back on without
    editing the database by hand — which is how a kill switch becomes an outage. One
    route, one exemption, audited below.
    """
    actor = access.require_write(request, exempt_switch=True)
    lang = request_lang(request)
    if body.enabled:
        typed = (body.confirm_email or "").strip().lower()
        if typed != (actor.get("email") or "").strip().lower():
            raise HTTPException(status_code=400, detail=pick(
                "请输入自己的邮箱以确认重新开启 / type your own email address to confirm "
                "turning the console back on", lang))
    now = deployment.set_console_login_enabled(body.enabled,
                                              actor=actor.get("email") or "")
    _log(actor, "console.login_switch", "on" if now else "off")
    return {"read_only": not now, "console_login": now}


# ── modules ──────────────────────────────────────────────────────────────────

@router.get("/modules")
async def get_modules(request: Request):
    """The registry, the deployment default, and where the default came from.

    ⚠️ `source` is the field that matters. It is `env` when `KLADO_MODULES` is set and
    unset in the table, `table` once somebody has saved here, and `registry` when the
    deployment has expressed no opinion at all (every module on). An operator who edits
    the list and sees nothing happen was editing the wrong one — this is the answer to
    that, returned on every read so it cannot go stale.
    """
    access.require_operator(request)
    catalogue = modules.catalogue()
    default = modules.deployment_default()
    from_table = deployment.read("modules.deployment_default", None)
    if isinstance(from_table, dict) and isinstance(from_table.get("keys"), list):
        source = "table"
        stored = [k for k in from_table["keys"] if k in modules.BY_KEY]
    elif (settings.KLADO_MODULES or "").strip():
        source = "env"
        stored = list(default)
    else:
        source = "registry"
        stored = list(default)
    return {
        "modules": catalogue,
        "deployment_default": stored,
        "source": source,
        "effective": list(modules.resolve({})),
        "env_hint": (settings.KLADO_MODULES or "").strip(),
    }


class ModulesIn(BaseModel):
    keys: list = []


@router.put("/modules")
async def set_modules(body: ModulesIn, request: Request):
    """Replace the deployment default.

    ⚠️ Refuses an unknown key rather than ignoring it. `KLADO_MODULES` naming a module
    that does not exist is a startup error in the main app for exactly this reason: a
    silently dropped key reads as "that module is now off", which is a different and much
    worse belief than "that was a typo".
    """
    actor = access.require_write(request)
    unknown = [k for k in body.keys if k not in modules.BY_KEY]
    if unknown:
        raise HTTPException(status_code=400, detail=pick(
            f"未知的模块：{'、'.join(unknown)}（可用：{'、'.join(modules.BY_KEY)}）/ unknown "
            f"module(s): {', '.join(unknown)}; available: {', '.join(modules.BY_KEY)}",
            request_lang(request)))
    deployment.ensure_table()
    deployment.write("modules.deployment_default", {"keys": list(body.keys)},
                     actor=actor.get("email") or "")
    return {"deployment_default": list(body.keys), "source": "table",
            "effective": list(modules.resolve({}))}


# ── accounts ─────────────────────────────────────────────────────────────────

@router.get("/accounts")
async def list_accounts(request: Request, q: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
    access.require_operator(request)
    return {"users": accounts.list_users(q, limit)}


class AccountPatch(BaseModel):
    disabled: Optional[bool] = None
    role: Optional[str] = None
    display_name: Optional[str] = None
    password: Optional[str] = None


@router.patch("/accounts/{user_id}")
async def patch_account(user_id: int, body: AccountPatch, request: Request):
    """Change one account. Refuses to disable or demote the caller.

    ⚠️ The self-check is here and not in the shared store because it is about the *HTTP
    session*, which the store knows nothing about. It is duplicated from the main app's
    endpoint on purpose: the console and the app are two doors into the same table, and a
    rule that only one of them enforces is a rule that gets bypassed by using the other.
    """
    actor = access.require_write(request)
    lang = request_lang(request)
    target = accounts.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if target["id"] == actor["id"] and (body.disabled
                                        or (body.role and body.role != "admin")):
        raise HTTPException(status_code=400, detail=pick(
            "不能禁用或降级自己 / cannot disable or demote yourself", lang))
    if body.password is not None:
        if len(body.password) < MIN_PASSWORD_LEN:
            raise HTTPException(status_code=400, detail=pick(
                f"密码至少 {MIN_PASSWORD_LEN} 位 / password too short", lang))
        accounts.set_password(user_id, body.password)
    updated = accounts.update_user(user_id, body.disabled, body.role, body.display_name)
    return {"ok": True, "user": accounts._public(updated)}


@router.get("/accounts/{user_id}/impact")
async def account_impact(user_id: int, request: Request):
    """What closing this account would take with it. Read-only, never mutating.

    ⚠️ Read-only is the point. An operator has to be able to look at the blast radius,
    close the tab, and come back without having started anything — so this must not share
    a code path with the close itself.
    """
    access.require_operator(request)
    if not accounts.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail=pick(
            "用户不存在 / user not found", request_lang(request)))
    try:
        return lifecycle.dependents(user_id)
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), request_lang(request)))


@router.delete("/accounts/{user_id}")
async def close_account(user_id: int, request: Request, confirm: str = Query("")):
    """Close an account into the recycle bin. `confirm` must be the account's address.

    ⚠️ Close and purge are two routes with two buttons, never one. `DELETE` here is
    reversible for 30 days; `DELETE /purge` is not. The confirmation string is the same in
    both — the cheapest thing that reliably catches a mis-click, and it doubles as proof
    that the impact dialog the operator was shown is the one being acted on.
    """
    actor = access.require_write(request)
    lang = request_lang(request)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail=pick("不能删除自己 / cannot delete yourself", lang))
    target = accounts.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if not target.get("deleted_at") and (confirm or "").strip().lower() != \
            (target["email"] or "").strip().lower():
        raise HTTPException(status_code=400, detail=pick(
            f"请输入该账号邮箱以确认 / type the account email to confirm ({target['email']})",
            lang))
    try:
        view = lifecycle.soft_delete(user_id, actor=actor.get("email", ""))
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), lang))
    return {"ok": True, "user": view}


@router.post("/accounts/{user_id}/restore")
async def restore_account(user_id: int, request: Request):
    """Undo a close, inside the grace period. Exact: same row ids, nothing deleted."""
    actor = access.require_write(request)
    lang = request_lang(request)
    target = accounts.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if not target.get("deleted_at"):
        raise HTTPException(status_code=400, detail=pick(
            "该账号未被删除 / this account is not pending deletion", lang))
    try:
        return {"ok": True, "user": lifecycle.restore(user_id, actor=actor.get("email", ""))}
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), lang))


@router.delete("/accounts/{user_id}/purge")
async def purge_account(user_id: int, request: Request, confirm: str = Query("")):
    """Erase for good. Irreversible. **Operator only** — an enterprise administrator has
    no route to this at all, which is the design's decision (docs §6.2): purge is the one
    act that cannot be undone, and the person who can destroy a whole company's history
    should not be somebody the company appointed."""
    actor = access.require_write(request)
    lang = request_lang(request)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail=pick("不能删除自己 / cannot delete yourself", lang))
    target = accounts.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick("用户不存在 / user not found", lang))
    if (confirm or "").strip().lower() != (target["email"] or "").strip().lower():
        raise HTTPException(status_code=400, detail=pick(
            f"请输入该账号邮箱以确认 / type the account email to confirm ({target['email']})",
            lang))
    try:
        return {"ok": True, "report": lifecycle.purge_account(user_id)}
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), lang))


@router.get("/agent-codes")
async def list_agent_codes(request: Request):
    """Every issued agent access code, with its owner.

    ⚠️ These are credentials that bypass the browser entirely, so this list belongs beside
    the accounts rather than inside a per-company view: "who in this installation can act
    without logging in" is a question only the operator can answer.
    """
    access.require_operator(request)
    return {"tokens": accounts.list_agent_tokens()}


@router.delete("/agent-codes/{token_id}")
async def revoke_agent_code(token_id: int, request: Request):
    actor = access.require_write(request)
    if not accounts.revoke_agent_token(token_id, actor.get("email") or ""):
        raise HTTPException(status_code=404, detail=pick(
            "授权码不存在或已吊销 / not found or already revoked", request_lang(request)))
    return {"ok": True, "revoked": token_id}


@router.delete("/agent-codes/{token_id}/purge")
async def purge_agent_code(token_id: int, request: Request):
    """Delete a revoked code for good. 404 (not 403) while it is still live: from this
    route's point of view a live code is simply not eligible, and the message says which
    step is missing."""
    access.require_write(request)
    if not accounts.purge_agent_token(token_id):
        raise HTTPException(status_code=404, detail=pick(
            "只有已吊销的授权码才能删除（先吊销） / revoke it first", request_lang(request)))
    return {"ok": True, "purged": token_id}


@router.get("/codes")
async def pending_codes(request: Request, limit: int = Query(50, ge=1, le=200)):
    """Live registration and invitation codes, value included.

    ⚠️ The mail path is best-effort — this server talking to a mailbox provider with a
    client-specific password the provider can revoke — so a code nobody can read is a
    registration that cannot be finished. Showing the value is the fallback that keeps an
    unconfigured deployment usable at all.
    """
    access.require_operator(request)
    return {"codes": accounts.pending_codes(limit)}


@router.delete("/codes")
async def clear_codes(request: Request, email: str = Query(..., min_length=3)):
    """Retract a live code. The counterpart of "resend": cuts somebody off mid-invitation
    without waiting for its TTL. Idempotent — clearing nothing is fine."""
    access.require_write(request)
    return {"ok": True, "email": email.strip().lower(),
            "cleared": accounts.clear_codes(email)}


@router.get("/mail")
async def get_mail(request: Request):
    """Effective SMTP settings. The password is never included — `mail_config.describe()`
    omits it server-side, so no route can leak it by accident."""
    access.require_operator(request)
    return mail_config.describe()


class MailIn(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    security: Optional[str] = None
    username: Optional[str] = None
    sender: Optional[str] = None
    #: omitted → keep the stored password · "" → clear it · a string → store it
    password: Optional[str] = None


@router.put("/mail")
async def set_mail(body: MailIn, request: Request):
    """⚠️ `password=None` means KEEP, not "clear". An operator editing the port must not
    have to retype the mailbox password to do it, and a UI that sends an empty string for
    an untouched field would silently delete the credential they can no longer see."""
    actor = access.require_write(request)
    lang = request_lang(request)
    if body.security is not None and body.security.strip().lower() not in ("ssl", "starttls", "plain"):
        raise HTTPException(status_code=400, detail=pick(
            "security 只能是 ssl、starttls 或 plain / security must be ssl, starttls or "
            "plain", lang))
    if body.port is not None and not 1 <= body.port <= 65535:
        raise HTTPException(status_code=400, detail=pick("端口不合法 / invalid port", lang))
    if body.sender is not None and body.sender.strip() and "@" not in body.sender:
        raise HTTPException(status_code=400, detail=pick(
            "发信地址需要是完整邮箱地址 / sender must be a full email address", lang))
    return mail_config.save(
        host=(body.host or None), port=body.port,
        security=(body.security.strip().lower() if body.security else None),
        username=(body.username or None), sender=(body.sender or None),
        password=(mail_config.KEEP if body.password is None else body.password),
        actor=actor.get("email", ""))


@router.delete("/mail")
async def clear_mail(request: Request):
    """Forget the stored settings and fall back to the environment."""
    access.require_write(request)
    return mail_config.clear()


def _log(actor: dict, kind: str, detail: str = "") -> None:
    """Write the console's own actions to the shared access log.

    ⚠️ Best-effort. An action that has already been committed must not be reported as a
    failure because the audit insert hit a problem — the alternative is rolling back a
    policy change because the log table was busy, which trades a small audit gap for a
    change that silently did not happen. The gap shows up as a missing line in the audit
    view, which is the cheaper failure.
    """
    from klado_shared import accounts
    try:
        accounts.log_access(actor, kind, "CONSOLE", f"/api/admin-console/{kind}", detail)
    except Exception:  # noqa: BLE001 — never fail a committed action on its audit trail
        import logging
        logging.getLogger(__name__).warning("could not write the console audit line: %s",
                                            kind, exc_info=True)
