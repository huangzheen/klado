"""嵌入 Workspace 时隐藏 deck 自己的底条，独立打开时保留；顶部栏接管「全部页面」。

⚠️ 2026-09-29 用户要求：「保留顶部的，移掉底部的」。顶部栏（`index.html` 的 `#rpt-viewer-bar`）
与 deck 运行时自己的底部黑条（`.deck-bar`，含 全部页面 / EN·中文 / ‹ › / 页码）同时显示，
是同一组控件出现两次。

**不能无条件删掉那条 bar**：它在报告「独立打开」时是唯一控件 ——
`/r/{slug}` 直开、`/s/{token}` 分享链接（全站唯一免登录入口，见 format-report-html §2）、
以及下载后的自包含 HTML，这三种场景下顶部栏根本不存在。

所以判据是「我是否被嵌在 iframe 里」：`window.parent !== window` ⇒ 隐藏。
⚠️ 刻意**不**判「宿主是不是本应用」——报告被贴进任何别的 iframe 时也会失去控件，
那属于误伤。独立 tab / 下载文件 / 分享链接都是顶层文档，一律保留。

「全部页面」这个功能原本只长在底条上，所以顶部栏补了一个 `#rpt-modebtn`；
外壳已有 postMessage 通道（`{action:'mode'}`），deck 侧 `setMode` 一直在收（report-deck.js:377）。

验证方式与 `verify_deck_fit_ui.py` 一致：用**服务端同一个** `_inline_deck_runtime()` 注入，
再用真实的父子 iframe 关系分别测「嵌入」与「独立」两种加载方式。

    .venv312/bin/python api/tests/verify_deck_bar_ui.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.reports import _inject_base_tag, _inline_deck_runtime   # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


# A bilingual 2-page deck, like the one this was reported on.
AUTHORED = """<!doctype html><html><head><title>bar</title></head><body>
<div class="deck" data-deck-title="Bar test">
  <section class="slide" data-lang="en"><h1>One</h1></section>
  <section class="slide" data-lang="en"><h1>Two</h1></section>
  <section class="slide" data-lang="zh"><h1>一</h1></section>
  <section class="slide" data-lang="zh"><h1>二</h1></section>
</div>
</body></html>"""

DOC = _inline_deck_runtime(_inject_base_tag(AUTHORED, "/")).replace('<base href="/">', "<base href='./'>")
FIXTURE = Path("/tmp/deck-bar-fixture.html")
FIXTURE.write_text(DOC, encoding="utf-8")

# A parent page that embeds the fixture exactly the way #rpt-frame does, and forwards
# keys the way the Workspace viewer does — so the deck is genuinely in a child frame.
HOST = """<!doctype html><html><head><title>host</title></head><body>
<div id="bar"><button id="mode">全部页面</button><span id="page"></span></div>
<iframe id="f" src="deck-bar-fixture.html" style="width:1200px;height:700px;border:0"></iframe>
<script>
  const f = document.getElementById('f');
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (!d || d.type !== 'report-deck:state') return;
    document.getElementById('page').textContent =
      d.mode === 'all' ? '全部页面' : (d.index + 1) + ' / ' + d.count;
    // The host button mirrors what report-deck.js's own bar button used to do.
    document.getElementById('mode').textContent = d.mode === 'all' ? '单页浏览' : '全部页面';
  });
  document.getElementById('mode').addEventListener('click', () => {
    const cur = document.getElementById('mode').textContent === '单页浏览' ? 'paged' : 'all';
    f.contentWindow.postMessage({ type: 'report-deck', action: 'mode', mode: cur }, '*');
  });
</script>
</body></html>"""
Path("/tmp/deck-bar-host.html").write_text(HOST, encoding="utf-8")

PROBE = """() => {
  const bar = document.querySelector('.deck-bar');
  const deck = document.querySelector('.deck');
    const slides = [...document.querySelectorAll('.slide')];
  return {
    barInDom: !!bar,
    barVisible: bar ? getComputedStyle(bar).display !== 'none' && !!bar.offsetParent : false,
    barButtons: bar ? [...bar.querySelectorAll('button')].map((b) => b.textContent) : [],
    scale: +(getComputedStyle(deck).getPropertyValue('--deck-scale').trim() || 0),
    // Count pages of the ACTIVE language only. A bilingual deck holds 4 <section>
    // elements but shows 2 at a time, and the other language's pages are hidden by
    // `display:none` (the language filter), not by .deck-lang-off alone.
    visibleSlides: [...document.querySelectorAll('.slide')].filter(
      (s) => getComputedStyle(s).display !== 'none').length,
    current: document.querySelectorAll('.slide.is-current').length,
    api: !!(window.reportDeck && window.reportDeck.next),
  };
}"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # ── 1. Standalone (file:// top level): the bar MUST stay ──────────────────
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(FIXTURE.as_uri(), wait_until="load")
    page.wait_for_timeout(400)
    solo = page.evaluate(PROBE)
    check("独立打开：底条仍在", solo["barInDom"] and solo["barVisible"], solo)
    check("独立打开：底条控件齐全（全部页面/EN/中文/‹/›）",
          len(solo["barButtons"]) >= 4, solo["barButtons"])
    # In paged mode exactly ONE page of the active language is on screen (the deck
    # stacks them and shows the current one), so the language switch is proven by
    # the count of pages the deck itself knows about, not by what is displayed.
    check("独立打开：双语各 2 页（reportDeck.count 按语言过滤）",
          solo["api"] and page.evaluate("() => window.reportDeck.langs.join(',')") == "en,zh",
          page.evaluate("() => window.reportDeck.langs"))
    check("独立打开：当前语言只显示 1 页（分页态）", solo["visibleSlides"] == 1, solo["visibleSlides"])
    check("独立打开：分页可用（window.reportDeck 就绪）", solo["api"], solo)

    # Independent open is the ONLY place a share-link recipient can switch language,
    # so the language buttons must actually be there for a bilingual deck.
    check("独立打开：底条上有语言按钮（分享链接场景唯一入口）",
          any("EN" in t or "中文" in t for t in solo["barButtons"]), solo["barButtons"])
    # Actually press it: the share-link recipient has no other way to reach 中文.
    page.click(".deck-bar [data-deck-lang='zh']")
    page.wait_for_timeout(300)
    after_zh = page.evaluate(
        "() => ({lang: window.reportDeck.lang,"
        " shown: [...document.querySelectorAll('.slide')]"
        "   .filter((s) => getComputedStyle(s).display !== 'none')"
        "   .map((s) => s.querySelector('h1').textContent)})")
    check("独立打开：点「中文」真的切到了中文页",
          after_zh["lang"] == "zh" and after_zh["shown"] == ["一"], after_zh)
    page.close()

    # ── 2. Embedded in the Workspace-style host: the bar MUST be gone ─────────
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(Path("/tmp/deck-bar-host.html").as_uri(), wait_until="load")
    page.wait_for_timeout(600)
    frame = page.frame(name=None)
    child = [f for f in page.frames if f != page.main_frame][0]
    inside = child.evaluate(PROBE)
    check("嵌入预览：底条已移除（不在 DOM 里）", not inside["barInDom"], inside)
    check("嵌入预览：底条不可见", not inside["barVisible"], inside)
    check("嵌入预览：deck 本身仍正常（API 就绪、可分页）", inside["api"], inside)

    # The page must still fit after the 60px BAR_SPACE reserve is given back.
    check("嵌入预览：缩放有效（未因去掉留白而算坏）", inside["scale"] > 0.05, inside["scale"])
    check("嵌入预览：分页态只有一页 is-current", inside["current"] == 1, inside["current"])

    # ── 3. The host's 全部页面 button still reaches the feature ──────────────
    page.click("#mode")
    page.wait_for_timeout(500)
    after = child.evaluate(PROBE)
    check("顶部栏「全部页面」按钮：切到全部页面后两页同时可见",
          after["visibleSlides"] == 2, after)
    label = page.inner_text("#mode")
    check("顶部栏按钮：文案随之变成「单页浏览」", label == "单页浏览", label)
    pageinfo = page.inner_text("#page")
    check("顶部栏页码：反映全部页面模式", pageinfo == "全部页面", pageinfo)

    # And back again — the toggle must not be one-way.
    page.click("#mode")
    page.wait_for_timeout(500)
    back = child.evaluate(PROBE)
    check("顶部栏按钮：再点一次回到单页浏览（只剩一页在显示）",
          back["visibleSlides"] == 1 and back["current"] == 1
          and page.inner_text("#mode") == "全部页面", back)

    # ── 4. Keyboard/programmatic paging still works while embedded ───────────
    # ⚠️ `current` alone cannot prove this: it is 1 on the first page too. The
    # page NUMBER reported back to the host is what actually changed.
    child.evaluate("() => window.reportDeck.goTo(1)")
    child.wait_for_timeout(300)
    idx = child.evaluate("() => window.reportDeck.index")
    check("嵌入预览：翻到第二页（index 变成 1，不是 0）", idx == 1, idx)
    check("嵌入预览：翻页后顶部页码同步为 2 / 2",
          page.inner_text("#page") == "2 / 2", page.inner_text("#page"))
    page.close()
    browser.close()

print("\n%d checks failed" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
