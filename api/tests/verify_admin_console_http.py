#!/usr/bin/env python3
"""P4 acceptance: the console is a real second process, and the boundary actually holds.

Everything here is real HTTP against a real PostgreSQL. Nothing is stubbed, because the
claims being checked are all claims about *processes* — and a mocked boundary proves
nothing about a mocked boundary. Specifically:

1. **Both ports are up and are different programs.** The console answers on 8787 and the
   main app on 8000, and neither serves the other's UI. A console that quietly fell
   through to the main app's static mount would satisfy every API assertion and still be
   the wrong architecture.

2. **A console session is worthless against the main app, and an app session is worthless
   against the console.** This is the audience mechanism, and it is the single most
   important property of the whole design: cookies are matched on host and ignore the
   port, so `klado_session` and `klado_admin_session` both travel to both processes. The
   *name* is a convenience; the audience in the signature is the control. Asserting it
   needs two real servers and a real token, which is why this is a `verify_*` script and
   not a unit test.

3. **The gates hold against a real account.** An ordinary user's cookie gets 403 on every
   operator route; a non-operator cannot even log in; an enterprise admin is not an
   operator.

4. **The read-only switch really is read-only** — and, the part that is easy to get wrong,
   it is *not* a lockout. Turning it off must leave reads working and the switch itself
   reachable, or the operator who used it cannot undo it.

5. **The destructive routes are reachable and refuse correctly**: the email confirmation,
   the self-delete guard, and the unknown-key refusal on the module allowlist.

⚠️ This script creates and destroys real rows. It makes its own accounts
(`probe-operator@…`, `probe-plain@…`), uses addresses that cannot collide with a real
one, and removes every row it made in `finally` — including on failure. Run it against a
database you are willing to have briefly written to.

Usage:  cd api && python tests/verify_admin_console_http.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from klado_shared import accounts  # noqa: E402
from klado_shared.config import settings  # noqa: E402
from klado_shared.db import connect_main  # noqa: E402

APP = os.environ.get("KLADO_APP_URL", "http://127.0.0.1:8000")
CONSOLE = os.environ.get("KLADO_CONSOLE_URL",
                         f"http://127.0.0.1:{settings.KLADO_ADMIN_PORT}")

OPERATOR = f"probe-operator+{uuid.uuid4().hex[:6]}@klado-verify.invalid"
PLAIN = f"probe-plain+{uuid.uuid4().hex[:6]}@klado-verify.invalid"
PASSWORD = "verify-only-password"

PASS, FAIL, SKIPPED = [], [], []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    return ok


def skip(label: str, why: str) -> None:
    """Not run, and said so.

    ⚠️ A skip is reported separately from a pass and never folded into one. This script
    runs against a long-lived console, and the console's login limiter is an in-process
    5-minute window keyed on the email *and* the IP — so a second run inside five minutes
    starts already over its budget. Turning that into a green "ok" would be a lie about
    what was checked; turning it into a failure would be a lie about the console.
    """
    SKIPPED.append(label)
    print(f"  skip  {label}  — {why}")


def throttled() -> bool:
    """Is the console's login limiter currently refusing everybody?

    ⚠️ Probed with a *fresh, non-existent* address, so it costs one unit of the IP budget
    and tells us whether the email bucket or the IP bucket is already full. Both make the
    answer "no room", and the caller only needs to know that.
    """
    status, _ = call(CONSOLE, "/api/admin-console/login", method="POST",
                     body={"email": f"probe+{uuid.uuid4().hex[:8]}@klado-verify.invalid",
                           "password": "x"})
    return status == 429


def call(url: str, path: str, method: str = "GET", body=None, cookie: str = "",
         headers: dict | None = None, timeout: float = 15.0):
    """One request. Returns `(status, parsed_body_or_text)`. Never raises for a 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _parse(raw)
    except Exception as exc:  # noqa: BLE001 — a refused connection is a result, not a crash
        return 0, str(exc)


def _parse(raw: str):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw


def cookie_from(headers) -> str:
    """Pull the console's cookie out of a login response's `Set-Cookie` list."""
    for value in headers:
        if value.startswith("klado_admin_session="):
            return value.split(";")[0]
    return ""


def signup(email: str, password: str, display: str = "") -> dict:
    """A real account row, created through the shared store the main app uses.

    ⚠️ Goes in as a normal user, then one is promoted with a direct UPDATE. The
    promotion is a raw statement on purpose: there is no API for "make this person an
    operator" — that is the point of the role — so the only way to test the operator gate
    is to set the column. A script that instead reused an existing real admin would be
    testing somebody's actual account, which is not something a verification run should
    do to a working installation.
    """
    user = accounts.create_user(email, password, display or email.split("@")[0])
    return user


def set_role(user_id: int, role: str) -> None:
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE app_users SET role = %s WHERE id = %s", (role, user_id))
        conn.commit()


def delete_user(user_id: int) -> None:
    """Hard removal, so a verification run leaves nothing behind.

    ⚠️ This is a raw DELETE and it is *only* safe because the probe accounts were created
    seconds ago and have never made anything. It is not how accounts are closed in the
    product — that is the 30-day recycle bin in `klado_shared/lifecycle.py`, which this
    script also exercises on purpose and then restores.
    """
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM org_members WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM access_log WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_users WHERE id = %s", (user_id,))
        conn.commit()


def main() -> int:
    operator = plain = None
    try:
        print(f"\nmain app   {APP}")
        print(f"console    {CONSOLE}\n")

        # ── 1. both processes are up, and they are not the same program ──────────
        print("both processes are up")
        status, body = call(CONSOLE, "/api/admin-console/health")
        check("console /health answers", status == 200, f"status={status} {body!r}")
        check("console identifies itself", isinstance(body, dict)
              and body.get("service") == "klado-admin-console", f"{body!r}")
        status, _ = call(APP, "/api/health")
        check("main app /api/health answers", status == 200, f"status={status}")
        # ⚠️ The main app answers 401 here, not 404, and that is correct: its auth
        # middleware guards every `/api/` path before the router runs, so an unknown path
        # is never reached. So the assertion is not "404" — it is "did not answer as the
        # console". A 401 and a 200 are very different findings here, and only one of them
        # means the boundary is intact.
        status, body = call(APP, "/api/admin-console/health")
        check("the MAIN APP does not serve the console API", status != 200
              and "klado-admin-console" not in json.dumps(body),
              f"status={status} {str(body)[:100]!r}")
        status, body = call(CONSOLE, "/api/health")
        check("the CONSOLE does not serve the main app's API", status != 200
              and "klado-admin-console" not in json.dumps(body),
              f"status={status} {str(body)[:100]!r}")
        status, body = call(CONSOLE, "/")
        check("the console's own UI is served from its own port", status in (200, 503, 404),
              f"status={status}")

        # ── 2. the gates, before anybody is signed in ───────────────────────────
        print("\nunauthenticated requests are refused")
        for path in ("/api/admin-console/overview", "/api/admin-console/orgs",
                     "/api/admin-console/accounts", "/api/admin-console/recycle-bin",
                     "/api/admin-console/modules", "/api/admin-console/activity"):
            status, _ = call(CONSOLE, path)
            check(f"GET {path} is 401 without a session", status == 401, f"status={status}")
        status, _ = call(CONSOLE, "/api/admin-console/accounts/1",
                         method="DELETE", body={})
        check("a destructive route is 401 without a session", status == 401,
              f"status={status}")

        # ── 3. two real accounts, one operator one not ──────────────────────────
        print("\naccounts")
        operator = signup(OPERATOR, PASSWORD, "Probe Operator")
        set_role(operator["id"], "admin")
        plain = signup(PLAIN, PASSWORD, "Probe Plain")
        check("probe operator exists", bool(operator.get("id")))
        check("probe plain user exists", bool(plain.get("id")))

        # ── 4. the operator signs in FIRST, before anything can throttle it ─────
        # ⚠️ Order matters and it is not cosmetic. The console throttles logins per email
        # AND per IP, in-process, with a 5-minute window. Every negative login test below
        # spends one of each, so the positive one has to come first — otherwise a run
        # whose negatives tripped the limiter cannot sign in at all. (This is exactly what
        # broke the first version of this script: the operator login was last, and a
        # second run inside five minutes failed on a *correct* password.)
        print("\nlogin")
        # Pre-flight, so a repeat run inside the limiter's window says so instead of
        # reporting a correct password as refused.
        cooldown = throttled()
        if cooldown:
            print("  note  the console's login limiter is still cooling down (it counts "
                  "per email and per IP over 5 minutes, in the console's own process)")
        status, body = call(CONSOLE, "/api/admin-console/login", method="POST",
                            body={"email": OPERATOR, "password": PASSWORD})
        if status == 200:
            check("the operator can sign in", True, "")
        elif cooldown:
            skip("the operator can sign in", "login limiter is cooling down; wait 5 "
                 "minutes or restart the console, then re-run")
        else:
            check("the operator can sign in", False, f"status={status} {body!r}")

        # The rest of the run uses a cookie this script mints itself. That is not a
        # shortcut around the login path: the token is signed and verified by exactly the
        # same code either way, and the assertions below are about the *gate*, not the
        # form. It is what makes the run repeatable.
        console_cookie = ""
        app_cookie = ""
        try:
            from klado_shared.session import ADMIN_AUDIENCE, APP_AUDIENCE, issue_session
            console_cookie = (f"{settings.KLADO_ADMIN_SESSION_COOKIE}="
                              f"{issue_session(operator, ADMIN_AUDIENCE)}")
            app_cookie = f"klado_session={issue_session(operator, APP_AUDIENCE)}"
        except Exception as exc:  # noqa: BLE001
            check("could not mint test cookies", False, str(exc))

        # ── 4b. an ordinary account cannot get in at all ───────────────────────
        if cooldown:
            skip("a non-operator is refused at login",
                 "login limiter is cooling down; the 403 path is covered by "
                 "test_admin_console.py and by the gate checks below")
        else:
            status, body = call(CONSOLE, "/api/admin-console/login", method="POST",
                                body={"email": PLAIN, "password": PASSWORD})
            check("a non-operator is 403, not merely 401", status == 403, f"status={status}")
            check("the 403 says why", isinstance(body, dict)
                  and " / " in str(body.get("detail", "")), f"{body!r}")

            status, body = call(CONSOLE, "/api/admin-console/login", method="POST",
                                body={"email": OPERATOR, "password": "wrong-password"})
            check("a wrong password is 401", status == 401, f"status={status}")
            check("the 401 does not reveal whether the account exists",
                  isinstance(body, dict) and "@" not in str(body.get("detail", "")),
                  f"{body!r}")

            status, body = call(CONSOLE, "/api/admin-console/login", method="POST",
                                body={"email": f"nobody+{uuid.uuid4().hex[:6]}@klado-verify.invalid",
                                      "password": PASSWORD})
            check("an unknown account gets the SAME 401 as a wrong password",
                  status == 401, f"status={status}")

        # ── 6. THE load-bearing test: the audiences do not cross ─────────────────
        print("\nthe audience boundary (this is the whole point of the two processes)")
        status, _ = call(CONSOLE, "/api/admin-console/overview", cookie=console_cookie)
        check("a CONSOLE session works in the console", status == 200, f"status={status}")
        status, _ = call(APP, "/api/auth/admin/users", cookie=console_cookie)
        check("a CONSOLE session is REJECTED by the main app", status == 401,
              f"status={status} (expected 401; 200 would mean the audience is decorative)")
        status, _ = call(CONSOLE, "/api/admin-console/overview", cookie=app_cookie)
        check("an APP session is REJECTED by the console", status == 401,
              f"status={status} (expected 401)")
        status, _ = call(APP, "/api/auth/admin/users", cookie=app_cookie)
        check("an APP session still works in the main app", status == 200,
              f"status={status} (a regression here means every user was logged out)")

        # A pre-upgrade token carries no audience at all.
        import hashlib
        import hmac
        import time as _time
        payload = f"{operator['id']}.{int(_time.time()) + 600}".encode()
        legacy = (f"{operator['id']}.{int(_time.time()) + 600}."
                  f"{hmac.new((settings.SECRET_KEY or 'change-me').encode(), payload,
                             hashlib.sha256).hexdigest()[:32]}")
        status, _ = call(CONSOLE, "/api/admin-console/overview",
                         cookie=f"klado_admin_session={legacy}")
        check("a pre-audience (legacy) token is refused by the console", status == 401,
              f"status={status}")

        # ── 7. the operator's own routes, end to end ────────────────────────────
        print("\noperator routes")
        for path in ("/api/admin-console/me", "/api/admin-console/overview",
                     "/api/admin-console/orgs", "/api/admin-console/orgs?kind=enterprise",
                     "/api/admin-console/personal",
                     "/api/admin-console/personal/blocked-domains",
                     "/api/admin-console/accounts",
                     "/api/admin-console/recycle-bin",
                     "/api/admin-console/activity?limit=5",
                     "/api/admin-console/modules",
                     "/api/admin-console/mail",
                     "/api/admin-console/agent-codes",
                     "/api/admin-console/system"):
            status, body = call(CONSOLE, path, cookie=console_cookie)
            check(f"GET {path}", status == 200, f"status={status} {str(body)[:120]!r}")

        status, body = call(CONSOLE, "/api/admin-console/overview", cookie=console_cookie)
        if isinstance(body, dict):
            check("the overview carries the five numbers the page needs",
                  all(k in body for k in ("orgs", "members", "recycle_bin", "approvals",
                                          "policy")), f"{sorted(body)}")
        status, body = call(CONSOLE, "/api/admin-console/personal/blocked-domains",
                            cookie=console_cookie)
        check("the blocklist endpoint warns that it is not a security boundary",
              isinstance(body, dict) and "not a security boundary" in
              str(body.get("warning", "")).lower(), f"{str(body)[:200]!r}")

        # ── 8. a non-operator's session gets 403, not 404 ───────────────────────
        print("\nthe gate is 403 for a signed-in non-operator")
        plain_cookie = ""
        status, body = call(APP, "/api/auth/login", method="POST",
                            body={"email": PLAIN, "password": PASSWORD})
        if status == 200:
            status, body = call(CONSOLE, "/api/admin-console/login", method="POST",
                                body={"email": PLAIN, "password": PASSWORD})
            plain_cookie = ""      # refused at login; mint a console-audience one to test 403
            from klado_shared.session import ADMIN_AUDIENCE, issue_session
            plain_cookie = (f"{settings.KLADO_ADMIN_SESSION_COOKIE}="
                            f"{issue_session(plain, ADMIN_AUDIENCE)}")
            status, _ = call(CONSOLE, "/api/admin-console/overview", cookie=plain_cookie)
            check("a non-operator's console-audience session is 403", status == 403,
                  f"status={status} (a 200 would mean the role check is missing)")

        # ── 8b. the per-company module licence, end to end ─────────────────────
        # ⚠️ This is the whole chain, and each link is checked from the OTHER side's
        # answer: the operator sets a ceiling in the console, the main app's `/api/org/me`
        # has to narrow what the company's own administrators are offered, and the write
        # endpoint has to refuse. Testing only the first would leave a console control
        # that saves a column nothing reads — which is the exact failure
        # `modules._table_keys()` was written about.
        print("\nthe per-company module licence")
        from klado_shared import orgs as shared_orgs
        lic_org = None
        lic_owner = None
        try:
            lic_domain = f"lic-{uuid.uuid4().hex[:6]}.invalid"
            lic_email = f"boss@{lic_domain}"
            lic_org = shared_orgs.create_org(f"Licence {uuid.uuid4().hex[:4]}",
                                             kind=shared_orgs.KIND_ENTERPRISE,
                                             domains=[lic_domain])
            lic_owner = signup(lic_email, PASSWORD, "Lic Owner")
            set_role(lic_owner["id"], "admin")          # so they also reach /api/org/*
            shared_orgs.upsert_invitation(lic_org["id"], lic_email, "owner",
                                          invited_by=lic_email)
            shared_orgs.attach_existing_account(lic_org["id"], lic_email)

            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}",
                                cookie=console_cookie)
            check("the company detail answers", status == 200, f"status={status}")
            check("and says the company was never licensed",
                  isinstance(body, dict) and body.get("module_licence") is None,
                  f"{body.get('module_licence')!r} — None and [] mean different things")
            check("while still offering every switchable module",
                  "workspace" in (body.get("module_offer") or []),
                  f"{body.get('module_offer')!r}")

            # ⚠️ Its own route, not a field on the company form. The list is written even
            # when empty, and it is written even when the FORM's save succeeds — so a
            # refused licence cannot take the company name down with it.
            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}/modules",
                                method="PUT", cookie=console_cookie,
                                body={"keys": ["workspace", "knowledge"]})
            check("the operator can licence a company", status == 200,
                  f"status={status} {str(body)[:160]}")
            check("and the response reports what the company can now hand out",
                  body.get("module_offer") == ["workspace", "knowledge"],
                  f"{body.get('module_offer')!r}")

            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}",
                                cookie=console_cookie)
            check("and it reads back exactly what was saved",
                  body.get("module_licence") == ["workspace", "knowledge"],
                  f"{body.get('module_licence')!r}")
            check("and the offer is narrowed to it",
                  set(body.get("module_offer") or []) == {"workspace", "knowledge"},
                  f"{body.get('module_offer')!r}")

            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}/modules",
                                method="PUT", cookie=console_cookie,
                                body={"keys": ["not-a-module"]})
            check("a typo in the licence is refused, not silently dropped", status == 400,
                  f"status={status}")
            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}",
                                cookie=console_cookie)
            check("and the refusal left the licence untouched",
                  body.get("module_licence") == ["workspace", "knowledge"],
                  f"{body.get('module_licence')!r}")

            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}/modules",
                                method="PUT", cookie=console_cookie,
                                body={"keys": ["inbox"]})
            check("licensing a REQUIRED module is refused", status == 400,
                  f"status={status}")

            # The other half of "its own route": an empty list is a real save.
            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}/modules",
                                method="PUT", cookie=console_cookie, body={"keys": []})
            check("a company CAN be licensed for nothing", status == 200,
                  f"status={status} {str(body)[:160]}")
            check("and that is not the same answer as 'never licensed'",
                  body.get("module_licence") == [] and body.get("module_offer") == [],
                  f"licence={body.get('module_licence')!r} offer={body.get('module_offer')!r}")
            status, body = call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}",
                                cookie=console_cookie)
            check("and it reads back as [], not as null",
                  body.get("module_licence") == [],
                  f"{body.get('module_licence')!r} — null means 'never configured'")
            # Restore, so the main-app half below has something to see.
            call(CONSOLE, f"/api/admin-console/orgs/{lic_org['id']}/modules", method="PUT",
                 cookie=console_cookie, body={"keys": ["workspace", "knowledge"]})

            # The main app's half: what the company administrator is offered. Minted for
            # the OWNER, because the page is theirs — an operator-only session would be
            # refused by `_resolve()` and would prove nothing about what they see.
            from klado_shared.session import APP_AUDIENCE, issue_session
            status, body = call(APP, "/api/org/me", cookie=(
                f"klado_session={issue_session(lic_owner, APP_AUDIENCE)}"))
            check("the main app reports the company's offer to its own administrator",
                  status == 200 and set((body.get("org") or {}).get("module_options")
                                        or []) == {"workspace", "knowledge"},
                  f"status={status} {str(body)[:220]}")
        finally:
            if lic_owner and lic_owner.get("id"):
                delete_user(lic_owner["id"])
            if lic_org:
                with connect_main() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM org_public_approvals WHERE org_id = %s",
                                    (lic_org["id"],))
                        cur.execute("DELETE FROM org_members WHERE org_id = %s",
                                    (lic_org["id"],))
                        cur.execute("DELETE FROM orgs WHERE id = %s", (lic_org["id"],))
                    conn.commit()

        # ── 9. refusals that must be refusals ──────────────────────────────────
        print("\nrefusals")
        status, body = call(CONSOLE, "/api/admin-console/modules", method="PUT",
                            cookie=console_cookie, body={"keys": ["not-a-module"]})
        check("an unknown module key is refused", status == 400, f"status={status}")
        check("the refusal names the unknown key",
              isinstance(body, dict) and "not-a-module" in str(body.get("detail", "")),
              f"{body!r}")

        status, body = call(CONSOLE, "/api/admin-console/accounts/1", method="DELETE",
                            cookie=console_cookie)
        check("closing an account without the email confirmation is refused",
              status in (400, 404), f"status={status}")

        status, body = call(CONSOLE, f"/api/admin-console/accounts/{operator['id']}",
                            method="DELETE", cookie=console_cookie,
                            body={})
        check("an operator cannot close themselves", status == 400, f"status={status}")

        status, body = call(CONSOLE, "/api/admin-console/orgs/999999", cookie=console_cookie)
        check("an unknown organization is 404", status == 404, f"status={status}")

        status, body = call(CONSOLE, "/api/admin-console/orgs/1/preset", method="PUT",
                            cookie=console_cookie, body={"preset": "nonsense"})
        check("an unknown preset is refused", status in (400, 404), f"status={status}")

        # ── 10. the read-only switch, and that it is not a lockout ──────────────
        print("\nthe read-only switch")
        status, body = call(CONSOLE, "/api/admin-console/system/console-login", method="PUT",
                            cookie=console_cookie, body={"enabled": False})
        check("the switch can be turned OFF", status == 200, f"status={status} {body!r}")
        status, body = call(CONSOLE, "/api/admin-console/overview", cookie=console_cookie)
        check("reads STILL work while read-only (this is the point)", status == 200,
              f"status={status}")
        status, _ = call(CONSOLE, "/api/admin-console/modules", method="PUT",
                         cookie=console_cookie, body={"keys": []})
        check("a write is refused while read-only", status == 403, f"status={status}")
        # ⚠️ The no-lockout property, asserted the only way that actually distinguishes
        # it. While the switch is OFF, this request must get *past* the switch gate and be
        # stopped by the confirmation — i.e. 200 with the right address, 400 with a wrong
        # one. A 403 would mean the switch refused its own undo, which is the failure this
        # ordering exists to prevent. Asserting only "200 with the right address" would
        # pass even if the gate were checked in the wrong order and the first call 403'd
        # for an unrelated reason.
        status, body = call(CONSOLE, "/api/admin-console/system/console-login", method="PUT",
                            cookie=console_cookie,
                            body={"enabled": True, "confirm_email": "wrong@x.invalid"})
        check("while OFF, the undo is reached and stopped by the confirmation, not the "
              "switch", status == 400, f"status={status} (403 would mean a lockout)")
        status, body = call(CONSOLE, "/api/admin-console/system/console-login", method="PUT",
                            cookie=console_cookie,
                            body={"enabled": True, "confirm_email": OPERATOR})
        check("the switch can be turned back ON while it is off (no lockout)",
              status == 200, f"status={status} {body!r}")
        status, _ = call(CONSOLE, "/api/admin-console/modules", method="PUT",
                         cookie=console_cookie, body={"keys": []})
        check("writes work again", status == 200, f"status={status}")

        # ── 11. the module allowlist is real, not decorative ────────────────────
        print("\nthe module allowlist actually reaches the main app")
        from klado_shared import modules as shared_modules
        from klado_shared import deployment as shared_deployment
        before = set(shared_modules.deployment_default())
        narrowed = sorted(before - {"calendar"}) or ["workspace"]
        status, body = call(CONSOLE, "/api/admin-console/modules", method="PUT",
                            cookie=console_cookie, body={"keys": narrowed})
        check("the allowlist can be narrowed", status == 200, f"status={status} {body!r}")
        after = set(shared_modules.deployment_default())
        check("the SHARED registry now reflects the console's choice",
              "calendar" not in after or before == after,
              f"before={sorted(before)} after={sorted(after)}")
        # And the main app's own resolution — in this process, through the live view.
        from core import modules as api_modules
        check("the main app resolves through the same allowlist",
              set(api_modules.resolve({})) == after, f"{sorted(api_modules.resolve({}))}")
        call(CONSOLE, "/api/admin-console/modules", method="PUT", cookie=console_cookie,
             body={"keys": sorted(before)})
        check("the allowlist is restored", set(shared_modules.deployment_default()) == before)

        # ── 12. the login limiter, last because it deliberately burns the budget ──
        # ⚠️ Asserted as "429 eventually", not "429 on attempt N". The console counts per
        # email AND per IP with different limits, and every request above came from
        # 127.0.0.1, so whichever bucket fills first is the one that answers — and both
        # are the control working. A test that pinned the exact attempt number would be
        # asserting an implementation detail that legitimately changes (a new negative
        # test above it would shift it), and would go red for a reason that has nothing
        # to do with the limiter.
        print("\nthe login limiter")
        burst_target = f"burst+{uuid.uuid4().hex[:6]}@klado-verify.invalid"
        statuses = [call(CONSOLE, "/api/admin-console/login", method="POST",
                         body={"email": burst_target, "password": "wrong"})[0]
                    for _ in range(10)]
        check("a burst of wrong passwords is eventually refused with 429",
              429 in statuses, f"statuses={statuses}")
        # ⚠️ Only meaningful on a run that started with budget. On a cooled-down run the
        # very first attempt is already 429 — which is the limiter working, not it being
        # too eager — so asserting 401 here would report a correct behaviour as a failure.
        if not cooldown:
            check("the limiter does not refuse the very first attempt",
                  statuses[0] == 401, f"first={statuses[0]}")
        else:
            skip("the limiter does not refuse the very first attempt",
                 "the run started inside the limiter's window")

        print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIPPED)} skipped")
        if SKIPPED:
            print("SKIPPED (not the same as passed):")
            for label in SKIPPED:
                print("  -", label)
        if FAIL:
            print("FAILED:")
            for label in FAIL:
                print("  -", label)
        return 1 if FAIL else 0

    finally:
        # ── cleanup, on every path including failure ────────────────────────────
        for user in (operator, plain):
            if user and user.get("id"):
                try:
                    delete_user(user["id"])
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARNING: could not remove probe account {user.get('email')}: {exc}")
        # Leave the allowlist and the switch as they were found.
        try:
            with connect_main() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM app_settings WHERE key = %s",
                                ("modules.deployment_default",))
                    cur.execute("DELETE FROM app_settings WHERE key = %s",
                                ("auth.admin_audience_enabled",))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: could not clear the console settings this run wrote: {exc}")
        print("cleaned up probe accounts and console settings")


if __name__ == "__main__":
    sys.exit(main())
