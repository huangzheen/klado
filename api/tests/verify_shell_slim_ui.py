"""The slimmed-down app shell — what is left, and what must stay gone.

This repository used to ship a much larger product: a hover-flyout sidebar with 25 business
links, a module ticker in the title bar, a deploy-version chip, a global search box and a
Database Explorer button. 2026-10-01 removed every business module, and with it that whole
chrome. The shell is now six areas — Data Center, Inbox, Workspace, Knowledge base,
Calendar, Settings — over a deliberately blank landing page.

Two kinds of assertion live here, and the second is the one that rots:

* the surviving areas still open, and only their own container is on screen;
* nothing that was removed comes back. A deleted feature that quietly returns (a
  reinstated sidebar, a re-added search box) is invisible in a diff of a 18k-line file.

Needs playwright (present in `.venv312`) and a local Chrome.

    .venv312/bin/python api/tests/verify_shell_slim_ui.py
"""
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8843

FAILURES = []

NAV_TABS = ["Data Center", "Inbox", "Workspace", "Dashboard", "Knowledge base", "Calendar", "Settings"]

# Everything the slim-down deleted. `id=`/`class=` selectors, so a re-introduced element
# fails loudly instead of silently coming back.
REMOVED = {
    "sidebar":            "#sidebar",
    "sidebar open zone":  "#sidebarOpenZone",
    "sidebar resize":     "#resizeL",
    "module ticker":      "#nav-mq",
    "deploy version chip": ".deploy-version",
    "global search":      ".nav-search-wrap",
    "Database Explorer":  ".nav-db-btn",
    "DB Explorer overlay": "#db-overlay",
}

# (nav label, key in PAGES that must be the only thing on screen)
SHELL_AREAS = [
    ("Inbox", "inbox"),
    ("Workspace", "reports"),
    ("Dashboard", "dashboard"),
    ("Knowledge base", "knowledge"),
    ("Calendar", "calendar"),
    ("Settings", "system-settings"),
]


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    httpd = _Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


STUB = """
window.fetch = function (url) {
  const u = String(url);
  let body;
  if (u.indexOf('/files/browse') >= 0) body = {folders: [], files: []};
  else if (u.indexOf('/files/tree') >= 0) body = [];
  else if (u.indexOf('/datasets') >= 0) body = [];
  else if (u.indexOf('/files') >= 0) body = {files: []};
  // A signed-in ADMIN: `navTo()` refuses system-settings to anyone else, and the auth
  // gate would otherwise lock the shell before a single check runs.
  else if (u.indexOf('/auth/me') >= 0) body = {authenticated: true, enabled: true,
      user: {email: 'admin@local', role: 'admin', display_name: 'Admin'}};
  else body = {reports: [], count: 0, categories: [], scope: 'mine', items: [], events: []};
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
};
"""


def visible_pages(page):
    return page.evaluate("""() => Object.entries(PAGES)
      .filter(([, id]) => { const e = document.getElementById(id);
                            return e && getComputedStyle(e).display !== 'none'; })
      .map(([name]) => name)""")


def main() -> int:
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(STUB)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => typeof PAGES !== 'undefined' && typeof navTo === 'function'")

            # ── the surviving areas, in order ────────────────────────────────
            tabs = page.evaluate("""() => Array.from(document.querySelectorAll('.nav-center .nav-tab'))
              .map(t => t.textContent.replace(/\\d+$/, '').trim())""")
            check("顶部导航正好是保留的七项", tabs == NAV_TABS, tabs)
            check("导航第一项背后是 Data Center",
                  "switchToDataCenter" in (page.get_attribute(".nav-center .nav-tab:nth-of-type(1)",
                                                             "onclick") or ""),
                  page.get_attribute(".nav-center .nav-tab:nth-of-type(1)", "onclick"))

            # ── nothing that was removed comes back ──────────────────────────
            for label, selector in REMOVED.items():
                n = page.eval_on_selector_all(selector, "els => els.length")
                check("已删除的 %s 没有回来" % label, n == 0, "found %d" % n)

            # The slim-down used to guard `text="Dashboard"` here. 2026-10-02 added
            # Dashboard back as a REAL module, so the label can no longer tell the
            # retired business dashboard from the new one. The guard is re-scoped to
            # the only thing still worth asserting: the one "Dashboard" tab is the
            # module's, identified by its registry key — so a second, unkeyed tab
            # (the old one coming back) still fails.
            dash_tabs = page.eval_on_selector_all(
                '.nav-center .nav-tab',
                "els => els.filter(t => t.textContent.trim() === 'Dashboard')"
                "            .map(t => t.getAttribute('data-nav-key'))")
            check("唯一的 Dashboard 标签是模块页（带 data-nav-key）",
                  dash_tabs == ["dashboard"], dash_tabs)

            # ── the landing page holds only what was put on it ───────────────
            # It is deliberately blank: a dashboard promoted onto it appears as a
            # `.home-card` container, and nothing else may ever land there.
            home_kids = page.evaluate("""() => {
              const h = document.getElementById('page-home');
              if (!h) return -1;
              const all = Array.from(h.querySelectorAll('*'));
              return {
                total: all.length,
                foreign: all.filter(e => !e.closest('.home-card')).length,
              }; }""")
            check("落地页只有 home-card 容器（无其他子元素）",
                  home_kids["total"] == 0 or home_kids["foreign"] == 0, home_kids)
            check("初始加载不点亮任何导航项",
                  page.evaluate("""() => Array.from(document.querySelectorAll('.nav-tab'))
                    .findIndex(t => t.classList.contains('active'))""") == -1)
            check("品牌仍是回主页的入口",
                  "goHome" in (page.get_attribute("#nav-brand", "onclick") or ""),
                  page.get_attribute("#nav-brand", "onclick"))

            # ── each surviving area opens, and only it is on screen ──────────
            for label, key in SHELL_AREAS:
                page.evaluate("([p, t]) => navTo(null, p, t)", [key, label])
                shown = visible_pages(page)
                check("%s 打开后只有它自己在屏幕上" % label, shown == [key], shown)

            # ── Data Center is an overlay: in, and back out through the nav ──
            # ⚠️ This used to click a `.dc-back-btn`. That button existed only because
            # the overlay was `inset: 0` at the same `z-index` as `.nav` and coming later
            # in the document, so it covered the global navigation and Data Center was
            # the one page with no nav bar at all — the Back button was the only exit.
            # The overlay now starts at `top: var(--nav-h)`, the nav is its SIBLING (not
            # inside `#shell-wrap`, which is what `switchToDataCenter` hides), and the
            # button is gone because it is no longer needed.
            #
            # So the assertion is now the property the fix actually delivered, measured
            # the way the project measures it elsewhere: `elementFromPoint` at the tab
            # centre. A real click would make Playwright retry for 30s and then time out
            # when the overlay covers the nav — and a timeout reports a stack, not a
            # statement about what is wrong.
            page.evaluate("() => switchToDataCenter()")
            check("Data Center 打开后外壳隐藏",
                  page.evaluate("""() => getComputedStyle(document.getElementById('datacenter-page')).display !== 'none'
                    && getComputedStyle(document.getElementById('shell-wrap')).display === 'none'"""))
            geometry = page.evaluate("""() => {
              const nav = document.querySelector('.nav');
              const nr = nav.getBoundingClientRect();
              const hit = document.elementFromPoint(nr.left + nr.width / 2, nr.top + nr.height / 2);
              return {
                navHeight: Math.round(nr.height),
                navTop: Math.round(nr.top),
                dcTop: Math.round(document.getElementById('datacenter-page')
                                   .getBoundingClientRect().top),
                hitsNav: !!(hit && hit.closest && hit.closest('.nav')),
                hasBackBtn: !!document.querySelector('.dc-back-btn'),
              };
            }""")
            check("⚠️ Data Center 不再盖住全局导航（导航在它上方且可点）",
                  geometry["hitsNav"] and geometry["navHeight"] > 0, geometry)
            check("⚠️ 两者上下相接、不重叠",
                  geometry["dcTop"] >= geometry["navTop"] + geometry["navHeight"] - 1,
                  geometry)
            check("不再需要那个「唯一出口」返回按钮",
                  not geometry["hasBackBtn"], geometry)
            # And the exit genuinely works: a real click on a real nav tab.
            page.evaluate("""() => {
              const t = [...document.querySelectorAll('.nav [data-nav-key]')]
                        .find(e => e.dataset.navKey === 'inbox');
              if (t) t.click();
            }""")
            page.wait_for_timeout(800)
            check("点导航页签能从 Data Center 出来、外壳恢复",
                  page.evaluate("""() => getComputedStyle(document.getElementById('shell-wrap')).display !== 'none'
                    && getComputedStyle(document.getElementById('datacenter-page')).display === 'none'
                    && getComputedStyle(document.getElementById('page-inbox')).display !== 'none'"""))
            check("出来之后落在被点的那一页，不是空白主页",
                  visible_pages(page) == ["inbox"], visible_pages(page))

            check("整页没有 JS 报错", errors == [], errors[:3])
            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())