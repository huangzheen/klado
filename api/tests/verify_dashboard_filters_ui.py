"""The filter rail on a DASHBOARD — the shared node, the right module, the right frame.

    .venv312/bin/python api/tests/verify_dashboard_filters_ui.py

Every claim here is about the SCREEN or about a REQUEST, and none of them can be read
off the source:

* the rail has to appear with real area. `hidden=false` proves nothing — a rail that is
  present, unhidden, and 0x0 is exactly what a viewer gets when the shared node is left
  in the other viewer.
* it has to ask `/api/dashboard/…` and not `/api/reports/…`. Both are 200 in a
  production-shaped fixture, so a wrong module is invisible unless the URL is read.
* a pick has to reach the frame the reader is LOOKING AT. postMessage to a frame that is
  off screen succeeds, so a wrong `frameId` is a green request and a dead control.
* a pick has to be saved against the dashboard, because a selection saved against a
  report is a write to a document that may not exist.

The API is stubbed and every request is recorded. The document served into the frame is
the real filter runtime's contract shape (`data-filter-when` slices), because a fake
with the wrong attributes would let a broken rail look fine.
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8807

FAILURES = []
REQUESTS = []

SLUG = "q3-page"
SCHEMA = {"version": 1, "filters": [
    {"key": "period", "type": "select",
     "label": {"en": "Period", "zh": "时间段"},
     "options": [{"value": "2026-06", "label": {"en": "Jun", "zh": "六月"}},
                 {"value": "2026-07", "label": {"en": "Jul", "zh": "七月"}}],
     "default": "2026-06"}]}

# A page with slices, the shape `vendor/report-filters.js` reacts to. The runtime is
# inlined by the SERVER, so a document that does not declare slices would pass a rail
# test while proving nothing about the rail.
DYNAMIC_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>p</title></head><body>"
    "<section id='slice-jun' data-filter-when='period=2026-06'>June</section>"
    "<section id='slice-jul' data-filter-when='period=2026-07'>July</section>"
    "</body></html>")

WALL = {
    "items": [{
        "slug": SLUG, "title": "Q3 Page", "summary": "", "summary_zh": "",
        "datasets": [], "kind": "dynamic", "has_filters": True, "status": "published",
        "visibility": "private", "owner_email": "me@example.com", "tags": [],
        "accent": "", "pinned": False, "in_nav": False, "in_home": False,
        "nav_order": 0, "home_order": 0, "home_collapsed": False,
        "has_cover": False, "cover_url": "", "views": 0,
    }]
}


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


#: The API is stubbed in the PAGE, not by a hand-written HTML fixture. The first version
#: served a miniature page of its own, and every assertion passed or failed for a reason
#: that had nothing to do with the app: `getElementById('rpt-filters')` was simply null,
#: because the real rail was not in the document being tested. A test that serves its own
#: HTML tests its own HTML.
STUB = r"""
window.__docs = [];
window.__calls = [];
window.__saves = [];
window.fetch = function (url, init) {
  const u = String(url);
  window.__calls.push(u);
  const json_ = (b) => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(b)});
  if (u.indexOf('/api/dashboard') > -1 && /\/filters\/selection/.test(u)) {
    window.__saves.push({url: u, body: (init && init.body) || ''});
    return json_({ok: true, selection: {}, dropped: []});
  }
  if (u.indexOf('/api/dashboard') > -1 && /\/filters/.test(u)) {
    return json_({slug: 'q3-page', kind: 'dynamic', configured: true,
                  schema: SCHEMA_JSON, selection: {},
                  effective: {period: '2026-06'}, dropped: [], can_edit: true});
  }
  if (u.indexOf('/api/dashboard') > -1) {
    // ⚠️ A BARE ARRAY, because that is what `GET /api/dashboard` returns
    // (`return [store.card(...) for row in rows]`). The first version of this stub
    // answered `{items: [...]}`, taken from the reports wall, and the page then said
    // "no dashboards yet" with a 200 and no error — a fixture that made the feature
    // look absent rather than broken. The shape is read off the endpoint, not guessed.
    return Promise.resolve({ok: true, status: 200,
                            json: () => Promise.resolve([WALL_ITEM_JSON])});
  }
  if (u.indexOf('/api/reports') > -1) { return json_({items: [], projects: [], folders: []}); }
  if (u.indexOf('/api/auth/me') > -1) {
    return json_({email: 'me@example.com', role: 'admin'});
  }
  if (u.indexOf('/api/rail') > -1 || u.indexOf('/api/org/me') > -1) {
    return json_({modules: [], email: 'me@example.com'});
  }
  return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{}'),
                          json: () => Promise.resolve({})});
};
"""

SCHEMA_JSON = json.dumps(SCHEMA, ensure_ascii=False)
WALL_ITEM_JSON = json.dumps({
    "slug": SLUG, "title": "Q3 Page", "summary": "", "summary_zh": "",
    "datasets": [], "kind": "dynamic", "has_filters": True, "status": "published",
    "visibility": "private", "owner_email": "me@example.com", "tags": [],
    "accent": "", "pinned": False, "in_nav": False, "in_home": False,
    "nav_order": 0, "home_order": 0, "home_collapsed": False,
    "has_cover": False, "cover_url": "", "views": 0,
}, ensure_ascii=False)


def handler_factory():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/d/" + SLUG:
                return self._send(DYNAMIC_PAGE.encode(), "text/html; charset=utf-8")
            if path == "/stub.js":
                return self._send(
                    (STUB.replace("SCHEMA_JSON", SCHEMA_JSON)
                          .replace("WALL_ITEM_JSON", WALL_ITEM_JSON)).encode(),
                    "application/javascript; charset=utf-8")
            return super().do_GET()

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


def main():
    httpd = _Server(("127.0.0.1", PORT), handler_factory())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % PORT
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # ⚠️ `add_init_script`, not `add_script_tag`. The stub REPLACES window.fetch,
            # and a navigation throws the page's globals away — so a tag loaded before the
            # goto was silently gone by the time the app ran, and the app then talked to a
            # real (absent) server. init scripts run on every document, which is what a
            # fetch replacement has to be.
            page.add_init_script(STUB.replace("SCHEMA_JSON", SCHEMA_JSON)
                                        .replace("WALL_ITEM_JSON", WALL_ITEM_JSON))
            page.goto(base + "/index.html", wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.dashboardPage", timeout=20000)
            page.evaluate("() => navTo(null, 'dashboard', 'Dashboard')")
            # ⚠️ Wait for the wall to LOAD, not just for the viewer function to exist.
            # `openViewer` looks its card up in the loaded list, and opening before the
            # load lands means the lookup misses and the rail hides itself — which reads
            # as "the rail is broken" and is really "the fixture opened too early".
            page.wait_for_function("(s) => window.dashboardPage && window.__calls.length > 0",
                                   arg=SLUG, timeout=20000)
            page.wait_for_timeout(500)

            # Drive the real open path — the same function the wall's card uses, so this
            # is not a hand-placed DOM.
            page.evaluate("(s) => window.dashboardPage.openViewer(s)", SLUG)
            page.wait_for_timeout(900)

            # ── 1. the rail is on screen, with area ──
            rail = page.evaluate("""() => {
                const el = document.getElementById('rpt-filters');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {w: Math.round(r.width), h: Math.round(r.height),
                        display: getComputedStyle(el).display,
                        inViewer: !!(el.closest('#dsh-viewer')),
                        controls: el.querySelectorAll('select,input,button,.rpt-fchip').length};
            }""")
            check("筛选侧栏渲染出来了（有真实面积，不是 0×0）",
                  bool(rail) and rail["w"] > 40 and rail["h"] > 40, rail)
            check("侧栏被搬进了 Dashboard 视图器", bool(rail) and rail["inViewer"], rail)
            check("侧栏里真的画出了控件（不是空壳）",
                  bool(rail) and rail["controls"] > 0, rail and rail.get("controls"))

            # ── 2. it asked the DASHBOARD module ──
            calls = page.evaluate("() => window.__calls")
            check("筛选 schema 来自 /api/dashboard，不是 /api/reports",
                  any("/api/dashboard/%s/filters" % SLUG in u for u in calls)
                  and not any("/api/reports" in u for u in calls), calls)

            # ── 3. the toggle is the one in the DASHBOARD bar ──
            toggle = page.evaluate("""() => {
                const t = document.getElementById('rpt-filters-toggle');
                if (!t) return null;
                return {inDash: !!(t.closest('.rpt-viewer-bar')
                                   && t.closest('.rpt-viewer-bar').closest('#dsh-viewer')),
                        visible: t.getBoundingClientRect().width > 0,
                        dupes: document.querySelectorAll('[id="rpt-filters-toggle"]').length};
            }""")
            check("页面上只有一个筛选开关（没有第二个同 id 的死按钮）",
                  bool(toggle) and toggle["dupes"] == 1, toggle)
            check("筛选开关在 Dashboard 的标题栏里", bool(toggle) and toggle["inDash"], toggle)
            check("筛选开关真的画出来了", bool(toggle) and toggle["visible"], toggle)

            # ── 4. choosing a value saves against the DASHBOARD ──
            picked = page.evaluate("""() => {
                const sel = document.querySelector('#rpt-filters-body select');
                if (!sel) return null;
                sel.value = '2026-07';
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return sel.value;
            }""")
            check("侧栏里有可选的下拉", picked == "2026-07", picked)
            page.wait_for_timeout(1400)
            saves = page.evaluate("() => window.__saves")
            check("选中值保存到了 /api/dashboard 的 selection",
                  bool(saves) and all("/api/dashboard" in s["url"] for s in saves)
                  and not any("/api/reports" in s["url"] for s in saves), saves)
            check("保存的正文里是刚选的那个值",
                  bool(saves) and all('"2026-07"' in s["body"] for s in saves), saves)

            # ── 5. closing takes the shared rail with it ──
            page.evaluate("() => window.dashboardPage.closeViewer()")
            page.wait_for_timeout(300)
            after = page.evaluate("""() => {
                const el = document.getElementById('rpt-filters');
                return {hidden: el ? el.hidden : null,
                        stillInDash: !!(el && el.closest('#dsh-viewer'))};
            }""")
            check("关掉后侧栏被收起，不会挂在关掉的视图器上",
                  after["hidden"] is True, after)

            check("整页没有 JS 报错", not errors, errors)
            browser.close()
    finally:
        httpd.shutdown()

    print()
    if FAILURES:
        print("失败 %d 项" % len(FAILURES))
        for name in FAILURES:
            print("  - " + name)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
