#!/usr/bin/env python3
"""P6 acceptance: the enterprise settings surface, over real HTTP.

The unit suites pin the rules with the stores faked. What they cannot reach is the part
P6 actually changed, which is a set of claims about the running product:

1. **A company administrator can open Settings at all.** The page used to be gated on
   `role === 'admin'` — the operator flag — so an enterprise admin was bounced off the
   page that exists for them. That gate is now `/api/org/me`, and the interesting case
   is not "it returned 200", it is "an ordinary member still gets nothing".

2. **The roster is the COMPANY's, and it is the company's because the server decided
   so.** A member of a different company, or no company at all, must not be able to read
   it by naming another one. `?org_id=` is refused rather than honoured.

3. **The three module states are three different writes.** This is the one worth a live
   run: `on → off → inherit` has to produce a row, a row, and no row, and a client that
   sends `false` for the third one produces a stale entitlement that outlives the
   deployment setting. Asserting "the response is 200" would pass against that bug.

4. **Clearing one module does not clear the others.** Two functions in the store are
   both called "clear" and take different arguments; binding the wrong one returns 200
   and silently discards every other narrowing.

5. **Settings changes are narrow, not destructive.** Narrowing the suffix list flags
   members instead of removing them, emptying it is refused, and tightening the policy
   does not retroactively revoke anything already shared.

6. **The stuff that left this page really left it**, and is reachable from the console
   instead: the recycle bin, the system mailbox, the deployment allowlist and the
   operator's agent codes.

⚠️ This script creates and destroys real rows in the real database: one organization, two
accounts, one publication request. Every row is removed in `finally`, including on
failure, and the organization's own settings are the ones it created. Run it against a
database you are willing to have briefly written to.

Usage:  ../.venv312/bin/python tests/verify_org_settings_http.py
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

from klado_shared import accounts, orgs              # noqa: E402
from klado_shared.db import connect_main             # noqa: E402

APP = os.environ.get("KLADO_APP_URL", "http://127.0.0.1:8000")
CONSOLE = os.environ.get("KLADO_CONSOLE_URL", "http://127.0.0.1:8787")

# ⚠️ A domain of its own, not a real one. The suffix list is what decides who counts as
# staff, so a probe sharing `klado.test` with somebody else's probe would make the
# "outside the suffix is refused" assertion depend on run order.
DOMAIN = f"corp-{uuid.uuid4().hex[:6]}.invalid"
OWNER = f"boss@{DOMAIN}"
MATE = f"mate@{DOMAIN}"
STRANGER = f"stranger@{DOMAIN}"
PASSWORD = "verify-only-password"
ORG_NAME = f"Verify Corp {uuid.uuid4().hex[:4]}"

PASS, FAIL = [], []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    return ok


def call(path, method="GET", body=None, cookie="", headers=None, base=None, timeout=15.0):
    """One request. Returns `(status, parsed_body_or_text)`. Never raises for a 4xx/5xx."""
    url = (base or APP) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 — a refused connection is a result, not a crash
        return 0, str(exc)


def _parse(raw):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw


def cookie_from(headers):
    for value in headers:
        if value.startswith("klado_session="):
            return value.split(";")[0]
    return ""


def login(email, password=PASSWORD):
    """A real session, through the real login route. Returns the cookie or ""."""
    req = urllib.request.Request(
        APP + "/api/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return cookie_from(resp.headers.get_all("Set-Cookie") or [])
    except urllib.error.HTTPError:
        return ""


def overrides_of(user_id):
    """The account's module rows, read straight from the table.

    ⚠️ Deliberately not the API. The point of these assertions is what is PERSISTED, and
    an endpoint that renders from the same rows it just wrote would report success for a
    write that did not happen.
    """
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT module_key, enabled FROM account_modules "
                        "WHERE user_id = %s ORDER BY module_key", (user_id,))
            return {k: bool(v) for k, v in cur.fetchall()}


def deployment_keys():
    with connect_main() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s",
                        ("modules.deployment_default",))
            row = cur.fetchone()
    if not row:
        return None
    return list((row[0] or {}).get("keys") or [])


def main() -> int:
    org = owner = mate = stranger = approval_id = None
    saved_allowlist = deployment_keys()
    try:
        print(f"\nmain app   {APP}")
        print(f"console    {CONSOLE}\n")

        # ── fixtures ───────────────────────────────────────────────────────────
        print("a real company, with a real owner and a real colleague")
        org = orgs.create_org(ORG_NAME, kind=orgs.KIND_ENTERPRISE, domains=[DOMAIN])
        owner = accounts.create_user(OWNER, PASSWORD, "boss")
        mate = accounts.create_user(MATE, PASSWORD, "mate")
        stranger = accounts.create_user(STRANGER, PASSWORD, "stranger")
        # ⚠️ The two steps the product itself takes, in order: the address joins the
        # roster as an invitation, and only then can an EXISTING account be attached to
        # it. Attaching first is refused, which is the correct answer — a company is not
        # a bag of accounts somebody can be dropped into.
        for address, role in ((OWNER, "owner"), (MATE, "member"), (STRANGER, "member")):
            orgs.upsert_invitation(org["id"], address, role, invited_by=OWNER)
            orgs.attach_existing_account(org["id"], address)

        boss = login(OWNER)
        mate_cookie = login(MATE)
        check("the owner signed in", bool(boss))
        check("the colleague signed in", bool(mate_cookie))

        # ── 1. who may open Settings ──────────────────────────────────────────
        print("\n/who may administer a company/")
        status, body = call("/api/org/me", cookie=boss)
        check("the owner is an enterprise administrator",
              status == 200 and body.get("is_org_admin") is True
              and body.get("is_operator") is False, f"{body!r}")
        check("the owner's own company comes back with no parameter",
              (body.get("org") or {}).get("id") == org["id"], f"{body!r}")
        status, body = call("/api/org/me", cookie=mate_cookie)
        check("an ordinary member is not an administrator",
              status == 200 and body.get("is_org_admin") is False, f"{body!r}")

        # ── 2. the roster is the company's ─────────────────────────────────────
        print("\n/the roster is scoped to the caller's own company/")
        status, body = call("/api/org/members", cookie=boss)
        check("the owner reads their own roster", status == 200, f"status={status}")
        emails = {m["email"].lower() for m in (body.get("members") or [])}
        check("the roster holds exactly the company's three accounts",
              emails == {OWNER.lower(), MATE.lower(), STRANGER.lower()},
              f"got {sorted(emails)}")
        member_ids = {m["email"].lower(): m["id"] for m in (body.get("members") or [])}
        target = member_ids[MATE.lower()]

        status, body = call("/api/org/members", cookie=mate_cookie)
        check("an ordinary member is refused the roster", status == 403,
              f"status={status}")

        other = orgs.create_org(f"Other {uuid.uuid4().hex[:4]}",
                                kind=orgs.KIND_ENTERPRISE,
                                domains=[f"other-{uuid.uuid4().hex[:4]}.invalid"])
        try:
            status, body = call(f"/api/org/members?org_id={other['id']}", cookie=boss)
            check("naming another company is 404, not the other company's data",
                  status == 404, f"status={status} {str(body)[:80]!r}")
        finally:
            with connect_main() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM orgs WHERE id = %s", (other["id"],))
                conn.commit()

        # ── 3. the three module states are three different writes ──────────────
        print("\n/the module cell has three states, and they are three writes/")
        # ⚠️ A module this deployment actually offers, named rather than discovered, so
        # a change to the registry cannot make this script quietly assert nothing.
        modkey = next((m.key for m in orgs and []) or "", "")
        modkey = "workspace"
        if modkey not in deployment_keys_or_all():
            modkey = next((k for k in ("workspace", "knowledge", "calendar")
                           if k in deployment_keys_or_all()), "workspace")
        path = f"/api/org/members/{target}/modules/{modkey}"

        status, body = call(path, "PUT", {"enabled": True}, cookie=boss)
        check("click 1 (on) writes a row", status == 200, f"status={status} {body!r}")
        check("and the row says true", overrides_of(mate["id"]).get(modkey) is True,
              f"{overrides_of(mate['id'])}")

        status, body = call(path, "PUT", {"enabled": False}, cookie=boss)
        check("click 2 (off) writes a row", status == 200, f"status={status}")
        check("and the row says false", overrides_of(mate["id"]).get(modkey) is False,
              f"{overrides_of(mate['id'])}")

        status, body = call(path, "PUT", {"enabled": None}, cookie=boss)
        check("click 3 (inherit) is accepted", status == 200, f"status={status}")
        check("⚠️ and leaves NO row behind — a false here is a stale entitlement",
              modkey not in overrides_of(mate["id"]), f"{overrides_of(mate['id'])}")

        # ── 4. clearing one module is not clearing the row ─────────────────────
        print("\n/clearing ONE module keeps the others/")
        other_key = next((k for k in ("knowledge", "calendar", "dashboard")
                          if k != modkey and k in deployment_keys_or_all()), None)
        if other_key is None:
            check("a second switchable module exists to test with", False,
                  "the deployment offers only one switchable module")
        else:
            call(path, "PUT", {"enabled": True}, cookie=boss)
            call(f"/api/org/members/{target}/modules/{other_key}", "PUT",
                 {"enabled": False}, cookie=boss)
            before = overrides_of(mate["id"])
            check("two modules are narrowed before the reset",
                  before.get(modkey) is True and before.get(other_key) is False,
                  f"{before}")
            status, body = call(path, "PUT", {"enabled": None}, cookie=boss)
            after = overrides_of(mate["id"])
            check("clearing one dropped only that one", status == 200
                  and modkey not in after and after.get(other_key) is False, f"{after}")

            status, body = call(f"/api/org/members/{target}/modules", "DELETE",
                                cookie=boss)
            after = overrides_of(mate["id"])
            check("'恢复默认' on the row cleared what was left", status == 200
                  and after == {}, f"status={status} {after!r}")
            check("and it reported the count it removed", isinstance(body, dict)
                  and body.get("cleared", 0) >= 1, f"{body!r}")

        # ── 5. settings are narrow, not destructive ────────────────────────────
        print("\n/enterprise settings/")
        status, body = call("/api/org/settings", "PUT",
                            {"name": ORG_NAME + " GmbH"}, cookie=boss)
        check("the name is saved", status == 200
              and body["org"]["name"] == ORG_NAME + " GmbH", f"{body!r}")
        check("⚠️ and the suffix list was NOT blanked by a name-only form",
              body["org"]["email_domains"] == [DOMAIN], f"{body['org']!r}")

        status, body = call("/api/org/settings", "PUT",
                            {"email_domains": [DOMAIN, f"sub.{DOMAIN}"]}, cookie=boss)
        check("narrowing the suffix list upward is allowed", status == 200
              and len(body["org"]["email_domains"]) == 2, f"{body!r}")

        status, body = call("/api/org/settings", "PUT", {"email_domains": []}, cookie=boss)
        check("emptying the suffix list is refused", status == 400, f"status={status}")
        check("⚠️ and says WHICH field — an operator must not be left guessing",
              isinstance(body, dict) and "suffix" in str(body.get("detail", "")),
              f"{body!r}")

        # Narrow the list so one member no longer matches, and check they are FLAGGED.
        call("/api/org/settings", "PUT", {"email_domains": [f"sub.{DOMAIN}"]}, cookie=boss)
        status, body = call("/api/org/members", cookie=boss)
        flagged = {m["email"].lower(): m.get("domain_ok") for m in body.get("members", [])}
        check("a member outside the narrowed list is FLAGGED, not removed",
              flagged.get(MATE.lower()) is False and flagged.get(MATE.lower()) is not None
              and MATE.lower() in flagged, f"{flagged}")
        check("and the company still has all three rows", len(flagged) == 3, f"{flagged}")
        call("/api/org/settings", "PUT", {"email_domains": [DOMAIN]}, cookie=boss)

        # ── 6. the policy presets ──────────────────────────────────────────────
        print("\n/the two policy dials, as three presets/")
        for preset in ("global", "internal", "external"):
            status, body = call("/api/org/scopes", "PUT", {"preset": preset}, cookie=boss)
            check(f"preset '{preset}' is applied", status == 200
                  and body.get("preset") == preset, f"status={status} {body!r}")
        status, body = call("/api/org/scopes", "PUT", {"preset": "wide-open"}, cookie=boss)
        check("an unknown preset is refused", status == 400, f"status={status}")

        # ── 7. the approvals queue ─────────────────────────────────────────────
        print("\n/the queue a human has to empty/")
        request_row = orgs.request_public_approval(
            MATE, "report", "verify-slug-" + uuid.uuid4().hex[:6], "anyone_link")
        approval_id = request_row["id"]
        status, body = call("/api/org/approvals?status=pending", cookie=boss)
        check("the owner sees the request", status == 200
              and any(a["id"] == approval_id for a in body.get("approvals", [])),
              f"status={status} {body!r}")
        status, body = call("/api/org/approvals?status=pending", cookie=mate_cookie)
        check("a plain member is refused the queue", status == 403, f"status={status}")

        status, body = call(f"/api/org/approvals/{approval_id}/maybe", "POST", cookie=boss)
        check("an unknown decision is 400, not 404", status == 400, f"status={status}")
        status, body = call(f"/api/org/approvals/{approval_id}/approve", "POST", cookie=boss)
        check("approving works", status == 200
              and body.get("approval", {}).get("status") == "approved", f"{body!r}")
        status, body = call("/api/org/approvals?status=pending", cookie=boss)
        check("and it leaves the pending queue",
              not any(a["id"] == approval_id for a in body.get("approvals", [])),
              f"{body!r}")

        # ── 8. what an agent code may and may not do here ───────────────────────
        # ⚠️ The skill package tells agents exactly this, so it is checked against the
        # running server rather than against the source of a list. A prefix added to
        # `_AGENT_WRITE_ALLOWED_PREFIXES` is a silent capability grant: nothing crashes,
        # the API just quietly does more than its documentation claims.
        print("\n/a machine credential, against the same endpoints/")
        status, body = call("/api/auth/agent-tokens", "POST", {"label": "org-probe"},
                            cookie=boss)
        agent_code = (body.get("code") if isinstance(body, dict) else None)
        agent_token_id = ((body.get("token") or {}).get("id")
                          if isinstance(body, dict) else None)
        check("the owner can issue an agent code", bool(agent_code), f"{body!r}")
        agent = {"X-Klado-Agent-Token": agent_code} if agent_code else {}
        if agent_code:
            # Read the company's own state first, so the comparison below is "the agent
            # changed nothing" rather than "the name is still what step 4 set it to" —
            # an earlier step renames the company and never renames it back.
            _before = call("/api/org/me", cookie=boss)[1].get("org", {})
            status, body = call("/api/org/me", headers=agent)
            check("⚠️ but it may READ its own organization",
                  status == 200 and bool(body.get("org")) and body.get("org_id") == org["id"],
                  f"status={status} {body!r}")
            for path_, method_, payload in (
                    ("/api/org/invite", "POST", {"email": f"x@{DOMAIN}", "role": "member"}),
                    ("/api/org/scopes", "PUT", {"preset": "global"}),
                    ("/api/org/settings", "PUT", {"name": "Renamed By Agent"}),
                    ("/api/org/members/1/modules", "DELETE", None),
                    ("/api/org/approvals/1/approve", "POST", None),
                    ("/api/org/public-requests", "POST",
                     {"target_type": "report", "target_id": "x", "kind": "public"})):
                status, body = call(path_, method_, payload, headers=agent)
                check(f"⚠️ but it may not write {method_} {path_}", status == 403,
                      f"status={status} {body!r}")
            # …and the writes really did not happen. A 403 could equally have come from
            # the handler for some unrelated reason; the company's own settings are the
            # thing a machine would have changed.
            _after = call("/api/org/me", cookie=boss)[1].get("org", {})
            check("and the company is untouched",
                  _after.get("name") == _before.get("name")
                  and _after.get("share_scope") == _before.get("share_scope")
                  and _after.get("public_scope") == _before.get("public_scope"),
                  f"before={_before.get('name')!r}/{_before.get('share_scope')!r} "
                  f"after={_after.get('name')!r}/{_after.get('share_scope')!r}")
        if agent_token_id:
            status, body = call(f"/api/auth/agent-tokens/{agent_token_id}", "DELETE",
                                cookie=boss)
            check("the probe code is revoked again", status == 200, f"status={status} {body!r}")

        # ── 9. the publication request body is a closed vocabulary ──────────────
        print("\n/opening a publication request/")
        slug = "verify-req-" + uuid.uuid4().hex[:6]
        call("/api/org/scopes", "PUT", {"preset": "internal"}, cookie=boss)
        status, body = call("/api/org/public-requests", "POST",
                            {"target_type": "report", "target_id": slug, "kind": "public"},
                            cookie=mate_cookie)
        check("a member can submit one", status == 200, f"status={status} {body!r}")

        def _requests_for_this_document():
            with connect_main() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM org_public_approvals "
                                "WHERE target_type = 'report' AND target_id = %s",
                                (slug,))
                    return cur.fetchone()[0]

        status, body = call("/api/org/public-requests", "POST",
                            {"target_type": "report", "target_id": slug, "kind": "public"},
                            cookie=mate_cookie)
        rows = _requests_for_this_document()
        check("⚠️ submitting twice leaves ONE row, not a queue of copies",
              status == 200 and rows == 1, f"status={status} rows={rows} {body!r}")
        for bad, why in (
                ({"target_type": "report", "target_id": slug, "kind": "carrier_pigeon"},
                 "an unknown channel"),
                ({"target_type": "spaceship", "target_id": slug, "kind": "public"},
                 "an unknown target type"),
                ({"target_type": "report", "kind": "public"}, "a missing target id")):
            status, body = call("/api/org/public-requests", "POST", bad, cookie=mate_cookie)
            check(f"{why} is 400, not a silent 200", status == 400,
                  f"status={status} {body!r}")

        # ── 10. what left this page ────────────────────────────────────────────
        print("\n/what moved to the console, and is really there/")
        for path_, label in (
            ("/api/admin-console/recycle-bin", "the recycle bin"),
            ("/api/admin-console/mail", "the system mailbox"),
            ("/api/admin-console/modules", "the deployment allowlist"),
            ("/api/admin-console/orgs", "the company list"),
        ):
            status, body = call(path_, base=CONSOLE)
            check(f"{label} is served by the console", status in (200, 401),
                  f"status={status} {str(body)[:80]!r}")
            status, _ = call(path_)
            check(f"⚠️ {label} is NOT served by the main app", status != 200,
                  f"status={status}")

        status, body = call("/api/org/members", cookie=mate_cookie)
        check("⚠️ the operator's agent codes are not on the company roster",
              status == 403, f"status={status}")

        print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            print("FAILED:")
            for label in FAIL:
                print("  -", label)
        return 1 if FAIL else 0

    finally:
        # ── cleanup, on every path including failure ────────────────────────────
        for user in (owner, mate, stranger):
            if user and user.get("id"):
                try:
                    with connect_main() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM account_modules WHERE user_id = %s",
                                        (user["id"],))
                            cur.execute("DELETE FROM org_members WHERE user_id = %s",
                                        (user["id"],))
                            cur.execute("DELETE FROM access_log WHERE user_id = %s",
                                        (user["id"],))
                            cur.execute("DELETE FROM app_users WHERE id = %s",
                                        (user["id"],))
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARNING: could not remove {user.get('email')}: {exc}")
        if org and org.get("id"):
            try:
                with connect_main() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM org_public_approvals WHERE org_id = %s",
                                    (org["id"],))
                        cur.execute("DELETE FROM org_members WHERE org_id = %s",
                                    (org["id"],))
                        cur.execute("DELETE FROM orgs WHERE id = %s", (org["id"],))
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: could not remove the probe organization: {exc}")
        if saved_allowlist is None:
            with connect_main() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM app_settings WHERE key = %s",
                                ("modules.deployment_default",))
                conn.commit()
        print("cleaned up the probe company, its three accounts and its requests")


def deployment_keys_or_all():
    """What a switchable module can be tested against: the saved allowlist if there is
    one, otherwise every key in the registry (the environment is unconfigured here)."""
    from core import modules as core_modules
    saved = deployment_keys()
    if saved is not None:
        return set(saved)
    return set(core_modules.ALL_KEYS)


if __name__ == "__main__":
    sys.exit(main())
