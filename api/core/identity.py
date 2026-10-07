"""Who is calling? — the one shared identity resolver for API routers.

`current_identity()` has the same semantics as `routers/reports.py::_identity()`:

* the auth middleware puts the caller on `request.state.current_user` (and its
  presentation — cookie vs Basic/Bearer — on `request.state.auth_kind`);
* with `AUTH_ENABLED` off there is no middleware at all, so a local,
  single-machine deployment falls back to the first `AUTH_ADMIN_EMAILS` entry
  (`legacy_owner()`);
* when even that is unset there is no implicit identity, and the request gets a
  plain 401.

Kept in `core/` rather than imported from the report router so that Data Center
stamps its rows with **exactly** the address Workspace treats as the legacy
owner — a second, drifting fallback is how two halves of the app start disagreeing
about who "the local user" is.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from core.access import admin_emails, is_admin_identity
from core.config import settings
from core.i18n import pick, request_lang


def legacy_owner() -> str:
    """The identity adopted by rows that predate authenticated ownership.

    Sourced from `AUTH_ADMIN_EMAILS` (first entry), which is empty by default:
    this repository names no administrator. Returns "" when none is configured —
    callers must treat that as "no implicit identity", not as a real address.
    """
    return (settings.AUTH_ADMIN_EMAILS or "").split(",")[0].strip().lower()


def current_identity(request: Request) -> tuple[str, str]:
    """Return `(email, auth_kind)` for the caller of `request`, or raise 401."""
    user = getattr(request.state, "current_user", None) or {}
    email = str(user.get("email") or user.get("username") or "").strip().lower()
    if not email and not settings.AUTH_ENABLED:
        # Local single-user development mode has no login middleware.
        email = legacy_owner()
    if not email:
        raise HTTPException(
            status_code=401,
            detail=pick("请先登录 / sign in first", request_lang(request)))
    return email, getattr(request.state, "auth_kind", "browser") or "browser"


def is_admin_caller(request: Request) -> bool:
    """True when the resolved caller is an operator.

    Two sources, because the local fallback has no `current_user` dict to read a
    `role` from: the store's `role` when there is a user, otherwise the resolved
    address against `AUTH_ADMIN_EMAILS`.
    """
    if is_admin_identity(getattr(request.state, "current_user", None)):
        return True
    try:
        email, _kind = current_identity(request)
    except HTTPException:
        return False
    return email in admin_emails()