"""The top nav must exist on EVERY page — asserted against the screen, not the source.

    docker compose exec -T app sh -c 'cd /app/api/tests && python verify_nav_always_ui.py'

Why this can only be a browser test:

* the failure mode is INVISIBILITY, not absence. `#rpt-viewer` is `position: fixed;
  z-index: 8000` with an opaque background, so a full-bleed `inset: 0` paints over a
  `.nav` that is still in the DOM, still `display: flex`, and still at y=0. Every
  stylesheet read reports a healthy nav. `document.elementFromPoint` at the nav's own
  centre is the only thing that reports what the reader actually sees — it is the
  assertion in `hitInsideNav`.
* the Dashboard tab was once `hidden` in the markup and filtered out of `inNav()` by a
  `rail_only` flag. Both are one line of source, so both are readable statically; the
  point of checking them here is that "the tab is in the DOM" is not "the tab is on
  screen", and only a rect proves the second.
* a MODAL must still cover the nav. Lowering the viewer until the nav always wins would
  pass a naive version of this file while breaking every dialog, so `kldDialog` is
  asserted to be ON TOP — the layering is pinned from both sides.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
passed, failed = [], []


def ok(m):
    passed.append(m); print("  ✓", m)


def bad(m):
    failed.append(m); print("  ✗", m)


# Samples three points across the bar (brand / tabs / right cluster) rather than one:
# an overlay that covers only part of the row — a filter rail, a wide tooltip, a
# right-aligned panel — passes a single centre sample.
PROBE = """() => {
  const nav = document.querySelector('nav.nav');
  if (!nav) return {missing: true};
  const b = nav.getBoundingClientRect();
  const cs = getComputedStyle(nav);
  const y = Math.round(b.top + b.height / 2);
  const xs = [Math.round(b.left + 40), Math.round(b.left + b.width / 2), Math.round(b.right - 60)];
  const hits = xs.map((x) => {
    const el = document.elementFromPoint(x, y);
    return el ? (el.id || el.tagName) : null;
  });
  const top = document.elementFromPoint(xs[1], y);
  return {
    top: Math.round(b.top), height: Math.round(b.height), width: Math.round(b.width),
    display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
    zIndex: cs.zIndex, hasRects: nav.getClientRects().length > 0,
    hits: hits, insideNav: top ? nav.contains(top) : false,
    topEl: top ? (top.id || top.tagName) : null,
  };
}"""


def check(page, label, need_top=0):
    r = page.evaluate(PROBE)
    if r.get("missing"):
        bad(f"{label}: 顶栏根本不在 DOM 里")
        return None
    if r["display"] == "none" or r["visibility"] == "hidden" or not r["hasRects"]:
        bad(f"{label}: 顶栏被隐藏了 (display={r['display']} vis={r['visibility']} rects={r['hasRects']})")
        return r
    if r["height"] < 30 or r["width"] < 600:
        bad(f"{label}: 顶栏盒子不对 {r['width']}x{r['height']}")
        return r
    if r["top"] != need_top:
        bad(f"{label}: 顶栏不在 y={need_top}，在 y={r['top']}")
        return r
    if not r["insideNav"]:
        bad(f"{label}: 顶栏被盖住了 —— 中心点命中 {r['topEl']} 而不是顶栏本身"
            f"（三个采样点 {r['hits']}）")
        return r
    ok(f"{label}: 顶栏在 {r['width']}x{r['height']} @y{r['top']}，中心点命中 {r['topEl']}")
    return r


with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-sandbox"])
    page = br.new_context(viewport={"width": 1600, "height": 1000}).new_page()
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    # ── 1. every module page, opened by its own opener ──
    check(page, "首页 / home")
    for label, js in [
        ("数据中心 / Data Center", "switchToDataCenter()"),
        ("收件箱 / Inbox", "navTo(null,'inbox','Inbox')"),
        ("工作台 / Workspace", "reportsPage.openFromNav(document.getElementById('nav-tab-workspace'))"),
        ("仪表盘 / Dashboard", "dashboardPage.openFromNav(document.getElementById('nav-tab-dashboard'))"),
        ("知识库 / Knowledge", "navTo(null,'knowledge','Knowledge base')"),
        ("日历 / Calendar", "navTo(null,'calendar','Calendar')"),
        ("设置 / Settings", "navTo(null,'system-settings','System Settings')"),
    ]:
        try:
            page.evaluate(js)
        except Exception as e:
            bad(f"{label}: 入口自己抛了 {str(e)[:90]}")
            continue
        page.wait_for_timeout(1100)
        check(page, label)

    # ── 2. the report viewer: the one page-level overlay that used to cover the nav ──
    page.evaluate("navTo(null,'reports','Workspace')")
    page.wait_for_timeout(2500)
    n = page.evaluate("document.querySelectorAll('#page-reports .rpt-card').length")
    if not n:
        bad("工作台一张卡片都没有，读不了报告（数据不对，后面的断言会跟着失真）")
    else:
        page.evaluate("document.querySelector('#page-reports .rpt-card').click()")
        page.wait_for_timeout(3000)
        vis = page.evaluate("!!(document.querySelector('#rpt-viewer') || {}).classList"
                            "&& document.querySelector('#rpt-viewer').classList.contains('open')")
        if not vis:
            bad("点了卡片但阅读器没打开，后面的断言没有意义")
        check(page, "报告阅读器 / report viewer")

        r = page.evaluate("""() => { const f = document.querySelector('#rpt-viewer .rpt-frame');
            if (!f) return null; const b = f.getBoundingClientRect();
            return {top: Math.round(b.top), h: Math.round(b.height), w: Math.round(b.width)}; }""")
        if not r or r["h"] < 200:
            bad(f"阅读器里的文档被压扁了：{r}")
        elif r["top"] < 52:
            bad(f"阅读器里的文档顶到了顶栏下面：top={r['top']}（应当 ≥ 52）")
        else:
            ok(f"阅读器里的文档仍是满高的：{r['w']}x{r['h']} @y{r['top']}")

        page.keyboard.press("Escape")
        page.wait_for_timeout(1200)
        check(page, "阅读器关闭后 / after close")

    # ── 3. the dashboard viewer reuses .rpt-viewer, so it inherits the same rule ──
    page.evaluate("navTo(null,'dashboard','Dashboard')")
    page.wait_for_timeout(2500)
    nd = page.evaluate("document.querySelectorAll('#page-dashboard .rpt-card').length")
    if not nd:
        bad("仪表盘一张卡片都没有")
    else:
        page.evaluate("document.querySelector('#page-dashboard .rpt-card').click()")
        page.wait_for_timeout(3000)
        check(page, "仪表盘阅读器 / dashboard viewer")
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)

    # ── 4. a knowledge page open in the shell ──
    page.evaluate("navTo(null,'knowledge','Knowledge base')")
    page.wait_for_timeout(2500)
    page.evaluate("""(() => { const rows = document.querySelectorAll(
        '#page-knowledge .kb-doc, #page-knowledge .kb-item, #page-knowledge tbody tr');
        if (rows[0]) rows[0].click(); })()""")
    page.wait_for_timeout(2500)
    check(page, "知识库正文 / knowledge reader")

    # ── 5. Dashboard is back in the top bar, and the tab actually navigates ──
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)
    tab = page.evaluate("""() => { const t = document.getElementById('nav-tab-dashboard');
        if (!t) return null; const b = t.getBoundingClientRect();
        return {hidden: t.hasAttribute('hidden'), w: Math.round(b.width), h: Math.round(b.height),
                text: t.textContent.trim()}; }""")
    if tab is None:
        bad("顶栏里没有 dashboard 标签")
    elif tab["hidden"] or tab["w"] == 0:
        bad(f"dashboard 标签在 DOM 里但不可见：hidden={tab['hidden']} {tab['w']}x{tab['h']}")
    else:
        ok(f"dashboard 标签在顶栏里可见：{tab['w']}x{tab['h']} 「{tab['text']}」")

    order = page.evaluate("""() => [...document.querySelectorAll('#nav-center [data-nav-key]')]
        .filter(e => !e.hidden).map(e => e.getAttribute('data-nav-key'))""")
    # ⚠️ Membership, never `.index()`: a tab that is missing is exactly what this file
    # exists to catch, and `list.index()` raises ValueError on it — which aborts the
    # run and silently skips the three assertions after it. A guard that crashes on
    # the failure it was written for reports less than one that never fires.
    if order and "dashboard" in order:
        ok("顶栏模块顺序：" + " → ".join(order))
    else:
        bad(f"顶栏顺序里没有 dashboard：{order}")

    if tab is None:
        bad("标签不在 DOM 里，跳过「点它能不能进页面」这条")
    else:
        page.evaluate("document.getElementById('nav-tab-dashboard').click()")
        page.wait_for_timeout(2500)
        shown = page.evaluate("""() => { const p = document.getElementById('page-dashboard');
            return p ? getComputedStyle(p).display !== 'none' : null; }""")
        lit = page.evaluate("""() => { const t = document.getElementById('nav-tab-dashboard');
            return t ? t.classList.contains('active') : null; }""")
        if shown and lit:
            ok("点顶栏 dashboard 标签 → 仪表盘页打开，标签高亮")
        else:
            bad(f"点顶栏 dashboard 标签没进页面：pageShown={shown} tabActive={lit}")
    check(page, "从顶栏进仪表盘")

    # ── 6. a rail module is in BOTH bars, and the rail still lists it ──
    # ⚠️ Real pointer moves, not a synthetic `mouseenter`: a single `mouse.move`
    # teleports and Chrome does not synthesise the `mouseenter` it would have caused,
    # and the first move after a load can land before the listeners are bound. Three
    # moves from three places is the path a real pointer takes (see verify_rail_ui.py).
    rail_has = False
    for _ in range(5):
        page.mouse.move(800, 500); page.wait_for_timeout(150)
        page.mouse.move(200, 500); page.wait_for_timeout(150)
        page.mouse.move(4, 500)
        try:
            page.wait_for_function(
                "() => document.body.classList.contains('rail-open')", timeout=1500)
        except Exception:
            pass
        rail_has = page.evaluate(
            "() => !!document.querySelector('#rail [data-rail-mod=\"dashboard\"]')")
        if rail_has:
            break
    if rail_has:
        ok("dashboard 同时在左侧栏里（两个入口，不是搬家）")
    else:
        bad("dashboard 从左侧栏里消失了 —— 它应该是「两个入口」，不是搬家")
    page.mouse.move(900, 500)
    page.wait_for_timeout(500)

    # ── 7. a MODAL must still cover the nav (pins the layering from the other side) ──
    # ⚠️ Do NOT `await` the dialog: `kldDialog.confirm` resolves on a CLICK, so an
    # awaited promise inside `page.evaluate` waits for a click that never comes and
    # the whole script hangs with no output. Fire it and let the page own the promise.
    page.evaluate("() => { kldDialog.confirm({title: 't', body: 'b', ok: 'ok', cancel: 'c'}); }")
    page.wait_for_timeout(1200)
    cov = page.evaluate(PROBE)
    if cov.get("missing"):
        bad("弹窗把顶栏从 DOM 里删了（不该发生）")
    elif cov["insideNav"]:
        bad("模态框没有盖住顶栏 —— 弹窗期间还能点到导航，这比导航消失更糟")
    else:
        ok(f"模态框照旧盖住顶栏（中心点命中 {cov['topEl']}）—— 弹窗优先级没有被改坏")
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)

    br.close()

print()
print(f"passed={len(passed)} failed={len(failed)}")
for f in failed:
    print("  FAIL:", f)
sys.exit(1 if failed else 0)
