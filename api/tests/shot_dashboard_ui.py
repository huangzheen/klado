"""Screenshot the three new surfaces with a REAL signed-in session.

The other UI scripts stub the API, which is right for "does this component render"
and wrong for "does this module work". This one signs in as a throwaway admin
against a real server and a real database, publishes a real dashboard, and captures:

  1. the Dashboard page (card grid, the placement dialog);
  2. the landing page with the dashboard promoted to a container;
  3. Settings → a member's module dialog, including a real per-account override;
  4. the top bar in both themes, to check the promoted nav tab and the data-driven
     tabs survive a theme switch.

Everything it creates it removes, including the accounts.

    .venv312/bin/python api/tests/shot_dashboard_ui.py [outdir]
"""
import os
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                      # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402
from services import auth_store, dashboard_store   # noqa: E402

PORT = int(os.environ.get("DASH_SHOT_PORT", "18891"))
BASE = f"http://127.0.0.1:{PORT}"
# Trailing slash: these strings are concatenated with "api/…" inside page.evaluate,
# where a missing slash produces "…:18891api/auth/login" and a confusing URL error.
WEB = BASE + "/"
PASSWORD = "dash-shot-pass-1"
ADMIN = ("shot", "dash-shot-admin@example.com")
USER = ("user", "dash-shot-user@example.com")
DASH = "quarterly-sales"
TABLE = "shot_sales"          # a real dataset, so the page really queries something

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/klado-dashboard-shots")
OUT.mkdir(parents=True, exist_ok=True)

DASH_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Quarterly sales</title>
<style>
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background:#f6f7f9; color:#18243 3; padding:24px; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:#6c7a91; font-size:13px; margin-bottom:16px; }
  .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  .tile { background:#fff; border:1px solid #dce1e7; border-radius:10px; padding:14px; }
  .tile b { display:block; font-size:22px; margin-top:4px; }
  .tile span { font-size:12px; color:#6c7a91; }
  table { width:100%; border-collapse:collapse; margin-top:16px; font-size:13px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #e6eaef; }
</style></head>
<body>
  <h1 id="t">Quarterly sales</h1>
  <div class="sub">Live from the Data Center · <span id="when">loading…</span></div>
  <div class="grid" id="tiles"></div>
  <table><thead><tr><th>Region</th><th>Revenue</th></tr></thead><tbody id="rows"></tbody></table>
<script>
  // The page asks the shared Data Center gateway for its own numbers. It may only
  // read datasets the READER can see — not the ones the author could.
  (async function () {
    document.getElementById('when').textContent =
      new Date().toLocaleString();
    try {
      const res = await window.kldQuery(
        "SELECT region, amount FROM " + window.kladoDashboard.datasets[0] + " ORDER BY region");
      const rows = (res && res.rows) || [];
      const total = rows.reduce((a, r) => a + Number(r.amount || 0), 0);
      document.getElementById('tiles').innerHTML =
        '<div class="tile"><span>Rows</span><b>' + rows.length + '</b></div>' +
        '<div class="tile"><span>Total</span><b>' + total + '</b></div>' +
        '<div class="tile"><span>Datasets</span><b>' + window.kladoDashboard.datasets.length + '</b></div>';
      document.getElementById('rows').innerHTML = rows.map(
        r => '<tr><td>' + r.region + '</td><td>' + r.amount + '</td></tr>').join('');
    } catch (e) {
      document.getElementById('t').textContent = 'Query refused: ' + e.message;
    }
  })();
</script>
</body></html>"""


def _prepare():
    auth_store._ensure_schema()
    dashboard_store.ensure_schema()
    _prepare_dataset()
    with auth_store._db() as conn, conn.cursor() as cur:
        for name, email, role in ((ADMIN[0], ADMIN[1], "admin"),
                                  (USER[0], USER[1], "user")):
            cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
            cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                        "VALUES (%s, %s, %s, %s)",
                        (email, auth_store._hash(PASSWORD), name, role))
        conn.commit()
    with dashboard_store.connect_main() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai_dashboards WHERE slug = %s", (DASH,))
        conn.commit()
    _prepare_company()


def _prepare_company():
    """A probe company with the admin as owner and the throwaway user as a member.

    ⚠️ Needed because Settings is the ENTERPRISE page: a platform operator with no
    company cannot open it at all, and the module decision now lives on a member row.
    A screenshot script pointed at the old operator-only matrix would have had nothing
    to point at, and would have said so by crashing rather than by being wrong.
    """
    from klado_shared import orgs
    _cleanup_company()
    org = orgs.create_org("Dash Shot Co", kind=orgs.KIND_ENTERPRISE,
                          domains=["dash-shot.invalid"])
    for email, role in ((ADMIN[1], "owner"), (USER[1], "member")):
        orgs.upsert_invitation(org["id"], email, role, invited_by=ADMIN[1])
        orgs.attach_existing_account(org["id"], email)


def _cleanup_company():
    from klado_shared import orgs
    row = orgs.get_org_by_slug(orgs.slugify("Dash Shot Co")) if hasattr(orgs, "slugify") \
        else None
    if row is None:
        with orgs._db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM orgs WHERE name = %s", ("Dash Shot Co",))
            found = cur.fetchone()
        row = {"id": found[0]} if found else None
    if not row:
        return
    with orgs._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM org_public_approvals WHERE org_id = %s", (row["id"],))
        cur.execute("DELETE FROM org_members WHERE org_id = %s", (row["id"],))
        cur.execute("DELETE FROM orgs WHERE id = %s", (row["id"],))
    conn.commit()


def _prepare_dataset():
    """A real dataset owned by the throwaway admin, so the published page has
    something to query. A screenshot of an error message is not an acceptance test."""
    from routers import reports
    with reports._db() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}")
        cur.execute(f"CREATE TABLE public.{TABLE} (region text, amount int)")
        cur.execute(f"INSERT INTO public.{TABLE} VALUES "
                    "('North', 184000), ('South', 121500), "
                    "('East', 97600), ('West', 143200), ('Central', 65800)")
        cur.execute("DELETE FROM public._import_registry WHERE table_name = %s", (TABLE,))
        cur.execute(
            "INSERT INTO public._import_registry (table_name, display_name, target_db, "
            "row_count, fingerprint, columns, source_file, sheet_name, modules, "
            "description, owner_email) VALUES (%s, 'Regional sales', 'pg', 5, 'shot', "
            "'{}'::jsonb, 'shot.csv', 'Sheet1', '{}', 'Screenshot fixture', %s)",
            (TABLE, ADMIN[1]))
        conn.commit()


def _cleanup():
    from routers import reports
    with reports._db() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}")
        cur.execute("DELETE FROM public._import_registry WHERE table_name = %s", (TABLE,))
        conn.commit()
    with dashboard_store.connect_main() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai_dashboards WHERE slug = %s", (DASH,))
        conn.commit()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = ANY(%s)",
                    ([ADMIN[1], USER[1]],))
        conn.commit()
    _cleanup_company()


def _serve():
    from main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        try:
            if requests.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise SystemExit("server did not come up")


def _publish():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN[1], "password": PASSWORD})
    r.raise_for_status()
    r = s.put(f"{BASE}/api/dashboard/{DASH}", json={
        "title": "Quarterly sales", "summary": "Revenue by region, live.",
        "summary_zh": "各区域营收，实时。", "datasets": [TABLE], "html": DASH_HTML,
        "status": "published", "visibility": "private",
        "in_nav": True, "in_home": True, "home_order": 0, "nav_order": 0,
    })
    r.raise_for_status()
    return s


def main():
    _prepare()
    _serve()
    _publish()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/", wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.dashboardPage")
            # Sign in through the app's own gate.
            page.evaluate(
                """async ([email, pw, base]) => {
                    const r = await fetch(base + 'api/auth/login', {
                        method: 'POST', credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ email, password: pw }) });
                    return r.status;
                }""", [ADMIN[1], PASSWORD, WEB])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                "() => window.kladoModules && kladoModules.has('dashboard')", timeout=15000)
            page.wait_for_timeout(1200)

            # 1 — the landing page with the promoted container
            page.goto(f"{BASE}/", wait_until="domcontentloaded")
            page.wait_for_selector(".home-card", timeout=15000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / "1-home-container.png"), full_page=False)

            # 2 — the Dashboard page
            page.evaluate("() => dashboardPage.openFromNav()")
            page.wait_for_selector("#dsh-grid .rpt-card", timeout=15000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(OUT / "2-dashboard-cards.png"))

            # 3 — the placement dialog
            page.evaluate("() => dashboardPage.openPlacement('quarterly-sales')")
            page.wait_for_selector("#dsh-placement:not([hidden])", timeout=8000)
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "3-placement-dialog.png"))
            page.evaluate("() => dashboardPage.closePlacement()")

            # 4 — the live page in its viewer
            page.evaluate("() => dashboardPage.openViewer('quarterly-sales')")
            page.wait_for_selector("#dsh-viewer:not([hidden])", timeout=8000)
            page.wait_for_timeout(1800)
            page.screenshot(path=str(OUT / "4-dashboard-viewer.png"))
            title = page.eval_on_selector("#dsh-frame", "f => f.contentDocument.title")
            body = page.eval_on_selector("#dsh-frame", "f => f.contentDocument.body.innerText")
            datasets = page.eval_on_selector(
                "#dsh-frame", "f => f.contentWindow.kladoDashboard.datasets")
            print("viewer title :", title)
            print("viewer data  :", datasets)
            print("viewer body  :", " | ".join(x for x in body.split("\n") if x.strip())[:200])
            if "Query refused" in body:
                print("!! the page could not query its dataset")
                sys.exit(1)
            page.evaluate("() => dashboardPage.closeViewer()")

            # 5 — Settings → a member's module dialog, with a real override
            # Look the id up — the throwaway accounts are created with
            # auto-increment ids, so a hard-coded 2 is a guess that silently
            # overrides the wrong account (or none).
            #
            # ⚠️ The admin signs in as the OWNER of a probe company, not as a bare
            # platform operator. Settings is the enterprise page, so an operator with no
            # company cannot open it — and the module decision now lives on a member ROW
            # (it used to be a matrix only the operator branch could see).
            with auth_store._db() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM app_users WHERE email = %s", (USER[1],))
                user_id = cur.fetchone()[0]
            print("override uid :", user_id, USER[1])
            page.evaluate(
                """async ([uid, base]) => {
                    await fetch(base + 'api/settings/modules/accounts/' + uid
                      + '/calendar', { method: 'PUT', credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ enabled: false }) });
                }""", [user_id, WEB])
            page.evaluate("() => navTo(null, 'system-settings', 'System Settings')")
            row = page.locator(f'#adm-users-body tr:has-text("{USER[1]}")')
            page.wait_for_selector("#adm-users-body button.mod-btn", timeout=15000)
            row.first.locator("button.mod-btn").click()
            page.wait_for_selector("#kld-dlg .mod-pick-box", timeout=15000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / "5-settings-modules.png"))
            cells = page.eval_on_selector_all(
                "#kld-dlg .mod-pick-box",
                "els => els.map(e => e.dataset.key + '=' + e.checked)")
            print("module boxes :", cells)
            # The overridden module must read as UNTICKED, and the rest ticked. Two
            # states, both of which the box has to make legible on its own.
            unchecked = page.eval_on_selector_all(
                "#kld-dlg .mod-pick-box:not(:checked)",
                "els => els.map(e => e.dataset.key)")
            print("unticked     :", unchecked)
            if unchecked != ["calendar"]:
                print("!! the per-account override did not render as an unticked box")
                sys.exit(1)
            page.click("#kld-dlg-cancel")
            page.wait_for_timeout(400)

            # 6 — dark theme, to confirm the promoted nav tab and cards hold up
            page.evaluate("() => window.kladoTheme.toggle()")
            page.wait_for_timeout(700)
            page.evaluate("() => navTo(null, 'dashboard', 'Dashboard')")
            page.wait_for_timeout(700)
            page.screenshot(path=str(OUT / "6-dashboard-dark.png"))
            page.evaluate("() => window.kladoTheme.toggle()")
            page.wait_for_timeout(400)

            # 7 — the promoted top-bar tab
            page.goto(f"{BASE}/", wait_until="domcontentloaded")
            page.wait_for_function("() => !!document.querySelector('[data-dash-slug]')",
                                   timeout=15000)
            page.wait_for_timeout(900)
            page.screenshot(path=str(OUT / "7-nav-promoted.png"),
                            clip={"x": 0, "y": 0, "width": 1500, "height": 120})
            navkeys = page.eval_on_selector_all(
                "#nav-center .nav-tab", "els => els.map(e => e.getAttribute('data-nav-key'))")
            print("nav keys     :", navkeys)
            browser.close()
    finally:
        _cleanup()

    print()
    if errors:
        print("JS ERRORS:", errors)
        sys.exit(1)
    print("no JS errors; shots in", OUT)
    for f in sorted(OUT.glob("*.png")):
        print("  ", f, f.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
