"""The dashboard card wall: one height, two lines, right-click menu, and a way out.

    .venv312/bin/python api/tests/verify_dashboard_card_menu_ui.py

Four reader complaints, and none of them can be read off the source:

* **Fixed height.** The summary had no rule at all — no clamp, and a bare `<p>`'s
  1em bottom margin. So a four-line description made its card ~60px taller and
  stepped the whole grid row out of line. Asserted as an *invariant* (every card
  the same height), not as a pixel value, because the cover is 16:9 and moves with
  the viewport.
* **Two lines, truncated.** `scrollHeight > clientHeight` has to be TRUE for at
  least one card. "Every summary is two lines" is also what an empty wall says, and
  this fixture deliberately has a 5-line card to prove the clamp is what is holding
  it down rather than the text being short.
* **Right-click.** There was no `contextmenu` listener on `#dsh-grid` *at all* —
  the menu only opened from a ⋮ button. Dispatching a click here instead would open
  the VIEWER and every assertion below would be measuring the wrong surface.
* **A way out.** `#dsh-menu` is its own element, so the Workspace wall's
  outside-click handler (which only ever looked at `#rpt-menu`) never dismissed it.
  That is the whole reason a menu opened here could not be closed.

The dismiss checks use `page.mouse.click(x, y)` on real coordinates, never
`element.click()`: a synthetic click dispatches straight at the element and does no
hit-testing, so it passes on a target that is completely covered — which is exactly
the bug being guarded.

`Turn into a knowledge page` is here for the reason it is subtle: the Workspace
version of this action builds `?report=<slug>` and `GET /api/reports/<slug>/raw`.
Reusing it for a dashboard would hand the reader a prompt pointing a dashboard slug
at the REPORT path, i.e. a 404 they only discover after pasting it into an agent.
The assertion is on the prompt TEXT, because "the menu has the item" and "the item
tells the agent to fetch something real" are different claims and only the second
one is the feature.

The API is stubbed and every request is recorded; the app under test is the real
`frontend/out`, served off disk, so `app.css` is the real stylesheet.
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
PORT = 8809

FAILURES = []

# Summaries from absent to five lines. The long ones are what make "two lines"
# mean something: without them the clamp could be deleted and this test would still
# pass.
CARDS = [
    ("alpha", "无摘要卡 / No summary", ""),
    ("beta", "一行摘要 / One line", "45 个区域明细与价格带矩阵。"),
    ("gamma", "三行摘要 / Three lines",
     "价格带与区域的经营概览：四关键指标、量排名、月度走势，"
     "全部可下钻到明细页，每一行都可以复制到表格里核对口径。"),
    ("delta", "五行摘要 / Five lines",
     "Channel performance sliced by period and metric; readers switch views on the "
     "left rail, no controls in the page. 一页可转发的窄栏简报，结论先行，数字"
     "都是可复制的文字，这样合打印。The quick brown fox jumps over the lazy dog and "
     "then keeps running rather a good while longer than anybody expected."),
    ("epsilon", "中文长摘要 / Long zh",
     "按严重度排序的异常清单，加上收入构成与渠道争议案，供区域经理逐条销项；"
     "每一行都可以复制到表格里核对口径，避免口径漂移；刷新后仍然保留当前"
     "筛选条件与会话位置，不会把读者丢回第一页。"),
    ("zeta", "两行摘要 / Two lines",
     "六个月的 sell-in 相对 2025 全部走低 43.3%，均匀分布在三个渠道，"
     "低价带跌得最狠。"),
    # Six more, filler summaries. They are here so the wall is TALLER than the
    # viewport: a dismissal test for "scroll" run against a page that cannot scroll
    # fires no scroll event at all and is green for free.
    ("eta", "填充卡 / Filler 1", "第一份季度汇总，供销例会逐条过。"),
    ("theta", "填充卡 / Filler 2",
     "第二份渠道复盘，覆盖量、价、折扣三段，结论先行。"),
    ("iota", "填充卡 / Filler 3", "第三份区域明细，供区域经理自行下钻核对。"),
    ("kappa", "填充卡 / Filler 4",
     "第四份价格带决策台账，一行一个价格带，可编辑可批注。"),
    ("lambda", "填充卡 / Filler 5", "第五份异常清单，按严重度排序，逐条销项。"),
    ("mu", "填充卡 / Filler 6", "第六份月度走势，含量排名与环比。"),
]

WALL = [{
    "slug": slug, "title": title,
    "summary": summary, "summary_zh": summary,
    "datasets": [], "kind": "static", "has_filters": False,
    "status": "published", "visibility": "private",
    "owner_email": "me@example.com", "tags": [],
    "accent": "", "pinned": False, "in_nav": False, "in_home": False,
    "nav_order": 0, "home_order": 0, "home_collapsed": False,
    "has_cover": False, "cover_url": "", "views": 0,
} for slug, title, summary in CARDS]

#: Answers read off the real endpoints, not guessed. `GET /api/dashboard` is a BARE
#: ARRAY — a first cut of this fixture returned `{items: […]}`, and the page then
#: said "no dashboards yet" with a 200 and no error, so every geometry assertion
#: below would have measured an empty wall.
STUB = r"""
window.__calls = [];
window.__clip = null;
window.fetch = function (url, init) {
  const u = String(url);
  window.__calls.push(u);
  const json_ = (b) => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(b)});
  if (u.indexOf('/api/dashboard') > -1 && /\/api\/dashboard\/[a-z-]+\/filters/.test(u)) {
    return json_({slug: 'alpha', kind: 'static', configured: false, schema: null,
                  selection: {}, effective: {}, dropped: [], can_edit: false});
  }
  if (u.indexOf('/api/dashboard') > -1 && /\/api\/dashboard\/[a-z-]+/.test(u)) {
    return json_({html: '<html><body>page</body></html>'});
  }
  if (u.indexOf('/api/dashboard') > -1) {
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(WALL_JSON)});
  }
  if (u.indexOf('/api/reports') > -1) { return json_({items: [], projects: [], folders: []}); }
  if (u.indexOf('/api/auth/me') > -1) { return json_({email: 'me@example.com', role: 'admin'}); }
  if (u.indexOf('/api/rail') > -1 || u.indexOf('/api/org/me') > -1) {
    return json_({modules: [], email: 'me@example.com'});
  }
  return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{}'),
                          json: () => Promise.resolve({})});
};
// The clipboard is a real permission boundary; record instead of granting.
Object.defineProperty(navigator, 'clipboard', {
  configurable: true,
  value: {writeText: (t) => { window.__clip = t; return Promise.resolve(); }},
});
"""
STUB = STUB.replace("WALL_JSON", json.dumps(WALL, ensure_ascii=False))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def right_click(page, index=0, fx=0.5, fy=0.5):
    """A REAL right-click at a point inside the card — mouse, not dispatchEvent."""
    box = page.evaluate("""([i, fx, fy]) => {
      const card = document.querySelectorAll('#dsh-grid .rpt-card')[i];
      const r = card.getBoundingClientRect();
      return {x: r.left + r.width * fx, y: r.top + r.height * fy};
    }""", [index, fx, fy])
    page.mouse.click(box["x"], box["y"], button="right")
    page.wait_for_timeout(250)
    return box


def blank_point(page):
    """A coordinate that is genuinely blank: the wall's own background, not a card.

    Scoped to `#dsh-grid` on purpose. A first cut scanned the whole viewport and took
    the first hit-test miss, which came back as a rail icon — clicking it navigated
    away, every card then measured 0×0, and the "the menu closes" checks went green
    against a menu that had never opened. Blank space that belongs to the wall cannot
    navigate, so it cannot fake a dismissal.
    """
    return page.evaluate("""() => {
      const grid = document.getElementById('dsh-grid');
      const menu = document.getElementById('dsh-menu');
      if (!grid) return null;
      for (let y = 4; y < grid.clientHeight - 4; y += 8) {
        for (let x = 4; x < grid.clientWidth - 4; x += 8) {
          const el = document.elementFromPoint(
            grid.getBoundingClientRect().left + x,
            grid.getBoundingClientRect().top + y);
          if (!el || !grid.contains(el)) continue;
          if (el.closest('.rpt-card')) continue;
          if (menu && menu.contains(el)) continue;
          return {x: Math.round(grid.getBoundingClientRect().left + x),
                  y: Math.round(grid.getBoundingClientRect().top + y),
                  tag: el.tagName, cls: (el.className || '').toString().slice(0, 40)};
        }
      }
      return null;
    }""")


def scroller(page):
    """The element that actually scrolls — NOT `document.scrollingElement`.

    The shell is `height: calc(100vh - var(--nav-h))` and the wall scrolls inside
    `.rpt-body` (`overflow-y: auto`), so the window itself never overflows:
    `scrollingElement` reports `room: 0` on a wall that plainly does not fit. Asking
    the wrong element makes the scroll-dismiss check pass without a scroll event ever
    being emitted — which is what happened on the first run of this file.
    """
    return page.evaluate("""() => {
      const out = [];
      for (const el of document.querySelectorAll('html, body, .shell, .rpt-body, .rpt-viewer, #page-dashboard')) {
        if (el.scrollHeight - el.clientHeight > 20) {
          out.push({sel: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
                          + (el.className ? '.' + String(el.className).trim().split(/\\s+/).join('.') : ''),
                    top: el.scrollTop, room: el.scrollHeight - el.clientHeight});
        }
      }
      return out;
    }""")


def menu_open(page):
    return page.evaluate("() => { const m = document.getElementById('dsh-menu');"
                         " return !!m && !m.hidden && m.getBoundingClientRect().width > 0; }")


def main():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def log_message(self, *a):
            pass

    httpd = _Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % PORT
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        # ⚠️ `add_init_script`, not `add_script_tag`: the stub REPLACES window.fetch,
        # and a navigation throws the page's globals away, so a tag loaded before the
        # goto is silently gone by the time the app runs.
        page.add_init_script(STUB)
        page.goto(base + "/index.html", wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.dashboardPage", timeout=20000)
        page.evaluate("() => navTo(null, 'dashboard', 'Dashboard')")
        page.wait_for_function("() => document.querySelectorAll('#dsh-grid .rpt-card').length > 0",
                               timeout=20000)
        page.wait_for_timeout(500)

        n = page.evaluate("() => document.querySelectorAll('#dsh-grid .rpt-card').length")
        check("卡片墙渲染出来了", n == len(CARDS), "cards=%d expected=%d" % (n, len(CARDS)))

        # ── 1. one height for every card ──────────────────────────────────────
        heights = page.evaluate(
            "() => [...document.querySelectorAll('#dsh-grid .rpt-card')]"
            ".map((c) => Math.round(c.getBoundingClientRect().height * 10) / 10)")
        spread = max(heights) - min(heights)
        check("每张卡同高（墙是齐的）", spread <= 1,
              "heights=%s spread=%.1f" % (heights, spread))

        bodies = page.evaluate(
            "() => [...new Set([...document.querySelectorAll('#dsh-grid .rpt-card-body')]"
            ".map((b) => Math.round(b.getBoundingClientRect().height)))]")
        check("卡片正文高度也一致（不靠 grid 拉伸撑着）", len(bodies) == 1, bodies)

        # The byline is the one row that must sit ON the bottom edge. A card with no
        # summary has slack where the summary would be, and `margin-top: auto` is what
        # parks the byline on the floor instead of letting it float up under the title.
        slack = page.evaluate("""() => [...document.querySelectorAll('#dsh-grid .rpt-card')]
          .map((c) => {
            const body = c.querySelector('.rpt-card-body');
            const sub = c.querySelector('.rpt-card-sub');
            const pad = parseFloat(getComputedStyle(body).paddingBottom);
            return {slug: c.dataset.slug,
                    gap: Math.round((body.getBoundingClientRect().bottom - pad
                                     - sub.getBoundingClientRect().bottom) * 10) / 10};
          })""")
        check("作者/日期行贴在卡片底部（无摘要的卡也不上浮）",
              all(abs(s["gap"]) <= 1.5 for s in slack), slack)

        # ── 2. two lines, actually truncated ──────────────────────────────────
        sums = page.evaluate("""() => [...document.querySelectorAll('#dsh-grid .rpt-card')]
          .map((c) => {
            const s = c.querySelector('.rpt-card-sum');
            if (!s) return {slug: c.dataset.slug, none: true};
            const cs = getComputedStyle(s);
            const lh = parseFloat(cs.lineHeight);
            return {slug: c.dataset.slug, none: false,
                    clamp: cs.webkitLineClamp, display: cs.display,
                    client: s.clientHeight, scroll: s.scrollHeight,
                    lineH: lh, lines: Math.round(s.scrollHeight / lh * 10) / 10,
                    truncated: s.scrollHeight > s.clientHeight};
          })""")
        with_sum = [s for s in sums if not s["none"]]
        check("有摘要的卡片都设了 2 行夹断",
              all(s["clamp"] == "2" for s in with_sum),
              [(s["slug"], s["clamp"]) for s in with_sum])
        check("摘要高度正好等于 2 行",
              all(abs(s["client"] - 2 * s["lineH"]) <= 1.5 for s in with_sum),
              [(s["slug"], s["client"], s["lineH"]) for s in with_sum])
        check("超长的摘要确实被截断了（scrollHeight > clientHeight）",
              any(s["truncated"] for s in with_sum),
              [(s["slug"], s["scroll"], s["client"]) for s in with_sum])
        check("没有摘要的卡片不渲染空 <p>",
              all(s["none"] for s in sums if s["slug"] == "alpha"), sums[0])

        # ── 3. no ⋮ button; the menu opens on right-click ────────────────────
        check("卡上不再有 ⋮ 按钮",
              page.evaluate("() => !document.querySelector('#dsh-grid .rpt-card-acts, "
                            "#dsh-grid .rpt-more')"),
              page.evaluate("() => document.querySelectorAll('#dsh-grid button').length"))

        blank0 = blank_point(page)
        check("找得到一个真正的空白坐标（在卡片墙自己的底色上）", blank0 is not None, blank0)
        if blank0:
            page.mouse.click(blank0["x"], blank0["y"])
            page.wait_for_timeout(250)
            check("在空白处左键不会凭空开菜单", not menu_open(page),
                  "clicked <%s class=%r>" % (blank0["tag"], blank0["cls"]))

        at = right_click(page, 2)
        check("右键卡片打开了菜单", menu_open(page))
        geo = page.evaluate("""() => {
          const m = document.getElementById('dsh-menu');
          const r = m.getBoundingClientRect();
          return {x: r.left, y: r.top, slug: m.dataset.slug,
                  count: m.querySelectorAll('[data-act]').length};
        }""")
        check("菜单是那张卡的（dataset.slug 对得上）", geo["slug"] == "gamma", geo["slug"])
        check("菜单左上角跟着鼠标走",
              abs(geo["x"] - at["x"]) <= 20 and abs(geo["y"] - at["y"]) <= 20,
              "mouse=(%.0f,%.0f) menu=(%.0f,%.0f)" % (at["x"], at["y"], geo["x"], geo["y"]))
        check("菜单不是空盒子", geo["count"] >= 3, geo["count"])

        acts = page.evaluate(
            "() => [...document.querySelectorAll('#dsh-menu [data-act]')].map((b) => b.dataset.act)")
        check("菜单里有「转成知识库页面」", "to-knowledge" in acts, acts)
        check("菜单里仍有加入左侧栏", "rail-add" in acts, acts)

        # ── 4. four ways out ──────────────────────────────────────────────────
        # ⚠️ Every one of these asserts the menu was OPEN before asserting it closed.
        # A dismissal check run against a menu that never opened is green no matter how
        # broken the dismissal is — which is exactly how "the menu cannot be closed"
        # survived: the earlier draft clicked the top bar by accident, navigated off
        # the page, and every card then measured 0×0 while the menu stayed shut.
        def dismiss(label, act):
            right_click(page, 2)
            if not check("（前置）%s 之前菜单是开的" % label, menu_open(page)):
                return
            act()
            page.wait_for_timeout(300)
            check("%s 能关掉菜单" % label, not menu_open(page))

        blank = blank_point(page)
        check("找得到一个真正的空白坐标", blank is not None, blank)
        if blank:
            page.mouse.click(blank["x"], blank["y"])
            page.wait_for_timeout(250)
            check("点击空白处关闭菜单", not menu_open(page),
                  "clicked <%s class=%r> at %s" % (blank["tag"], blank["cls"],
                                                   (blank["x"], blank["y"])))

        dismiss("Escape", lambda: page.keyboard.press("Escape"))

        # ⚠️ Guarded: if the wall cannot scroll, no `scroll` event is ever emitted and
        # "the menu closed on scroll" would pass without anything happening.
        boxes = scroller(page)
        check("页面确实能滚动（否则下面那条是空断言）", bool(boxes), boxes)
        right_click(page, 2)
        if check("（前置）滚动之前菜单是开的", menu_open(page)):
            box = page.evaluate("""() => {
              const r = document.querySelectorAll('#dsh-grid .rpt-card')[2].getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }""")
            page.mouse.move(box["x"], box["y"])
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(400)
            moved = scroller(page)
            check("鼠标滚轮真的滚动了页面",
                  any(b["top"] > 0 for b in moved), moved)
            check("滚动 能关掉菜单", not menu_open(page))

        # A left-click on ANOTHER card must not leave the menu pinned to the first.
        right_click(page, 0)
        if check("（前置）右键另一张卡后菜单是开的", menu_open(page)):
            check("菜单换到了那张卡",
                  page.evaluate("() => document.getElementById('dsh-menu').dataset.slug") == "alpha")
        page.evaluate("() => document.querySelectorAll('#dsh-grid .rpt-card')[3].click()")
        page.wait_for_timeout(400)
        check("左键开页面时菜单也收掉（不会压在页面上面）", not menu_open(page))
        check("左键确实打开了查看器",
              page.evaluate("() => document.getElementById('dsh-viewer').classList.contains('open')"))
        page.evaluate("() => window.dashboardPage.closeViewer()")
        page.wait_for_timeout(300)

        # ── 5. the prompt has to point at something that exists ───────────────
        right_click(page, 3)
        if check("（前置）知识库菜单项可点", menu_open(page)) and page.evaluate(
                "() => !!document.querySelector('#dsh-menu [data-act=\"to-knowledge\"]')"):
            page.evaluate("() => document.querySelector('#dsh-menu [data-act=\"to-knowledge\"]').click()")
        page.wait_for_timeout(600)
        clip = page.evaluate("() => window.__clip")
        check("点「转成知识库页面」真的复制了提示词", bool(clip), (clip or "")[:40])
        if clip:
            check("提示词给出页面地址 /d/<slug>", "/d/delta" in clip,
                  [l for l in clip.split("\n") if l.startswith("页面地址")])
            check("提示词让 agent 取的原文是仪表盘端点",
                  "GET /api/dashboard/delta" in clip or "/api/dashboard/delta" in clip,
                  [l for l in clip.split("\n") if "原文" in l])
            # ⚠️ The trap this exists for: the Workspace action builds `?report=` and
            # `api/reports/…/raw`, and a DASHBOARD slug on the report path is a 404
            # the reader only finds out about after pasting the prompt.
            check("提示词里没有残留的报告端点",
                  "api/reports" not in clip and "?report=" not in clip,
                  [l for l in clip.split("\n") if "api/" in l])
            check("提示词说明了原文在 html 字段里", "html" in clip,
                  [l for l in clip.split("\n") if "html" in l])

        browser.close()

    check("页面没有 JS 报错", not errors, errors[:3])
    httpd.shutdown()

    print("\n" + "=" * 64)
    if FAILURES:
        print("失败 %d 项：" % len(FAILURES))
        for f in FAILURES:
            print("  ✗", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
