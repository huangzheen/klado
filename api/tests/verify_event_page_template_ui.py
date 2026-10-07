"""The event page template: one fixed layout, whatever the author wrote.

The user's complaint (2026-09-29): "这个排版还是不太好… 比如 'Q3 channel review: …' 这个放的位置就
很不好。造成其它卡片都没对齐" — a free-form page lets the length of one piece of text move
everything else.

So the template pins every box, and this suite measures that promise instead of trusting it:

* the three side cards are equal thirds of the side column, on every page;
* a **4-line description and a 1-line description put the cards in the same place**;
* a title that overflows its 3-line box truncates instead of pushing the columns down;
* the attachments are real **links** — built by `services/ai/calendar_page.py`, relative to
  the mount point (a leading slash would land on the platform gateway), opening in a new tab
  so the event page stays put behind them;
* the deadline box says something even when the event has no deadline.

    .venv312/bin/python api/tests/verify_event_page_template_ui.py
"""
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ai import calendar_page                                     # noqa: E402
from services.ai.calendar_events import Attachment, CalendarEvent         # noqa: E402
from routers.reports import _inline_deck_runtime                          # noqa: E402

PORT = 8821
FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


EVENT = CalendarEvent(
    id=1, slug="q3-channel-review", title="Q3 渠道复盘会", kind="review", category="渠道",
    start_date="2026-10-12", end_date="2026-10-16", deadline="2026-10-15",
    partners=["zhen.huang@example.com", "li.ming@example.com"],
    attachments=[Attachment("knowledge", "channel-caliber-notes", "渠道口径速查"),
                 Attachment("report", "q3-deck", "Q3 Deck"),
                 Attachment("file", "datacenter-raw/calendar-attachments/quotes.pdf", "报价表.pdf")])


def page_html(description: str, title: str = "Q3 渠道复盘会") -> str:
    """The template skeleton — exactly what `format-calendar-event` §3 tells an author to write.

    ⚠️ `.ev-desc` belongs INSIDE `.ev-main`. This helper used to put it beside `.ev-cols`,
    which nobody writes and which the stylesheet then flung to the bottom of the slide —
    the geometry checks still passed, so the divergence went unnoticed. The template's
    stylesheet moves the description to the right column from there (see `event-page.css`).
    """
    return f"""<!doctype html><html><head><title>event</title></head><body>
<div class="deck">
  <section class="slide ev-page" data-lang="en">
    <div class="ev-head">
      <span class="ev-kicker" data-ev="kicker"></span>
      <span class="ev-when" data-ev="schedule"></span>
    </div>
    <h1 class="ev-title" data-ev="title">{title}</h1>
    <div class="ev-cols">
      <div class="ev-main">
        <p class="ev-desc">{description}</p>
        <h2 class="ev-block-title">To-do</h2>
        <ul class="ev-todo">
          <li>Align sell-in / sell-out / inventory for 6 channels</li>
          <li>Attribute any YoY anomaly channel</li>
          <li>Assign action items to owners</li>
        </ul>
      </div>
      <aside class="ev-side">
        <div class="ev-card"><h2 class="ev-block-title">Partners</h2>
          <div class="ev-card-body" data-ev="partners"></div></div>
        <div class="ev-card"><h2 class="ev-block-title">Deadline</h2>
          <div class="ev-card-body" data-ev="deadline"></div></div>
        <div class="ev-card"><h2 class="ev-block-title">Attachments</h2>
          <div class="ev-card-body" data-ev="attachments"></div></div>
      </aside>
    </div>
  </section>
</div>
</body></html>"""


SHORT = "Q3 渠道复盘：各渠道进销存、同比环比、异常归因与行动清单。"
LONG = ("Q3 渠道复盘：把 6 个渠道的 sell-in / sell-out / inventory 三条口径对齐，"
        "逐渠道看同比与环比，对异常做归因，并把行动项落到负责人与时间点；"
        "结论用于下季度的铺货与价格带调整，因此需要渠道、计划、供应链三方在会前各自核过数。")

current = {"html": ""}


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/page"):
            payload = current["html"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


PROBE = """() => {
  const boxes = (sel) => Array.from(document.querySelectorAll(sel)).map((el) => {
    const r = el.getBoundingClientRect();
    return {cls: String(el.className || '').slice(0, 18), top: Math.round(r.top),
            left: Math.round(r.left), h: Math.round(r.height), w: Math.round(r.width),
            bottom: Math.round(r.bottom), border: getComputedStyle(el).borderTopWidth,
            // ⚠️ A box measure cannot see clipping (`overflow: hidden` swallows it); only
            // `scrollHeight > clientHeight` distinguishes "fits" from "was cut off inside".
            body_scroll: el.querySelector('.ev-card-body')
                         ? el.querySelector('.ev-card-body').scrollHeight : 0,
            body_client: el.querySelector('.ev-card-body')
                         ? el.querySelector('.ev-card-body').clientHeight : 0};
  });
  const links = Array.from(document.querySelectorAll('.ev-link')).map((a) => ({
    href: a.getAttribute('href'), target: a.getAttribute('target'),
    cls: a.className, text: a.textContent.trim(),
    bottom: Math.round(a.getBoundingClientRect().bottom)}));
  const cards = Array.from(document.querySelectorAll('.ev-card'));
  return {cards: boxes('.ev-card'), links: links,
          desc: boxes('.ev-desc')[0], cols: boxes('.ev-cols')[0], side: boxes('.ev-side')[0],
          deadline: (document.querySelector('[data-ev="deadline"]') || {}).textContent,
          partners: Array.from(document.querySelectorAll('.ev-person')).map((p) => p.textContent.trim()),
          empty_notes: Array.from(document.querySelectorAll('.ev-empty')).map((e) => e.textContent.trim()),
          // ⚠️ Since 2026-09-30 the title lives on the HEAD line and never wraps, so "too long"
          // shows as a horizontal overflow (ellipsis), not as a box whose content spills out.
          title: (() => { const t = document.querySelector('.ev-title');
            if (!t) return null; const r = t.getBoundingClientRect();
            return {h: Math.round(r.height), top: Math.round(r.top), left: Math.round(r.left),
                    font: getComputedStyle(t).fontSize, nowrap: getComputedStyle(t).whiteSpace,
                    ellipsis: t.scrollWidth > t.clientWidth}; })(),
          head: (() => { const h = document.querySelector('.ev-head'); if (!h) return null;
            const r = h.getBoundingClientRect();
            return {h: Math.round(r.height), top: Math.round(r.top), bottom: Math.round(r.bottom)}; })(),
          // The kicker slot is retired: the server must not fill it AND the stylesheet must
          // hide it (old pages still carry `KIND · category` in that span).
          kicker: (() => { const k = document.querySelector('.ev-kicker'); if (!k) return null;
            return {display: getComputedStyle(k).display, text: k.textContent.trim()}; })(),
          // ⚠️ A box measure (`boxes('.ev-desc')`) cannot see clipping: `overflow: hidden`
          // swallows it. Only `scrollHeight > clientHeight` says the text does not fit —
          // and it did not, for every 4-line description, until 2026-09-30.
          descClip: (() => { const d = document.querySelector('.ev-desc'); if (!d) return null;
            return {client: d.clientHeight, scroll: d.scrollHeight,
                    clipped: d.scrollHeight > d.clientHeight + 1}; })(),
          // The event page IS the document: /e/<slug> is what the detail viewer's iframe
          // loads, so the SPA shell must never be wrapped around it. Measured from the DOM
          // rather than from the source, because a stylesheet could bring the chrome back.
          shell: ['.nav', '.sidebar', '.resize-handle-l', '.workspace-bar', '.rpt-toolbar',
                  '#page-home', '#page-calendar', '#page-knowledge', '#inbox-split']
                   .filter((s) => document.querySelector(s)),
          deck: (() => { const d = document.querySelector('.deck');
            return d ? Math.round(d.getBoundingClientRect().width) : -1; })(),
          // The canvas width, so the suite can prove it is measuring canvas px and not scaled
          // screen px (the deck runtime scales the whole 1920×1080 slide to fit the viewport).
          canvasW: (() => { const p = document.querySelector('.ev-page');
            return p ? Math.round(p.getBoundingClientRect().width) : -1; })(),
          canvas_bottom: (() => { const p = document.querySelector('.ev-page');
            return p ? Math.round(p.getBoundingClientRect().bottom) : -1; })(),
          // The deck's own page-number bar. Content must stay ABOVE it — running under it is
          // the failure mode that a "inside 1080" check alone cannot see.
          foot_top: (() => { const f = document.querySelector('.deck-foot');
            return f ? Math.round(f.getBoundingClientRect().top) : null; })(),
          partners_drawn: document.querySelectorAll('.ev-person').length,
          links_drawn: document.querySelectorAll('.ev-link').length,
          winW: window.innerWidth};
}"""


def build(description: str, title: str = "Q3 渠道复盘会", event=None) -> str:
    """The page exactly as `/e/<slug>` serves it — template AND deck runtime.

    `event` lets a later case (the crowded page) render the SAME template with a much longer
    list; the default stays the two-partner / three-attachment fixture the geometry is
    pinned against.

    ⚠️ The runtime used to be left out, which meant there was no `.slide` at all: no
    1920×1080, no 76/92/100 padding. Every measurement was then taken in a bare
    content-sized box, so the template's px numbers could not be checked against anything —
    the "cards stay inside the 16:9 canvas" assertion only passed by coincidence, and the
    description's `50%` column could not be told from any other height (2026-09-30: the
    column measured 1059px without the runtime and 800px with it).
    """
    return _inline_deck_runtime(
        calendar_page.render(page_html(description, title),
                             event if event is not None else EVENT,
                             base_href="/"))


socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(Handler, directory="/tmp"))
threading.Thread(target=httpd.serve_forever, daemon=True).start()

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        shots = {}
        results = {}
        for label, html in (("short", build(SHORT)), ("long", build(LONG)),
                            ("long_title", build(LONG,
                                  "Q3 渠道复盘：六个渠道的进销存口径对齐、同比环比异常归因、下季度铺货建议与"
                                  "价格带调整方案，以及会后两周内的行动项跟踪与责任人确认"
                                  "（渠道 × 计划 × 供应链三方会前各自核数）"))):
            current["html"] = html
            page = browser.new_page(viewport={"width": 1920, "height": 1200})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"http://127.0.0.1:{PORT}/page", wait_until="load")
            # ⚠️ Pin the canvas at 1:1 before measuring. The deck runtime SCALES the whole
            # 1920×1080 slide to fit the viewport (measured 0.977 at 1920×1200, 1.07 at
            # 2100×1400), so every rect would otherwise be a screen px and the template's own
            # numbers — 1080, 400, 116 — could not be asserted at all. No resize happens during
            # a test, so the runtime does not recompute this.
            page.evaluate("() => { const d = document.querySelector('.deck');"
                          " if (d) d.style.setProperty('--deck-scale', '1'); }")
            page.wait_for_timeout(500)
            page.evaluate("() => { const d = document.querySelector('.deck');"
                          " if (d) d.style.setProperty('--deck-scale', '1'); }")
            page.wait_for_timeout(150)
            results[label] = page.evaluate(PROBE)
            results[label]["errors"] = errors
            shots[label] = page.screenshot()
            page.close()

        short, long, long_title = results["short"], results["long"], results["long_title"]

        # ── the template is applied at all ───────────────────────────────────────
        check("服务端填了排期/截止/参与人/附件四格",
              bool(short["cards"]) and short["deadline"] and short["partners"]
              and len(short["links"]) == 3, short["deadline"])
        check("容器是模版（三块事实条目）", len(short["cards"]) == 3, short["cards"])
        check("★ 事件文档是独立页：DOM 里不含任何应用外壳（导航 / 侧栏 / 各页容器 / 页内工具条）",
              short["shell"] == [], short["shell"])
        check("事件文档铺满视口（不是被塞进应用壳里的一小块）",
              short["deck"] > 0 and short["deck"] >= short["winW"] - 20,
              (short["deck"], short["winW"]))
        # ⚠️ Everything below is compared against canvas px (1080, 400, 116…). That only holds
        # while the deck renders at 1:1 — a narrower/taller viewport scales the canvas and every
        # number silently becomes a screen px. The viewport above is picked for exactly this.
        check("画布按 1:1 渲染（下面的几何值就是画布 px）",
              all(abs(results[k]["canvasW"] - 1920) <= 1 for k in ("short", "long", "long_title")),
              [results[k]["canvasW"] for k in ("short", "long", "long_title")])
        check("没有 JS 报错", short["errors"] == [], short["errors"][:2])

        # ── the promise: fixed positions ─────────────────────────────────────────
        tops_short = [c["top"] for c in short["cards"]]
        tops_long = [c["top"] for c in long["cards"]]
        check("★ 描述从 1 行变 4 行，卡片位置一格都不动",
              tops_short == tops_long and short["cols"]["top"] == long["cols"]["top"],
              {"short": tops_short, "long": tops_long,
               "cols": [short["cols"]["top"], long["cols"]["top"]]})
        check("★ 标题长到放不下也只在自己那行省略（卡片位置一格都不动）",
              long_title["cards"] and [c["top"] for c in long_title["cards"]] == tops_short
              and long_title["title"] and long_title["title"]["ellipsis"],
              {"cards": [c["top"] for c in long_title["cards"]], "title": long_title["title"]})
        # It sits ON the head line: one line, inside the band, and nothing below moves for it.
        check("★ 标题只占页眉那一行（不换行、不压到下面的内容）",
              short["title"] and short["head"] and short["title"]["nowrap"] == "nowrap"
              and short["title"]["h"] <= short["head"]["h"]
              and short["title"]["top"] >= short["head"]["top"] - 2
              and short["cols"]["top"] >= short["head"]["bottom"] - 2,
              {"title": short["title"], "head": short["head"], "cols_top": short["cols"]["top"]})
        # The bug this suite used to miss: the box was 3 lines, `overflow: hidden` ate the
        # 4th, and the position checks still passed.
        check("★ 项目描述完整显示 —— 不在框里被静默裁掉",
              short["descClip"] and long["descClip"]
              and not short["descClip"]["clipped"] and not long["descClip"]["clipped"],
              {"short": short["descClip"], "long": long["descClip"]})
        check("页眉行放的是事件标题，kicker 已取消（服务端不填、样式表也不渲染）",
              short["kicker"] and short["kicker"]["display"] == "none"
              and short["kicker"]["text"] == "", short["kicker"])
        # ⚠️ Rewritten 2026-09-30 with the second design pass. The three "cards" used to be
        # filled rounded rectangles stretched to equal heights so they filled the column
        # exactly (`spare == 0`). The user's objection to that look — "AI 味太重，与我项目的
        # 设计也有一些差异" — was precisely the three-equal-widgets rhythm, and they are now
        # content-sized spec entries separated by hairline rules. So the contract to pin is
        # what actually makes them read as one list: a 1px rule on top of each, stacked in
        # order with no overlap, and the last one inside the canvas.
        check("★ 三块事实条目：各带一条发丝分隔线、依次下落、互不重叠",
              len(short["cards"]) == 3
              and all(c["border"] == "1px" for c in short["cards"])
              and all(b["top"] >= a["bottom"] - 1
                      for a, b in zip(short["cards"], short["cards"][1:])),
              {"borders": [c["border"] for c in short["cards"]],
               "tops": [c["top"] for c in short["cards"]],
               "bottoms": [c["bottom"] for c in short["cards"]]})
        # ⚠️ "Content-sized" is the look (a short fact is a short row), but it must still be
        # CAPPED: with plain `min-content` rows and nothing capping them, 12 partners +
        # 8 attachments put the last entry 311px past the bottom of the canvas — measured
        # 2026-09-30 while finishing the redesign. See `crowded` below for the regression.
        check("事实条目按内容取高，但都被自己的框限制住（长名单不会撑破右栏）",
              len({c["h"] for c in short["cards"]}) > 1
              and all(40 <= c["h"] <= 220 for c in short["cards"]),
              {"heights": [c["h"] for c in short["cards"]]})
        # ⚠️ The description's box and the band reserved for it are TWO separate numbers in the
        # stylesheet (`.ev-desc { height: 210px }` and `.ev-cols:has(.ev-desc) > .ev-side
        # { padding-top: 234px }` = 210 + the 24px row gap). If they drift, the first fact
        # entry slides under the description — so pin the RELATION between the two boxes, not
        # the idea that the right column splits 50/50 (it no longer does).
        gap = short["cards"][0]["top"] - short["desc"]["bottom"]
        check("★ 描述下面的留白正好是那一行间距（预留给描述的高度没有漂移）",
              20 <= gap <= 28 and abs(gap - 24) <= 1,
              {"desc": short["desc"], "card_top": short["cards"][0]["top"], "gap": gap})
        check("★ 描述块在右栏、三块事实之上（左栏整栏留给 to-do）",
              short["desc"] and short["desc"]["left"] >= short["side"]["left"] - 2
              and short["desc"]["bottom"] <= short["cards"][0]["top"],
              {"desc": short["desc"], "side_left": short["side"]["left"],
               "card_top": short["cards"][0]["top"]})
        check("卡片在 16:9 画布内（不越界）",
              all(c["bottom"] <= 1080 for c in short["cards"]) and short["cards"][0]["top"] > 0,
              short["cards"][-1])

        # ── worst case: a page that lists a lot ──────────────────────────────────
        # ⚠️ The template's promise (§3.3) is "每格都是固定框，写多了只会在框里被裁掉".
        # The 2026-09-30 redesign broke it: content-sized entries with no cap let a long
        # partner/attachment list push the last entry past the canvas and under the deck's
        # page-number bar (measured 311px past). This case exists so the bound cannot be
        # dropped again by a "make it more editorial" pass.
        crowded_event = CalendarEvent(
            id=3, slug="crowded", title="很挤的一页", kind="review",
            start_date="2026-10-12", end_date="2026-10-16", deadline="2026-10-15",
            partners=["p%02d@example.com" % i for i in range(12)],
            attachments=[Attachment("report", "deck-%d" % i, "Q3 渠道口径对齐与同比异常归因说明文档 %d" % i)
                         for i in range(8)])
        current["html"] = build(LONG, event=crowded_event)
        page = browser.new_page(viewport={"width": 1920, "height": 1200})
        page.goto(f"http://127.0.0.1:{PORT}/page", wait_until="load")
        page.evaluate("() => { const d = document.querySelector('.deck');"
                      " if (d) d.style.setProperty('--deck-scale', '1'); }")
        page.wait_for_timeout(400)
        page.evaluate("() => { const d = document.querySelector('.deck');"
                      " if (d) d.style.setProperty('--deck-scale', '1'); }")
        page.wait_for_timeout(150)
        crowded = page.evaluate(PROBE)
        page.close()

        check("★ 长名单不会让右栏越过画布或压到页脚线（每格仍是固定框）",
              crowded["cards"] and crowded["foot_top"] is not None
              and crowded["cards"][-1]["bottom"] < crowded["foot_top"]
              and crowded["cards"][-1]["bottom"] <= crowded["canvas_bottom"],
              {"last_bottom": crowded["cards"][-1]["bottom"],
               "foot_top": crowded["foot_top"], "canvas_bottom": crowded["canvas_bottom"],
               "heights": [c["h"] for c in crowded["cards"]]})
        check("★ 装不下的内容是在自己那一格里被裁掉，而不是把格子撑大",
              all(c["h"] <= 220 for c in crowded["cards"])
              and any(c["body_scroll"] > c["body_client"] + 1 for c in crowded["cards"]),
              [(c["h"], c["body_scroll"], c["body_client"]) for c in crowded["cards"]])
        check("被裁的是显示，不是内容：12 个人 / 8 个附件都还在 DOM 里",
              crowded["partners_drawn"] == 12 and crowded["links_drawn"] == 8,
              {"partners": crowded["partners_drawn"], "links": crowded["links_drawn"]})
        check("同伴的框没有被挤到零高（三块事实都还在页面上）",
              all(c["h"] >= 40 for c in crowded["cards"]) and len(crowded["cards"]) == 3,
              [c["h"] for c in crowded["cards"]])

        # ── the attachments are links, and the link shapes are ours ──────────────
        hrefs = [link["href"] for link in short["links"]]
        check("★ 三个附件都是可点链接，且 href 由服务端按类型生成",
              hrefs == ["?kb=channel-caliber-notes", "r/q3-deck",
                        "api/storage/serve?path=datacenter-raw%2Fcalendar-attachments%2Fquotes.pdf&download=1"],
              hrefs)
        check("★ 链接一律相对地址（带前导斜杠会落到平台网关根）",
              all(not h.startswith("/") and "://" not in h for h in hrefs), hrefs)
        check("链接在新标签打开（不把事件页顶掉）",
              all(link["target"] == "_blank" for link in short["links"]), short["links"][0])
        check("链接带类型标注（Wiki / Report / File）",
              [link["text"] for link in short["links"]] ==
              ["渠道口径速查Wiki", "Q3 DeckReport", "报价表.pdfFile"],
              [link["text"] for link in short["links"]])

        # ── empty values still say something ─────────────────────────────────────
        empty_event = CalendarEvent(id=2, slug="e", title="无截止的事件", kind="event")
        current["html"] = calendar_page.render(page_html(SHORT), empty_event)
        page = browser.new_page(viewport={"width": 1920, "height": 1200})
        page.goto(f"http://127.0.0.1:{PORT}/page", wait_until="load")
        # ⚠️ Same 1:1 pin as the panels above — see the note there.
        page.evaluate("() => { const d = document.querySelector('.deck');"
                      " if (d) d.style.setProperty('--deck-scale', '1'); }")
        page.wait_for_timeout(400)
        page.evaluate("() => { const d = document.querySelector('.deck');"
                      " if (d) d.style.setProperty('--deck-scale', '1'); }")
        page.wait_for_timeout(150)
        blank = page.evaluate(PROBE)
        check("没有截止/参与人/附件时三格写「—」而不是留空块",
              blank["deadline"].strip() == "—" and len(blank["empty_notes"]) == 3, blank["empty_notes"])
        page.close()
        browser.close()

        Path("/tmp/event-template-preview.html").write_text(current["html"], encoding="utf-8")
        Path("/tmp/event-template-short.png").write_bytes(shots["short"])
        Path("/tmp/event-template-long.png").write_bytes(shots["long"])
        print("\nwrote /tmp/event-template-{short,long}.png")
finally:
    httpd.shutdown()

print("\n%d checks failed" % len(FAILURES))
sys.exit(1 if FAILURES else 0)