"""Remote user authentication through the enterprise user-info endpoint."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request

from core.config import settings


@dataclass(frozen=True)
class AuthError(Exception):
    status_code: int
    detail: str


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cached(token: str) -> dict[str, Any] | None:
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    item = _cache.get(key)
    if not item or item[0] <= time.monotonic():
        _cache.pop(key, None)
        return None
    return item[1]


def _store(token: str, user: dict[str, Any]) -> None:
    ttl = max(settings.AUTH_CACHE_TTL_SECONDS, 0)
    if ttl:
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        _cache[key] = (time.monotonic() + ttl, user)


def _success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return False
    code = payload.get("code")
    if code is not None and str(code).upper() not in {"0", "200", "SUCCESS", "OK"}:
        return False
    return payload.get("data") not in (None, "", [], {})


def _normalise_user(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload["data"]
    if isinstance(data, list):
        data = data[0] if len(data) == 1 else None
    if not isinstance(data, dict):
        raise AuthError(401, "Authentication service did not return a user")

    subject = next((data.get(k) for k in ("id", "userId", "user_id", "account", "username", "userName", "loginName") if data.get(k)), None)
    if not subject:
        raise AuthError(401, "Authentication service returned an invalid user")
    if data.get("enabled") is False or data.get("active") is False or data.get("disabled") is True:
        raise AuthError(401, "User is disabled")
    roles = data.get("roles", data.get("role", []))
    if isinstance(roles, str):
        roles = [roles]
    elif not isinstance(roles, list):
        roles = []
    return {
        "id": str(subject),
        "username": data.get("username") or data.get("userName") or data.get("name") or str(subject),
        "display_name": data.get("displayName") or data.get("realName") or data.get("name") or data.get("username") or str(subject),
        "roles": [str(role) for role in roles],
        "organization_id": data.get("orgId") or data.get("org_id") or data.get("organizationId"),
    }


async def authenticate_token(token: str) -> dict[str, Any]:
    """Validate a caller token remotely. Tokens are never logged or persisted."""
    cached = _cached(token)
    if cached:
        return cached
    if not settings.AUTH_USER_INFO_URL or not settings.AUTH_TOOL_CODE:
        raise AuthError(503, "Authentication is enabled but AUTH_USER_INFO_URL or AUTH_TOOL_CODE is not configured")
    try:
        async with httpx.AsyncClient(timeout=settings.AUTH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                settings.AUTH_USER_INFO_URL,
                data={"toolCode": settings.AUTH_TOOL_CODE, "accessToken": token},
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError:
        raise AuthError(503, "Authentication service is unavailable") from None
    if response.status_code >= 500:
        raise AuthError(503, "Authentication service is unavailable")
    try:
        payload = response.json()
    except ValueError:
        raise AuthError(503, "Authentication service returned an invalid response") from None
    if response.status_code >= 400 or not _success(payload):
        raise AuthError(401, "Invalid or expired access token")
    user = _normalise_user(payload)
    _store(token, user)
    return user


def access_token_from_request(request: Request) -> str | None:
    """Read the host platform's cookie (`<platform>-${AUTH_TOOL_CODE}`); Bearer is for
    non-browser API clients.

    ⚠️ Only reached when `AUTH_USER_INFO_URL` + `AUTH_TOOL_CODE` are both set. Klado does
    not set them, so this branch is dormant here — the name comes from the upstream
    platform whose cookie it reads, not from this application.
    """
    if settings.AUTH_TOOL_CODE:
        cookie_token = request.cookies.get(f"aihub-{settings.AUTH_TOOL_CODE}")
        if cookie_token:
            return cookie_token
    authorization = request.headers.get("Authorization", "")
    scheme, _, bearer_token = authorization.partition(" ")
    return bearer_token.strip() if scheme.lower() == "bearer" and bearer_token.strip() else None
