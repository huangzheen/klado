"""Process configuration — read once from the repository's `.env`.

This is the shared copy of what used to be `api/core/config.py`. It moved here because
the admin console has to resolve the **same** `SECRET_KEY` and the **same** database: a
console that loaded a different secret would issue tokens the app cannot verify, and one
that loaded a different `POSTGRES_HOST` would quietly administer a different database.
`api/core/config.py` imports and re-exports this, so no main-app call site changed.

⚠️ The two paths below are derived from THIS file's location, never from the caller's
cwd. The documented way to start the app is `cd api && … uvicorn main:app`, and a
cwd-relative lookup would then look for `api/.env` and silently find nothing — the
console, started as `cd api-admin && …`, would fail the same way for the same reason.

`override=False`: a variable exported in the shell still wins over the file.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings

# ── Where this checkout's configuration lives ───────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))          # …/klado_shared
REPO_DIR = os.path.dirname(_HERE)                            # …/<repo>
_API_DIR = os.path.join(REPO_DIR, "api")                    # …/<repo>/api
ENV_FILES = (os.path.join(REPO_DIR, ".env"), os.path.join(_API_DIR, ".env"))

# ⚠️ This is NOT the same thing pydantic-settings does below, and it has to happen
# before anything reads `os.environ`. pydantic reads `.env` into the Settings *object*
# only; roughly fifty call sites across the app read `os.environ` directly — this
# package's `db.py` is the first one that matters, and it is what raises
# "POSTGRES_PASSWORD is not set".
for _env_file in ENV_FILES:
    if os.path.isfile(_env_file):
        load_dotenv(_env_file, override=False)


class Settings(BaseSettings):
    APP_NAME: str = "Klado"
    APP_VERSION: str = "0.1.0"
    SECRET_KEY: str = "change-me"
    APP_BASE_PATH: str = ""
    DB_BOOTSTRAP_STRICT: bool = False

    # ── Admin console (api-admin/, its own process and port) ─────────────────
    # The console is a separate deployment unit, not a route on the main app. It
    # shares this settings object on purpose — see the module docstring.
    KLADO_ADMIN_PORT: int = 8787
    # The console's cookie name. ⚠️ MUST differ from the main app's `SESSION_COOKIE`:
    # cookies are matched on host, not on port, so a shared name means the two
    # processes overwrite each other's login. See `klado_shared/session.py`.
    KLADO_ADMIN_SESSION_COOKIE: str = "klado_admin_session"

    # Unified authentication.  When disabled, Klado retains its current
    # standalone behaviour; when enabled every business API request is
    # authenticated by the upstream user-info service.
    AUTH_ENABLED: bool = False
    AUTH_USER_INFO_URL: str = ""
    AUTH_TOOL_CODE: str = ""
    AUTH_TIMEOUT_SECONDS: float = 5.0
    AUTH_CACHE_TTL_SECONDS: int = 300
    # ── Local accounts (services/auth_store.py) ─────────────────────────────
    # Used only when AUTH_USER_INFO_URL is empty: people register with an email
    # address that a mailed code confirms, and agents authenticate with
    # Authorization: Basic <base64(email:password)>.
    #
    # Both are EMPTY by default: this repository ships no identity of its own, so
    # a fresh checkout names nobody an administrator. Set them in `.env`:
    #   AUTH_ALLOWED_EMAIL_DOMAIN  deployment-wide fallback that restricts sign-up
    #                              and invitations to one domain. ⚠️ DEMOTED as of
    #                              the orgs work: per-organization suffixes live in
    #                              the `orgs.email_domains` column, and this setting
    #                              is now only a "reject outright" backstop for a
    #                              single-domain deployment. Empty accepts any
    #                              well-formed address.
    #   AUTH_ADMIN_EMAILS          comma-separated. The first address is also the
    #                              identity that local single-user mode (no login
    #                              middleware) acts as — see reports._legacy_owner.
    AUTH_ALLOWED_EMAIL_DOMAIN: str = ""
    AUTH_ADMIN_EMAILS: str = ""
    # Which product modules this deployment offers, comma-separated, e.g.
    #   KLADO_MODULES=datacenter,workspace,dashboard
    # Empty (the default) means every module in `core/modules.py`. An account can
    # still be narrowed below this by a row in `account_modules`; it can never be
    # widened past it. Naming a module that does not exist is a startup error
    # rather than a silent "that module is now off".
    KLADO_MODULES: str = ""
    AUTH_CODE_TTL_SECONDS: int = 600
    # An invitation is read hours or days after it arrives, so it cannot share the
    # 10-minute code TTL: the colleague would open the mail and find a dead code.
    # Long-lived on purpose, and bounded by the same guards (single use, cleared the
    # moment it is consumed or superseded, readable only by an admin).
    AUTH_INVITE_TTL_SECONDS: int = 1209600                # 14 days
    AUTH_SESSION_TTL_SECONDS: int = 2592000            # 30 days
    # Where the downloadable agent skill package lives, as it goes into an email.
    # A relative path is resolved against the app's own mount point by the SPA and the
    # mail templates, so a message stays valid for whoever received it without naming a
    # host. Set an absolute URL here if this installation has a stable public name.
    AGENT_SKILL_URL: str = "agent.zip"
    # ── First-admin bootstrap ───────────────────────────────────────────────
    # Seed the first account at startup so a fresh database is reachable without a
    # mail server (with SMTP unset a verification code only reaches the server log).
    # Seeded from a **pre-computed PBKDF2 hash**, never a plaintext password, so a
    # repository reader learns nothing they can log in with. Applied only when that
    # email has no account yet (ON CONFLICT DO NOTHING), and only if the hash looks
    # like one. Rotate the password afterwards and this line becomes inert.
    AUTH_BOOTSTRAP_ADMIN_EMAIL: str = ""
    AUTH_BOOTSTRAP_ADMIN_HASH: str = ""
    # Registration mail. Configured from the admin console (services/mail_config.py,
    # stored in the `mail_settings` table); these environment variables are the
    # per-field fallback for a deployment that would rather set them here.
    # Empty SMTP_HOST/SMTP_FROM means "unconfigured": codes then surface on the
    # console instead of by email.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_SECURITY: str = "ssl"                          # ssl | starttls | plain

    @field_validator("AUTH_TIMEOUT_SECONDS", "AUTH_CACHE_TTL_SECONDS", "AUTH_CODE_TTL_SECONDS",
                     "AUTH_INVITE_TTL_SECONDS", "AUTH_SESSION_TTL_SECONDS", "SMTP_PORT",
                     "KLADO_ADMIN_PORT",
                     mode="before")
    @classmethod
    def _blank_numeric_env_means_default(cls, value, info):
        """
        An empty string is how an environment file says "not set here", but pydantic
        refuses to parse "" as a number — which takes the whole application down at
        import time over a value nobody meant to supply.
        Treat blank as "use the default" instead.
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    # PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "klado"
    POSTGRES_USER: str = "klado"
    POSTGRES_PASSWORD: str = ""

    @property
    def DATABASE_URL(self) -> str:
        from urllib.parse import quote
        user = quote(self.POSTGRES_USER, safe="")
        password = quote(self.POSTGRES_PASSWORD, safe="")
        db = quote(self.POSTGRES_DB, safe="")
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{db}"
            f"?ssl=disable"
        )

    # Object storage (see services/oss_storage.py)
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_ENDPOINT: str = ""
    OSS_BUCKET_NAME: str = ""
    # 火山云
    VOLCENGINE_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/coding/v3"
    VOLCENGINE_MODEL: str = "deepseek-v4-flash"
    ARK_API_KEY: str = ""
    ARK_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    VOLCENGINE_IMAGE_MODEL: str = "doubao-seedream-4-5-251128"

    # Google Gemini image generation
    GEMINI_API_KEY: str = ""
    GEMINI_IMAGE_MODEL: str = "gemini-3-pro-image-preview"

    # MiniMax
    MINIMAX_API_KEY: str = ""
    # MiniMax image model used for report covers (services/report_cover.py).
    # image-01 is what the coding-plan key accepts; image-02 / MiniMax-M3 return
    # 2013 "unsupported model" (M3 is a chat model).
    MINIMAX_IMAGE_MODEL: str = "image-01"
    MINIMAX_BASE_URL: str = "https://api.minimax.chat/v1"

    # DeepSeek
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # Brave Search
    BRAVE_SEARCH_API_KEY: str = ""
    BRAVE_SEARCH_BASE_URL: str = "https://api.search.brave.com/res/v1/web/search"

    class Config:
        # Absolute paths, so the answer does not depend on the working directory.
        env_file = ENV_FILES
        # `.env` also carries variables this settings object does not own
        # (service credentials, feature switches); ignore rather than reject them.
        extra = "ignore"


settings = Settings()


# ── Assets an email may reference ────────────────────────────────────────────
# ⚠️ Deliberately a *list*, and deliberately not the main app's `resolve_frontend_dir()`.
# That one answers "where does the SPA live" and must keep answering that, so it is still
# `frontend/out` and nothing else — pointing it at the console would make the main app
# start serving the wrong bundle. An email banner is a different question: "does an
# image with this name exist anywhere in this checkout", and both processes send mail.
#
# The candidates are built from REPO_DIR, never from the caller's cwd, for the reason the
# two entry points differ (`cd api` versus `cd api-admin`).
def mail_asset_dirs() -> tuple[str, ...]:
    """Directories that may hold the images embedded in emails, main app first.

    Missing directories are dropped rather than returned, so a caller can just iterate
    and compare. An empty result is normal on a fresh checkout with no banners added —
    the mailer then omits the image instead of linking to a 404, which is the behaviour
    that was already there when the file does not exist.
    """
    candidates = (
        os.environ.get("FRONTEND_DIR", ""),
        os.path.join(REPO_DIR, "frontend", "out"),
        os.path.join(REPO_DIR, "api-admin", "frontend"),
    )
    seen, out = set(), []
    for candidate in candidates:
        if candidate and candidate not in seen and os.path.isdir(candidate):
            seen.add(candidate)
            out.append(candidate)
    return tuple(out)
