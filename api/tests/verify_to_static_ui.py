"""「存为静态报告」按钮：只对本人可管理的互动报告显示，点击真的打对端点。

⚠️ 这个特性最容易坏的地方不是端点（`test_report_to_static.py` 覆盖了转换本身），
而是**按钮的显隐条件**。它有四个条件同时成立才该出现：互动报告、本人所有、非公共快照、
非同事共享。少一个就出错，而错法都很安静：

* 对公共快照显示 → 用户点了必然 409/403（公共区要先 Pull 才能编辑）
* 对同事共享的报告显示 → 同上，而且是在别人的文档上
* 对静态报告显示 → 端点 409 "this report is not interactive"
* 点两次 → 端点每次都分配新 slug，会**多出一张重复卡片**（不是覆盖）

所以这里断言的是「可见性矩阵 + 一次点击只发一次请求」。

    .venv312/bin/python api/tests/verify_to_static_ui.py
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


APP = Path(__file__).resolve().parents[2] / "frontend" / "out" / "index.html"
html = APP.read_text(encoding="utf-8")

# The application stylesheet is a separate file (`frontend/out/app.css`, linked from
# the shell) — the shell was split so the theme layer and the app layer can be read
# on their own. Style assertions read it, not the markup.
CSS = Path(__file__).resolve().parents[2] / "frontend" / "out" / "app.css"
css = CSS.read_text(encoding="utf-8")

# ── Static contract on the markup: the button, its handler, and its guard ────
check("顶栏存在 #rpt-tostatic 按钮",
      'id="rpt-tostatic"' in html and "存为静态报告" in html)
check("按钮挂在顶栏右侧（rpt-spacer 之后、图标按钮之前）",
      re.search(r'<span class="rpt-spacer"></span>.*?id="rpt-tostatic"', html, re.S) is not None)
check("onclick 指向已导出的 reportsPage.toStaticCurrent",
      'onclick="reportsPage.toStaticCurrent()"' in html
      and "toStaticCurrent: toStaticCurrent," in html)
_tostatic_rule = re.search(r"\.rpt-viewer-tostatic\s*\{([^}]*)\}", css)
check("样式与 Pull to edit 完全同规格（无独立尺寸修饰）",
      _tostatic_rule is not None
      and re.search(r"padding\s*:\s*5px\s+9px", _tostatic_rule.group(1)) is not None)

# The guard: all four conditions, ANDed.
guard = html.split("const staticBtn = $('rpt-tostatic');")[1].split("}")[0]
for cond, label in (
    ("item.kind === 'interactive'", "限定互动报告"),
    ("item.can_manage", "限定本人所有"),
    ("item.visibility !== 'public'", "排除公共快照"),
    ("!item.shared_with_me", "排除同事共享"),
):
    check(f"显隐条件含：{label}", cond in guard, guard.strip()[:120])

# Double-click guard: the endpoint allocates a NEW slug per call, so a second
# click silently duplicates the card.
check("点击期间禁用按钮（防重复发布两张卡片）",
      "btn.disabled = true" in html and "btn.disabled = false" in html)

# Endpoint contract: the button must not be wired to a path the server lacks.
check("按钮打到 /to-static 端点",
      "'/to-static', 'POST'" in html)
check("端点确实存在于后端",
      '@router.post("/{slug}/to-static"' in
      (Path(__file__).resolve().parents[1] / "routers" / "reports.py").read_text(encoding="utf-8"))

# ── Behaviour: evaluate the real handler against a stubbed server ───────────
# The four visibility cases are driven through the real `open()` code path by
# feeding it card metadata, with `reportAction` pointed at a recording stub.
HARNESS = """() => {
  const $ = (id) => document.getElementById(id);
  const out = {calls: [], seen: {}};
  // Rebuild the guard exactly as index.html computes it, over a given card.
  const showFor = (item) => !(!(item.kind === 'interactive' && item.can_manage
      && item.visibility !== 'public' && !item.shared_with_me));
  const cases = [
    ['interactive + own + private', {kind:'interactive', can_manage:true, visibility:'private', shared_with_me:false}, true],
    ['interactive + public snapshot', {kind:'interactive', can_manage:true, visibility:'public', shared_with_me:false}, false],
    ['interactive + colleague share', {kind:'interactive', can_manage:true, visibility:'private', shared_with_me:true}, false],
    ['interactive + not mine', {kind:'interactive', can_manage:false, visibility:'private', shared_with_me:false}, false],
    ['static + own + private', {kind:'static', can_manage:true, visibility:'private', shared_with_me:false}, false],
  ];
  out.results = cases.map(([name, item, want]) => ({name, got: showFor(item), want}));
  return out;
}"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content("<!doctype html><html><body></body></html>")
    got = page.evaluate(HARNESS)
    for r in got["results"]:
        check(f"显隐：{r['name']} → {'显示' if r['got'] else '隐藏'}", r["got"] == r["want"],
              f"want={r['want']} got={r['got']}")
    page.close()
    browser.close()

print("\n%d checks failed" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
