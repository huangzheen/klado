"""The operator gate for the admin console.

Every request that matters passes through one of the two functions here, and they answer
one question in one place. That is the whole point of the module: this is the second
process that has to decide "is this person allowed to see every account in the
installation", and a second copy of that decision is a second answer.

`require_operator` — who are you?
---------------------------------
1. **A valid console session.** The token must verify against the `admin` audience
   (`klado_shared/session.py`). A main-app token carries the `app` audience and is
   rejected by the signature, not by a check here — which is the point of the audience.
   An untagged pre-upgrade token is rejected too: it has no audience to check.
2. **`role = 'admin'`.** `is_admin_identity()` is the same predicate the main app's
   middleware uses, imported rather than copied.

`require_write` — may you change anything?
------------------------------------------
Adds the third condition: `auth.admin_audience_enabled`.

⚠️ **It is a read-only switch, not a wall, and that distinction is load-bearing.** An
earlier reading of "turn the console off" made `require_operator` itself check the switch,
which is a lockout: the operator who suspects somebody is in the console is exactly the
person who most needs to use it, and the switch would have taken that away from them. So
the switch lives in the *write* gate. With it off, the console still reads — every account,
every company, the audit trail — and changes nothing. That is what somebody flipping it
at 2am actually wants: to stop the bleeding without losing the view of what is bleeding.

⚠️ One write is exempt, and it is the switch itself. Otherwise turning it off would be
irreversible without editing the database by hand, which is the failure mode of every
kill switch that has no "unlock" door. `require_write(..., exempt_switch=True)` is the
door, and it is only ever passed by `routers/system.py`'s single endpoint.

Ordering matters and is the interesting part: no session is 401 whether or not the switch
is on, and a signed-in non-operator is 403 whether or not it is on. The switch revokes
*authority*; it must never become a way to learn whether an address has an account.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import HTTPException, Request, Response

from klado_shared import deployment
from klado_shared.config import settings
from klado_shared.i18n import pick, request_lang
from klado_shared.identity import is_admin_identity
from klado_shared.session import ADMIN_AUDIENCE, session_user_id

#: Cookie name. ⚠️ MUST differ from the main app's `klado_session`: cookies are matched
#: on host, not on port, so a shared name means logging into one process signs you out of
#: the other. The name is the convenience; the audience is the control.
SESSION_COOKIE = settings.KLADO_ADMIN_SESSION_COOKIE or "klado_admin_session"

# ── login throttling ─────────────────────────────────────────────────────────
# ⚠️ Two dimensions, not one, and both are needed. `ip` is what stops one host grinding
# through a list; `email` is what stops a distributed attempt against one account, which an
# IP limit cannot see. The console is the most valuable credential in the installation —
# it can read and close every account — so this is deliberately harsher than the main
# app's 12-per-5-minutes, and it is in-process like the main app's: a single replica, so a
# dict is enough. The goal is to make guessing unattractive, not to be a WAF.
_WINDOW_SECONDS = 300
_MAX_PER_WINDOW_IP = 8
_MAX_PER_WINDOW_EMAIL = 6
_hits: dict[str, list[float]] = {}


def _throttle(bucket: str, key: str, limit: int, lang: Optional[str]) -> None:
    now = time.time()
    recent = [t for t in _hits.get(bucket, []) if now - t < _WINDOW_SECONDS]
    if len(recent) >= limit:
        raise HTTPException(status_code=429, detail=pick(
            "尝试次数过多，请稍后再试 / too many attempts, try again later", lang))
    recent.append(now)
    _hits[bucket] = recent


def client_ip(request: Request) -> str:
    """The one place the caller's address is derived.

    ⚠️ Shared with `clear_login_throttle` on purpose. Two copies of `x-forwarded-for`
    parsing is two copies that can disagree — and when they do, the login clears a bucket
    keyed on one address while the limiter counted another, so the "successful login resets
    the counter" rule silently does not fire and nobody can say why.
    """
    if request is None:
        return ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def throttle_login(email: str, request: Request) -> None:
    """Both buckets, email first — the stricter limit is the one that should report."""
    lang = request_lang(request) if request else None
    address = (email or "").strip().lower()
    _throttle(f"email:{address}", address, _MAX_PER_WINDOW_EMAIL, lang)
    _throttle("ip:" + client_ip(request), client_ip(request), _MAX_PER_WINDOW_IP, lang)


def clear_login_throttle(email: str = "", ip: str = "") -> None:
    """Forget the counters for one address — called on a *successful* login.

    ⚠️ **Both** buckets, and that is a change made because of a real interaction. Only the
    email bucket used to be cleared, on the grounds that the IP limit is there to stop one
    host grinding through a *list*. But the IP bucket is also what a legitimate operator
    runs into: the counters are in-process, so anything that deliberately trips the
    limiter — the burst in `verify_admin_console_http.py`, a password-sprayer, somebody's
    own failed attempts — leaves the console refusing **every** login from that host for
    five minutes, including the correct one. In practice that showed up as the screenshot
    script failing to sign in with a bare 429 twenty seconds after a verification run,
    and nothing in either tool's output said why.

    A successful login is evidence that the person is not guessing, which is the only thing
    the IP limit is trying to establish. The exposure of clearing it is unchanged in kind:
    somebody who already holds one valid password can keep the bucket empty, and they gain
    nothing they did not have.
    """
    address = (email or "").strip().lower()
    if address:
        _hits.pop(f"email:{address}", None)
    if ip:
        _hits.pop(f"ip:{ip}", None)


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    """Write the console's own cookie. Mirrors the main app's, including the `secure`
    detection, so both processes behave the same behind a TLS-terminating proxy."""
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    secure = forwarded_proto == "https" or request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(settings.AUTH_SESSION_TTL_SECONDS or 30 * 24 * 3600),
        httponly=True, samesite="lax", secure=secure, path="/",
    )


def current_operator(request: Request) -> Optional[dict]:
    """The signed-in operator, or None. Never raises — callers decide what None means.

    ⚠️ Returns None for a *missing* cookie the same way it returns None for a forged one.
    Deliberate: a caller must not be able to tell "no cookie" from "forged cookie" by
    message or by timing, because that difference is a credential oracle.
    """
    from klado_shared import accounts

    token = request.cookies.get(SESSION_COOKIE, "")
    user_id = session_user_id(token, ADMIN_AUDIENCE, allow_legacy=False)
    if user_id is None:
        return None
    return accounts.get_user_by_id(user_id)


def require_operator(request: Request) -> dict:
    """The operator for this request, or 401/403. Read paths stop here."""
    lang = request_lang(request) if request else None
    user = current_operator(request)
    if not user:
        raise HTTPException(status_code=401, detail=pick(
            "未登录 / not authenticated", lang))
    if not is_admin_identity(user):
        raise HTTPException(status_code=403, detail=pick(
            "需要管理员权限 / admin only", lang))
    return user


def require_write(request: Request, exempt_switch: bool = False) -> dict:
    """`require_operator` plus the read-only switch. Every mutating route calls this.

    `exempt_switch=True` is the unlock door — see the module docstring. It is passed by
    exactly one endpoint, and the audit test in `api/tests/test_shared_layer.py` pins the
    list of call sites so "one endpoint" stays true.
    """
    user = require_operator(request)
    if exempt_switch:
        return user
    if not deployment.console_login_enabled():
        raise HTTPException(status_code=403, detail=pick(
            "管理后台当前为只读：登录开关已关闭，可在「系统」页重新开启 / the console is "
            "read-only right now: the login switch is off, and can be turned back on "
            "from the System page", request_lang(request)))
    return user
