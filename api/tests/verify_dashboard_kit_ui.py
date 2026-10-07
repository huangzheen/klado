"""Does each published dashboard actually RENDER, in both themes?

A dashboard whose queries all fail still serves 200 with a tidy empty shell, so
"the page loaded" is not the bar. Each one is measured for:
  · the house palette actually applied (body bg == --bg, not the browser default)
  · content: KPI numbers, tables, bars, charts — counted, not assumed
  · no failed request and no uncaught error
  · dark theme: the tokens follow, and the charts are still drawn
  · a narrow viewport: the grid collapses instead of overflowing
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
SLUGS = [
    ("exec-overview", "A 经营概览", {"kpi": 4, "bar": 3, "spark": 3, "row": 5}),
    ("ops-monitor", "B 运营监控", {"alert": 3, "donut": 1, "bar": 4, "spark": 5}),
    ("data-ledger", "C 明细台账", {"row": 45, "cellbar": 45, "heat": 27}),
    ("business-brief", "D 经营简报", {"figure": 5, "row": 3, "para": 4}),
]
passed, failed = [], []


def ok(m):
    passed.append(m); print("  ✓", m)


def bad(m):
    failed.append(m); print("  ✗", m)


def count(page, sel):
    return page.evaluate(f"document.querySelectorAll({sel!r}).length")


with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-sandbox"])
    ctx = br.new_context(viewport={"width": 1440, "height": 1000})
    page = ctx.new_page()

    for slug, label, want in SLUGS:
        errs, bad_reqs = [], []
        page.on("pageerror", lambda e: errs.append(str(e)[:120]))
        page.on("response", lambda r: bad_reqs.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        page.goto(f"{BASE}/d/{slug}", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2200)

        if errs:
            bad(f"{label}: 页面抛错 {errs[:2]}")
        if bad_reqs:
            bad(f"{label}: 有失败请求 {bad_reqs[:2]}")

        # The palette: a page with no theme.css computes body background as transparent.
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        themed = bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)")
        if themed:
            ok(f"{label}: 主题生效 body={bg}")
        else:
            bad(f"{label}: body 背景是 {bg} —— 套件没生效")

        # Content, by count. A number that reads "—" everywhere still renders rows.
        for key, n in want.items():
            sel = {"kpi": ".kld-kpi .val", "bar": ".kld-bar-row", "spark": ".kld-spark",
                   "row": ".kld-table tbody tr, .plain tbody tr", "alert": ".alert",
                   "donut": ".kld-donut", "cellbar": ".kld-table .inline",
                   "heat": ".heat", "figure": ".figures b", "para": ".sheet p"}[key]
            got = count(page, sel)
            if got >= n:
                ok(f"{label}: {key} {got} 个（期望 ≥{n}）")
            else:
                bad(f"{label}: {key} 只有 {got} 个（期望 ≥{n}）")

        # An empty-state or error box means the numbers did not arrive.
        for marker, txt in ((".kld-empty", "空态"), (".kld-skel", "骨架还在")):
            if count(page, marker):
                bad(f"{label}: 留下了{txt} {marker} —— 数据没取到")
        dashes = page.evaluate(
            "() => [...document.querySelectorAll('.kld-table td, .plain td, .kld-kpi .val')]"
            ".filter(e => e.textContent.trim() === '—').length")
        if dashes:
            bad(f"{label}: {dashes} 个格子是破折号 —— 数字没取到")

        page.screenshot(path=f"/tmp/klado-specs/dash_{slug}.png",
                        clip={"x": 0, "y": 0, "width": 1440, "height": 900})
        page.on("pageerror", lambda e: None)
        page.remove_listener("pageerror", page.listeners("pageerror")[-1]) if False else None

    # ── dark theme + narrow viewport, on one page each ──
    print("\n── 深色主题 / 窄屏")
    page.goto(f"{BASE}/d/exec-overview", wait_until="networkidle", timeout=60000)
    # ⚠️ `kladoTheme`, not `kldTheme` — theme.js's export. Typing the shorter name
    # throws a ReferenceError, and a thrown evaluate() aborts the whole script, so the
    # narrow viewport and every later assertion would silently not run.
    page.evaluate("() => kladoTheme.set('dark')")
    page.wait_for_timeout(1200)
    dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
    if dark == "rgb(14, 22, 33)":
        ok(f"深色主题：body={dark}（--bg 深色值）")
    else:
        bad(f"深色主题没跟上：body={dark}")
    if count(page, ".kld-spark") >= 3:
        ok("深色下图表仍然画着（不是被主题切换清空）")
    else:
        bad("深色下图表消失了")
    page.screenshot(path="/tmp/klado-specs/dash_exec_dark.png",
                    clip={"x": 0, "y": 0, "width": 1440, "height": 760})

    page.set_viewport_size({"width": 420, "height": 900})
    page.wait_for_timeout(900)
    over = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if over <= 1:
        ok("窄屏 420px 不横向溢出")
    else:
        bad(f"窄屏横向溢出 {over}px")
    page.screenshot(path="/tmp/klado-specs/dash_narrow.png",
                    clip={"x": 0, "y": 0, "width": 420, "height": 760})
    br.close()

print()
print(f"通过 {len(passed)} / 失败 {len(failed)}")
for f in failed:
    print("  FAIL:", f)
sys.exit(1 if failed else 0)
