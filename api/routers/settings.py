"""
Settings API router — system-level configuration.

The System Settings page now keeps only its Admin tab (users / access / live
codes / mail delivery), which is served by ``routers/auth.py``. Everything left
here is the configuration the *retained* features still read:

* ``GET /build-version``      — the deploy-version chip in the app shell.
* ``GET/PUT /app/{key}``      — the generic page/user preference store written by
  the documents, reports and knowledge pages.
"""
import os
import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Any, Optional
from core.db import connect_main
from core.i18n import pick, request_lang

router = APIRouter()


def _pg_conn():
    return connect_main()


def _version_from_image_ref(image_ref: str) -> str:
    """Return the tag (or a short digest) from a deployed container image ref."""
    value = (image_ref or "").strip()
    if not value or "${" in value:
        return ""
    if "@sha256:" in value:
        digest = value.split("@sha256:", 1)[1]
        return f"sha256:{digest[:12]}" if digest else ""
    image_name = value.rsplit("/", 1)[-1]
    colon = image_name.rfind(":")
    return image_name[colon + 1:] if colon >= 0 and colon < len(image_name) - 1 else ""


# ── Lookups ──────────────────────────────────────────────────────────────────


@router.get("/build-version")
def get_build_version(request: Request):
    """Return the version of the code this process is running.

    `BUILD_COMMIT` wins when the environment sets it (a build script, a launcher),
    then `BUILD_IMAGE_REF` for installs that run from a tagged image, then the
    `build_version` row in `app_settings`. Reports `{"commit": "", "source": "none"}`
    when none of them is set, which is normal for a plain source checkout.
    """
    env_ver = os.environ.get("BUILD_COMMIT", "").strip()
    if env_ver:
        return {"commit": env_ver, "source": "env"}
    image_ref = os.environ.get("BUILD_IMAGE_REF", "").strip()
    image_version = _version_from_image_ref(image_ref)
    if image_version:
        return {"commit": image_version, "source": "image", "image": image_ref}
    try:
        conn = _pg_conn()
        _ensure_app_settings(conn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT value FROM app_settings WHERE key = %s", ("build_version",))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row["value"]:
            return row["value"]
        return {"commit": "", "source": "none"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=pick(str(e), request_lang(request) if request else None))


# ── App Settings (key-value store for cross-browser persistence) ────────────────

class AppSettingBody(BaseModel):
    value: Any


def _ensure_app_settings(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key        TEXT        PRIMARY KEY,
            value      JSONB       NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.commit()
    cur.close()


@router.get("/app/{key}")
def get_app_setting(key: str, request: Request):
    try:
        conn = _pg_conn()
        _ensure_app_settings(conn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail=pick("未找到 / not found", request_lang(request) if request else None))
        return {"value": row["value"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=pick(str(e), request_lang(request) if request else None))


@router.put("/app/{key}", status_code=200)
def put_app_setting(key: str, body: AppSettingBody, request: Request):
    try:
        conn = _pg_conn()
        _ensure_app_settings(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, psycopg2.extras.Json(body.value))
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"value": body.value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=pick(str(e), request_lang(request) if request else None))

# ─── Module switches ─────────────────────────────────────────────────────────
# The graphical picker the Settings page draws itself from. The registry
# (`core.modules`) is the only place a module is defined; this endpoint exposes it
# and writes the caller's own row in `account_modules`.
#
# Self-service is safe by construction: `core.modules.resolve()` intersects the
# account row with the deployment allowlist, so a row can only ever *narrow* what
# the deployment already sells. Turning a module on here cannot grant something
# the deployment switched off.

class ModuleToggle(BaseModel):
    """One module's state for one account.

    ⚠️ `Optional[bool]`, and `null` is a REAL state rather than a missing field. The
    control has three positions — opened for this person, closed for this person, and
    *nobody said anything, so follow the deployment* — and only the first two are
    `true`/`false`. With a plain `bool`, a `null` collapsed through `bool(None)` to
    `false` and silently wrote a row that outlives the deployment setting, which is the
    exact stale entitlement the intersection in `core.modules.resolve()` exists to
    neutralise.
    """
    enabled: Optional[bool] = None


def _current_user_id(request: Request) -> int | None:
    user = getattr(request.state, "current_user", None) or {}
    try:
        return int(user["id"]) if user.get("id") else None
    except (TypeError, ValueError):
        return None


def _current_user(request: Request) -> dict:
    """The whole caller row, because `entitled()` needs the address as well as the id.

    ⚠️ Deliberately the *raw* state object, not a narrowed projection: the company licence
    is looked up by email, so dropping the field here would silently turn the per-company
    ceiling back off for every endpoint on this router while `main.py`'s gate kept it.
    """
    return getattr(request.state, "current_user", None) or {}


@router.get("/modules")
def get_modules(request: Request):
    """What this account has, and what the deployment could give it."""
    from core import modules as core_modules
    from services import account_modules
    user_id = _current_user_id(request)
    overrides = account_modules.overrides_for(user_id) if user_id else {}
    return {
        # ⚠️ `entitled()` rather than `resolve(overrides)`: this is the payload the org
        # module dialog reads the registry from, and it must not describe a set the
        # middleware gate would refuse. The registry (`deployment`) travels whole on
        # purpose — the dialog needs the icon and label of every module, including the
        # ones this company may not hand out.
        "modules": core_modules.for_client(
            account_modules.entitled(_current_user(request))),
        "overrides": overrides,
        "account": user_id,
        "deployment": core_modules.deployment_default(),
    }


@router.put("/modules/{module_key}", status_code=200)
def put_module(module_key: str, body: ModuleToggle, request: Request):
    """Switch one module for the caller. A required module cannot be turned off.

    ⚠️ `{"enabled": null}` sends that one module back to following the deployment
    default, which is a DIFFERENT act from `{"enabled": false}`.
    """
    from core import modules as core_modules
    from services import account_modules
    module = core_modules.BY_KEY.get(module_key)
    if module is None:
        raise HTTPException(404, pick("未知模块 / unknown module: {k}"
                                      .format(k=module_key), request_lang(request)))
    if module.required and body.enabled is False:
        raise HTTPException(400, pick("必需模块不能关闭 / this module is required and cannot be switched off",
                                      request_lang(request)))
    user_id = _current_user_id(request)
    if not user_id:
        # Single-user / auth-off deployment: there is no account to narrow, and the
        # deployment default is the whole answer. Say so instead of silently
        # pretending the switch was saved.
        raise HTTPException(400, pick("当前部署没有账号概念，模块由部署统一控制"
                                      " / this deployment has no accounts; modules are deployment-wide",
                                      request_lang(request)))
    if body.enabled is None:
        account_modules.clear_one(user_id, module_key)
    else:
        account_modules.set_module(user_id, module_key, body.enabled)
    overrides = account_modules.overrides_for(user_id)
    return {"ok": True, "overrides": overrides,
            "modules": core_modules.for_client(
                account_modules.entitled(_current_user(request)))}


@router.delete("/modules/{module_key}", status_code=200)
def clear_module(module_key: str, request: Request):
    """Drop the caller's override for ONE module, returning it to the default.

    ⚠️ `clear_one`, not `clear_for`. The URL names one module and the response is the
    caller's whole override map, so a reader has no way to tell the two apart — which
    is exactly how this route came to discard every module the account had narrowed
    while claiming to reset one.
    """
    from core import modules as core_modules
    from services import account_modules
    if core_modules.BY_KEY.get(module_key) is None:
        raise HTTPException(404, pick("未知模块 / unknown module: {k}"
                                      .format(k=module_key), request_lang(request)))
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(400, pick("当前部署没有账号概念，模块由部署统一控制"
                                      " / this deployment has no accounts; modules are deployment-wide",
                                      request_lang(request)))
    account_modules.clear_one(user_id, module_key)
    overrides = account_modules.overrides_for(user_id)
    return {"ok": True, "overrides": overrides,
            "modules": core_modules.for_client(
                account_modules.entitled(_current_user(request)))}


@router.get("/modules/accounts")
def list_module_accounts(request: Request):
    """Admin view: every account that has narrowed the deployment default."""
    from core.access import is_admin_identity
    from services import account_modules
    if not is_admin_identity(getattr(request.state, "current_user", None)):
        raise HTTPException(403, pick("仅管理员可查看 / admin only", request_lang(request)))
    try:
        return {"overrides": account_modules.list_all()}
    except Exception as e:
        raise HTTPException(500, pick(str(e), request_lang(request)))


@router.put("/modules/accounts/{user_id}/{module_key}", status_code=200)
def put_account_module(user_id: int, module_key: str, body: ModuleToggle, request: Request):
    """Admin: narrow (or restore) one module for one account.

    Cannot widen past the deployment: `core.modules.resolve()` intersects, so a row
    written here for a module the deployment does not sell has no effect — the API
    still rejects it rather than accepting a row that reads as a grant.

    ⚠️ `{"enabled": null}` drops the row and sends the module back to following the
    deployment. It is checked BEFORE the "does this deployment offer it" rule on
    purpose: removing a narrowing for a module the deployment has since withdrawn is
    exactly the cleanup that should be allowed.
    """
    from core import modules as core_modules
    from core.access import is_admin_identity
    from services import account_modules
    if not is_admin_identity(getattr(request.state, "current_user", None)):
        raise HTTPException(403, pick("仅管理员可修改 / admin only", request_lang(request)))
    module = core_modules.BY_KEY.get(module_key)
    if module is None:
        raise HTTPException(404, pick("未知模块 / unknown module: {k}"
                                      .format(k=module_key), request_lang(request)))
    if module.required and body.enabled is False:
        raise HTTPException(400, pick("必需模块不能关闭 / this module is required and cannot be switched off",
                                      request_lang(request)))
    try:
        if body.enabled is None:
            account_modules.clear_one(int(user_id), module_key)
        else:
            if body.enabled and module_key not in core_modules.deployment_default():
                raise HTTPException(400, pick("本部署未开放该模块 / this deployment does not offer that module",
                                              request_lang(request)))
            account_modules.set_module(int(user_id), module_key, body.enabled)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, pick(str(e), request_lang(request)))
    return {"ok": True, "overrides": account_modules.list_all()}


@router.delete("/modules/accounts/{user_id}", status_code=200)
def clear_account_modules(user_id: int, request: Request):
    """Admin: return one account to the deployment default for every module."""
    from core import modules as core_modules
    from core.access import is_admin_identity
    from services import account_modules
    if not is_admin_identity(getattr(request.state, "current_user", None)):
        raise HTTPException(403, pick("仅管理员可修改 / admin only", request_lang(request)))
    account_modules.clear_for(int(user_id))
    return {"ok": True, "overrides": account_modules.list_all()}
