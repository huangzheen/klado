"""Personal users, the registration blocklist, and the one switch that governs both.

A "personal user" is somebody who registered themselves on an address that matches no
company's email suffix. They get a one-person organization (`kind='personal'`) so that
every account in the installation is reachable through the same model — no second code path
for "people who are not in a company".

⚠️ **The blocklist is a seat-and-compliance control, not a security boundary.** It stops a
company employee opening a second, unmanaged identity on a public mailbox. It stops
nothing about data leaving: a pull, a download and a share are data rights, not account
states, and `orgs.py`'s share gate is what governs those. The design (docs §7) says this
in the same words, and this module repeats it on the read endpoint so an operator reading
the JSON — not the design document — sees it too.

⚠️ **A blocked domain does not close an account that already exists.** It is checked at
`register/complete` in the main app and nowhere else. Adding a domain to the list
therefore stops the *next* registration and leaves current personal users alone. That is
the intended behaviour and it is also the one people are surprised by, so the list endpoint
returns, for each blocked domain, how many personal accounts already sit on it — the
number that turns "did this work?" into something answerable.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from admin_console import access
from klado_shared import deployment, orgs
from klado_shared.i18n import pick, request_lang

router = APIRouter(prefix="/api/admin-console/personal", tags=["个人用户"])


@router.get("")
async def list_personal(request: Request, limit: int = 500):
    access.require_operator(request)
    return {"users": orgs.list_personal_members(limit=limit)}


class BlocklistIn(BaseModel):
    #: Raw strings, cleaned by the shared validator. Not `list[str]` with a stricter type
    #: here, because the input is a textarea full of things like `@gmail.com` and a
    #: leading space, and rejecting the whole save for one untidy line is a worse
    #: experience than dropping that line and saying which one.
    domains: list = Field(default_factory=list)


@router.get("/blocked-domains")
async def get_blocklist(request: Request):
    """The list, plus how many personal accounts already sit on each entry.

    ⚠️ The counts are the reason this is more than a string list. Without them an operator
    who has just blocked `gmail.com` cannot tell a working blocklist from one that only
    looks like it, because in both cases the page renders identically and no existing
    account has changed.
    """
    access.require_operator(request)
    domains = deployment.blocked_email_domains()
    users = orgs.list_personal_members(limit=2000)
    counts: dict[str, int] = {d: 0 for d in domains}
    for row in users:
        domain = (row.get("email") or "").rsplit("@", 1)[-1].lower()
        if domain in counts:
            counts[domain] += 1
    return {
        "domains": domains,
        "counts": counts,
        "default_domains": list(deployment.DEFAULT_BLOCKED_DOMAINS),
        "warning": pick(
            "这不是安全边界：它只挡住「用公共邮箱另开一个个人身份」，挡不住任何数据外流"
            "（拉取 / 下载是数据权利，不看账号状态）/ Not a security boundary: it stops a "
            "second unmanaged identity on a public mailbox, and stops nothing about data "
            "leaving — a pull or a download is a data right, not an account state",
            request_lang(request)),
    }


@router.put("/blocked-domains")
async def set_blocklist(body: BlocklistIn, request: Request):
    actor = access.require_write(request)
    stored = deployment.set_blocked_email_domains(body.domains,
                                                 actor=actor.get("email") or "")
    _log(actor, "policy.blocked_domains", f"{len(stored)} domains")
    return {"domains": stored}


@router.post("/domains/{domain}/check")
async def check_domain(domain: str, request: Request, email: Optional[str] = None):
    """Would this address be refused? A read, so it works in read-only mode.

    ⚠️ The answer is about REGISTRATION only. A 200 here does not mean the person is in a
    company, and a 409 does not mean their account is disabled — it means a new
    registration for that domain would be turned into "contact your administrator"
    instead of a personal account.
    """
    access.require_operator(request)
    address = (email or domain or "").strip().lower()
    if "@" in address:
        address = address.rsplit("@", 1)[1]
    blocked = deployment.is_blocked_domain(address)
    matched = orgs.matching_org_for_email(f"probe@{address}") if address else None
    return {
        "domain": address,
        "blocked": blocked,
        "would_match_org": (matched or {}).get("name"),
        "reason": pick(
            "该域名在黑名单里，自助注册会被拒绝 / this domain is on the blocklist, so "
            "self-registration is refused"
            if blocked else
            "该域名可以自助注册成个人用户 / this domain may self-register as a personal "
            "user",
            request_lang(request)),
    }


class DisableIn(BaseModel):
    reason: str = ""


@router.post("/{member_id}/disable")
async def disable_personal(member_id: int, body: DisableIn, request: Request):
    """Take a self-registered person out of the installation — without closing their
    account.

    ⚠️ Deliberately *not* a close. Closing belongs to the recycle bin and the 30-day grace
    period, because it is about an account. This is about a membership: the address stops
    being offered as a share target and stops appearing in the roster, and the documents
    they made stay theirs. An operator who wants the account gone has a second, separately
    named button for it, because "remove this person from the company" and "destroy
    everything they ever made" must never be one click.
    """
    actor = access.require_write(request)
    from klado_shared import orgs as _orgs
    try:
        member = _orgs.disable_member(member_id, expected_org_id=None)
    except _orgs.OrgError as exc:
        status = 404 if isinstance(exc, _orgs.MemberScopeError) else 400
        raise HTTPException(status_code=status,
                            detail=pick(str(exc), request_lang(request))) from exc
    _log(actor, "personal.disable", f"{member_id} {body.reason}")
    return {"member": member}


def _log(actor: dict, kind: str, detail: str = "") -> None:
    from klado_shared import accounts
    try:
        accounts.log_access(actor, kind, "CONSOLE", f"/api/admin-console/{kind}", detail)
    except Exception:  # noqa: BLE001 — never fail a committed action on its audit trail
        import logging
        logging.getLogger(__name__).warning("could not write the console audit line: %s",
                                            kind, exc_info=True)
