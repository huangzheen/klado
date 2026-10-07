"""End-to-end: modules, dataset sharing and the Dashboard module, over real HTTP.

Everything else in this repo's test suite is either pure logic or a stubbed page.
This one signs in as three real accounts against a real server and a real
PostgreSQL, because the claims worth checking here are all claims about *identity*:

  * a module switched off for an account really does 403 its endpoints AND its
    standalone `/d/` page — the reader is the easy one to forget;
  * a dataset share grants a grantee the rows and nothing else — not the file,
    not the owner's other datasets;
  * `/api/dashboard/query` cannot reach a table the caller cannot see, and uses the
    same guard as Data Center (a second allow-list is the failure mode to fear);
  * a shared dashboard is readable and immutable; a private one is a 404, not a 403;
  * deleting a dashboard takes its shares with it.

    .venv312/bin/python api/tests/verify_dashboard_e2e.py
"""
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                      # noqa: E402
from services import auth_store      # noqa: E402
from services import dashboard_store, dataset_shares   # noqa: E402
from routers import dashboard, data_center, reports      # noqa: E402

PORT = int(os.environ.get("DASH_E2E_PORT", "18877"))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "dash-e2e-pass-1"

OWNER = ("owner", "dash-owner@example.com")
GUEST = ("guest", "dash-guest@example.com")
STRANGER = ("stranger", "dash-stranger@example.com")
# Deleting a dataset is deliberately operator-only (`core/access.py::_ADMIN_ONLY_ROUTES`),
# so the HTTP path is exercised with a throwaway admin rather than by loosening that
# policy inside a test. The data-layer cascade is pinned separately in
# tests/test_dataset_shares.py::CascadeTests.
OP = ("op", "dash-op@example.com")

FAILURES = []
PENDING = []          # (name, fn) — run after the server is up

TABLE = "e2e_dash_sales"
DASH = "e2e-dash"

DASH_HTML = (
    "<!doctype html><html><head><title>E2E</title></head><body>"
    "<h1 id='h'>E2E dashboard</h1></body></html>"
)


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


def _auth(who):
    return {"Authorization": "Basic "
            + base64.b64encode(f"{who[1]}:{PASSWORD}".encode()).decode()}


def _browser(who):
    session = requests.Session()
    r = session.post(f"{BASE}/api/auth/login", json={"email": who[1], "password": PASSWORD})
    if r.status_code != 200:
        print("login refused (%s) for %s" % (r.status_code, who[1]))
    return session


# ── fixtures ─────────────────────────────────────────────────────────────────

def _prepare():
    _prepare_accounts()
    _prepare_datasets()
    _reset_dashboard()


def _prepare_accounts():
    auth_store._ensure_schema()
    dashboard_store.ensure_schema()
    dataset_shares.ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        for name, email, role in ((OWNER[0], OWNER[1], "user"), (GUEST[0], GUEST[1], "user"),
                                  (STRANGER[0], STRANGER[1], "user"), (OP[0], OP[1], "admin")):
            cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
            cur.execute(
                "INSERT INTO app_users (email, password_hash, display_name, role) "
                "VALUES (%s, %s, %s, %s)", (email, auth_store._hash(PASSWORD), name, role))
        cur.execute("DELETE FROM account_modules WHERE user_id IN "
                    "(SELECT id FROM app_users WHERE email = ANY(%s))",
                    ([e for _, e in (OWNER, GUEST, STRANGER, OP)],))
        conn.commit()


def _prepare_datasets():
    """(Re)create the two fixture datasets. Called again before each group that
    queries them: the cascade group deliberately re-owns the recycled name, and a
    stale fixture turns later assertions into false failures that look like bugs."""
    with reports._db() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}")
        cur.execute(f"CREATE TABLE public.{TABLE} (region text, amount int)")
        cur.execute(f"INSERT INTO public.{TABLE} VALUES ('north', 10), ('south', 20)")
        cur.execute(f"DELETE FROM public._import_registry WHERE table_name = %s", (TABLE,))
        cur.execute(
            "INSERT INTO public._import_registry (table_name, display_name, target_db, "
            "row_count, fingerprint, columns, source_file, sheet_name, modules, "
            "description, owner_email) "
            "VALUES (%s, 'E2E sales', 'pg', 2, 'e2e', '{}'::jsonb, 'e2e.csv', 'Sheet1', '{}', '', %s)",
            (TABLE, OWNER[1]))
        # A second, unrelated table the grantee must never be able to read.
        cur.execute("DROP TABLE IF EXISTS public.e2e_dash_private")
        cur.execute("CREATE TABLE public.e2e_dash_private (secret text)")
        cur.execute("INSERT INTO public.e2e_dash_private VALUES ('classified')")
        cur.execute("DELETE FROM public._import_registry WHERE table_name = 'e2e_dash_private'")
        cur.execute(
            "INSERT INTO public._import_registry (table_name, display_name, target_db, "
            "row_count, fingerprint, columns, source_file, sheet_name, modules, "
            "description, owner_email) "
            "VALUES ('e2e_dash_private', 'E2E private', 'pg', 1, 'e2e', '{}'::jsonb, 'p.csv', 'S', '{}', '', %s)",
            (OWNER[1],))
        conn.commit()


def _reset_dashboard():
    with dashboard_store.connect_main() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai_dashboards WHERE slug = %s", (DASH,))
        conn.commit()


def _cleanup():
    with reports._db() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}")
        cur.execute("DROP TABLE IF EXISTS public.e2e_dash_private")
        cur.execute("DELETE FROM public._import_registry WHERE table_name IN (%s, 'e2e_dash_private')",
                    (TABLE,))
        conn.commit()
    with dataset_shares.connect_main() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM dataset_shares WHERE table_name = ANY(%s)", ([TABLE],))
        conn.commit()
    with dashboard_store.connect_main() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai_dashboards WHERE slug = %s", (DASH,))
        conn.commit()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = ANY(%s)",
                    ([e for _, e in (OWNER, GUEST, STRANGER, OP)],))
        conn.commit()


# ── the checks ───────────────────────────────────────────────────────────────

def t_dataset_share_grants_rows_only():
    owner, guest, stranger = (_browser(w) for w in (OWNER, GUEST, STRANGER))
    r = owner.post(f"{BASE}/api/data-center/datasets/{TABLE}/shares", json={"email": GUEST[1]})
    check("owner shares a dataset by email", r.status_code == 200, r.status_code)

    r = guest.get(f"{BASE}/api/data-center/datasets")
    names = [d["table_name"] for d in r.json()]
    check("grantee sees the shared dataset", TABLE in names, names)
    check("grantee does NOT see the owner's other dataset", "e2e_dash_private" not in names, names)

    r = guest.get(f"{BASE}/api/data-center/datasets/shared-with-me")
    shared = [d["table_name"] for d in r.json()]
    check("shared-with-me lists exactly the grant", shared == [TABLE], shared)
    check("shared-with-me says who shared it",
          r.json() and r.json()[0].get("shared_by") == OWNER[1], r.json())

    # Read the rows: the point of the grant.
    r = guest.post(f"{BASE}/api/data-center/query",
                   json={"sql": f"SELECT region, amount FROM {TABLE} ORDER BY region"})
    check("grantee can query the shared rows", r.status_code == 200, r.status_code)
    check("grantee gets the real rows",
          r.status_code == 200 and [x["region"] for x in r.json().get("rows", [])] == ["north", "south"],
          r.text[:160])

    # And nothing beyond it.
    r = guest.post(f"{BASE}/api/data-center/query",
                   json={"sql": "SELECT secret FROM e2e_dash_private"})
    check("grantee cannot query a dataset they were not given", r.status_code == 403, r.status_code)

    # The file side is deliberately NOT shared.
    r = guest.get(f"{BASE}/api/data-center/files")
    check("grantee gets no files from the owner's library",
          r.status_code == 200 and not r.json(), r.status_code)

    # A stranger cannot even confirm the dataset exists.
    r = stranger.get(f"{BASE}/api/data-center/datasets/{TABLE}/shares")
    check("non-owner is 404 (not 403) on a dataset's shares", r.status_code == 404, r.status_code)
    r = stranger.post(f"{BASE}/api/data-center/datasets/{TABLE}/shares", json={"email": STRANGER[1]})
    check("non-owner cannot create a grant", r.status_code == 404, r.status_code)

    # Self-share and malformed addresses.
    r = owner.post(f"{BASE}/api/data-center/datasets/{TABLE}/shares", json={"email": OWNER[1]})
    check("owner cannot share with themselves", r.status_code == 400, r.status_code)
    r = owner.post(f"{BASE}/api/data-center/datasets/{TABLE}/shares", json={"email": "not-an-email"})
    check("a malformed address is refused", r.status_code == 400, r.status_code)

    # Revoke really revokes.
    r = owner.delete(f"{BASE}/api/data-center/datasets/{TABLE}/shares/{GUEST[1]}")
    check("owner revokes the grant", r.status_code == 200, r.status_code)
    r = guest.post(f"{BASE}/api/data-center/query",
                   json={"sql": f"SELECT 1 FROM {TABLE}"})
    check("after revoke the rows are gone too", r.status_code == 403, r.status_code)
    names = [d["table_name"] for d in guest.get(f"{BASE}/api/data-center/datasets").json()]
    check("after revoke it leaves the grantee's list", TABLE not in names, names)


def t_grant_does_not_outlive_the_dataset():
    owner, guest, op = _browser(OWNER), _browser(GUEST), _browser(OP)
    owner.post(f"{BASE}/api/data-center/datasets/{TABLE}/shares", json={"email": GUEST[1]})
    check("re-shared for the cascade check",
          owner.get(f"{BASE}/api/data-center/datasets/{TABLE}/shares").status_code == 200)

    r = guest.delete(f"{BASE}/api/data-center/datasets/{TABLE}")
    check("a non-admin cannot delete a dataset (operator-only by design)", r.status_code == 403,
          r.status_code)
    r = op.delete(f"{BASE}/api/data-center/datasets/{TABLE}")
    check("an operator deletes the dataset", r.status_code == 200, r.status_code)

    r = owner.get(f"{BASE}/api/data-center/datasets/{TABLE}/shares")
    check("the grant is gone with the dataset", r.status_code == 404, r.status_code)
    with dataset_shares.connect_main() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dataset_shares WHERE table_name = %s", (TABLE,))
        left = cur.fetchone()[0]
    check("no grant row survives the drop", left == 0, left)

    # The dangerous case: the name comes back as somebody ELSE's table.
    with reports._db() as conn, conn.cursor() as cur:
        cur.execute(f"CREATE TABLE public.{TABLE} (other text)")
        cur.execute(f"INSERT INTO public.{TABLE} VALUES ('someone elses data')")
        cur.execute(
            "INSERT INTO public._import_registry (table_name, display_name, target_db, "
            "row_count, fingerprint, columns, source_file, sheet_name, modules, "
            "description, owner_email) "
            "VALUES (%s, 'Recycled name', 'pg', 1, 'e2e', '{}'::jsonb, 'x.csv', 'S', '{}', '', %s)", (TABLE, STRANGER[1]))
        conn.commit()
    r = guest.post(f"{BASE}/api/data-center/query", json={"sql": f"SELECT other FROM {TABLE}"})
    check("a recycled name does not restore the old grantee's access", r.status_code == 403, r.status_code)


def t_dashboard_publish_and_read():
    _prepare_datasets()
    _reset_dashboard()
    owner, guest, stranger = (_browser(w) for w in (OWNER, GUEST, STRANGER))
    body = {
        "title": "E2E dashboard", "summary": "s", "summary_zh": "摘要",
        "datasets": [TABLE], "html": DASH_HTML, "status": "published",
        "visibility": "private", "in_nav": True, "in_home": True, "home_order": 2,
    }
    r = owner.put(f"{BASE}/api/dashboard/{DASH}", json=body)
    check("owner publishes a dashboard", r.status_code == 200, r.text[:200])

    r = owner.get(f"{BASE}/api/dashboard?scope=mine")
    cards = r.json()
    check("it appears in the owner's list", any(c["slug"] == DASH for c in cards), len(cards))
    card = next((c for c in cards if c["slug"] == DASH), {})
    check("the card carries its placement flags",
          card.get("in_nav") is True and card.get("in_home") is True and card.get("home_order") == 2, card)
    check("the card does not carry the html body", "html" not in card, sorted(card)[:6])

    # The standalone page.
    r = owner.get(f"{BASE}/d/{DASH}")
    check("the reader serves the page to its owner", r.status_code == 200, r.status_code)
    body_html = r.text
    check("the page is the stored document", "E2E dashboard" in body_html, body_html[:120])
    check("the runtime is injected", "kladoDashboard" in body_html and "kldQuery" in body_html)
    check("the runtime is injected exactly once", body_html.count("data-dashboard-runtime") == 1,
          body_html.count("data-dashboard-runtime"))
    check("the page resolves api/ against the app base", "<base" in body_html, body_html[:200])

    # A stranger gets 404 — the slug must not be confirmed.
    r = stranger.get(f"{BASE}/api/dashboard/{DASH}")
    check("a private dashboard is 404 for a stranger", r.status_code == 404, r.status_code)
    r = stranger.get(f"{BASE}/d/{DASH}")
    check("the reader is 404 for a stranger too", r.status_code == 404, r.status_code)


def t_dashboard_query_uses_the_shared_guard():
    """The whole point of extracting the guard: one implementation, no widening."""
    _prepare_datasets()
    _reset_dashboard()
    owner, guest, stranger = (_browser(w) for w in (OWNER, GUEST, STRANGER))
    owner.post(f"{BASE}/api/data-center/datasets/{TABLE}/shares", json={"email": GUEST[1]})
    try:
        r = owner.post(f"{BASE}/api/dashboard/query",
                       json={"sql": f"SELECT region, amount FROM {TABLE} ORDER BY region"})
        check("dashboard query reads the owner's own dataset", r.status_code == 200, r.text[:140])
        check("dashboard query returns the rows",
              r.status_code == 200 and len(r.json().get("rows", [])) == 2, r.text[:140])

        r = guest.post(f"{BASE}/api/dashboard/query",
                       json={"sql": f"SELECT region FROM {TABLE}"})
        check("a grantee can read the shared dataset through a dashboard",
              r.status_code == 200, r.status_code)

        r = guest.post(f"{BASE}/api/dashboard/query",
                       json={"sql": "SELECT secret FROM e2e_dash_private"})
        check("a grantee still cannot read an unshared dataset", r.status_code == 403, r.status_code)

        r = stranger.post(f"{BASE}/api/dashboard/query",
                          json={"sql": f"SELECT region FROM {TABLE}"})
        check("a stranger cannot read through a dashboard", r.status_code == 403, r.status_code)

        # The write barrier, not the text check, is what stops a write.
        r = owner.post(f"{BASE}/api/dashboard/query", json={"sql": f"DELETE FROM {TABLE}"})
        check("a DELETE is refused", r.status_code == 400, r.status_code)
        r = owner.post(f"{BASE}/api/dashboard/query", json={"sql": "DROP TABLE " + TABLE})
        check("a DROP is refused", r.status_code == 400, r.status_code)
        r = owner.post(f"{BASE}/api/dashboard/query",
                       json={"sql": f"WITH x AS (DELETE FROM {TABLE} RETURNING 1) SELECT * FROM x"})
        check("a data-modifying CTE is refused by the READ ONLY transaction",
              r.status_code in (400, 500), r.status_code)
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM public.{TABLE}")
            survived = cur.fetchone()[0]
        check("the rows are still there afterwards", survived == 2, survived)
    finally:
        owner.delete(f"{BASE}/api/data-center/datasets/{TABLE}/shares/{GUEST[1]}")


def t_dashboard_sharing():
    _reset_dashboard()
    owner, guest, stranger = (_browser(w) for w in (OWNER, GUEST, STRANGER))
    r = owner.put(f"{BASE}/api/dashboard/{DASH}", json={
        "title": "E2E dashboard", "html": DASH_HTML, "visibility": "private",
        "datasets": [TABLE], "status": "published"})
    check("owner republishes for the sharing check", r.status_code == 200, r.status_code)
    r = owner.post(f"{BASE}/api/dashboard/{DASH}/shares/colleagues", json={"email": GUEST[1]})
    check("owner shares the dashboard with a colleague", r.status_code == 200, r.text[:160])
    r = owner.get(f"{BASE}/api/dashboard/{DASH}/shares")
    check("the owner sees the grant", r.status_code == 200 and len(r.json()) == 1, r.text[:160])

    r = guest.get(f"{BASE}/api/dashboard?scope=shared")
    check("it lands in the grantee's shared scope", any(c["slug"] == DASH for c in r.json()),
          [c["slug"] for c in r.json()])
    card = next((c for c in r.json() if c["slug"] == DASH), {})
    check("a shared card is not manageable", card.get("can_manage") is False, card)
    r = guest.put(f"{BASE}/api/dashboard/{DASH}", json={"title": "hijacked"})
    check("the grantee cannot rewrite it", r.status_code == 409, r.status_code)
    r = stranger.put(f"{BASE}/api/dashboard/{DASH}", json={"title": "hijacked"})
    check("a stranger is 404 (not 409) — the slug must not be confirmed",
          r.status_code == 404, r.status_code)
    r = guest.delete(f"{BASE}/api/dashboard/{DASH}")
    check("the grantee cannot delete it", r.status_code == 404, r.status_code)
    r = stranger.get(f"{BASE}/d/{DASH}")
    check("a non-grantee still gets 404", r.status_code == 404, r.status_code)

    # Deleting the dashboard takes the share with it.
    r = owner.delete(f"{BASE}/api/dashboard/{DASH}")
    check("owner deletes the dashboard", r.status_code == 200, r.status_code)
    with dashboard_store.connect_main() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ai_dashboard_colleague_shares s "
                    "JOIN ai_dashboards d ON d.id = s.dashboard_id WHERE d.slug = %s", (DASH,))
        left = cur.fetchone()[0]
    check("no share row survives the dashboard", left == 0, left)
    r = guest.get(f"{BASE}/api/dashboard?scope=shared")
    check("the shared list is empty again", not r.json(), r.text[:120])


def t_module_gate():
    """The gate has to cover the standalone page too — an iframe only has a cookie."""
    from services import account_modules
    _reset_dashboard()
    owner, guest = _browser(OWNER), _browser(GUEST)
    owner.put(f"{BASE}/api/dashboard/{DASH}", json={"title": "gate check", "html": DASH_HTML})

    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app_users WHERE email = %s", (GUEST[1],))
        guest_id = cur.fetchone()[0]
    account_modules.set_module(guest_id, "dashboard", False)
    try:
        r = guest.get(f"{BASE}/api/dashboard")
        check("the API is 403 once the module is off", r.status_code == 403, r.status_code)
        check("the 403 names the module", r.json().get("module") == "dashboard", r.text[:160])
        r = guest.get(f"{BASE}/d/{DASH}")
        check("the standalone reader is 403 too (not a 401, not a 200)", r.status_code == 403, r.status_code)
        r = guest.get(f"{BASE}/api/health")
        check("health still answers with the module off", r.status_code == 200, r.status_code)
        keys = r.json()["modules"]["enabled"]
        check("health no longer lists dashboard", "dashboard" not in keys, keys)
        check("health still lists what is on", "inbox" in keys, keys)
    finally:
        account_modules.clear_for(guest_id)
    r = guest.get(f"{BASE}/api/dashboard")
    check("switching it back restores access", r.status_code == 200, r.status_code)
    owner.delete(f"{BASE}/api/dashboard/{DASH}")


def t_required_module_cannot_be_switched_off():
    from services import account_modules
    owner = _browser(OWNER)
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app_users WHERE email = %s", (OWNER[1],))
        uid = cur.fetchone()[0]
    try:
        r = owner.put(f"{BASE}/api/settings/modules/inbox", json={"enabled": False})
        check("a required module cannot be switched off over the API", r.status_code == 400, r.status_code)
        r = owner.put(f"{BASE}/api/settings/modules/nope", json={"enabled": True})
        check("an unknown module key is a 404", r.status_code == 404, r.status_code)
        r = owner.get(f"{BASE}/api/settings/modules")
        check("the picker gets the whole catalogue",
              r.status_code == 200 and len(r.json()["modules"]["catalogue"]) == 8, r.text[:160])
    finally:
        account_modules.clear_for(uid)


# ── runner ───────────────────────────────────────────────────────────────────

def _serve():
    from main import app
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        try:
            if requests.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    print("server did not come up")
    sys.exit(1)


def main():
    if not _reachable():
        print("PostgreSQL is not reachable — start the container first")
        sys.exit(1)
    _cleanup()
    _prepare()
    _serve()
    try:
        for name, fn in PENDING:
            fn()
    finally:
        _cleanup()
    print()
    if FAILURES:
        print("%d checks FAILED: %s" % (len(FAILURES), "; ".join(FAILURES)))
        sys.exit(1)
    print("all checks passed")


def _reachable():
    try:
        from core.db import connect_main
        conn = connect_main()
        conn.close()
        return True
    except Exception:
        return False


PENDING.extend([
    ("dataset share", t_dataset_share_grants_rows_only),
    ("grant cascade", t_grant_does_not_outlive_the_dataset),
    ("dashboard publish", t_dashboard_publish_and_read),
    ("dashboard query", t_dashboard_query_uses_the_shared_guard),
    ("dashboard sharing", t_dashboard_sharing),
    ("module gate", t_module_gate),
    ("required modules", t_required_module_cannot_be_switched_off),
])

if __name__ == "__main__":
    main()
