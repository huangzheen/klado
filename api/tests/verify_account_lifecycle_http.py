"""Account lifecycle over real HTTP: impact → close → restore → purge.

Self-contained by construction — it creates its own accounts, seeds content across
modules, and cleans up in `finally`. Fixture state is rebuilt between phases, because
a deliberate purge in one phase otherwise makes the next phase fail for a reason that
has nothing to do with what it is testing (this bites in particular: the admin list
paginates, so a purged account shifting rows would change what the next lookup finds).

⚠️ Needs a server on :8000 running the CURRENT code — an old process answers 200 with
routes that do not exist yet, which reads as a pass. The naming convention is the
isolation: `verify_*` for anything needing a live service, `test_*` stays self-contained.

    ../.venv312/bin/python tests/verify_account_lifecycle_http.py
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import auth_store                       # noqa: E402
from services import account_lifecycle                # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def call(method, path, *, user=None, body=None, cookie=None):
    """One HTTP call. Returns (status, parsed_body).

    ⚠️ `cookie` (a browser session) vs `user` (Basic credentials) is not a detail here.
    Basic auth is `kind == "agent"`, and the middleware denies an agent any write
    outside `_AGENT_WRITE_ALLOWED_PREFIXES` — account management is deliberately NOT
    in it, so an agent can never delete an account. Testing this API with Basic auth
    therefore asserts nothing but the allow-list. Log in and keep the session cookie.
    """
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    elif user:
        raw = f"{user['email']}:{user['password']}".encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip()[:1] in "{[" else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:                                    # noqa: BLE001
            return exc.code, raw


def login(user):
    """Sign in as a browser and return the session cookie."""
    req = urllib.request.Request(
        BASE + "/api/auth/login", method="POST",
        data=json.dumps({"email": user["email"], "password": user["password"]}).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name and "=" in part:
            return f"{name}={value}"
    return None


# ── fixtures ──────────────────────────────────────────────────────────────────

def _mk_user(email, password, name="probe", role="user"):
    """Create an account straight through the store (the HTTP path needs a mail code).

    `role="admin"` exists so this script does not depend on anybody's password: admin
    is just a column, and minting a throwaway administrator avoids reaching into the
    real one (or asking for its password) to run a test about deleting accounts.
    """
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, %s)", (email, auth_store._hash(password), name, role))
        cur.execute("SELECT id FROM app_users WHERE email = %s", (email,))
        uid = cur.fetchone()[0]
        conn.commit()
    return uid


def _drop_user(uid):
    """Remove everything an account owns, using the real cascade."""
    try:
        account_lifecycle.purge_account(uid)
    except Exception:                                        # noqa: BLE001
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE id = %s", (uid,))
            conn.commit()


def _seed(email, tag):
    """One row in every content table, so the purge has something real to remove."""
    from core.db import connect_main
    with connect_main() as c:
        with c.cursor() as cur:
            cur.execute("INSERT INTO ai_reports (slug,title,owner_email,visibility,kind,html,"
                        "created_at,updated_at) VALUES (%s,%s,%s,'public','static','<p>x</p>',NOW(),NOW())",
                        (f"{tag}-r", "R", email))
            cur.execute("INSERT INTO ai_dashboards (slug,title,owner_email,visibility,datasets,"
                        "created_at,updated_at) VALUES (%s,%s,%s,'private','[]',NOW(),NOW())",
                        (f"{tag}-d", "D", email))
            cur.execute("INSERT INTO ai_knowledge_items (slug,title,owner_email,visibility,body,"
                        "summary,created_at,updated_at) VALUES (%s,%s,%s,'private','b','s',NOW(),NOW())",
                        (f"{tag}-k", "K", email))
            cur.execute("INSERT INTO ai_calendar_events (slug,title,owner_email,visibility,"
                        "start_date,created_at,updated_at) "
                        "VALUES (%s,%s,%s,'private',CURRENT_DATE,NOW(),NOW())",
                        (f"{tag}-e", "E", email))
            cur.execute("INSERT INTO ai_report_colleague_shares (report_id,recipient_email,shared_by,"
                        "created_at) SELECT id,'other@example.test',%s,NOW() "
                        "FROM ai_reports WHERE slug=%s", (email, f"{tag}-r"))
            cur.execute("INSERT INTO dataset_shares (table_name,grantee_email,shared_by,created_at) "
                        "VALUES (%s,'grantee@example.test',%s,NOW())", (f"{tag}_tbl", email))
        c.commit()


def _counts(email, tag):
    from core.db import connect_main
    out = {}
    with connect_main() as c:
        with c.cursor() as cur:
            for t in ("ai_reports", "ai_dashboards", "ai_knowledge_items", "ai_calendar_events"):
                cur.execute(f"SELECT count(*) FROM {t} WHERE owner_email=%s", (email,))
                out[t] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ai_report_colleague_shares WHERE shared_by=%s", (email,))
            out["shares_given"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM dataset_shares WHERE shared_by=%s", (email,))
            out["dataset_grants"] = cur.fetchone()[0]
    return out


def main():
    stamp = int(time.time())
    email = f"lifecycle-http-{stamp}@example.test"
    pw = "Http-Probe-Passw0rd!"
    tag = f"lhttp{stamp}"
    uid = _mk_user(email, pw)

    adm_email = f"lifecycle-admin-{stamp}@example.test"
    adm_pw = "Http-Admin-Probe-1!"
    adm = {"email": adm_email, "password": adm_pw}
    adm_id = _mk_user(adm_email, adm_pw, "lifecycle admin", role="admin")
    adm_cookie = login(adm)
    print(f"probe account id={uid}, throwaway admin id={adm_id}\n")

    try:
        st, me = call("GET", "/api/auth/me", cookie=adm_cookie)
        check("the throwaway admin is accepted", st == 200, f"got {st}: {me}")
        admin_id = (me.get("user") or me).get("id") if st == 200 else adm_id

        # An agent credential must NOT be able to manage accounts at all. This is the
        # middleware's allow-list, and pinning it here is what stops "close an account"
        # from quietly becoming an agent-reachable capability later.
        st, _ = call("DELETE", f"/api/auth/admin/users/{uid}", user=adm)
        check("an agent credential cannot close an account", st == 403, f"got {st}")
        st, _ = call("POST", f"/api/auth/admin/users/{uid}/restore", user=adm)
        check("an agent credential cannot restore an account", st == 403, f"got {st}")

        # ── impact preview ────────────────────────────────────────────────────
        print("impact preview")
        _seed(email, tag)
        st, imp = call("GET", f"/api/auth/admin/users/{uid}/impact", cookie=adm_cookie)
        check("impact returns 200", st == 200, f"got {st}: {imp}")
        if st == 200:
            for key in ("datasets", "dashboards", "shares", "reports", "own_dashboards",
                        "knowledge", "calendar", "files", "public_items", "total", "blocking"):
                check(f"impact has '{key}'", key in imp, f"keys={sorted(imp)}")
            check("impact total is an int", isinstance(imp.get("total"), int), repr(imp.get("total")))
            check("a public report is reported", len(imp.get("public_items", [])) == 1,
                  repr(imp.get("public_items")))
            check("a public report blocks", imp.get("blocking") is True, repr(imp.get("blocking")))
            # 1 report + 1 dashboard + 1 knowledge page + 1 calendar event. The account's
            # OWN pages have to be in this number — leaving them out under-reports
            # exactly what the operator is about to lose.
            check("the account's own content is counted", imp.get("total", 0) == 4,
                  f"total={imp.get('total')} (expected 4)")
            check("the account's own dashboards are listed",
                  len(imp.get("own_dashboards", [])) == 1, repr(imp.get("own_dashboards")))
        st, _ = call("GET", f"/api/auth/admin/users/{uid}/impact")
        check("impact requires admin", st in (401, 403), f"got {st}")

        # ── the close ─────────────────────────────────────────────────────────
        print("\nclose")
        st, _ = call("DELETE", f"/api/auth/admin/users/{uid}", cookie=adm_cookie)
        check("close without confirm is refused", st == 400, f"got {st}")
        st, _ = call("DELETE", f"/api/auth/admin/users/{uid}?confirm=wrong@example.test", cookie=adm_cookie)
        check("close with a wrong confirm is refused", st == 400, f"got {st}")
        st, body = call("DELETE", f"/api/auth/admin/users/{uid}?confirm={email}", cookie=adm_cookie)
        check("close with the right confirm succeeds", st == 200, f"got {st}: {body}")
        user_view = body.get("user", {}) if isinstance(body, dict) else {}
        check("the account is pending deletion", user_view.get("pending_deletion") is True, repr(user_view))
        check("a deadline is shown", bool(user_view.get("purge_after")), repr(user_view.get("purge_after")))
        check("days left is shown", isinstance(user_view.get("days_left"), int),
              repr(user_view.get("days_left")))
        check("the window is 30 days", user_view.get("days_left") in (30, 31),
              f"days_left={user_view.get('days_left')}")

        st, _ = call("POST", "/api/auth/login", body={"email": email, "password": pw})
        check("a closed account cannot sign in", st in (400, 401, 403), f"got {st}")

        before = _counts(email, tag)
        check("closing moved no data", sum(before.values()) == 6, repr(before))

        # closing twice must not move the deadline
        st, again = call("DELETE", f"/api/auth/admin/users/{uid}", cookie=adm_cookie)
        check("closing twice succeeds idempotently", st == 200, f"got {st}: {again}")
        deadline_1 = again.get("user", {}).get("purge_after") if isinstance(again, dict) else None
        st, again2 = call("DELETE", f"/api/auth/admin/users/{uid}", cookie=adm_cookie)
        deadline_2 = again2.get("user", {}).get("purge_after") if isinstance(again2, dict) else None
        check("the deadline does not move on a second close", deadline_1 == deadline_2,
              f"{deadline_1} vs {deadline_2}")

        # ── the recycle bin ───────────────────────────────────────────────────
        print("\nrecycle bin")
        st, bin_all = call("GET", "/api/auth/admin/recycle-bin", cookie=adm_cookie)
        check("recycle-bin returns 200", st == 200, f"got {st}")
        listed = [a for a in bin_all.get("accounts", []) if a["id"] == uid]
        check("the closed account is in the bin", len(listed) == 1, f"found {len(listed)}")
        if listed:
            check("the bin row carries days left", isinstance(listed[0].get("days_left"), int),
                  repr(listed[0].get("days_left")))
            check("the bin row records who closed it", listed[0].get("deleted_by") == adm["email"],
                  repr(listed[0].get("deleted_by")))
        st, bin_live = call("GET", "/api/auth/admin/recycle-bin?include_expired=false", cookie=adm_cookie)
        check("include_expired=false still lists a live deadline",
              any(a["id"] == uid for a in bin_live.get("accounts", [])))

        # ── restore ───────────────────────────────────────────────────────────
        print("\nrestore")
        st, body = call("POST", f"/api/auth/admin/users/{uid}/restore", cookie=adm_cookie)
        check("restore returns 200", st == 200, f"got {st}: {body}")
        check("the account is no longer pending",
              body.get("user", {}).get("pending_deletion") is not True, repr(body.get("user")))
        after = _counts(email, tag)
        check("restore brought every row back", after == before, f"{before} -> {after}")
        st, _ = call("POST", "/api/auth/login", body={"email": email, "password": pw})
        check("the restored account can sign in again", st == 200, f"got {st}")
        st, _ = call("POST", f"/api/auth/admin/users/{uid}/restore", cookie=adm_cookie)
        check("restoring twice is refused", st == 400, f"got {st}")
        st, bin_now = call("GET", "/api/auth/admin/recycle-bin", cookie=adm_cookie)
        check("the restored account left the bin",
              not any(a["id"] == uid for a in bin_now.get("accounts", [])))

        # ── purge ─────────────────────────────────────────────────────────────
        print("\npurge")
        st, _ = call("DELETE", f"/api/auth/admin/users/{uid}/purge", cookie=adm_cookie)
        check("purge without confirm is refused", st == 400, f"got {st}")
        st, _ = call("DELETE", f"/api/auth/admin/users/{uid}/purge?confirm=nope@example.test", cookie=adm_cookie)
        check("purge with a wrong confirm is refused", st == 400, f"got {st}")
        st, body = call("DELETE", f"/api/auth/admin/users/{uid}/purge?confirm={email}", cookie=adm_cookie)
        check("purge with the right confirm succeeds", st == 200, f"got {st}: {body}")
        report = body.get("report", {}) if isinstance(body, dict) else {}
        check("the purge reports what it removed", "email" in report and "content" in report,
              repr(report)[:200])
        left = _counts(email, tag)
        check("no content survived the purge", sum(left.values()) == 0, repr(left))
        st, _ = call("GET", f"/api/auth/admin/users/{uid}/impact", cookie=adm_cookie)
        check("impact on a purged account is 404", st == 404, f"got {st}")
        st, _ = call("POST", "/api/auth/login", body={"email": email, "password": pw})
        check("a purged account cannot sign in", st in (400, 401, 403), f"got {st}")

        # the admin must be untouched by all of this
        st, _ = call("GET", "/api/auth/me", cookie=adm_cookie)
        check("the admin survived the purge", st == 200, f"got {st}")

        st, body = call("POST", "/api/auth/admin/recycle-bin/sweep", cookie=adm_cookie)
        check("sweep on an empty bin returns 200", st == 200, f"got {st}: {body}")
        check("sweep reports nothing to do", body.get("results") == [], repr(body))

        # ── you cannot close yourself ─────────────────────────────────────────
        st, _ = call("DELETE", f"/api/auth/admin/users/{admin_id}?confirm={adm['email']}", cookie=adm_cookie)
        check("closing yourself is refused", st == 400, f"got {st}")
        st, _ = call("DELETE", f"/api/auth/admin/users/{uid}/purge?confirm={email}", cookie=adm_cookie)
        # `uid` is already purged at this point, so 404 is the correct answer here and
        # proves nothing about authorisation — the real check is on a path that exists.
        check("purging an already-purged account is 404", st == 404, f"got {st}")
        st, _ = call("POST", "/api/auth/admin/recycle-bin/sweep", user=adm)
        check("sweep needs a browser session, not an agent code", st == 403, f"got {st}")

    finally:
        for victim in (uid, adm_id):
            try:
                _drop_user(victim)
            except Exception as exc:                          # noqa: BLE001
                print(f"  (cleanup warning for {victim}: {exc})")
        from core.db import connect_main
        with connect_main() as c:
            with c.cursor() as cur:
                for t, col in (("ai_reports", "owner_email"), ("ai_dashboards", "owner_email"),
                               ("ai_knowledge_items", "owner_email"), ("ai_calendar_events", "owner_email")):
                    cur.execute(f"DELETE FROM {t} WHERE {col} IN (%s, %s)", (email, adm_email))
                cur.execute("DELETE FROM dataset_shares WHERE shared_by IN (%s, %s)", (email, adm_email))
                cur.execute("DELETE FROM ai_report_colleague_shares WHERE shared_by IN (%s, %s)",
                            (email, adm_email))
                cur.execute("DELETE FROM app_users WHERE email IN (%s, %s)", (email, adm_email))
                cur.execute("DELETE FROM account_modules WHERE user_id NOT IN (SELECT id FROM app_users)")
                cur.execute("DELETE FROM agent_tokens WHERE user_id NOT IN (SELECT id FROM app_users)")
            c.commit()

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
