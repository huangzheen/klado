"""The deck's scale-to-fit must measure the VIEWER's box, not the design canvas.

⚠️ This is the bug a user reported on 2026-09-29 as "双击事件出来的窗口…layout 有问题，无法显示全部的":
a real event page carried `.deck { width: 1920px; height: 1080px }` (the design-canvas numbers,
copied into the author's CSS). The injected deck CSS never declared a size for `.deck`, so the
author's rule won, the runtime measured **1920x1080 instead of the iframe**, computed scale .90
for every viewer size, and `overflow: hidden` cropped ~13% off the right and ~20% off the bottom
of every page. In the event overlay that is exactly "the page is not all there".

Why the other suites could not see it:

* the Calendar UI test served a hand-written stand-in for `/e/<slug>` — a plain page with no
  deck runtime, so the scale-to-fit code never ran;
* the report suites never render a deck in a browser either (they check bytes and contracts).

So this script renders a **real** deck — inlined by the same server function `/r/` and `/e/`
use — in a browser, with an author who writes a fixed `.deck` size, and measures the result.

    .venv312/bin/python api/tests/verify_deck_fit_ui.py
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


# An authoring page that does what the real one did: give `.deck` the design canvas size, and
# put its <style> in the body (so it comes AFTER the injected deck CSS — the worst case).
AUTHORED = """<!doctype html><html><head><title>fit</title></head><body>
<style>
  .deck { width: 1920px; height: 1080px; }
  .slide { background: #fff; }
</style>
<div class="deck">
  <section class="slide"><h1>Page one</h1><p>content</p></section>
  <section class="slide"><h1>Page two</h1></section>
</div>
</body></html>"""

DOC = _inline_deck_runtime(_inject_base_tag(AUTHORED, "/")).replace('<base href="/">', "<base href='./'>")
FIXTURE = Path("/tmp/deck-fit-fixture.html")
FIXTURE.write_text(DOC, encoding="utf-8")

PROBE = """() => {
  const deck = document.querySelector('.deck');
  const slide = document.querySelector('.slide.is-current') || document.querySelector('.slide');
  const root = document.documentElement;
  const s = slide.getBoundingClientRect();
  let clipped = 0;
  slide.querySelectorAll('*').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && (r.bottom > s.bottom + 1 || r.right > s.right + 1)) clipped += 1;
  });
  return {view: {w: root.clientWidth, h: root.clientHeight},
          deck: {w: deck.clientWidth, h: deck.clientHeight},
          scale: +(getComputedStyle(deck).getPropertyValue('--deck-scale').trim() || 1),
          slide: {w: Math.round(s.width), h: Math.round(s.height)},
          rect: {l: Math.round(s.left), t: Math.round(s.top), r: Math.round(s.right), b: Math.round(s.bottom)},
          // Position, not just size: a page centred on the wrong box has the right size and
          // still hangs off the viewer.
          fits: s.left >= -1 && s.top >= -1
                && s.right <= root.clientWidth + 1 && s.bottom <= root.clientHeight + 1,
          clipped: clipped};
}"""

SIZES = ((1452, 779), (1000, 600), (820, 1100))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width, height in SIZES:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(FIXTURE.as_uri(), wait_until="load")
        page.wait_for_timeout(300)
        got = page.evaluate(PROBE)
        label = f"{width}x{height}"
        check(f"{label} 容器填满视口（作者的固定尺寸不能赢）",
              got["deck"]["w"] == got["view"]["w"] and got["deck"]["h"] == got["view"]["h"], got)
        check(f"{label} 页面完整落在视口内（位置 + 尺寸都不越界）",
              got["fits"] and got["clipped"] == 0, got)
        # A page that is cropped is exactly a page whose box is bigger than the viewer.
        check(f"{label} 缩放是按视口算出来的（不是 1920x1080 那套数）",
              abs(got["scale"] - 0.9037) > 0.02, got["scale"])

        # The overlay's edit panel narrows the viewer: the deck must RE-FIT, not stay cropped.
        page.set_viewport_size({"width": max(400, width - 360), "height": height})
        page.wait_for_timeout(300)
        after = page.evaluate(PROBE)
        check(f"{label} 变窄后重新适配（面板打开那一档）",
              after["fits"] and after["clipped"] == 0
              and after["slide"]["w"] < got["slide"]["w"], after)
        page.close()
    browser.close()

print("\n%d checks failed" % len(FAILURES))
sys.exit(1 if FAILURES else 0)