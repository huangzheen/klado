"""Deployment-level settings the admin console owns, in the `app_settings` table.

Why a table and not environment variables
-----------------------------------------
Every other process setting lives in `.env`, and that is the right place for them: a
container is restarted to change one, and the file is not served to anybody. These two are
different in kind.

* `blocked_email_domains` is a **policy an operator edits while working** — turning a
  public mailbox domain on and off as a company decides what "self-registered" means. A
  restart per edit is not a workflow.
* `auth.admin_audience_enabled` is the console's own **kill switch**. It has to be
  flippable to `false` from a signed-in console session, because the moment somebody
  suspects the console has been walked into, "log everybody out and refuse new logins" is
  the first action — and an env var cannot be changed by the person who needs to.

The table already exists (`api/routers/settings.py` creates it for `build_version`), so
this adds no schema. Both processes read the same rows, which is the whole point: the
main app has to honour the same blocklist at `register/complete` that the console edits.

⚠️ **`blocked_email_domains` is not a security boundary, and the docstring on the getter
says so at length.** It stops a company employee from registering a second, unmanaged
identity on a public mailbox — a seat and compliance problem. It stops *nothing* about
data leaving: a pull, a download or a share are data rights, not account states, and the
share gate in `orgs.py` is what governs those. Calling this list a security control would
be the most dangerous thing in this file.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from klado_shared.db import connect_main

_LOG = logging.getLogger(__name__)

KEY_BLOCKED_DOMAINS = "blocked_email_domains"
KEY_CONSOLE_LOGIN = "auth.admin_audience_enabled"

#: Public mailbox providers. A self-registration on one of these is, in the common case,
#: somebody's personal address rather than a company seat — which is the whole reason the
#: list exists. It is a *starting point to edit*, not a fixed truth: every deployment that
#: ships a different set overwrites it on first write.
DEFAULT_BLOCKED_DOMAINS = (
    "gmail.com", "qq.com", "163.com", "126.com", "139.com", "sina.com",
    "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me",
)

DDL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _db():
    return connect_main()


def ensure_table() -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()


def read(key: str, default: Any = None) -> Any:
    """One setting. A missing row, an unparseable body and a dead database all read as
    the default — the console must still come up on a database where nothing has been
    written yet, and a settings read must never be the reason an operator cannot log in.
    """
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
        if not row:
            return default
        value = row[0]
        if isinstance(value, str):
            return json.loads(value)
        return value
    except Exception:  # noqa: BLE001 — a settings read is never worth a 500
        _LOG.warning("app_settings[%s] unreadable; using the built-in default", key,
                     exc_info=True)
        return default


def write(key: str, value: Any, actor: str = "") -> Any:
    """Upsert one setting and return what was stored.

    `actor` is not a column here — it is folded into the JSON body, because
    `app_settings` has exactly one writer per key and an audit trail of "who changed the
    blocklist" is worth more than a schema migration. The `access_log` table already
    carries the request-level trail; this is the in-place context.
    """
    body = value if isinstance(value, dict) else {"value": value}
    body = {**body, "updated_by": (actor or "").strip().lower()}
    # Let the database own the timestamp rather than formatting one here, then read the
    # row back so the response shows what was actually persisted.
    with _db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = NOW() RETURNING key, value, updated_at",
                (key, json.dumps(body)))
            row = dict(cur.fetchone())
        conn.commit()
    if row.get("updated_at"):
        row["updated_at"] = row["updated_at"].isoformat()
    return row


# ── blocked_email_domains ────────────────────────────────────────────────────

def _clean_domain(value: Any) -> str:
    """One bare domain: lowercased, no `@`, no scheme, no path, no leading dot.

    ⚠️ Fail-soft on purpose, and it is the only validator in this file that is: a stored
    value is edited by hand in a text box, and a single bad line must cost that one
    domain, not the whole list. `None` means "drop it".
    """
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text:
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.split("/", 1)[0].strip().strip(".")
    if not text or "." not in text or " " in text:
        return ""
    return text


def blocked_email_domains() -> list[str]:
    """The domains self-registration refuses to turn into a personal account.

    ⚠️ **Not a security boundary.** It exists for seats and compliance: it stops a company
    employee opening a second identity on a public mailbox, outside the organization's
    management. It does **not** stop data leaving — a pull, a download or a share are
    data rights, not account states, and `orgs.py`'s share gate is what governs those.
    Clearing this list is a supported configuration: any well-formed address can then
    self-register as a personal user, and the organization it lands in is the one its
    domain matches, or none.
    """
    stored = read(KEY_BLOCKED_DOMAINS, None)
    values: Optional[list] = None
    if isinstance(stored, dict):
        values = stored.get("domains")
    elif isinstance(stored, list):
        values = stored
    if values is None:
        return list(DEFAULT_BLOCKED_DOMAINS)
    cleaned, seen = [], set()
    for item in values:
        domain = _clean_domain(item)
        if domain and domain not in seen:
            seen.add(domain)
            cleaned.append(domain)
    return cleaned


def set_blocked_email_domains(domains: list, actor: str = "") -> list[str]:
    """Replace the list. Returns the cleaned result — which is what is now in force.

    ⚠️ The response is the *stored* list, not the one that was submitted. A text box
    where somebody typed `Gmail.com, @163.com` and a list with a blank entry in it is
    going to happen; echoing back what was kept is the only way the operator learns which
    line was dropped, and a silent difference between the box and the rule is a policy
    that quietly does not apply.
    """
    ensure_table()
    cleaned, seen = [], set()
    for item in (domains or []):
        domain = _clean_domain(item)
        if domain and domain not in seen:
            seen.add(domain)
            cleaned.append(domain)
    write(KEY_BLOCKED_DOMAINS, {"domains": cleaned}, actor=actor)
    return cleaned


def is_blocked_domain(domain: str) -> bool:
    """Case-insensitive exact match. Subdomains are NOT covered — see the note below.

    ⚠️ `evil.gmail.com` is not blocked by a list containing `gmail.com`, and that is
    deliberate rather than an oversight: matching suffixes would block every address on
    any domain that merely *ends* with the string, which is not what an operator typing
    "block the public mail providers" means. A domain that resolves to a provider's mail
    service has to be listed as itself.
    """
    return _clean_domain(domain) in set(blocked_email_domains())


# ── auth.admin_audience_enabled ──────────────────────────────────────────────

def console_login_enabled(default: bool = True) -> bool:
    """May anybody still sign in to the console? The kill switch.

    ⚠️ **Anything that is not an explicit `False` means ON.** This is not a style choice;
    the first version read the row and did `bool(value)`, and a row that was missing, or
    that held JSON `null`, or that could not be read at all, all produced `False` — which
    is the console quietly becoming read-only, with no error anywhere and no way back for
    the operator who did not switch it off. A kill switch whose failure mode is "the thing
    it controls stops being reachable" is a lockout wearing a label, so the default has to
    be the permissive direction and only a stored `false` may take authority away.

    `default=True` for the same reason: a brand-new database, and a database whose
    `app_settings` table does not exist yet, both mean "nothing has been decided", and
    "nothing has been decided" must not read as "decided against you".
    """
    raw = read(KEY_CONSOLE_LOGIN, None)
    if isinstance(raw, dict):
        raw = raw.get("enabled")
    if raw is None:
        return default
    return bool(raw)


def set_console_login_enabled(enabled: bool, actor: str = "") -> bool:
    ensure_table()
    write(KEY_CONSOLE_LOGIN, {"enabled": bool(enabled)}, actor=actor)
    return bool(enabled)
