"""Signed, stateless session tokens — with an audience.

Klado has no `sessions` table. A login returns `uid.expiry.signature` and every request
recomputes the HMAC, which is what lets the admin console be its own process without a
shared session store: it verifies the same signature against the same `SECRET_KEY`.

Why the audience exists
-----------------------
⚠️ **Cookies are not partitioned by port.** A cookie set on `127.0.0.1:8000` is sent to
`127.0.0.1:8787` as well, because the cookie jar matches on host and ignores the port. So
two processes that share a machine *will* clobber each other's login if they use the same
cookie name — you log into the console, go back to the app, and the console has logged
you out.

The fix is two halves and both are load-bearing:

1. **Distinct cookie names** — `klado_session` (app) vs `klado_admin_session` (console).
   Naming is the main app's business (`SESSION_COOKIE` in `routers/auth.py`); the
   console's is its own.
2. **An audience bound into the signature.** The token *string* is unchanged — still
   `uid.expiry.signature`, so `count(".") != 2` still validates it and no parsing
   changes. Only the signature's input grows an audience tag. That is what stops a
   console token from being replayed against the main app, where it would reach the
   operator endpoints.

Migration
---------
Tokens issued before this change were signed over `uid.expiry` with no tag. The main
app still accepts those (`allow_legacy=True`), so nobody is logged out. The console
never does (`allow_legacy=False`): an untagged token has no audience, so accepting it
there would be exactly the hole the tag exists to close. Retire the main app's legacy
branch once real sessions have cycled.

⚠️ `allow_legacy` is a *request*, not a grant. `LEGACY_TOLERANT_AUDIENCES` below is what
actually decides, and the console is not in it. This is not defensive coding for its own
sake: the first version of this function appended the untagged signature as a candidate
for whatever audience it was handed, so `session_user_id(old_token, ADMIN_AUDIENCE,
allow_legacy=True)` returned a user id — any pre-upgrade session could walk straight
into the operator console, which is precisely what the audience is supposed to prevent.
The flag now means "this caller opts in"; the set means "this process is allowed to".
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from klado_shared.config import settings

# The two audiences. Named constants rather than bare strings at the call sites, so
# "app" and "admin" cannot be swapped by a typo that still parses.
APP_AUDIENCE = "app"
ADMIN_AUDIENCE = "admin"

#: Audiences permitted to accept a pre-audience (untagged) token. The main app only.
#:
#: ⚠️ The console is deliberately absent. An untagged token carries no audience, so
#: there is nothing to check it against; honouring one there would hand a pre-upgrade
#: session the operator console's authority. A new audience added later must decide
#: about itself here rather than inherit the caller's `allow_legacy`.
LEGACY_TOLERANT_AUDIENCES = frozenset({APP_AUDIENCE})


def _sign(raw: bytes, audience: str | None = None) -> str:
    """HMAC-SHA256, truncated to 32 hex chars. `audience=None` reproduces the legacy
    signature (signed over the bare payload) — that is what `allow_legacy` matches."""
    key = (settings.SECRET_KEY or "change-me").encode("utf-8")
    if audience:
        raw = raw + b"." + audience.encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:32]


def _session_ttl() -> int:
    return int(settings.AUTH_SESSION_TTL_SECONDS or 30 * 24 * 3600)


def issue_session(user: dict, audience: str = APP_AUDIENCE) -> str:
    """`uid.expiry.signature` — no server-side session table needed.

    `user` only needs an `id`. `audience` picks which process will accept the result.
    """
    expiry = int(time.time()) + _session_ttl()
    payload = f"{user['id']}.{expiry}"
    return f"{payload}.{_sign(payload.encode(), audience)}"


def session_user_id(token: str, audience: str = APP_AUDIENCE,
                    allow_legacy: bool = False) -> Optional[int]:
    """Verify a session token; returns the user id, or None.

    ⚠️ An expired token is indistinguishable from a forged one here, and both return
    None. That is deliberate: the caller only ever learns "no session", and the
    signature is checked before the expiry so a wrong key cannot be timed out into
    revealing which half was wrong.

    `allow_legacy` only takes effect for an audience in `LEGACY_TOLERANT_AUDIENCES` —
    see that constant's comment. Passing it for the console audience is ignored, by
    design, not by omission.
    """
    # `isinstance` rather than duck typing: a non-string here would raise on `.count`,
    # and this runs on the request path where an exception is a 500. Cookies are always
    # strings, so no caller should reach this — but a token is untrusted input and the
    # rest of this function is total, which is worth more than the micro-optimisation.
    if not isinstance(token, str) or not token or token.count(".") != 2:
        return None
    uid, expiry, signature = token.split(".")
    if not (uid.isdigit() and expiry.isdigit()):
        return None
    payload = f"{uid}.{expiry}".encode()
    candidates = [audience]
    if allow_legacy and audience in LEGACY_TOLERANT_AUDIENCES:
        candidates.append(None)
    matched = False
    for candidate in candidates:
        if hmac.compare_digest(_sign(payload, candidate), signature):
            matched = True
            break
    if not matched:
        return None
    if int(expiry) < time.time():
        return None
    return int(uid)
