"""Reader notes, in a real browser, served the way the server serves them.

What the unit tests cannot see, and this script exists for:

* the note lands where the reader clicked — measured against the ANCHOR BOX, not
  against the document, because a 1920×1080 deck is displayed at 0.42 scale and a
  pixel coordinate would be a different sentence on a different monitor;
* the bubble is legible in a deck. The layer is a fixed overlay over the visible
  slide rather than a child of it; placed inside, it would inherit the deck's
  `transform: scale()` and shrink with it (verified here by comparing the rendered
  font size against the slide's scale factor);
* the toggle starts visible, and a document you hid the notes on stays hidden;
* a guest on a `/s/{token}` link SEES the notes but cannot add one, and their
  right-click menu is left alone;
* every request carries the mount point (`/…`). A request to
  `/api/annotations` would reach the platform gateway, which answers HTTP 200
  with 49 bytes of `{"code":1002,…}` — a "success" that renders as raw JSON.

The document is produced by the real `services.annotations.inline_runtime`, and
the API is a real HTTP server on 127.0.0.1 (not a route mock), so a wrong URL
fails here instead of being papered over.

    .venv312/bin/python api/tests/verify_annotation_ui.py
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import annotations   # noqa: E402
from routers.reports import _inject_base_tag, _inline_deck_runtime   # noqa: E402
from core.config import settings   # noqa: E402

MOUNT = "/klado/"        # a real sub-path mount: the note requests must carry it
PORT = 18891
FAILURES = []
POSTS = []
GETS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


# ── fixtures ─────────────────────────────────────────────────────────────────
#
# Built exactly the way `/r/<slug>` builds one: the same mount point, the same
# `<base>` injection, the same deck runtime, then the note layer on top. Anything
# less would be testing a document the server never serves.

settings.APP_BASE_PATH = MOUNT.strip("/")

DECK = """<!doctype html><html><head><title>deck</title><style>
  body { margin:0; font-family: system-ui; }
  .slide { background:#fff; color:#111; }
</style></head><body>
<div class="deck">
  <section class="slide is-current"><h1>Page one</h1><p>first page body</p></section>
  <section class="slide"><h1>Page two</h1><p>second page body</p></section>
</div>
</body></html>"""

LONGFORM = """<!doctype html><html><head><title>long</title></head><body style="margin:0">
<h1>Long report</h1><div style="height:1400px"></div><p>near the bottom</p>
</body></html>"""

# The wiki reading pane: a host element with a header for the toggle and a body
# for the content. This is the third host — the other two are served documents.
PANE = """<!doctype html><html><head><title>pane</title><style>
  body { margin:0; font-family: system-ui; }
  #kb-doc { position: relative; }
  .kb-doc-head { position: relative; padding: 8px 12px; background: #f8fafc; }
</style></head><body>
<article class="kb-doc" id="kb-doc">
  <div class="kb-doc-head" id="kb-doc-head"><h1 style="margin:0;font-size:18px">A wiki page</h1></div>
  <div class="kb-render" id="kb-render" style="padding:12px;height:600px">
    <p>Some page text with <a id="kb-link" href="#elsewhere">a link in it</a>.</p>
    <p>More text so the host has a real height.</p>
  </div>
</article>
</body></html>"""

PANE_MOUNT = """<script>window.addEventListener('DOMContentLoaded', function () {
  window.DocAnnotations.mount({type: 'knowledge', slug: 'channel-notes',
    container: '#kb-doc', content: '#kb-render',
    writable: true, viewer: 'zhen.huang@example.com'});
});</script>"""


def served(html, slug, *, writable, viewer="zhen.huang@example.com", read_url="", extra=""):
    body = _inline_deck_runtime(_inject_base_tag(html, MOUNT))
    page = annotations.inline_runtime(body, asset_type="report", slug=slug,
                                      viewer_email=viewer if writable else "",
                                      writable=writable, read_url=read_url)
    return page.replace("</body>", extra + "\n</body>")


PAGES = {
    "deck": served(DECK, "q4-deck", writable=True),
    "guest": served(DECK, "shared-doc", writable=False,
                    read_url=MOUNT + "s/abc123.zzz/annotations"),
    "long": served(LONGFORM, "long-report", writable=True),
    "pane": served(PANE, "channel-notes", writable=True, extra=PANE_MOUNT),
}


# ── the server ───────────────────────────────────────────────────────────────

NOTES = {
    "q4-deck": [
        {"id": 1, "asset_type": "report", "slug": "q4-deck", "body": "page one note",
         "x": 25.0, "y": 40.0, "page": 1, "author": "guest@example.com", "resolved": False,
         "created_at": "2026-09-29T10:00:00+00:00", "updated_at": "2026-09-29T10:00:00+00:00"},
        {"id": 2, "asset_type": "report", "slug": "q4-deck", "body": "page two note",
         "x": 60.0, "y": 70.0, "page": 2, "author": "guest@example.com", "resolved": True,
         "created_at": "2026-09-29T10:05:00+00:00", "updated_at": "2026-09-29T10:05:00+00:00"},
    ],
    "shared-doc": [
        {"id": 3, "asset_type": "report", "slug": "shared-doc", "body": "a colleague's note",
         "x": 30.0, "y": 50.0, "page": 1, "author": "someone@example.com", "resolved": False,
         "created_at": "2026-09-29T11:00:00+00:00", "updated_at": "2026-09-29T11:00:00+00:00"},
    ],
    "channel-notes": [
        {"id": 5, "asset_type": "knowledge", "slug": "channel-notes", "body": "a note on the page",
         "x": 50.0, "y": 30.0, "page": 0, "author": "zhen.huang@example.com", "resolved": False,
         "created_at": "2026-09-29T12:00:00+00:00", "updated_at": "2026-09-29T12:00:00+00:00"},
    ],
    "long-report": [
        {"id": 4, "asset_type": "report", "slug": "long-report", "body": "bottom note",
         "x": 10.0, "y": 90.0, "page": 0, "author": "guest@example.com", "resolved": False,
         "created_at": "2026-09-29T11:30:00+00:00", "updated_at": "2026-09-29T11:30:00+00:00"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        GETS.append(self.path)
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
        if path.startswith(MOUNT) and path.endswith(".html"):
            name = path[len(MOUNT):].replace(".html", "")
            page = PAGES.get(name)
            if page is None:
                return self._send(404, "no such fixture", "text/plain")
            return self._send(200, page, "text/html; charset=utf-8")
        if path == MOUNT + "api/annotations":
            slug = ""
            for pair in query.split("&"):
                if pair.startswith("slug="):
                    slug = pair[5:]
            return self._send(200, json.dumps({"notes": NOTES.get(slug, [])}))
        if path.startswith(MOUNT + "s/") and path.endswith("/annotations"):
            return self._send(200, json.dumps({"notes": NOTES.get("shared-doc", [])}))
        # Anything outside the mount point is the platform gateway in production:
        # HTTP 200 with 49 bytes of JSON, which is the trap this suite must not fall into.
        return self._send(200, '{"code":1002,"msg":"非法的请求","oK":false}\n')

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        POSTS.append({"path": self.path, "body": raw})
        if self.path.startswith(MOUNT + "api/annotations"):
            return self._send(201, json.dumps({"id": 99}))
        if self.path.startswith(MOUNT + "s/") and self.path.endswith("/annotations"):
            return self._send(405, '{"detail":"read only"}')
        return self._send(200, '{"code":1002}')


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}{MOUNT}"

PROBE = """() => {
  const layer = document.querySelector('.doc-anno-layer');
  const dots = [...document.querySelectorAll('.doc-anno')].map((n) => {
    const dot = n.querySelector('.doc-anno-dot');
    const d = dot.getBoundingClientRect();
    const b = layer.getBoundingClientRect();
    return {id: n.dataset.id, resolved: n.dataset.resolved,
            // where the dot's CENTRE sits, as a percentage of the anchor box
            x: +(((d.left + d.width / 2 - b.left) / b.width) * 100).toFixed(1),
            y: +(((d.top + d.height / 2 - b.top) / b.height) * 100).toFixed(1),
            fontPx: +getComputedStyle(dot).fontSize.replace('px','') || null,
            dotPx: +d.width.toFixed(1),
            // The note's TEXT is shown without anybody clicking the dot.
            cardShown: (() => { const c = n.querySelector('.doc-anno-card');
                return c ? getComputedStyle(c).display !== 'none' : false; })(),
            cardText: (() => { const c = n.querySelector('.doc-anno-card');
                return c ? c.textContent.replace(/\\s+/g, ' ').trim() : ''; })(),
            actsShown: (() => { const a = n.querySelector('.doc-anno-acts');
                return a ? getComputedStyle(a).display !== 'none' : false; })()};
  });
  const toggle = document.querySelector('.doc-anno-toggle');
  const tb = toggle ? toggle.getBoundingClientRect() : null;
  const slide = document.querySelector('.slide.is-current');
  return {dots, layer: layer ? {mode: layer.className, w: Math.round(layer.getBoundingClientRect().width),
                                h: Math.round(layer.getBoundingClientRect().height),
                                top: Math.round(layer.getBoundingClientRect().top)} : null,
          hidden: layer ? layer.classList.contains('is-hidden') : null,
          toggle: toggle ? {visible: toggle.dataset.visible, text: toggle.textContent.trim(),
                            title: toggle.title,
                            position: getComputedStyle(toggle).position,
                            fromRight: Math.round(window.innerWidth - tb.right),
                            fromBottom: Math.round(window.innerHeight - tb.bottom),
                            inHeader: toggle.closest('#kb-doc-head') !== null} : null,
          scale: slide ? +(getComputedStyle(slide.closest('.deck') || slide)
                            .getPropertyValue('--deck-scale') || 1) : 1,
          slideW: slide ? Math.round(slide.getBoundingClientRect().width) : 0};
}"""


def open_fixture(browser, name, width=1280, height=800):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.on("pageerror", lambda e: check(f"{name}: 没有 JS 报错", False, e))
    page.goto(BASE + name + ".html", wait_until="load")
    page.wait_for_function("() => !!document.querySelector('.doc-anno-toggle')")
    page.wait_for_timeout(250)
    return page


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # ── a static deck, a signed-in reader ────────────────────────────────────
    GETS.clear()
    page = open_fixture(browser, "deck")
    got = page.evaluate(PROBE)

    check("读取备注的请求带上了挂载点（/api/annotations…）",
          any(g.startswith(MOUNT + "api/annotations") and "type=report" in g and "slug=q4-deck" in g
              for g in GETS), GETS)
    check("第一页只显示第一页的备注（第二条在第 2 页）",
          [d["id"] for d in got["dots"]] == ["1"], got["dots"])
    check("气泡圆点正落在点击位置的百分比上（25% / 40%，误差 <0.3%）",
          got["dots"] and abs(got["dots"][0]["x"] - 25.0) < 0.3
          and abs(got["dots"][0]["y"] - 40.0) < 0.3, got["dots"])
    check("气泡跟着当前页的可见矩形走（不是整篇文档）",
          got["layer"] and got["layer"]["w"] == got["slideW"] and got["layer"]["w"] > 0, got["layer"])
    # The deck is scaled; a bubble inside the slide would be scaled with it.
    check("deck 被缩放时气泡仍然是可读的屏幕尺寸（不跟着缩）",
          got["dots"] and got["dots"][0]["dotPx"] >= 13, got["dots"])
    check("默认是显示的：图层没有 is-hidden", got["hidden"] is False, got["hidden"])
    check("显示/隐藏按钮带着本页备注数", got["toggle"] and "1" in got["toggle"]["text"], got["toggle"])

    # ── right-click → menu → composer → save ────────────────────────────────
    POSTS.clear()
    box = page.evaluate("""() => { const r = document.querySelector('.slide.is-current')
                                        .getBoundingClientRect();
                                 return {x: r.left + r.width * 0.8, y: r.top + r.height * 0.25}; }""")
    page.mouse.click(box["x"], box["y"], button="right")
    page.wait_for_selector(".doc-anno-menu", timeout=2000)
    menu = page.text_content(".doc-anno-menu")
    check("右键菜单里是「Add a note here」", "Add a note here" in menu, menu)
    check("菜单里标出了这是第几页", "Page 1" in menu, menu)
    page.click(".doc-anno-menu button")
    page.wait_for_selector(".doc-anno-compose textarea")
    page.fill(".doc-anno-compose textarea", "this number is from last quarter")
    page.click(".doc-anno-compose button.primary")
    page.wait_for_timeout(300)
    check("保存走 POST 到挂载点下的 /api/annotations",
          len(POSTS) == 1 and POSTS[0]["path"].startswith(MOUNT + "api/annotations"), POSTS)
    if POSTS:
        sent = json.loads(POSTS[0]["body"])
        check("发出去的是点击位置的百分比 + 页码，不是像素",
              abs(sent["x"] - 80.0) < 1.5 and abs(sent["y"] - 25.0) < 1.5 and sent["page"] == 1
              and sent["asset_type"] == "report" and sent["slug"] == "q4-deck", sent)

    # ── the bubble carries the note, without being opened ──────────────────
    got = page.evaluate(PROBE)
    check("备注正文默认就是可见的气泡（不再只是一个点）",
          bool(got["dots"]) and got["dots"][0]["cardShown"]
          and "guest" in got["dots"][0]["cardText"] and "page one note" in got["dots"][0]["cardText"],
          got["dots"])
    check("操作按钮默认收起，免得一页备注全是按钮",
          bool(got["dots"]) and not got["dots"][0]["actsShown"], got["dots"])
    page.click(".doc-anno .doc-anno-card")
    page.wait_for_timeout(200)
    got = page.evaluate(PROBE)
    check("点气泡才展开操作（标记已处理等）",
          bool(got["dots"]) and got["dots"][0]["actsShown"], got["dots"])
    page.click(".doc-anno .doc-anno-card")
    page.wait_for_timeout(150)

    # ── right-click a bubble → its own actions ─────────────────────────────
    page.click(".doc-anno .doc-anno-dot", button="right")
    page.wait_for_selector(".doc-anno-menu", timeout=2000)
    menu = page.text_content(".doc-anno-menu")
    check("别人写的备注：只给「标记已处理」，不给改/删",
          "Mark as resolved" in menu and "Delete this note" not in menu
          and "Edit this note" not in menu, menu)
    check("菜单标题说明这条是谁写的、什么状态", "guest · open" in menu, menu)
    page.keyboard.press("Escape")

    # ── turn the page ──────────────────────────────────────────────────────
    page.evaluate("""() => { const s = document.querySelectorAll('.slide');
        s[0].classList.remove('is-current'); s[1].classList.add('is-current'); }""")
    page.wait_for_timeout(1000)
    got = page.evaluate(PROBE)
    check("翻到第 2 页后显示的是第 2 页的备注",
          [d["id"] for d in got["dots"]] == ["2"], got["dots"])
    check("已解决的备注画成灰色", got["dots"] and got["dots"][0]["resolved"] == "1", got["dots"])
    page.close()

    # ── the toggle ─────────────────────────────────────────────────────────
    page = open_fixture(browser, "deck")
    check("按钮的默认状态是显示", page.get_attribute(".doc-anno-toggle", "data-visible") == "1")
    got = page.evaluate(PROBE)
    check("分页文档上的开关固定在右下角（不再压住右上角的内容）",
          bool(got["toggle"]) and got["toggle"]["position"] == "fixed"
          and got["toggle"]["fromRight"] < 40 and got["toggle"]["fromBottom"] < 40
          and not got["toggle"]["inHeader"], got["toggle"])
    page.click(".doc-anno-toggle")
    page.wait_for_timeout(150)
    got = page.evaluate(PROBE)
    check("点一下就整体隐藏", got["hidden"] is True and got["dots"], got["hidden"])
    check("隐藏时按钮变成「Notes off」", "Notes off" in (got["toggle"]["text"] or ""), got["toggle"])
    page.reload(wait_until="load")
    page.wait_for_selector(".doc-anno-toggle")
    page.wait_for_timeout(250)
    check("刷新后仍然是隐藏的（按文档记住，不按会话）",
          page.evaluate("() => document.querySelector('.doc-anno-layer').classList.contains('is-hidden')"))
    check("重新打开时只读这一份文档的偏好",
          page.evaluate("() => localStorage.getItem('doc-anno:report:q4-deck')") == "0")
    page.close()

    # ── the no-login share link ────────────────────────────────────────────
    GETS.clear()
    POSTS.clear()
    page = open_fixture(browser, "guest")
    got = page.evaluate(PROBE)
    check("匿名访客能读到别人写的备注",
          [d["id"] for d in got["dots"]] == ["3"], got["dots"])
    check("备注是走 token 作用域的只读地址读的",
          any(g.startswith(MOUNT + "s/abc123.zzz/annotations") for g in GETS), GETS)
    page.mouse.click(box["x"], box["y"], button="right")
    page.wait_for_timeout(250)
    check("访客的右键菜单不被劫持（不弹我们的菜单）",
          page.query_selector(".doc-anno-menu") is None)
    check("访客点了也不会发出任何写入请求", POSTS == [], POSTS)
    page.close()

    # ── a long-form document ───────────────────────────────────────────────
    page = open_fixture(browser, "long", height=700)
    got = page.evaluate(PROBE)
    check("长文档里图层铺满整篇而不是一屏（备注跟着文字滚动）",
          got["layer"] and got["layer"]["h"] > 1400, got["layer"])
    check("长文档里的气泡落在整篇的 90% 高度上",
          got["dots"] and abs(got["dots"][0]["y"] - 90.0) < 1.0, got["dots"])
    page.close()

    # ── the container host (the wiki reading pane) ────────────────────────
    GETS.clear()
    page = open_fixture(browser, "pane")
    got = page.evaluate(PROBE)
    check("阅读区里也能显示备注", [d["id"] for d in got["dots"]] == ["5"], got["dots"])
    check("只有一层：被替换掉的会话不会在页面里留下孤儿图层（切页时 fetch 迟到）",
          page.evaluate("() => document.querySelectorAll('.doc-anno-layer').length") == 1
          and page.evaluate("() => document.querySelectorAll('.doc-anno-toggle').length") == 1)
    check("阅读区的开关同样固定在页面右下角（不再挂进 wiki 头部）",
          bool(got["toggle"]) and got["toggle"]["position"] == "fixed"
          and got["toggle"]["fromRight"] < 40 and got["toggle"]["fromBottom"] < 40
          and not got["toggle"]["inHeader"], got["toggle"])
    page.mouse.click(box["x"], box["y"] + 40, button="right")
    page.wait_for_selector(".doc-anno-menu", timeout=2000)
    check("阅读区里右键正文也会给「Add a note here」",
          "Add a note here" in page.text_content(".doc-anno-menu"))
    page.keyboard.press("Escape")
    # A right-click on a link must still be the browser's own menu.
    link_box = page.evaluate("""() => { const r = document.getElementById('kb-link').getBoundingClientRect();
                                       return {x: r.left + 2, y: r.top + r.height / 2}; }""")
    page.mouse.click(link_box["x"], link_box["y"], button="right")
    page.wait_for_timeout(250)
    check("右键链接不会被我们的菜单抢走", page.query_selector(".doc-anno-menu") is None)
    page.close()

    browser.close()

server.shutdown()
print("\n%d checks failed" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
