"""Who is an operator?

The single question, asked by both processes, in one place.

`is_admin_identity()` answers *"is this person a super administrator?"* — the platform
level, above every organization. It deliberately does **not** know about enterprise admins
(`org_members.org_role`); those are a different, narrower thing with its own predicates
in `orgs.py`, so that this function keeps meaning exactly one thing and the main app's
existing `core/access.py` policy can keep calling it unchanged.

Why the platform SSO fallback looks odd
---------------------------------------
Local auth stores a `role` column, so that is authoritative — demoting somebody in the
admin page must actually take effect, which is why `role` wins outright rather than
being OR-ed with the configured list. The `AUTH_ADMIN_EMAILS` fallback exists for the
upstream SSO path, which carries no role information at all. That path is not in use by
this installation; the branch is kept because the code path still exists.
"""
from __future__ import annotations

from klado_shared.config import settings


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in (settings.AUTH_ADMIN_EMAILS or "").split(",") if e.strip()}


def is_admin_identity(user: dict | None) -> bool:
    """Operator? `role` wins when the store has one; otherwise the configured list."""
    if not user:
        return False
    role = user.get("role")
    if role:
        return str(role).strip().lower() == "admin"
    email = str(user.get("email") or user.get("username") or "").strip().lower()
    return bool(email) and email in admin_emails()
