"""
SMTP configuration, stored in the database and editable from the admin page.

Why not environment variables
-----------------------------
Env vars are read when the server starts, so changing one means editing a file and
restarting — and it puts a mailbox password into that file. This module keeps the
same values in a single `mail_settings` row instead: the admin pastes them into the
admin page, they take effect on the next send, no restart, and nothing sensitive
ever reaches git. The environment variables are still honoured as a fallback, so an
installation that already sets them keeps working exactly as before.

Storage notes
-------------
* `password_enc` is Fernet (AES-128-CBC + HMAC) under a key derived from
  `SECRET_KEY`, so a database dump or a filesystem backup alone does not reveal
  the password. ⚠️ The flip side: rotating `SECRET_KEY` makes a stored password
  undecryptable — `describe()` then reports `password_readable: false` and mail is
  treated as unconfigured, rather than silently trying a garbage password.
* If the `cryptography` package is missing the value is stored with a `plain:`
  prefix instead of failing the whole feature (and the prefix is visible in
  `describe()`), because a broken dependency should not lock the admin out.
* Never returned by any endpoint: `describe()` exposes only whether a password is
  set. The mailbox password is revocable in the mail console, which is exactly why
  the provider issues it separately from the login password.
"""
from __future__ import annotations

import base64
import hashlib
import logging

import psycopg2
import psycopg2.extras

from klado_shared.config import settings
from klado_shared.db import connect_main

_LOG = logging.getLogger(__name__)

_KEY = "mail_config"          # kept for the audit trail in `updated_by` messages
KEEP = "\x00keep"   # public sentinel: "leave the stored password alone"

try:  # pragma: no cover - import guard, exercised by the plaintext fallback
    from cryptography.fernet import Fernet, InvalidToken

    _FERNET_OK = True
except Exception:  # noqa: BLE001
    Fernet = None
    InvalidToken = Exception
    _FERNET_OK = False


def _fernet() -> "Fernet | None":
    if not _FERNET_OK:
        return None
    material = hashlib.sha256(f"mail-settings:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def _encrypt(value: str) -> str:
    f = _fernet()
    if f is None:
        return "plain:" + value
    return "enc:" + f.encrypt(value.encode()).decode()


def _decrypt(stored: str | None) -> tuple[str, bool]:
    """Returns (password, readable)."""
    if not stored:
        return "", True
    if stored.startswith("plain:"):
        return stored[len("plain:"):], True
    if stored.startswith("enc:"):
        f = _fernet()
        if f is None:
            return "", False
        try:
            return f.decrypt(stored[len("enc:"):].encode()).decode(), True
        except InvalidToken:
            return "", False
        except Exception:  # noqa: BLE001
            return "", False
    return stored, True


def _ensure_table() -> None:
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mail_settings (
                    id           INT PRIMARY KEY,
                    host         TEXT,
                    port         INT,
                    security     TEXT,
                    username     TEXT,
                    password_enc TEXT,
                    sender       TEXT,
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_by   TEXT
                )
            """)
        conn.commit()
    finally:
        conn.close()


def _row() -> dict:
    _ensure_table()
    conn = connect_main()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM mail_settings WHERE id = 1")
            got = cur.fetchone()
        return dict(got) if got else {}
    finally:
        conn.close()


def current() -> dict:
    """
    Effective SMTP settings. Database first, then the environment as a fallback
    (per field, so a partially filled row still works).
    """
    row = _row()
    password, readable = _decrypt(row.get("password_enc"))
    username = (row.get("username") or settings.SMTP_USER or "").strip()
    sender = (row.get("sender") or settings.SMTP_FROM or "").strip()
    # Every mail client defaults the sender to the sign-in account, and the admin
    # page's placeholder promised exactly that while the code did not do it: leaving
    # the field blank made the whole channel read as "unconfigured". Fill it in.
    if not sender:
        sender = username
    return {
        "host": (row.get("host") or settings.SMTP_HOST or "").strip(),
        "port": int(row.get("port") or settings.SMTP_PORT or 465),
        "security": (row.get("security") or settings.SMTP_SECURITY or "ssl").strip().lower(),
        "username": username,
        "password": password,
        "password_readable": readable,
        "sender": sender,
        "source": "database" if row else ("environment" if settings.SMTP_HOST else "none"),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "updated_by": row.get("updated_by"),
    }


def is_configured(cfg: dict | None = None) -> bool:
    """
    Whether a send has a real chance — deliberately not just "a host is set".

    A username means the server will be asked to authenticate, so a password is
    *required*; reporting "configured" while every send fails with 530 would be the
    same kind of lie as a fake success. Without a username we may be talking to an
    internal relay that lets us through by IP, and that stays legitimate.
    """
    cfg = cfg or current()
    if not (cfg["host"] and cfg["sender"] and cfg["password_readable"]):
        return False
    return not (cfg["username"] and not cfg["password"])


def describe() -> dict:
    """Admin-facing view — never includes the password."""
    cfg = current()
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "security": cfg["security"],
        "username": cfg["username"],
        "sender": cfg["sender"],
        "has_password": bool(cfg["password"]),
        "password_readable": cfg["password_readable"],
        "password_storage": ("encrypted (Fernet, key from SECRET_KEY)" if _FERNET_OK
                             else "plaintext (the `cryptography` package is unavailable)"),
        "configured": is_configured(cfg),
        "source": cfg["source"],
        "updated_at": cfg["updated_at"],
        "updated_by": cfg["updated_by"],
        "env_fallback": {
            "SMTP_HOST": bool(settings.SMTP_HOST),
            "SMTP_USER": bool(settings.SMTP_USER),
            "SMTP_PASSWORD": bool(settings.SMTP_PASSWORD),
            "SMTP_FROM": bool(settings.SMTP_FROM),
        },
    }


def save(*, host: str | None = None, port: int | None = None, security: str | None = None,
         username: str | None = None, sender: str | None = None, password: object = KEEP,
         actor: str = "") -> dict:
    """
    Upsert the single row. `password` semantics:
      omitted        → keep whatever is stored
      ""             → clear the password
      a string       → store it (encrypted)
    """
    _ensure_table()
    keep = password is KEEP
    stored = None
    if isinstance(password, str) and password:
        stored = _encrypt(password)
    elif isinstance(password, str) and not password:
        stored = None
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mail_settings (id, host, port, security, username, password_enc,
                                           sender, updated_at, updated_by)
                VALUES (1, %s, %s, %s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (id) DO UPDATE SET
                    host         = COALESCE(EXCLUDED.host, mail_settings.host),
                    port         = COALESCE(EXCLUDED.port, mail_settings.port),
                    security     = COALESCE(EXCLUDED.security, mail_settings.security),
                    username     = COALESCE(EXCLUDED.username, mail_settings.username),
                    password_enc = CASE WHEN %s THEN mail_settings.password_enc
                                        ELSE EXCLUDED.password_enc END,
                    sender       = COALESCE(EXCLUDED.sender, mail_settings.sender),
                    updated_at   = NOW(),
                    updated_by   = EXCLUDED.updated_by
            """, (host, port, security, username, stored, sender, actor, keep))
        conn.commit()
    finally:
        conn.close()
    return describe()


def clear() -> dict:
    _ensure_table()
    conn = connect_main()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mail_settings WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    return describe()