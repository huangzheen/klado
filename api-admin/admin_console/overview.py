"""The overview card: five numbers and the two queues that need a human.

⚠️ Every count here comes from its own authoritative row (`orgs.org_totals()`,
`lifecycle.list_pending()`), not from summing the lists the other pages render. Summing
truncated lists is how a dashboard ends up disagreeing with the page it links to, and the
person who notices is always the one who has just been asked a question in a meeting.

`recycle_bin` in particular is asked for as "how many accounts are due to be erased
today", which is a *point-in-time* fact. It is not the number of closed accounts: a
closed account with 29 days left is not going anywhere, and counting it would make every
number on this page look like an emergency.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from admin_console import access
from klado_shared import accounts, deployment, lifecycle, orgs

router = APIRouter(prefix="/api/admin-console", tags=["总览"])


@router.get("/overview")
async def overview(request: Request):
    """Read-only. Deliberately reachable while the console is in read-only mode — an
    operator investigating an incident needs the numbers most when they cannot change
    anything."""
    access.require_operator(request)
    totals = orgs.org_totals()
    pending = lifecycle.list_pending(include_expired=False)
    due_now = [row for row in pending if (row.get("days_left") or 0) <= 0]
    approvals = orgs.list_approvals(org_id=None, status="pending")
    return {
        "orgs": {
            "enterprises": totals["enterprises"],
            "personal_users": totals["personal_users"],
            "suspended": totals["suspended_orgs"],
        },
        "members": {
            "total": totals["members_total"],
            "active": totals["members_active"],
            "invited": totals["members_invited"],
        },
        "recycle_bin": {
            "waiting": len(pending),
            "due_now": len(due_now),
            "earliest_purge": min((r.get("purge_after") for r in pending
                                   if r.get("purge_after")), default=None),
        },
        "approvals": {
            "pending": len(approvals),
            "by_org": sorted({a.get("org_name") or "" for a in approvals} - {""}),
        },
        "accounts": {
            "total": len(accounts.list_users("", 1000)),
        },
        "policy": {
            "blocked_email_domains": deployment.blocked_email_domains(),
            "read_only": not deployment.console_login_enabled(),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
