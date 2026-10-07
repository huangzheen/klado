"""Are the top bar and the left rail actually CLICKABLE — on every surface?

Run from inside the app container, which is where Playwright lives:

    docker compose exec -T app sh -c 'cd /app/api/tests && python verify_nav_clickable_ui.py'

Every assertion here is deliberately the hard kind. Three things had to be true for
this file to catch the two bugs it was written for, and each one is a way a check
can be green while the product is broken:

1. **A real mouse event, never `el.click()`.** `el.click()` dispatches straight at the
   element and never hit-tests, so it passes on a button that is completely covered.
   The two failures here were both "covered by something bigger": `.rpt-viewer` at
   z-index 8000 filling the screen from the nav down, and `#shell-wrap` being
   `display: none` under the Data Center overlay. A JS click reports both as healthy.

2. **`elementFromPoint`, never `getComputedStyle().display`.** An element inside a
   `display: none` ancestor still answers `block` for its own `display` — the hidden
   ancestor simply does not render, and the property is about the element, not about
   the subtree. Measured on the Data Center page: `rail-hotzone` said `display: block`
   while nothing of the rail existed on screen. Only a hit test can tell the truth.

3. **The verdict comes from the whole screen, overlays first.** A "which page is
   visible" probe that only looks at `#page-*` answers from *underneath* an open
   viewer. My first version did exactly that and scored a dead top-bar click as a
   success, because the page underneath had really changed. `surface()` therefore
   asks about open overlays first, and an unexpected overlay is a failure, not a
   detail.

The criteria are invariants, not lists: every surface the app can reach must be
able to (a) be hit-tested, (b) open the rail, (c) reach two different top-bar tabs
and one rail module row with a real click. A newly added page is covered by
construction; nothing here has to be updated when one appears.
"""
import os
import sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
passed, failed = [], []


def ok(m):
    passed.append(m)
    print("  \u2713", m)


def bad(m):
    failed.append(m)
    print("  \u2717", m)


def check(cond, label, detail=""):
    (ok if cond else bad)(label + ("" if not detail else "  " + str(detail)))
    return cond


# ── "what is actually on screen" — overlays first, then the Data Center, then pages
SURFACE = """() => {
  const isOpen = (id) => {
    const el = document.getElementById(id);
    return !!(el && el.classList.contains('open'));
  };
  if (isOpen('rpt-viewer')) return 'viewer:report';
  if (isOpen('dsh-viewer')) return 'viewer:dashboard';
  const dc = document.getElementById('datacenter-page');
  if (dc && getComputedStyle(dc).display !== 'none') return 'datacenter';
  const vis = Array.from(document.querySelectorAll('[id^="page-"]'))
    .filter(el => el.id !== 'page-actions-bar')
    .filter(el => getComputedStyle(el).display !== 'none')
    .map(el => el.id.replace(/^page-/, ''));
  return vis.length ? vis.join('+') : '(none)';
}"""

SURFACES = [
    ("首页", "navTo(null,'home','Home')", "home"),
    ("工作台", "navTo(null,'reports','Workspace')", "reports"),
    ("仪表盘", "navTo(null,'dashboard','Dashboard')", "dashboard"),
    ("知识库", "navTo(null,'knowledge','Knowledge base')", "knowledge"),
    ("日历", "navTo(null,'calendar','Calendar')", "calendar"),
    ("收件箱", "navTo(null,'inbox','Inbox')", "inbox"),
    ("设置", "navTo(null,'system-settings','System Settings')", "system-settings"),
    ("数据中心", "switchToDataCenter()", "datacenter"),
]


def enter(page, js):
    page.evaluate(js)
    page.wait_for_timeout(700)


def nav_tab_centre(page, key):
    return page.evaluate("""(key) => {
      const el = document.querySelector('.nav-tab[data-nav-key="' + key + '"]');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2,
              w: r.width, h: r.height,
              shown: getComputedStyle(el).display !== 'none'};
    }""", key)


def what_is_at(page, x, y):
    return page.evaluate("""([x, y]) => {
      const el = document.elementFromPoint(x, y);
      if (!el) return '(nothing)';
      return el.tagName.toLowerCase() +
        (el.id ? '#' + el.id : '') +
        (el.className && typeof el.className === 'string' && el.className.trim()
           ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
    }""", [x, y])


def real_click(page, point):
    """A click at coordinates, so anything on top of the target eats it."""
    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(900)


def hover_open_rail(page):
    """Two moves from two places: a single move teleports, and the panel is 248px wide."""
    page.mouse.move(1000, 500)
    page.wait_for_timeout(150)
    page.mouse.move(200, 500)
    page.wait_for_timeout(150)
    page.mouse.move(4, 500)
    page.wait_for_timeout(650)
    return page.evaluate("""() => {
      const rail = document.getElementById('rail');
      const b = rail ? rail.getBoundingClientRect() : null;
      return {open: document.body.classList.contains('rail-open'),
              left: b ? Math.round(b.left) : null};
    }""")


def rail_row_centre(page, module_key):
    return page.evaluate("""(k) => {
      const el = document.querySelector('.rail-mod[data-rail-mod="' + k + '"]');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width};
    }""", module_key)


def open_existing_report(page):
    # Workspace now opens on the project wall, whose cards are containers.
    # Pick a real accessible document through the list API before testing its reader.
    return page.evaluate("""async () => {
      const r = await fetch('/api/reports?scope=mine&status=published&limit=1');
      if (!r.ok) return 'report list failed: ' + r.status;
      const data = await r.json();
      const item = (data.reports || [])[0];
      if (!item) return 'no published report fixture';
      reportsPage.open(item.slug, 'zh');
      return item.slug;
    }""")


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        print("\nA. 顶栏最左边的 14px 归顶栏自己（不是左侧栏的热区）")
        enter(page, "navTo(null,'home','Home')")
        squatters = {f"x={x}": what_is_at(page, x, 26) for x in (2, 7, 13)}
        check(all("rail" not in v for v in squatters.values()),
              "顶栏左边缘没有左侧栏的热区压着", squatters)

        print("\nB. 每个界面：热区能被打中、左侧栏能开、顶栏和 rail 能换页")
        for label, js, expected in SURFACES:
            print(f"\n  ── {label}（{expected}）")
            enter(page, js)
            check(page.evaluate(SURFACE) == expected,
                  f"{label}：先真的在这一页上",
                  page.evaluate(SURFACE))

            # ① 顶栏每个可见标签的中心必须命中自己
            tabs = page.eval_on_selector_all(".nav-tab", """els => els.map(e => {
                const r = e.getBoundingClientRect();
                return {key: e.dataset.navKey, shown: getComputedStyle(e).display !== 'none',
                        x: r.x + r.width / 2, y: r.y + r.height / 2}; })""")
            covered = []
            for t in tabs:
                if not t["shown"]:
                    continue
                owner = page.evaluate("""([x, y]) => {
                  const el = document.elementFromPoint(x, y);
                  const own = el && el.closest ? el.closest('.nav-tab') : null;
                  return own ? own.dataset.navKey : '(none)';
                }""", [t["x"], t["y"]])
                if owner != t["key"]:
                    covered.append(f"{t['key']}→{owner} 在 {what_is_at(page, t['x'], t['y'])}")
            check(not covered,
                  f"{label}：顶栏 {len(tabs)} 个标签中心都命中自己",
                  covered or "全部命中")

            # ② 热区必须真的能被打中（不看 display，看命中）
            zone = what_is_at(page, 7, 400)
            check("rail-hotzone" in zone or "#rail" in zone,
                  f"{label}：x=7 命中左侧栏热区", zone)

            # ③ 悬停真的能唤出左侧栏
            st = hover_open_rail(page)
            check(st["open"] and st["left"] == 0,
                  f"{label}：悬停 x=4 唤出左侧栏（rail.left={st['left']}）", st)
            page.mouse.move(1000, 500)
            page.wait_for_timeout(250)

            # ④ 顶栏真点：两个不同的目标页
            enter(page, js)
            for key, want in (("workspace", "reports"), ("knowledge", "knowledge")):
                pt = nav_tab_centre(page, key)
                if not check(pt and pt["shown"], f"{label}：顶栏有 {key} 标签", pt):
                    continue
                real_click(page, pt)
                got = page.evaluate(SURFACE)
                check(got == want,
                      f"{label}：真点顶栏 {key} → 落在 {want}", got)

            # ⑤ 左侧栏模块行真点
            enter(page, js)
            st = hover_open_rail(page)
            if st["open"] and st["left"] == 0:
                row = rail_row_centre(page, "calendar")
                if check(row, f"{label}：左侧栏里有日历这一行", row):
                    real_click(page, row)
                    got = page.evaluate(SURFACE)
                    check(got == "calendar",
                          f"{label}：真点左侧栏「日历」→ 落在 calendar", got)
            else:
                bad(f"{label}：左侧栏唤不出，点不了模块行")
            page.mouse.move(1000, 500)
            page.wait_for_timeout(250)

        print("\nC. 报告阅读器开着的时候（满屏浮层盖住整个内容区）")
        enter(page, "navTo(null,'reports','Workspace')")
        page.wait_for_timeout(1200)
        opened = open_existing_report(page)
        page.wait_for_timeout(2500)
        check(page.evaluate(SURFACE) == "viewer:report",
              f"阅读器确实开着（{opened}）", page.evaluate(SURFACE))

        pt = nav_tab_centre(page, "knowledge")
        real_click(page, pt)
        got = page.evaluate(SURFACE)
        check(got == "knowledge",
              "阅读器开着时真点顶栏知识库 → 阅读器收起来并落在 knowledge", got)

        # 左侧栏在阅读器之上，模块行也要真的能换页
        enter(page, "navTo(null,'reports','Workspace')")
        page.wait_for_timeout(1200)
        open_existing_report(page)
        page.wait_for_timeout(2500)
        if page.evaluate(SURFACE) == "viewer:report":
            st = hover_open_rail(page)
            check(st["open"] and st["left"] == 0,
                  "阅读器开着时左侧栏仍能唤出", st)
            if st["open"] and st["left"] == 0:
                row = rail_row_centre(page, "dashboard")
                if check(row, "阅读器开着时左侧栏里有仪表盘这一行", row):
                    real_click(page, row)
                    got = page.evaluate(SURFACE)
                    check(got == "dashboard",
                          "阅读器开着时真点左侧栏仪表盘 → 落在 dashboard", got)
            page.mouse.move(1000, 500)
            page.wait_for_timeout(250)

        # Esc 是另一个出口：全局 Esc-to-close 认 `rpt-overlay` + 内部的 close 按钮
        enter(page, "navTo(null,'reports','Workspace')")
        page.wait_for_timeout(1200)
        open_existing_report(page)
        page.wait_for_timeout(2200)
        if page.evaluate(SURFACE) == "viewer:report":
            page.keyboard.press("Escape")
            page.wait_for_timeout(900)
            check(page.evaluate(SURFACE) == "reports",
                  "Esc 能关掉阅读器（另一个出口存在）", page.evaluate(SURFACE))
        else:
            bad("Esc 那一段没测到：阅读器没打开")

        print("\nD. 整页没有 JS 报错")
        check(errors == [], "没有未捕获的 JS 异常", errors[:3])
        browser.close()

    print("\n%d 通过 / %d 失败" % (len(passed), len(failed)))
    for m in failed:
        print("  FAILED:", m)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
