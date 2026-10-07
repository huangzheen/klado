"""
Who may do operator-level work.

`is_admin_identity()` and `requires_admin()` answer two different questions:

* **Who is an operator?** Local auth stores a `role` column, so that is authoritative
  — demoting somebody in the admin page must actually take effect. The platform SSO
  (which is not in use, but the code path exists) has no role information at all, so
  there we fall back to `AUTH_ADMIN_EMAILS`.
* **Which requests need one?** System and organization administration stay gated
  by middleware and their own scoped checks. Personal content, including Data Center
  datasets and uploaded files, is managed by its exact owner in the router. An
  administrator role never substitutes for another account's ownership.

Reads are deliberately not covered: reading data is what ordinary users do.
"""
from __future__ import annotations

import re

from core import _shared_path  # noqa: F401  — side effect: repository root on sys.path
from klado_shared.identity import admin_emails, is_admin_identity  # noqa: F401  (re-export)

__all__ = ["ADMIN_REQUIRED_DETAIL", "admin_emails", "is_admin_identity",
           "may_write_app_setting", "requires_admin"]

# Paths whose *writes* are operator-only, matched as (method, regex) so that the
# parameterised routes (`/datasets/<table>`) are covered too.
# Personal-content writes are guarded by ownership in their routers, like Workspace.
# Administration remains under /api/settings; operator diagnostics have explicit gates.
_ADMIN_ONLY_ROUTES: tuple[tuple[str, re.Pattern], ...] = ()

# `/api/settings/app/{key}` is a shared key-value store. System keys there are
# operator-only, but the SPA also persists per-page UI preferences for EVERY user —
# grid layouts and the like — so the rule has to be per key rather than per path.
# Anything that does not look like a UI preference (notably `build_version`) needs
# an admin.
_UI_PREFERENCE_KEY = re.compile(r"^[a-z0-9_]+_settings(_v\d+)?$")
_APP_SETTING_PREFIX = "/api/settings/app/"

_SETTINGS_PREFIX = "/api/settings/"

# ⚠️ Exactly one segment, and never the reserved word `accounts`. Both halves are
# load-bearing: a loose `startswith("/api/settings/modules")` would swallow
# `/api/settings/modules/accounts/…`, which is the *operator* writing another
# account's row — the one form that must stay admin-only — and a future
# `PUT /api/settings/modules/accounts` would then become self-service by accident.
# The rule fails closed: anything under /api/settings/ that does not match exactly
# is operator-only, which is what the blanket rule below already does.
_SELF_SERVICE_MODULE_RE = re.compile(
    r"^/api/settings/modules(?:/(?!accounts$)[A-Za-z0-9_-]{1,64})?$")

ADMIN_REQUIRED_DETAIL = (
    "需要管理员身份 / admin required — 这类操作会改动系统配置，"
    "只对管理员开放。请用管理员账号在浏览器里操作（机器凭据不能执行这类操作）。"
)


# ⚠️ `admin_emails()` and `is_admin_identity()` are re-exported from
# `klado_shared.identity` (imported at the top) rather than defined here. They moved
# because the admin console asks the same question and must get the same answer; two
# copies would be two definitions of "operator", and the divergence would be invisible
# until somebody found a permission that behaved differently depending on which port
# they went through. This file's own policy — which requests need one — stays here,
# because the main app's route surface is its business alone.


def may_write_app_setting(user: dict | None, key: str) -> bool:
    """Per-key rule for the shared `/api/settings/app/{key}` store."""
    if is_admin_identity(user):
        return True
    return bool(_UI_PREFERENCE_KEY.match((key or "").strip().lower()))


def requires_admin(path: str, method: str) -> bool:
    """True when this request may only be made by an operator (any auth mode)."""
    method = (method or "").upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if path.startswith(_APP_SETTING_PREFIX):
        return not _UI_PREFERENCE_KEY.match(path[len(_APP_SETTING_PREFIX):].strip().lower())
    if _SELF_SERVICE_MODULE_RE.match(path):
        # An account narrowing ITS OWN module set. That is not operator work, and the
        # blanket rule below would make it a 403 for everyone but an admin — an
        # endpoint that exists and is unreachable in exactly the deployment it was
        # written for. Safe because `core.modules.resolve()` intersects with the
        # deployment allowlist, so a self-service row can only ever *remove* access.
        # The per-account form (`…/modules/accounts/…`) stays operator-only: it
        # deliberately does not match here.
        return False
    if path.startswith(_SETTINGS_PREFIX):
        # The Settings page's own API group (lookup tables, LLM config, value classes).
        return True
    return any(method == m and rx.match(path) for m, rx in _ADMIN_ONLY_ROUTES)