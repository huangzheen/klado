"""
Local user store for Klado — registration, email codes, sessions, access log.

Why this exists
---------------
Klado authenticates against **its own** account table: an email address plus a password,
with registration confirmed by a code mailed to that address. `core.third_auth` still
carries the branch that delegates to an upstream SSO (`AUTH_USER_INFO_URL` plus that
platform's cookie), and that branch is what runs if those settings are filled in — but it
is not the path this installation uses, and nothing here depends on it.

Two identities, deliberately distinct
-------------------------------------
* **People** log in in a browser and get a signed session token (HttpOnly cookie).
* **Agents** authenticate with `Authorization: Basic base64(email:password)` — the
  "just email and password" contract the Feishu agent uses. Their requests are
  recorded with kind='agent' so the admin page can show what a bot did.

⚠️ With `AUTH_ALLOWED_EMAIL_DOMAIN` unset there is **no suffix gate at all**: any
well-formed address may register. What actually keeps outsiders out is email ownership
(the code has to reach the mailbox). Set the domain if you want a restriction, and do
not treat any of this as SSO.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from contextlib import contextmanager

from klado_shared.config import settings
from klado_shared.db import connect_main

_LOG = logging.getLogger(__name__)

CODE_MAX_ATTEMPTS = 5

# ── office avatars ────────────────────────────────────────────────────────────
# ⚠️ These two tuples are the SERVER's list of what an avatar can be, and the SPA
# has a copy of the same six and five. They are duplicated on purpose rather than
# served from here: the endpoint that would serve them costs a request before the
# page has drawn anything, the room needs the names to be stable constants, and —
# the reason that actually settles it — a stale SPA cached by a service worker must
# not be able to send a name this build has never heard of. The server validates
# anyway, so a new animal shipped to the server before the SPA picks it up is
# ignored rather than trusted, and the other order is equally safe.
AVATAR_ANIMALS = ("tiger", "ox", "horse", "cat", "rabbit", "hippo")
AVATAR_CLOTHS = ("red", "blue", "yellow", "green", "black")


def _code_ttl() -> int:
    return int(settings.AUTH_CODE_TTL_SECONDS or 600)


def _session_ttl() -> int:
    return int(settings.AUTH_SESSION_TTL_SECONDS or 30 * 24 * 3600)
USER_KIND = ("browser", "agent")


# ── schema ───────────────────────────────────────────────────────────────────

_schema_ready = False


def _ensure_schema() -> None:
    """Create the three tables on first use (memoised per process)."""
    global _schema_ready
    if _schema_ready:
        return
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    id            SERIAL PRIMARY KEY,
                    email         TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name  TEXT NOT NULL DEFAULT '',
                    role          TEXT NOT NULL DEFAULT 'user',
                    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_login_at TIMESTAMPTZ
                )
            """)
            # ── the recycle bin ──
            # ⚠️ `deleted_at` is NOT the same as `disabled`. Disabled is an
            # operator's switch and the operator can flip it back; `deleted_at` means
            # the account asked to be closed, it is counting down to a real purge, and
            # the data it owned is already invisible to everyone else. Two columns
            # because "an admin paused this account" and "this account is on its way
            # out" are different facts and a UI that merges them cannot answer
            # "is this restorable?".
            # `purge_after` is materialised rather than computed from a setting, so a
            # grace period shortened in config does not retroactively move a deadline
            # that was already shown to the user.
            cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS deleted_at "
                        "TIMESTAMPTZ")
            cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS purge_after "
                        "TIMESTAMPTZ")
            cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS deleted_by "
                        "TEXT NOT NULL DEFAULT ''")
            cur.execute("CREATE INDEX IF NOT EXISTS app_users_pending_purge_idx "
                        "ON app_users (purge_after) WHERE purge_after IS NOT NULL")
            # Explicit, team-visible agent updates; never infer tasks from private URLs.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS office_updates (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    task TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS office_updates_user_time "
                        "ON office_updates (user_id, id DESC)")
            # ── the office avatar ──
            # ⚠️ Two columns, not one, and NOT NULL with a default. A single
            # "tiger" string cannot answer "which animal in which colour", and
            # a nullable pair would make "never chose" and "chose the default"
            # the same value — so the room could never tell an untouched account
            # from one that deliberately picked the first option. The defaults
            # below ARE a choice (tiger, blue), they are simply the choice an
            # account starts on before it makes one.
            cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS avatar_animal "
                        "TEXT NOT NULL DEFAULT 'tiger'")
            cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS avatar_cloth "
                        "TEXT NOT NULL DEFAULT 'blue'")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS login_codes (
                    id          SERIAL PRIMARY KEY,
                    email       TEXT NOT NULL,
                    code_hash   TEXT NOT NULL,
                    code_plain  TEXT,
                    attempts    INT NOT NULL DEFAULT 0,
                    consumed_at TIMESTAMPTZ,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("ALTER TABLE login_codes ADD COLUMN IF NOT EXISTS code_plain TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS login_codes_email_idx ON login_codes (email, created_at DESC)")
            # Access log is AGGREGATED per (user, kind, path, minute): the SPA fires
            # dozens of requests per screen, and one row per request would be both
            # useless and unbounded. hits answers "how often", first/last answers "when".
            # ── Agent access codes (授权码) ─────────────────────────────────
            # One row per issued code, per user. The *hash* is what authentication
            # uses; `code_enc` holds the same code encrypted (see _encrypt_code), so
            # the admin page can hand out a working code again instead of only the
            # unusable 12-character prefix. The code is accepted *only* as an
            # Authorization header — never as a cookie — so a leaked code cannot be
            # turned into a browser session.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_tokens (
                    id           SERIAL PRIMARY KEY,
                    user_id      INT NOT NULL,
                    label        TEXT NOT NULL DEFAULT '',
                    token_hash   TEXT NOT NULL UNIQUE,
                    display      TEXT NOT NULL DEFAULT '',
                    code_enc     TEXT,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_by   TEXT NOT NULL DEFAULT '',
                    last_used_at TIMESTAMPTZ,
                    revoked_at   TIMESTAMPTZ,
                    revoked_by   TEXT NOT NULL DEFAULT ''
                )
            """)
            cur.execute("ALTER TABLE agent_tokens ADD COLUMN IF NOT EXISTS code_enc TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS agent_tokens_user_idx "
                        "ON agent_tokens (user_id, created_at DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS access_log (
                    id          BIGSERIAL PRIMARY KEY,
                    user_id     INT,
                    user_email  TEXT NOT NULL DEFAULT '',
                    kind        TEXT NOT NULL,
                    method      TEXT NOT NULL,
                    path        TEXT NOT NULL,
                    status      INT NOT NULL DEFAULT 200,
                    ip          TEXT NOT NULL DEFAULT '',
                    ua          TEXT NOT NULL DEFAULT '',
                    bucket      TIMESTAMPTZ NOT NULL,
                    hits        INT NOT NULL DEFAULT 1,
                    first_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # ⚠️ `method` belongs in the key: without it a POST and a GET to the same
            # path within the same minute collapse into one row and the write disappears
            # — an audit log has to keep reads and writes apart.
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS access_log_key_idx "
                        "ON access_log (user_id, kind, method, path, bucket)")
            cur.execute("ALTER TABLE access_log "
                        "DROP CONSTRAINT IF EXISTS access_log_user_id_kind_path_bucket_key")
            cur.execute("CREATE INDEX IF NOT EXISTS access_log_recent_idx ON access_log (last_at DESC)")
        conn.commit()
        _schema_ready = True
    finally:
        conn.close()


@contextmanager
def _db():
    """Connection scope that always closes (psycopg2's `with conn` does not)."""
    conn = connect_main()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── passwords ────────────────────────────────────────────────────────────────

# PBKDF2-HMAC-SHA256 from the standard library rather than passlib/bcrypt: bcrypt
# ships without `__about__` in 4.x, which makes passlib log a trapped version error
# on every hash, and this password store should not depend on that pairing. 240k
# iterations is the usual guidance for this construction.
_PBKDF2_ITERATIONS = 240_000


def _hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify(password: str, password_hash: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = password_hash.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
    except Exception:  # noqa: BLE001 — malformed hash must read as "wrong password"
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# ── sessions (signed, stateless) ─────────────────────────────────────────────
#
# ⚠️ The three functions below live in `klado_shared/session.py` now, because the admin
# console has to verify the *same* tokens against the *same* SECRET_KEY without sharing
# a session table. They are re-exported here, not reimplemented, and this module's two
# call sites (`routers/auth.py`) are unchanged.
#
# The one behavioural change: a token's signature is now bound to an audience
# ("app" for this process, "admin" for the console). Sessions issued before that still
# verify here — `allow_legacy=True` — so nobody is logged out by the upgrade. The console
# never accepts an untagged token: with no audience there is nothing to check, and
# accepting one would hand the console's operator session to the main app.
from klado_shared.session import (ADMIN_AUDIENCE, APP_AUDIENCE,  # noqa: F401
                                 issue_session, session_user_id)


def _session_ttl() -> int:
    return int(settings.AUTH_SESSION_TTL_SECONDS or 30 * 24 * 3600)
USER_KIND = ("browser", "agent")


# ── users ────────────────────────────────────────────────────────────────────

def allowed_domain() -> str:
    """The domain sign-up is restricted to, or "" when any address is accepted.

    ⚠️ Empty is a real answer, not a missing one: callers must skip the suffix check
    rather than compare against "@" (which no address ends with).

    ⚠️ DEMOTED (orgs work). This is the deployment-wide single-domain backstop. Per
    organization suffixes live in `orgs.email_domains`; this setting stays as a
    "reject outright" floor for single-domain deployments. The two places that still
    read it as a *share* boundary — `routers/reports.is_allowed_recipient` and
    `dashboard_store.is_allowed_recipient` — are replaced by the org gate in phase P2,
    and the duplicated function is deleted there. Do not add a third reader.
    """
    return (settings.AUTH_ALLOWED_EMAIL_DOMAIN or "").strip().lower().lstrip("@")


def is_admin_email(email: str) -> bool:
    wanted = {a.strip().lower() for a in (settings.AUTH_ADMIN_EMAILS or "").split(",") if a.strip()}
    return (email or "").strip().lower() in wanted


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row.get("display_name") or "",
        "role": row.get("role") or "user",
        "disabled": bool(row.get("disabled")),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "last_login_at": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
        # `pending_deletion` is the one the UI branches on: it is the flag that makes
        # the account unrestorable, and it carries the deadline it must show.
        "deleted_at": row["deleted_at"].isoformat() if row.get("deleted_at") else None,
        "purge_after": row["purge_after"].isoformat() if row.get("purge_after") else None,
        "deleted_by": row.get("deleted_by") or "",
        "pending_deletion": bool(row.get("deleted_at")),
    }


_COLS = ("id, email, display_name, role, disabled, created_at, last_login_at, "
         "deleted_at, purge_after, deleted_by, avatar_animal, avatar_cloth")


def get_user_by_email(email: str) -> Optional[dict]:
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_COLS} FROM app_users WHERE lower(email) = lower(%s)", (email,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {_COLS} FROM app_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def create_user(email: str, password: str, display_name: str = "") -> dict:
    """Insert a verified user. Raises ValueError when the email is taken."""
    _ensure_schema()
    email = email.strip().lower()
    role = "admin" if is_admin_email(email) else "user"
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"INSERT INTO app_users (email, password_hash, display_name, role) "
                f"VALUES (%s, %s, %s, %s) RETURNING {_COLS}",
                (email, _hash(password), display_name.strip(), role),
            )
            row = dict(cur.fetchone())
    return row


def create_user_with_hash(email: str, password_hash: str, display_name: str = "",
                          role: str = "admin") -> Optional[dict]:
    """
    Seed an account from a pre-computed hash. Returns the new user, or None when the
    email already had an account (the caller must not overwrite an existing
    password). Used by the first-admin bootstrap — see `AUTH_BOOTSTRAP_ADMIN_HASH`
    in core/config.py — so a plaintext password never has to travel through git.
    """
    _ensure_schema()
    email = email.strip().lower()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"INSERT INTO app_users (email, password_hash, display_name, role) "
                f"VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING RETURNING {_COLS}",
                (email, password_hash.strip(), display_name.strip(), role),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def set_password(user_id: int, password: str) -> bool:
    """Change a password (admin reset, self-service change, bootstrap rotation)."""
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE app_users SET password_hash = %s WHERE id = %s",
                        (_hash(password), user_id))
            return cur.rowcount > 0


# ── agent access codes (授权码) ───────────────────────────────────────────────

AGENT_TOKEN_PREFIX = "klado_agent_"


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


# ── code encryption ──────────────────────────────────────────────────────────
# Verifying a code needs only its hash, but *showing* it again does not: the admin
# page has to be able to hand a working code to a robot. So the full value is also
# kept — encrypted, under a key derived from SECRET_KEY, the same construction
# `services/mail_config.py` uses for the mailbox password (different salt, so the
# two keys are unrelated). A database dump or a filesystem backup therefore
# still contains no usable code on its own.
# ⚠️ Rotating SECRET_KEY makes stored codes unreadable. That is handled, not hidden:
#   the row then reports `code: null` and the page asks for 「重新生成并发送」
#   instead of offering a copy button that hands out a dead string.
AGENT_CODE_SALT = "agent-tokens"

try:  # pragma: no cover - import guard, mirrors services/mail_config.py
    from cryptography.fernet import Fernet, InvalidToken

    _FERNET_OK = True
except Exception:  # noqa: BLE001
    Fernet = None
    InvalidToken = Exception
    _FERNET_OK = False


def _code_fernet() -> "Fernet | None":
    if not _FERNET_OK:
        return None
    material = hashlib.sha256(f"{AGENT_CODE_SALT}:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def _encrypt_code(token: str) -> str:
    f = _code_fernet()
    if f is None:
        # Same call as mail_config: a missing crypto dependency must not take the
        # feature down. Visible in the value itself (`plain:` prefix) rather than silent.
        return "plain:" + token
    return "enc:" + f.encrypt(token.encode()).decode()


def _decrypt_code(stored: Optional[str]) -> Optional[str]:
    """The full code, or None when it cannot be recovered (pre-column row, rotated key)."""
    if not stored:
        return None
    if stored.startswith("plain:"):
        return stored[len("plain:"):]
    if not stored.startswith("enc:"):
        return stored
    f = _code_fernet()
    if f is None:
        return None
    try:
        return f.decrypt(stored[len("enc:"):].encode()).decode()
    except Exception:  # noqa: BLE001 - InvalidToken, or a corrupted/tampered value
        return None


def create_agent_token(user_id: int, label: str = "", created_by: str = "") -> tuple[str, dict]:
    """
    Issue a code for `user_id`. Returns (plaintext, row).

    The plaintext is what the caller hands over (we email it); the same value is also
    stored encrypted (`code_enc`) so the admin page can show it again later — a copy
    button that yields half a code is a trap, not a feature. `label` is free text such
    as "Feishu agent", so a listing can say *which* robot holds which code.
    """
    _ensure_schema()
    token = AGENT_TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO agent_tokens (user_id, label, token_hash, display, code_enc, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (int(user_id), (label or "").strip()[:80], _token_hash(token),
                 token[:len(AGENT_TOKEN_PREFIX) + 6], _encrypt_code(token),
                 (created_by or "").strip()),
            )
            row = dict(cur.fetchone())
    row.pop("token_hash", None)
    return token, _agent_token_public(row)


def _agent_token_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "label": row.get("label") or "",
        "display": row.get("display") or "",
        # The full code, when it can be recovered, so the UI can offer a copy button
        # that copies something usable. `None` = issued before `code_enc` existed, or
        # SECRET_KEY was rotated; `display` (the prefix) is then all there is, and the
        # page says so instead of copying an unusable string.
        "code": _decrypt_code(row.get("code_enc")),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "created_by": row.get("created_by") or "",
        "last_used_at": row["last_used_at"].isoformat() if row.get("last_used_at") else None,
        "revoked_at": row["revoked_at"].isoformat() if row.get("revoked_at") else None,
        "revoked_by": row.get("revoked_by") or "",
        "active": not row.get("revoked_at"),
    }


def resolve_agent_token(token: str) -> Optional[dict]:
    """
    Turn a presented code into the user it belongs to, or None.

    `last_used_at` is refreshed at most once a minute: the agent calls this on every
    request, and a write per request is exactly the kind of thing that bloats a table.
    """
    token = (token or "").strip()
    if not token.startswith(AGENT_TOKEN_PREFIX):
        return None
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_tokens WHERE token_hash = %s AND revoked_at IS NULL",
                        (_token_hash(token),))
            row = cur.fetchone()
            if not row:
                return None
            user = get_user_by_id(row["user_id"])
            # A code is only as good as its owner. `soft_delete` revokes tokens up
            # front, so this only fires if a code somehow outlived the close (a race,
            # a row inserted by hand) — but it is the difference between "the account
            # is closed" and "some rows about it happen to be gone".
            if not user or user.get("deleted_at") or user.get("disabled"):
                return None
            cur.execute(
                "UPDATE agent_tokens SET last_used_at = NOW() WHERE id = %s "
                "AND (last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '60 seconds')",
                (row["id"],))
            # Preserve the code identity; siblings no longer collapse into one desk.
            return {**user, "_agent_token_id": row["id"]}


def list_agent_tokens(user_id: Optional[int] = None) -> list[dict]:
    """`user_id=None` lists every code (the admin view). Hashes are never returned;
    the decrypted copy is, so the page can show a code that actually works."""
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if user_id is None:
                cur.execute(
                    "SELECT t.*, u.email AS user_email FROM agent_tokens t "
                    "LEFT JOIN app_users u ON u.id = t.user_id "
                    "ORDER BY t.created_at DESC LIMIT 500")
            else:
                cur.execute(
                    "SELECT t.*, u.email AS user_email FROM agent_tokens t "
                    "LEFT JOIN app_users u ON u.id = t.user_id "
                    "WHERE t.user_id = %s ORDER BY t.created_at DESC LIMIT 200", (int(user_id),))
            rows = [dict(r) for r in cur.fetchall()]
    out = []
    for r in rows:
        pub = _agent_token_public(r)
        pub["user_email"] = r.get("user_email") or ""
        out.append(pub)
    return out


def ensure_initial_agent_code(user_id: int, label: str = "signup",
                              created_by: str = "") -> tuple[str, dict] | None:
    """
    Make sure `user_id` has an agent code, and return it — or None if it already had one.

    ⚠️ **"Already had one" means any row at all, not "has a live one".** This is the whole
    point of the function and the one way to write it wrong: a backfill keyed on "no *live*
    code" re-issues the moment an operator revokes one, so a restart silently hands back
    the credential somebody deliberately killed, and the revocation means nothing. A
    revoked row is the record of a decision, so it counts as "had one".

    Called at startup for every active account, and by the first-admin bootstrap. It is
    idempotent: the second call for the same account is a no-op that returns None.
    """
    _ensure_schema()
    user_id = int(user_id)
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM agent_tokens WHERE user_id = %s LIMIT 1", (user_id,))
            if cur.fetchone():
                return None
    return create_agent_token(user_id, label, created_by=created_by)


def backfill_missing_agent_codes(label: str = "signup") -> int:
    """
    Issue a code for every **active** account that has never had one. Returns how many.

    ⚠️ Scoped to `deleted_at IS NULL AND NOT disabled`: a code resolves to nothing when its
    owner is closed (`resolve_agent_token` checks the owner), so minting one for a closed
    account produces a credential that lists as valid and 401s on use.

    This exists because account creation has more than one door. Registration mints a code
    (`auth.py::_welcome_agent_code`), but the first administrator is seeded at startup from
    `AUTH_BOOTSTRAP_ADMIN_*` — a fresh install has no mail server to read a verification code
    from, so that account cannot register and therefore never got one. On a single-operator
    deployment that is the *only* account, and it is the one holding the Agent Skill. Rather
    than teach every creation path about codes, this closes the gap from the other side: the
    invariant is "every active account has a code", and it is re-established on every start.
    """
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT u.id, u.email FROM app_users u "
                "WHERE u.deleted_at IS NULL AND NOT COALESCE(u.disabled, FALSE) "
                "AND NOT EXISTS (SELECT 1 FROM agent_tokens t WHERE t.user_id = u.id)")
            missing = [(int(r["id"]), r["email"] or "") for r in cur.fetchall()]
    issued = 0
    for uid, email in missing:
        try:
            ensure_initial_agent_code(uid, label, created_by=email)
            issued += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop startup
            print(f"WARNING: could not issue the initial agent code for {email}: {exc}",
                  flush=True)
    return issued


def get_agent_token(token_id: int) -> Optional[dict]:
    """
    One issued code by id, with its owner's address — the target of a manual re-send
    ("send that email again", without rotating the code).

    Returns the same public view `list_agent_tokens` uses (decrypted `code`, no hash and
    no ciphertext), or None when the id does not exist.
    """
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT t.*, u.email AS user_email FROM agent_tokens t "
                "LEFT JOIN app_users u ON u.id = t.user_id WHERE t.id = %s",
                (int(token_id),))
            row = cur.fetchone()
    if not row:
        return None
    pub = _agent_token_public(dict(row))
    pub["user_email"] = row.get("user_email") or ""
    return pub


def revoke_agent_token(token_id: int, revoked_by: str = "", user_id: Optional[int] = None) -> bool:
    """
    Revoke one code. `user_id` restricts it to that owner's codes (self-service).

    Revoking also drops the stored copy of the code: a revoked credential is dead, and
    keeping a decryptable copy of it around only widens what a database dump is worth.
    """
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute("UPDATE agent_tokens SET revoked_at = NOW(), revoked_by = %s, "
                            "code_enc = NULL "
                            "WHERE id = %s AND revoked_at IS NULL", (revoked_by, int(token_id)))
            else:
                cur.execute("UPDATE agent_tokens SET revoked_at = NOW(), revoked_by = %s, "
                            "code_enc = NULL "
                            "WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                            (revoked_by, int(token_id), int(user_id)))
            return cur.rowcount > 0


def purge_agent_token(token_id: int, user_id: Optional[int] = None) -> bool:
    """
    Delete a revoked code for good — the row disappears instead of staying as a tombstone.

    ⚠️ Only revoked rows are eligible (`revoked_at IS NOT NULL` is part of the WHERE, not a
    caller-side check): the two are separate operations on purpose. A live credential is
    never destroyed by the delete button, so a mis-click cannot silently cut off a robot
    that is still working — 吊销 first, 删除 second. `user_id` restricts it to that owner's
    codes (the self-service view).
    """
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute("DELETE FROM agent_tokens WHERE id = %s AND revoked_at IS NOT NULL",
                            (int(token_id),))
            else:
                cur.execute("DELETE FROM agent_tokens WHERE id = %s AND user_id = %s "
                            "AND revoked_at IS NOT NULL", (int(token_id), int(user_id)))
            return cur.rowcount > 0


def authenticate(email: str, password: str) -> tuple[Optional[dict], str]:
    """
    Returns (user, reason). reason is '' on success, else 'unknown' / 'bad_password'
    / 'disabled' / 'deleted' — the caller decides what to expose.

    ⚠️ The `deleted` check is AFTER the password check on purpose. A closed account
    must not become an oracle that tells a stranger "this address existed and was
    closed" — and the password is verified first so the answer is the same 401 either
    way for anyone who does not already hold the credentials.
    """
    user = get_user_by_email(email)
    if not user:
        return None, "unknown"
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM app_users WHERE id = %s", (user["id"],))
            stored = cur.fetchone()[0]
    if not _verify(password, stored):
        return None, "bad_password"
    if user.get("disabled"):
        return None, "disabled"
    if user.get("deleted_at"):
        return None, "deleted"
    touch_login(user["id"])
    return user, ""


def touch_login(user_id: int) -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE app_users SET last_login_at = NOW() WHERE id = %s", (user_id,))


# ── email codes ──────────────────────────────────────────────────────────────

def _code_hash(email: str, code: str) -> str:
    key = (settings.SECRET_KEY or "change-me").encode()
    return hmac.new(key, f"{email.lower()}:{code}".encode(), hashlib.sha256).hexdigest()


def create_code(email: str, ttl_seconds: int | None = None) -> str:
    """
    Issue a fresh code for `email` (any older unused ones are invalidated).

    `ttl_seconds` overrides the 10-minute default for invitations, which are read
    long after they arrive — see settings.AUTH_INVITE_TTL_SECONDS.
    """
    _ensure_schema()
    email = email.strip().lower()
    code = f"{secrets.randbelow(1_000_000):06d}"
    lifetime = int(ttl_seconds) if ttl_seconds else _code_ttl()
    expires = datetime.now(timezone.utc) + timedelta(seconds=lifetime)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE login_codes SET consumed_at = NOW(), code_plain = NULL "
                        "WHERE lower(email) = lower(%s) AND consumed_at IS NULL", (email,))
            cur.execute("INSERT INTO login_codes (email, code_hash, code_plain, expires_at) "
                        "VALUES (%s, %s, %s, %s)",
                        (email, _code_hash(email, code), code, expires))
    return code


def clear_codes(email: str) -> int:
    """
    Kill any live code for `email` and drop its plaintext. Returns how many were still
    live. Admin page only — this is the "作废" button beside an invitation: the invitee's
    code stops working at once, so a typo'd address can be retracted instead of waiting
    out AUTH_INVITE_TTL_SECONDS, and an address that was invited by mistake stops being
    a way in.
    """
    _ensure_schema()
    email = email.strip().lower()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE login_codes SET consumed_at = NOW(), code_plain = NULL "
                        "WHERE lower(email) = lower(%s) AND consumed_at IS NULL", (email,))
            return cur.rowcount


def verify_code(email: str, code: str) -> tuple[bool, str]:
    """Single-use, TTL'd, attempt-capped. Returns (ok, reason)."""
    _ensure_schema()
    email = email.strip().lower()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, code_hash, attempts, expires_at FROM login_codes "
                "WHERE lower(email) = lower(%s) AND consumed_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
            if not row:
                return False, "no_code"
            if row["expires_at"] < datetime.now(timezone.utc):
                return False, "expired"
            if row["attempts"] >= CODE_MAX_ATTEMPTS:
                return False, "too_many_attempts"
            if not hmac.compare_digest(row["code_hash"], _code_hash(email, (code or "").strip())):
                cur.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
                return False, "wrong_code"
            cur.execute("UPDATE login_codes SET consumed_at = NOW(), code_plain = NULL WHERE id = %s", (row["id"],))
    return True, ""


def pending_codes(limit: int = 50) -> list[dict]:
    """
    Live (unused, unexpired) codes — admin page only.

    ⚠️ Why the value is readable here: mail delivery is best-effort (the sending
    host's SPF often does not cover whatever relay this installation uses, so the
    message can land in spam) and SMTP may simply be unset. Without a way to read a live code, a registration whose mail was filtered
    dead-ends with no recovery — this column is that recovery. It is bounded by
    design: single use, `AUTH_CODE_TTL_SECONDS` lifetime, cleared the moment the code
    is consumed (or superseded), and only exposed to an admin. A code nobody can read
    is not more secure, it is just broken.
    """
    _ensure_schema()
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT email, code_plain AS code, attempts, created_at, expires_at, "
                "(expires_at > NOW()) AS alive, (consumed_at IS NOT NULL) AS consumed "
                "FROM login_codes ORDER BY created_at DESC LIMIT %s", (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if not r.get("alive") or r.get("consumed"):
            r["code"] = None
        for key in ("created_at", "expires_at"):
            if r.get(key):
                r[key] = r[key].isoformat()
    return rows


# ── access log ───────────────────────────────────────────────────────────────

def log_access(user: Optional[dict], kind: str, method: str, path: str,
               status: int, ip: str = "", ua: str = "") -> None:
    """One aggregated row per (user, kind, method, path, minute). Never raises."""
    if not user or kind not in USER_KIND:
        return
    bucket = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    try:
        _ensure_schema()
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO access_log (user_id, user_email, kind, method, path, status, ip, ua, bucket)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, kind, method, path, bucket) DO UPDATE SET
                        hits = access_log.hits + 1,
                        last_at = NOW(),
                        status = EXCLUDED.status
                """, (user["id"], user["email"], kind, method, path[:300], status,
                      (ip or "")[:60], (ua or "")[:200], bucket))
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a request
        _LOG.warning("access log write failed: %s", exc)


def list_users(q: str = "", limit: int = 200) -> list[dict]:
    """Users plus a small usage summary, for the admin page."""
    _ensure_schema()
    where_sql, params = "", []
    if q.strip():
        where_sql = "WHERE u.email ILIKE %s OR u.display_name ILIKE %s"
        params = [f"%{q.strip()}%", f"%{q.strip()}%"]
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Spelled out, not derived from `_COLS` by string surgery. That version
            # prefixed columns by `.replace('email', 'u.email')` and friends, which
            # silently stops qualifying anything a new column name does not happen to
            # contain — the kind of break that only shows up in a query with a join.
            #
            # ⚠️ The filter is interpolated with an f-string, and the `f` is load-bearing:
            # as a plain string the `{where}` line went to the server verbatim and every
            # call raised a syntax error — a 500 that emptied the whole admin page. The
            # `%s` placeholders below are still parameterised.
            sql = f"""
                SELECT u.id, u.email, u.display_name, u.role, u.disabled,
                       u.created_at, u.last_login_at,
                       u.deleted_at, u.purge_after, u.deleted_by,
                       COALESCE(a.requests, 0) AS requests,
                       COALESCE(a.agent_requests, 0) AS agent_requests,
                       a.last_seen
                FROM app_users u
                LEFT JOIN (
                    SELECT user_id, SUM(hits) AS requests,
                           SUM(hits) FILTER (WHERE kind = 'agent') AS agent_requests,
                           MAX(last_at) AS last_seen
                    FROM access_log GROUP BY user_id
                ) a ON a.user_id = u.id
                {where_sql}
                ORDER BY u.created_at DESC LIMIT %s
            """
            cur.execute(sql, params + [limit])
            rows = [dict(r) for r in cur.fetchall()]
    # ⚠️ The users table renders "N days left to restore" in the STATUS cell, and that
    # badge came out EMPTY until this line existed: `_public()` carries the deadline but
    # not the remaining days, so the page read `undefined` and drew a bare coloured pill
    # with no text in it.
    #
    # Delegated, not reimplemented: `account_lifecycle` imports this module, so the
    # import has to be deferred to here — and the rounding rule (always round UP, so
    # 29 hours never reads as "1 day") must not exist in two places to drift apart.
    try:
        from services import account_lifecycle as _lifecycle
    except ImportError:                                      # pragma: no cover
        _lifecycle = None
    out = []
    for r in rows:
        item = _public(r)
        item["requests"] = int(r.get("requests") or 0)
        item["agent_requests"] = int(r.get("agent_requests") or 0)
        item["last_seen"] = r["last_seen"].isoformat() if r.get("last_seen") else None
        item["days_left"] = (_lifecycle.days_left(item)
                             if (_lifecycle and item.get("pending_deletion")) else None)
        out.append(item)
    return out


def update_user(user_id: int, disabled: Optional[bool] = None,
                role: Optional[str] = None, display_name: Optional[str] = None) -> Optional[dict]:
    sets, params = [], []
    if disabled is not None:
        sets.append("disabled = %s"); params.append(bool(disabled))
    if role is not None:
        sets.append("role = %s"); params.append("admin" if role == "admin" else "user")
    if display_name is not None:
        sets.append("display_name = %s"); params.append(display_name.strip()[:120])
    if not sets:
        return get_user_by_id(user_id)
    params.append(user_id)
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE app_users SET {', '.join(sets)} WHERE id = %s", params)
            if cur.rowcount == 0:
                return None
    return get_user_by_id(user_id)


def set_avatar(user_id: int, animal: str, cloth: str) -> Optional[dict]:
    """Choose the beast and the shirt this account's agent wears in the office.

    ⚠️ Self-service and self-only. `update_user` next door edits any account and is
    admin-gated at the router; this one is the reader picking their own face, so it
    takes only the id it is handed and the router passes the CALLER's id, never one
    from the body. That is the whole reason it is a separate function instead of a
    branch on `update_user` — a shared setter that accepts a user id is a
    privilege-escalation waiting for a caller to pass someone else's.

    Both values are validated against the vocabularies above and REJECTED if they
    are not in them, rather than coerced to a default. Silently turning a typo into
    "tiger" is how an account ends up wearing an animal it never chose and cannot
    find a way back out of.
    """
    if animal not in AVATAR_ANIMALS or cloth not in AVATAR_CLOTHS:
        return None
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_users SET avatar_animal = %s, avatar_cloth = %s WHERE id = %s",
                (animal, cloth, user_id),
            )
            if cur.rowcount == 0:
                return None
    return get_user_by_id(user_id)


def delete_user(user_id: int) -> bool:
    """Remove the account. The access log is kept (it carries user_email already)."""
    _ensure_schema()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE id = %s", (user_id,))
            deleted = cur.rowcount
            cur.execute("UPDATE access_log SET user_id = NULL WHERE user_id = %s", (user_id,))
    return bool(deleted)


def activity(user_id: Optional[int] = None, kind: str = "", limit: int = 200) -> list[dict]:
    """Recent aggregated access rows, newest first."""
    _ensure_schema()
    where, params = [], []
    if user_id:
        where.append("user_id = %s"); params.append(int(user_id))
    if kind in USER_KIND:
        where.append("kind = %s"); params.append(kind)
    sql = ("SELECT user_email, kind, method, path, status, hits, first_at, last_at, ip "
           "FROM access_log " + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY last_at DESC LIMIT %s")
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params + [limit])
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for key in ("first_at", "last_at"):
            if r.get(key):
                r[key] = r[key].isoformat()
    return rows


# How recently an agent has to have been seen for the desk to read as "在用". Chosen to
# be longer than any plausible gap between an agent's tool calls and short enough that
# "它刚走" is true within a coffee break. A stale timestamp must NOT read as active:
# the whole point of the desk is to say whether somebody is there right now.
OFFICE_ACTIVE_WINDOW_MIN = 15


def office_seats(limit: int = 120) -> dict:
    """Who holds a desk, and whether their agent has shown up. For the landing page.

    A desk belongs to a REGISTERED ACCOUNT, not to an agent — agents are not added by
    hand, they show up. So the seat list is the account list, oldest registration first
    (that is the order somebody can reason about: your desk number does not move when a
    colleague joins), and each seat carries whether that account's agent credential has
    ever been used and when it was last used.

    ⚠️ What this can and cannot say about "has the agent been here", because the name of
    the column is load-bearing in the UI and this is the whole truth about it:

    * `access_log` is bucketed per (user, minute, method, path template) and holds no
      token identity, so this answers "has this person's agent credential been used
      against this deployment" — not "which agent", and not "which project".
    * `kind = 'agent'` is not proof of an AI agent. `api/main.py` also classifies HTTP
      Basic auth, and a Bearer session token arriving WITHOUT a cookie, as `agent`. On a
      deployment where nobody uses Basic auth the two coincide; where somebody does, their
      browser calls can light up a desk. That is accepted, not designed around.
    * An account that has never had an agent credential used has no row at all here, so
      the desk reads "未到过" — which is the truthful answer, not a missing one.

    `limit` is a rendering guard, not a product rule: every registered account is meant to
    get a desk, and past a couple of hundred the isometric room stops being readable and
    starts being a wall of pixels. The response says how many were left out so the UI can
    say so rather than quietly showing a short room.
    """
    _ensure_schema()
    cap = max(1, min(int(limit), 500))
    sql = """
        SELECT u.id, u.display_name, u.email, u.role, u.created_at,
               u.avatar_animal, u.avatar_cloth,
               COALESCE(g.hits, 0) AS agent_hits,
               GREATEST(g.last_at, work.last_at) AS agent_last_at,
               COALESCE(work.updates, '[]'::json) AS updates
        FROM app_users u
        LEFT JOIN (
            SELECT user_id, SUM(hits) AS hits, MAX(last_at) AS last_at
            FROM access_log WHERE kind = 'agent' GROUP BY user_id
        ) g ON g.user_id = u.id
        LEFT JOIN LATERAL (
            SELECT MAX(e.created_at) AS last_at,
                   json_agg(e ORDER BY e.id DESC) AS updates
            FROM (SELECT id, state, task, summary, created_at
                  FROM office_updates WHERE user_id = u.id
                  ORDER BY id DESC LIMIT 12) e
        ) work ON TRUE
        WHERE u.disabled = FALSE AND u.deleted_at IS NULL
        ORDER BY u.created_at ASC, u.id ASC
        LIMIT %s
    """
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, [cap + 1])          # one extra, to learn whether we truncated
            rows = [dict(r) for r in cur.fetchall()]
    truncated = len(rows) > cap
    rows = rows[:cap]
    now = datetime.now(timezone.utc)
    seats = []
    for r in rows:
        last = r.get("agent_last_at")
        last_iso = last.isoformat() if last else None
        if not last_iso:
            presence = "never"
        elif (now - last).total_seconds() < OFFICE_ACTIVE_WINDOW_MIN * 60:
            presence = "active"
        else:
            presence = "idle"
        # ⚠️ Two fallbacks, not one, because the second one is a real shape and the
        # first one does not cover it: `display_name` may be empty (it defaults to
        # '') and the email local part is whatever precedes the '@'. An account
        # whose address has no local part left a seat with an EMPTY name, and the
        # room draws that as a card with no text on it — a desk with no account
        # behind it, which is the one thing this feature must never produce.
        name = (r.get("display_name") or "").strip()
        if not name:
            name = (r.get("email") or "").split("@")[0].strip()
        if not name:
            name = f"#{r['id']}"
        # Same rule as the name above, for the same reason: a seat must always be
        # renderable. A value that is not in the vocabulary — a name from a newer
        # build, a hand-edited row, a half-applied migration where one ALTER
        # landed and the other did not — falls back to the default here rather
        # than travelling to the SPA to be drawn as a blank animal.
        animal = r.get("avatar_animal") or AVATAR_ANIMALS[0]
        if animal not in AVATAR_ANIMALS:
            animal = AVATAR_ANIMALS[0]
        cloth = r.get("avatar_cloth") or AVATAR_CLOTHS[1]
        if cloth not in AVATAR_CLOTHS:
            cloth = AVATAR_CLOTHS[1]
        updates = r.get("updates") or []
        work_at = datetime.fromisoformat(updates[0]["created_at"]) if updates else None
        work_stale = bool(work_at and (now - work_at).total_seconds() >= OFFICE_ACTIVE_WINDOW_MIN * 60)
        seats.append({
            "user_id": r["id"],
            "name": name,
            "role": r.get("role") or "user",
            "joined_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "agent_hits": int(r.get("agent_hits") or 0),
            "agent_last_at": last_iso,
            "presence": presence,
            "avatar": {"animal": animal, "cloth": cloth},
            "updates": updates,
            "work_stale": work_stale,
        })
    return {"seats": seats, "truncated": truncated, "active_window_min": OFFICE_ACTIVE_WINDOW_MIN}


def report_office_work(user_id: int, state: str, task: str, summary: str = "") -> dict:
    """Append an explicitly shared update for the authenticated account only."""
    _ensure_schema()
    with _db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO office_updates (user_id, state, task, summary) "
            "VALUES (%s, %s, %s, %s) RETURNING id, state, task, summary, created_at",
            (user_id, state, task.strip(), summary.strip()))
        result = dict(cur.fetchone())
        result["created_at"] = result["created_at"].isoformat()
        conn.commit()
    return result


def search_users(q: str, limit: int = 8, exclude: Optional[list[str]] = None) -> list[dict]:
    """Registered colleagues whose email or name matches ``q``, for the share pickers.

    Deliberately separate from ``list_users``: that one is the admin table (it joins the
    access log and returns every account field). A share picker needs two fields and must
    never leak the account metadata, and it must skip deactivated accounts — an account
    that cannot sign in is not somebody you can share with.

    Returns ``[]`` for a query shorter than two characters: a one-letter query matches
    most of the company, which is neither a useful suggestion nor a good way to hand out
    the address book.
    """
    _ensure_schema()
    q = (q or "").strip()
    if len(q) < 2:
        return []
    like, prefix = f"%{q}%", f"{q}%"
    blocked = [e.strip().lower() for e in (exclude or []) if e and e.strip()]
    where = "WHERE u.disabled = FALSE AND (u.email ILIKE %s OR u.display_name ILIKE %s)"
    params: list[Any] = [like, like]
    if blocked:
        where += " AND lower(u.email) <> ALL(%s)"
        params.append(blocked)
    params += [prefix, prefix, max(1, min(int(limit), 25))]
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT u.email, u.display_name, u.role
                FROM app_users u
                {where}
                ORDER BY (u.email ILIKE %s) DESC, (u.display_name ILIKE %s) DESC,
                         u.last_login_at DESC NULLS LAST, u.email
                LIMIT %s
            """, params)
            return [{"email": r["email"],
                     "display_name": r.get("display_name") or "",
                     "role": r.get("role") or "user"} for r in cur.fetchall()]
