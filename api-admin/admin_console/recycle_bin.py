"""The recycle bin, and the two operations that act on it.

⚠️ **Erasing is irreversible and this is the only place in the product that can do it.**
An enterprise administrator has no route here at all — not in the console, not in the
main app. That asymmetry is the design's decision (docs §6.2): close and restore belong to
a company that can undo them, and "destroy everything this person ever made" belongs to
somebody outside every company.

Two things this module deliberately does **not** do:

* **It does not delete `mail_settings`.** That table is a single deployment-level row
  (`id = 1`) with no `owner_email`, so "delete everything this person owns" cannot
  address it — the cascade would either skip it or crash. Same reasoning excludes
  `ai_knowledge_documents` (a derived table with no owner) and `access_log` (an audit
  trail; closing somebody nulls their `user_id` and leaves the rows). The list of what a
  purge touches is data-driven in `klado_shared/lifecycle.py` for exactly this reason: a
  hand-written list is a list that quietly stops when somebody adds a module.

* **It does not disable the account.** `disabled` is the operator's own switch, and
  `restore()` must not re-enable somebody the operator had separately turned off. Closing
  writes `deleted_at` / `purge_after` / `deleted_by` and nothing else; the login gate reads
  `deleted_at` (see `main.py::_local_identity` and `accounts.resolve_agent_token`).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from admin_console import access
from klado_shared import accounts, lifecycle
from klado_shared.i18n import pick, request_lang

router = APIRouter(prefix="/api/admin-console", tags=["回收站"])


@router.get("/recycle-bin")
async def recycle_bin(request: Request, include_expired: bool = Query(True)):
    """Accounts waiting to be erased, with the days each one has left.

    `days_left` is **ceiled** and **materialised on `purge_after`** rather than computed at
    read time. Both halves matter: 29 hours left has to read as "2 days" or an operator
    clears the last account the company had on the strength of a number that will be
    different tomorrow, and computing it at read time would let a change to
    `GRACE_DAYS` silently move a deadline that has already been shown to somebody.
    """
    access.require_operator(request)
    rows = lifecycle.list_pending(include_expired=include_expired)
    return {"accounts": rows, "grace_days": lifecycle.GRACE_DAYS}


@router.post("/recycle-bin/sweep")
async def sweep(request: Request):
    """Erase everything past its deadline, now.

    ⚠️ This is irreversible and needs no per-account confirmation — by construction every
    row it touches is already past the deadline the operator was shown. It exists because
    the nightly sweep is a background job, and an operator who has just discovered a
    problem should not have to wait for it.
    """
    actor = access.require_write(request)
    results = lifecycle.sweep()
    return {"results": results, "count": len(results)}


@router.get("/recycle-bin/{user_id}/report")
async def pending_purge_report(user_id: int, request: Request):
    """What erasing this account *would* remove, without removing it.

    ⚠️ A dry run, and the only honest way to show somebody the blast radius of an
    irreversible act. It shares its cascade list with the real purge (both call
    `lifecycle.dependents()`), so the preview cannot drift from the action — a preview
    built from a second list is a preview that is wrong exactly when it matters.
    """
    access.require_operator(request)
    target = accounts.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail=pick(
            "用户不存在 / user not found", request_lang(request)))
    if not target.get("deleted_at"):
        raise HTTPException(status_code=400, detail=pick(
            "该账号未被删除，无法预演清除 / this account is not pending deletion, so there "
            "is nothing to preview", request_lang(request)))
    try:
        return {"user": lifecycle._deleted_view(target),
                "would_remove": lifecycle.dependents(user_id)}
    except lifecycle.LifecycleError as exc:
        raise HTTPException(status_code=404, detail=pick(str(exc), request_lang(request)))


@router.delete("/recycle-bin/{user_id}")
async def purge_now(user_id: int, request: Request, confirm: str = Query("")):
    """Erase one account immediately, without waiting for its deadline.

    ⚠️ The confirmation is the account's email address, in full, and it is required even
    for a row that is already in the bin. The grace period is a safety net for accidents,
    not a licence to skip the check on purpose: this button is how somebody destroys a
    colleague's history while trying to tidy up a list.
    """
    actor = access.require_write(request)
    lang = request_lang(request)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail=pick(
            "不能删除自己 / cannot delete yourself", lang))
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


@router.get("/activity")
async def activity(request: Request, user_id: int = Query(None), kind: str = Query(""),
                   limit: int = Query(200, ge=1, le=1000)):
    """The access log: who did what, newest first.

    ⚠️ Console actions are written here too, with method `CONSOLE` rather than an HTTP verb
    — so a line in this list cannot be traced back to which of the two doors it came
    through by reading the method, only by reading the path. That is enough to tell them
    apart and avoids inventing a verb that no other log consumer would understand.
    """
    access.require_operator(request)
    return {"activity": accounts.activity(user_id, kind, limit)}
