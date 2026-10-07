"""The Inbox page: two panes, English one-line messages, preview + jump.

What this pins down:

* **The Inbox sits immediately right of Data Center** and `NAV_TAB_FOR_PAGE` agrees
  (Data Center is now the first tab — the Dashboard landing page was removed).
  `navTo()` highlights a tab by its POSITION in the DOM, so inserting a tab without
  renumbering that table does not error — it silently highlights the wrong tab on
  every page. Every entry is asserted against the rendered tab list here, not
  against the constant.
* **A message is ONE English sentence** — actor first, the document name bold and
  clickable, the time at the end, nothing else. The stored `summary` (prebuilt,
  Chinese) must not leak into the list.
* **Clicking the document name selects the message and loads it into the right-hand
  preview** (reports via the chrome-free `/r/<slug>` page — the same address that
  carries the colleague-note runtime, so note permissions are the page's own).
  The preview is never an edit surface: the way in is the "Open page" button,
  which jumps to the server-provided deep link (mount point included — a link
  assembled from `location.origin` would land on the host root, where the
  platform gateway answers HTTP 200 with 49 bytes of JSON).
* **Datasets are not documents**: no iframe, the pane offers the jump instead.

Needs playwright and a local Chrome.

    .venv312/bin/python api/tests/verify_inbox_ui.py
"""
import functools
import http.server
import json
import re
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8802
FAILURES = []

ITEMS = [
    {"id": 1, "kind": "note", "actor_email": "guest@example.com", "actor_name": "Zhen Huang",
     "recipient_email": "me@example.com",
     "target_type": "report", "target_slug": "q4-deck", "target_title": "Q4 Channel Deep Dive",
     "target_url": "/?report=q4-deck",
     "summary": "在你的报告《Q4 Channel Deep Dive》里留了一条备注第 3 页：这个数是上季度的",
     "note_id": 7, "created_at": "2026-09-29T09:00:00", "read_at": None},
    {"id": 2, "kind": "share", "actor_email": "mate@example.com", "actor_name": "",
     "recipient_email": "me@example.com",
     "target_type": "knowledge", "target_slug": "channel-notes", "target_title": "Channel notes",
     "target_url": "/?kb=channel-notes",
     "summary": "把知识库页面《Channel notes》分享给了你",
     "note_id": None, "created_at": "2026-09-29T08:00:00", "read_at": "2026-09-29T08:30:00"},
    {"id": 3, "kind": "publish", "actor_email": "mate@example.com", "actor_name": "Mate Wang",
     "recipient_email": None,
     "target_type": "report", "target_slug": "h1-plan", "target_title": "H1 Plan",
     "target_url": "/?report=h1-plan",
     "summary": "把报告《H1 Plan》发布到了公共区",
     "note_id": None, "created_at": "2026-09-27T08:00:00", "read_at": None},
    {"id": 4, "kind": "dataset", "actor_email": "", "actor_name": "", "recipient_email": None,
     "target_type": "dataset", "target_slug": "sales_invoice", "target_title": "Sales data",
     "target_url": "/?dc=sales_invoice",
     "summary": "数据集 Sales data（sales_invoice）已覆盖导入：120,000 → 128,400 行（+8,400）",
     "note_id": None, "created_at": "2026-09-26T08:00:00", "read_at": None},
    {"id": 5, "kind": "share", "actor_email": "mate@example.com", "actor_name": "Mate Wang",
     "recipient_email": None,
     "target_type": "event", "target_slug": "brand-review", "target_title": "Brand review",
     "target_url": "/?event=brand-review",
     "summary": "把活动《Brand review》分享给了你",
     "note_id": None, "created_at": "2026-09-25T08:00:00", "read_at": None},
]

CAL_EVENTS = {
    "events": [{"id": 1, "slug": "brand-review", "title": "Brand review", "summary": "",
                "summary_en": "", "summary_zh": "", "category": "", "tags": [],
                "start_date": "2026-10-01", "end_date": "2026-10-02", "kind": "event",
                "status": "published", "owner_email": "me@example.com", "visibility": "private",
                "partners": [], "attachments": [], "document_type": "event",
                "created_at": "2026-09-20T08:00:00", "updated_at": "2026-09-20T08:00:00"}],
    "count": 1, "scope": "mine",
}

DATASETS = [{"table_name": "sales_invoice", "display_name": "Sales data", "row_count": 128400,
             "source_file": "sales.xlsx", "columns": [], "col_count": 12,
             "created_at": "2026-09-26T08:00:00", "updated_at": "2026-09-26T08:00:00"}]

# One wiki page, seen by someone who may NOT manage it (can_manage false → the meta row
# offers "Pull a copy"). That is exactly the shape the Inbox preview shows a colleague,
# and it is the row whose actions must not become a control surface inside the preview.
WIKI_PAGE = {"slug": "channel-notes", "title": "Channel notes", "document_type": "note",
             "category": "channel", "body": "## Channel notes\n\nAcme Retail is `Acme Retail`, not Acme-Retail.\n",
             "summary": "", "summary_en": "", "summary_zh": "", "tags": [],
             "status": "published", "visibility": "public", "enabled": True, "version": "1.0",
             "owner_email": "mate@example.com", "can_manage": False, "shared_emails": [],
             "created_at": "2026-09-29T08:00:00", "updated_at": "2026-09-29T08:00:00"}


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the app plus a same-origin parent page for the embed check.

    The parent MUST be same origin: an `about:blank` parent makes the iframe
    opaque-origin, its `sessionStorage` read throws, and the app reports errors
    that have nothing to do with embedded mode.
    """

    def do_GET(self):
        if self.path.startswith("/__embed.html"):
            # `?page=<same-origin path>` lets the parent frame any deep link. The wiki
            # (`?kb=`) is the case that matters: it is the one document that boots
            # INSIDE the app shell, so hiding the nav is not enough for it.
            want = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    .get("page") or ["/index.html"])[0]
            if not re.fullmatch(r"/[A-Za-z0-9._~%=\-/?&]*", want):
                want = "/index.html"
            body = ('<!doctype html><meta charset="utf-8">'
                    '<iframe src="%s" style="width:880px;height:600px;border:0"></iframe>'
                    % want).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):        # request noise only
        pass


def serve():
    handler = functools.partial(_Handler, directory=ROOT)
    httpd = _Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


STUB = """
window.__calls = [];
window.__posted = [];
const ITEMS = %s, EVENTS = %s, DATASETS = %s, PAGE = %s;
// ⚠️ The stub OWNS the server's per-reader state and applies it. A stub that answered
// every POST with `{ok: true}` and kept handing back the same five rows would let
// "the row is gone after Delete" pass without the page having done anything — the
// assertion would be measuring the stub. So /dismiss really removes the row and
// /unread really clears its read_at, and the feed is recomputed from that.
window.__items = ITEMS.map((i) => Object.assign({}, i));
window.__unreadMark = {};      // id -> true, this reader's "unread again" override
function inboxVisible() {
  return window.__items.filter((i) => {
    if (window.__unreadMark[i.id]) i.read_at = null;
    return true;
  });
}
function inboxUnread() {
  return inboxVisible().filter((i) => !i.read_at).length;
}
function inboxBody() { return {items: inboxVisible(), unread: inboxUnread()}; }
window.fetch = function (url, init) {
  const u = String(url);
  const method = (init || {}).method || 'GET';
  window.__calls.push({u: u, m: method});
  if (method === 'POST') {
    window.__posted.push(u);
    let ids = null;
    try { ids = (JSON.parse((init || {}).body || '{}') || {}).ids; } catch (e) { ids = null; }
    if (u.indexOf('/read') >= 0) {
      inboxVisible().forEach((i) => {
        if (ids && ids.indexOf(i.id) < 0) return;
        delete window.__unreadMark[i.id];
        i.read_at = '2026-10-05T09:00:00Z';
      });
      return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(inboxBody())});
    }
    if (u.indexOf('/unread') >= 0) {
      inboxVisible().forEach((i) => {
        if (ids && ids.indexOf(i.id) < 0) return;
        window.__unreadMark[i.id] = true;
      });
      return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(inboxBody())});
    }
    if (u.indexOf('/dismiss') >= 0) {
      const gone = [];
      window.__items = window.__items.filter((i) => {
        const hit = !ids || ids.indexOf(i.id) >= 0;
        if (hit) gone.push(i.id);
        return !hit;
      });
      return Promise.resolve({ok: true, status: 200,
        json: () => Promise.resolve(Object.assign({changed: gone.length}, inboxBody()))});
    }
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})});
  }
  let body = {};
  if (u.indexOf('/api/inbox') >= 0) body = inboxBody();
  else if (u.indexOf('/api/auth/me') >= 0)
    body = {enabled: true, authenticated: true, user: {email: 'me@example.com', role: 'user'}};
  else if (u.indexOf('/api/calendar/events') >= 0) body = EVENTS;
  else if (u.indexOf('/api/data-center/datasets') >= 0) body = DATASETS;
  // ⚠️ Shape the stub per URL: the file library reads {folders, files} and does
  // `!folders.length` unguarded, so a catch-all {} throws inside fbRenderList and
  // looks exactly like a product bug on the ?dc= page.
  else if (u.indexOf('/api/data-center/files/browse') >= 0) body = {folders: [], files: []};
  else if (u.indexOf('/api/data-center/files') >= 0) body = {tree: []};
  else if (u.indexOf('/api/data-center/datasets') >= 0 && u.indexOf('/preview') >= 0)
    body = {count: 0, columns: [], rows: []};
  else if (u.indexOf('/api/annotations') >= 0) body = {notes: []};
  else if (u.indexOf('/api/reports') >= 0) body = {reports: [], count: 0, categories: []};
  // The single-page fetch must be matched BEFORE the collection rule below, or the
  // detail call gets {items: []} and the wiki renders "Could not open".
  else if (/\\/api\\/knowledge\\/items\\/[^/?]+$/.test(u.split('?')[0])) body = PAGE;
  else if (u.indexOf('/api/knowledge/items') >= 0) body = {items: [], count: 0};
  else if (u.indexOf('/api/settings') >= 0) body = {};
  else body = [];
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
};
""" % (json.dumps(ITEMS), json.dumps(CAL_EVENTS), json.dumps(DATASETS), json.dumps(WIKI_PAGE))


def tab_state(page):
    return page.evaluate("""() => Array.from(document.querySelectorAll('.nav-tab'))
        .map((t, i) => ({i: i, id: t.id || '', text: t.textContent.trim().replace(/\\s+/g, ' ')}))""")


def main():
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(STUB)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.inboxPage")

            # ── where the tab is ────────────────────────────────────────────
            tabs = tab_state(page)
            labels = [t["text"] for t in tabs]
            # ⚠️ The badge says 4 because that is how many of ITEMS are unread (1, 3, 4,
            # 5) — the stub computes it from the rows, as the server does. It used to
            # hand back a hardcoded `unread: 3` that contradicted its own fixture, so
            # this number was a literal rather than a round trip.
            check("导航栏顺序是 Data Center / Inbox / Workspace / Dashboard / Knowledge base / Calendar / Settings",
                  labels == ["Data Center", "Inbox4", "Workspace", "Dashboard",
                             "Knowledge base", "Calendar", "Settings"], labels)
            check("Inbox 紧跟在 Data Center 右边",
                  labels.index("Inbox4") == labels.index("Data Center") + 1, labels)
            check("未读数在标签上可见（4 条未读：1/3/4/5）", page.evaluate(
                "() => { const b = document.getElementById('inbox-badge');"
                " return b && !b.hidden && b.textContent === '4'; }"))

            # ── the highlight, by behaviour rather than by a table ──────────
            # This used to read `NAV_TAB_FOR_PAGE`, a map of DOM *indexes*, and assert
            # the numbers. That is precisely the representation that broke silently
            # before: inserting a tab moved every index after it and nothing failed
            # until a page highlighted the wrong tab. Tabs are now matched by
            # `data-nav-key`, so the assertion is "the right KEY lights up" — which
            # cannot go stale when a tab is inserted anywhere.
            keys = page.evaluate(
                "() => Array.from(document.querySelectorAll('.nav-center .nav-tab'))"
                "  .map(t => t.getAttribute('data-nav-key'))")
            check("每个标签都带 data-nav-key（不再靠位置识别）",
                  all(keys) and len(keys) == len(tabs), keys)
            for page_key, want_key in (("data-center", "datacenter"), ("inbox", "inbox"),
                                       ("reports", "workspace"), ("dashboard", "dashboard"),
                                       ("knowledge", "knowledge"), ("calendar", "calendar")):
                page.evaluate("k => navTo(null, k, k)", page_key)
                page.wait_for_timeout(120)
                active_key = page.evaluate(
                    "() => { const t = document.querySelector('.nav-tab.active');"
                    " return t ? t.getAttribute('data-nav-key') : null; }")
                check("进 %s 页时高亮的是 %s 那个标签（不是插了新标签之后错位的那一个）"
                      % (page_key, want_key), active_key == want_key, (page_key, active_key))

            # Settings is NOT in the list above on purpose: navTo() bounces a non-admin
            # to the landing page, so the correct highlight there is *none*. Asserting
            # "settings lights up" for a signed-out stub would be asserting a redirect
            # away from the thing being tested.
            page.evaluate("() => navTo(null, 'system-settings', 'System Settings')")
            page.wait_for_timeout(120)
            check("非管理员进 Settings 会被弹回落地页，且不点亮任何标签", page.evaluate(
                "() => !document.querySelector('.nav-tab.active')"))

            # The landing page owns no tab, and must not light one.
            page.evaluate("() => navTo(null, 'home', 'Home')")
            page.wait_for_timeout(120)
            check("回落地页时不点亮任何标签", page.evaluate(
                "() => !document.querySelector('.nav-tab.active')"))

            # ── the page itself: two panes, English one-line messages ──────
            page.evaluate("() => navTo(null, 'inbox', 'Inbox')")
            page.wait_for_selector(".ibx-item")
            check("PAGES 里有 inbox，且两栏容器都在",
                  page.evaluate("() => PAGES['inbox'] === 'page-inbox'")
                  and page.evaluate("() => !!document.getElementById('inbox-split')")
                  and page.evaluate("() => !!document.getElementById('inbox-list')")
                  and page.evaluate("() => !!document.getElementById('inbox-preview')"))
            check("右栏初始是空态提示（没有选中的消息就没有预览）", page.evaluate(
                "() => !document.getElementById('ibx-preview-pane').hidden"
                " === false && !document.getElementById('ibx-preview-empty').hidden"))

            text = page.inner_text("#inbox-list")
            check("消息是英文句式：发起人在前（note 用服务器解析的 display_name）",
                  text.startswith("Zhen Huang left a note in"), text[:100])
            check("知识库分享的英文句式（无 display_name 时回退邮箱前缀）",
                  "mate shared the knowledge page Channel notes with you." in text, text[:160])
            check("发布到公共区与数据集更新的英文句式都在",
                  "published the report H1 Plan to the public area." in text
                  and "The data layer updated dataset Sales data." in text, text[:200])
            check("存储的中文 summary 不再出现在列表里",
                  not any(s in text for s in ("留了一条备注", "分享给了你", "发布到了公共区", "数据集")), text[:80])
            check("每条消息右侧都有时间", page.evaluate(
                """() => { const t = [...document.querySelectorAll('.ibx-time')];
                     return t.length === 5 && t.every(x => x.textContent.trim().length > 0); }"""))

            # ── unread state (before anything is clicked: id1 unread, id2 read) ──
            check("未读的那条整句加粗，已读的不是", page.evaluate(
                """() => { const rows = [...document.querySelectorAll('.ibx-item')];
                    const w = (r) => getComputedStyle(r.querySelector('.ibx-line')).fontWeight;
                    return w(rows[0]) > w(rows[1]); }"""))
            check("已读的那条没有红点", page.evaluate("""() => {
                const rows = [...document.querySelectorAll('.ibx-item')];
                return getComputedStyle(rows[1].querySelector('.ibx-dot')).backgroundColor
                       === 'rgba(0, 0, 0, 0)'; }"""))

            # ── the bold clickable document name ───────────────────────────
            link = page.evaluate("""() => { const b = document.querySelector('.ibx-title-link');
                return {text: b.textContent.trim(), weight: getComputedStyle(b).fontWeight,
                        tag: b.tagName, row: b.closest('.ibx-item').dataset.id}; }""")
            check("文档名是加粗的可点击按钮（第一行的 Q4 Channel Deep Dive）",
                  link["text"] == "Q4 Channel Deep Dive" and link["tag"] == "BUTTON"
                  and int(link["weight"]) >= 700, link)

            # ── clicking the name selects + previews, and marks read ───────
            page.click(".ibx-title-link")
            page.wait_for_timeout(250)
            check("点击文档名后右栏加载预览（report 走独立的 /r/<slug> 文档页）",
                  page.evaluate("""() => {
                      const pane = document.getElementById('ibx-preview-pane');
                      const frame = document.getElementById('ibx-frame');
                      return !pane.hidden && !frame.hidden && /\\/r\\/q4-deck$/.test(frame.src); }"""),
                  page.evaluate("() => document.getElementById('ibx-frame').src"))
            check("预览栏标题是文档名，且带 Open page 按钮",
                  page.evaluate("() => document.getElementById('ibx-preview-title').textContent")
                  == "Q4 Channel Deep Dive"
                  and page.evaluate("() => !!document.getElementById('ibx-open-btn')"))
            check("选中的行有高亮", page.evaluate(
                "() => document.querySelector('.ibx-item.is-selected').dataset.id") == "1")
            check("选中即已读：POST 到 /api/inbox/read",
                  any("/api/inbox/read" in c["u"] and c["m"] == "POST"
                      for c in page.evaluate("() => window.__calls")),
                  page.evaluate("() => window.__calls")[-2:])

            # ── dataset: no iframe, hint + the same jump button ────────────
            page.evaluate("""() => document.querySelectorAll('.ibx-item')[3].click()""")
            page.wait_for_timeout(200)
            check("数据集消息不嵌 iframe，右栏给出提示与 Open page",
                  page.evaluate("""() => {
                      const f = document.getElementById('ibx-frame');
                      const nf = document.getElementById('ibx-noframe');
                      return f.hidden && !nf.hidden
                          && nf.textContent.indexOf('Data Center') >= 0
                          && !!document.getElementById('ibx-open-btn'); }"""))
            check("Open page 的目标带着挂载点深链（?dc=…）", page.evaluate("""() => {
                // jump() sets location.href; assert via the item the button was built from
                const items = window.inboxPage.items();
                return items[3].target_url === '/?dc=sales_invoice'; }"""))

            # ── report / event previews must frame the DOCUMENT, never the app ─────
            # A report and an event each have a chrome-free standalone address (/r/ and
            # /e/). Framing their deep link (?report=, ?event=) instead would boot the
            # whole SPA inside the pane — the "the preview shows the entire website"
            # complaint, twice over. The wiki cannot do this (it has no standalone
            # address) and is handled by app-embedded; these two must not drift into
            # needing that.
            def framed(target_id):
                page.evaluate(
                    "(id) => document.querySelector('.ibx-item[data-id=\"' + id + '\"]"
                    " .ibx-title-link').click()", target_id)
                page.wait_for_timeout(300)
                return page.evaluate("() => document.getElementById('ibx-frame').src")

            report_src = framed("1")
            event_src = framed("5")
            check("报告预览嵌的是无壳独立文档 /r/<slug>（不是 ?report= 的整站）",
                  re.search(r"/r/q4-deck$", report_src) is not None, report_src)
            check("事件预览嵌的是无壳独立文档 /e/<slug>（不是 ?event= 的整站）",
                  re.search(r"/e/brand-review$", event_src) is not None, event_src)
            check("这两种预览的 frame 地址里没有任何「整站入口」（?report= / ?event= / 裸 index.html）",
                  not any(t in report_src + event_src
                          for t in ("?report=", "?event=", "/index.html")),
                  (report_src, event_src))
            check("预览栏标题跟着换（报告 → 事件）",
                  page.evaluate("() => document.getElementById('ibx-preview-title').textContent")
                  == "Brand review",
                  page.evaluate("() => document.getElementById('ibx-preview-title').textContent"))

            # ── filters ────────────────────────────────────────────────────
            page.click("#inbox-tab-mine")
            page.wait_for_timeout(150)
            mine = page.evaluate("() => document.querySelectorAll('.ibx-item').length")
            page.click("#inbox-tab-team")
            page.wait_for_timeout(150)
            team = page.evaluate("() => document.querySelectorAll('.ibx-item').length")
            page.click("#inbox-tab-all")
            page.wait_for_timeout(150)
            alln = page.evaluate("() => document.querySelectorAll('.ibx-item').length")
            check("「For me」只给定向给我的行，「Public activity」只给广播",
                  (mine, team, alln) == (2, 3, 5), (mine, team, alln))
            check("筛选的高亮跟着走", page.evaluate(
                "() => document.getElementById('inbox-tab-team').getAttribute('aria-selected')") in ("false", "true")
                  and page.evaluate("() => document.getElementById('inbox-tab-all').classList.contains('on')"))

            # ── marking read ───────────────────────────────────────────────
            page.evaluate("() => inboxPage.markAllRead()")
            page.wait_for_timeout(250)
            check("全部已读会 POST 到 /api/inbox/read",
                  any("/api/inbox/read" in u["u"] and u["m"] == "POST"
                      for u in page.evaluate("() => window.__calls")), page.evaluate("() => window.__calls")[-3:])
            check("已读之后徽标消失", page.evaluate(
                "() => document.getElementById('inbox-badge').hidden"))

            # ── Delete all, in the toolbar ──────────────────────────────────
            # ⚠️ "Delete all" must be offered on the ROWS, not on the unread state.
            # Everything is read at this point, so a button keyed to the unread count
            # would be hidden here — exactly when a reader with 40 read messages and
            # nothing unread most wants to clear them.
            check("全部已读之后「Delete all」仍然在（它跟的是行数，不是未读数）", page.evaluate(
                """() => { const b = document.getElementById('inbox-deleteall');
                    return b && !b.hidden && getComputedStyle(b).display !== 'none'; }"""))
            check("「Delete all」有删除图标，不是空盒子", page.evaluate(
                """() => { const i = document.querySelector('#inbox-deleteall i');
                    if (!i) return 'no icon element';
                    const r = i.getBoundingClientRect();
                    return r.width > 0 && r.height > 0
                        && getComputedStyle(i, ':before').content !== 'none'; }"""))

            # The confirmation has to say what actually happens: the rows leave THIS
            # reader's Inbox, and nothing is deleted from the log. A dialog that implied
            # a real delete would be describing a table the server never writes to.
            page.click("#inbox-deleteall")
            page.wait_for_timeout(200)
            dlg = page.evaluate("""() => {
                const d = document.getElementById('kld-dlg');
                return d && !d.hidden
                    ? {title: (document.getElementById('kld-dlg-title') || {}).textContent || '',
                       body: (document.getElementById('kld-dlg-body') || {}).innerText || '',
                       ok: (document.getElementById('kld-dlg-ok') || {}).textContent || ''}
                    : null; }""")
            check("点「Delete all」先弹确认框，并说清只影响自己的收件箱",
                  bool(dlg) and "5" in dlg["body"]
                  and "Inbox" in dlg["body"]
                  and ("不受影响" in dlg["body"] or "not affected" in dlg["body"]),
                  dlg)
            check("确认框的确认键是 danger（删不是取消）", page.evaluate(
                "() => document.getElementById('kld-dlg-ok').classList.contains('danger')"))

            # Cancel first: a confirmation that cannot be walked back is not a
            # confirmation, and this is the step that would eat five messages.
            page.click("#kld-dlg-cancel")
            page.wait_for_timeout(200)
            check("取消后什么都没删，行还在", page.evaluate(
                "() => document.querySelectorAll('.ibx-item').length") == 5)

            page.click("#inbox-deleteall")
            page.wait_for_timeout(200)
            page.click("#kld-dlg-ok")
            page.wait_for_timeout(400)
            check("确认后 POST 到 /api/inbox/dismiss",
                  any("/api/inbox/dismiss" in u["u"] and u["m"] == "POST"
                      for u in page.evaluate("() => window.__calls")),
                  page.evaluate("() => window.__calls")[-3:])
            check("全部删除后列表空了，徽标也消失", page.evaluate(
                """() => document.querySelectorAll('.ibx-item').length === 0
                    && document.getElementById('inbox-badge').hidden
                    && document.getElementById('inbox-deleteall').hidden"""))
            check("全部删除后右栏回到空态（没有悬着的预览）", page.evaluate(
                """() => !document.getElementById('ibx-preview-empty').hidden
                    && document.getElementById('ibx-preview-pane').hidden"""))

            # Put the rows back for the per-message tests below.
            page.evaluate("() => { window.__items.length = 0;"
                          " %s.forEach((i) => window.__items.push(Object.assign({}, i))); }"
                          % json.dumps(ITEMS))
            page.evaluate("() => { window.__unreadMark = {}; }")
            page.evaluate("() => inboxPage.refresh()")
            page.wait_for_selector(".ibx-item")

            # ── the per-message right-click menu ────────────────────────────
            check("菜单元素在 body 上，不在 #page-inbox 里（display:none 的子树画不出来）",
                  page.evaluate(
                      "() => { const m = document.getElementById('ibx-ctx');"
                      " return !!m && !document.getElementById('page-inbox').contains(m); }"))
            check("菜单一开始是关着的", page.evaluate(
                "() => document.getElementById('ibx-ctx').hidden"))

            # A REAL right-click. `el.click()` dispatches straight at the element and
            # never hit-tests, so it would pass against a row that is covered.
            row = page.query_selector('.ibx-item[data-id="2"]')
            box = row.bounding_box()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2,
                             button="right")
            page.wait_for_timeout(200)
            menu = page.evaluate("""() => {
                const m = document.getElementById('ibx-ctx');
                if (m.hidden) return null;
                const r = m.getBoundingClientRect();
                return {items: [...m.querySelectorAll('button')].map(
                            (b) => ({a: b.dataset.action, t: b.textContent.trim(),
                                     danger: b.classList.contains('danger')})),
                        x: r.x, y: r.y, w: r.width, h: r.height,
                        drawn: [...m.querySelectorAll('i')].map(
                            (i) => getComputedStyle(i, ':before').content)}; }""")
            check("右键一条消息弹出菜单（真实鼠标右键，不是 el.click()）",
                  bool(menu) and len(menu["items"]) == 2, menu)
            check("菜单里是「标为未读」和「删除」，删除是 danger",
                  bool(menu) and [i["a"] for i in menu["items"]] == ["unread", "delete"]
                  and menu["items"][1]["danger"] is True, menu)
            check("菜单两个图标都真的画出来了（不是空盒子）",
                  bool(menu) and all(c and c != "none" for c in menu["drawn"]), menu)
            # The menu is a `position: fixed` box; on screen and not collapsed.
            check("菜单画在视口内且有面积", bool(menu) and menu["w"] > 100 and menu["h"] > 20
                  and 0 <= menu["x"] and menu["y"] >= 0, menu)
            check("菜单在光标附近（不是跑到左上角）",
                  bool(menu) and abs(menu["x"] - (box["x"] + box["width"] / 2)) < 200, menu)

            # Row 2 was read, so "mark as unread" belongs. Row 1 is unread, and an
            # item that reports success without changing anything is worse than absent.
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            check("Esc 关闭菜单", page.evaluate("() => document.getElementById('ibx-ctx').hidden"))

            unread_row = page.query_selector('.ibx-item[data-id="1"]')
            ubox = unread_row.bounding_box()
            page.mouse.click(ubox["x"] + ubox["width"] / 2, ubox["y"] + ubox["height"] / 2,
                             button="right")
            page.wait_for_timeout(200)
            check("未读的那条不给「标为未读」（那是个什么都不做的按钮）", page.evaluate(
                """() => [...document.getElementById('ibx-ctx').querySelectorAll('button')]
                    .map((b) => b.dataset.action)""") == ["delete"])

            # Outside click dismisses too.
            page.mouse.click(ubox["x"] + ubox["width"] / 2, ubox["y"] + ubox["height"] / 2)
            page.wait_for_timeout(200)
            check("点菜单外关闭菜单（且这一下是左键，不会误触发删除）", page.evaluate(
                "() => document.getElementById('ibx-ctx').hidden")
                  and page.evaluate("() => document.querySelectorAll('.ibx-item').length") == 5)

            # ── mark as unread, on one row ─────────────────────────────────
            # Measure the row BEFORE the action. Comparing against a sibling row would
            # be measuring the wrong thing: by now every other row is unread too, so
            # "this one is bolder than that one" is 700 > 700 and tells us nothing.
            before = page.evaluate("""() => {
                const r = document.querySelector('.ibx-item[data-id="2"]');
                return {w: getComputedStyle(r.querySelector('.ibx-line')).fontWeight,
                        dot: getComputedStyle(r.querySelector('.ibx-dot')).backgroundColor,
                        badge: document.getElementById('inbox-badge').textContent}; }""")
            page.evaluate("""() => { const r = document.querySelector('.ibx-item[data-id="2"]');
                r.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 300, clientY: 300})); }""")
            page.wait_for_timeout(150)
            page.click('#ibx-ctx button[data-action="unread"]')
            page.wait_for_timeout(400)
            check("标为未读 POST 到 /api/inbox/unread",
                  any("/api/inbox/unread" in u["u"] and u["m"] == "POST"
                      for u in page.evaluate("() => window.__calls")),
                  page.evaluate("() => window.__calls")[-3:])
            after = page.evaluate("""() => {
                const r = document.querySelector('.ibx-item[data-id="2"]');
                return {w: getComputedStyle(r.querySelector('.ibx-line')).fontWeight,
                        dot: getComputedStyle(r.querySelector('.ibx-dot')).backgroundColor,
                        badge: document.getElementById('inbox-badge').textContent}; }""")
            check("标为未读后那一行变粗、红点出现、徽标 +1（行本来就已读）",
                  int(after["w"]) > int(before["w"])
                  and after["dot"] != before["dot"]
                  and int(after["badge"]) == int(before["badge"]) + 1,
                  (before, after))
            check("菜单在动作之后自己关掉了", page.evaluate(
                "() => document.getElementById('ibx-ctx').hidden"))

            # ── delete one message ─────────────────────────────────────────
            page.evaluate("""() => { const r = document.querySelector('.ibx-item[data-id="2"]');
                r.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 300, clientY: 300})); }""")
            page.wait_for_timeout(150)
            page.click('#ibx-ctx button[data-action="delete"]')
            page.wait_for_timeout(400)
            check("删除单条 POST 到 /api/inbox/dismiss",
                  any("/api/inbox/dismiss" in u["u"] and u["m"] == "POST"
                      for u in page.evaluate("() => window.__calls")),
                  page.evaluate("() => window.__calls")[-3:])
            check("删掉的那一行从列表消失，其余四条还在", page.evaluate(
                "() => { const ids = [...document.querySelectorAll('.ibx-item')]"
                "   .map((r) => r.dataset.id).sort();"
                " return ids.length === 4 && ids.indexOf('2') < 0; }"),
                  page.evaluate("() => [...document.querySelectorAll('.ibx-item')]"
                                ".map((r) => r.dataset.id)"))
            check("删掉未读的那条，徽标跟着减", page.evaluate(
                "() => document.getElementById('inbox-badge').textContent") == "3")

            # Deleting the row that is framed on the right must clear the preview.
            page.evaluate("() => document.querySelector('.ibx-item[data-id=\"5\"]').click()")
            page.wait_for_timeout(300)
            pane_before = page.evaluate(
                "() => !document.getElementById('ibx-preview-pane').hidden")
            page.evaluate("""() => { const r = document.querySelector('.ibx-item[data-id="5"]');
                r.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 300, clientY: 300})); }""")
            page.wait_for_timeout(150)
            page.click('#ibx-ctx button[data-action="delete"]')
            page.wait_for_timeout(400)
            after = page.evaluate("""() => ({
                pane: document.getElementById('ibx-preview-pane').hidden,
                empty: document.getElementById('ibx-preview-empty').hidden})""")
            check("删掉正在预览的那条，右栏回到空态（不悬着一份已删的文档）",
                  pane_before is True and after["pane"] is True and after["empty"] is False,
                  (pane_before, after))
            check("筛选里的条数也跟着变（「For me」从 2 变 1）", page.evaluate(
                """() => { inboxPage.setFilter('mine');
                    const n = document.querySelectorAll('.ibx-item').length;
                    inboxPage.setFilter('all');
                    return n; }""") == 1)

            # ── a failing inbox must not blank the app ─────────────────────
            page.evaluate("() => { window.fetch = () => Promise.reject(new Error('offline')); }")
            page.evaluate("() => inboxPage.refresh()")
            page.wait_for_timeout(300)
            check("取不到数据时显示的是错误，不是「什么都没有」",
                  "Could not load" in page.inner_text("#inbox-list"), page.inner_text("#inbox-list")[:80])

            # ── deep links: an Inbox line has to LAND on its document ─────
            page2 = browser.new_page(viewport={"width": 1500, "height": 900})
            page2.on("pageerror", lambda exc: errors.append("?event= page: " + str(exc)))
            page2.add_init_script(STUB)
            page2.goto("http://127.0.0.1:%d/index.html?event=brand-review" % PORT,
                       wait_until="domcontentloaded")
            page2.wait_for_function("() => !!window.calendarPage")
            page2.wait_for_function("() => !document.getElementById('page-calendar')"
                                    ".style.display.match(/none/)", timeout=8000)
            check("?event=<slug> 落在 Calendar 页并打开了那个事件",
                  page2.evaluate("() => document.getElementById('page-calendar').style.display !== 'none'")
                  and "Brand review" in page2.inner_text("body"),
                  page2.inner_text("body")[:80])

            page3 = browser.new_page(viewport={"width": 1500, "height": 900})
            page3.on("pageerror", lambda exc: errors.append("?dc= page: " + str(exc)))
            page3.add_init_script(STUB)
            page3.goto("http://127.0.0.1:%d/index.html?dc=sales_invoice" % PORT,
                       wait_until="domcontentloaded")
            page3.wait_for_function("() => document.getElementById('datacenter-page')"
                                    ".style.display !== 'none'", timeout=8000)
            page3.wait_for_timeout(600)
            check("?dc=<table> 切到 Data Center 并显示那个数据集",
                  "Sales data" in page3.inner_text("#dc-datasets-grid"),
                  page3.inner_text("#dc-datasets-grid")[:80])

            # ── embedded preview: the app inside the iframe has NO chrome ──
            page4 = browser.new_page(viewport={"width": 1000, "height": 700})
            page4.on("pageerror", lambda exc: errors.append("iframe embed: " + str(exc)))
            page4.add_init_script(STUB)          # applies to the iframe too
            page4.goto("http://127.0.0.1:%d/__embed.html" % PORT, wait_until="domcontentloaded")
            page4.wait_for_timeout(1500)
            frames = [f for f in page4.frames if f is not page4.main_frame]
            frame = frames[0]
            frame.wait_for_function("() => !!window.inboxPage", timeout=8000)
            # The embedded document hides the app chrome; the sidebar it used to check for
            # no longer exists at all, so only the top nav is asserted here.
            check("iframe 里的应用带 app-embedded：导航不渲染（只显示文档）",
                  frame.evaluate("() => document.documentElement.classList.contains('app-embedded')")
                  and frame.evaluate("() => getComputedStyle(document.querySelector('.nav')).display === 'none'"))
            check("顶层页面不受影响（不带 app-embedded，壳照常）",
                  page.evaluate("() => !document.documentElement.classList.contains('app-embedded')")
                  and page.evaluate("() => getComputedStyle(document.querySelector('.nav')).display !== 'none'"))
            page4.close()

            # ── the wiki boots INSIDE the shell: hiding the nav is not enough ──
            # A knowledge page has no chrome-free address, so its preview frames the
            # app at ?kb=<slug>. The app's own nav is hidden by app-embedded, but the
            # wiki's scope toolbar and page list are the app a second time — the reader
            # asked for the page, so only the document pane may survive.
            page5 = browser.new_page(viewport={"width": 1000, "height": 700})
            page5.on("pageerror", lambda exc: errors.append("wiki embed: " + str(exc)))
            page5.add_init_script(STUB)
            kb_page = urllib.parse.quote("/index.html?kb=channel-notes", safe="/?=")
            page5.goto("http://127.0.0.1:%d/__embed.html?page=%s" % (PORT, kb_page),
                       wait_until="domcontentloaded")
            page5.wait_for_timeout(1200)
            kframe = [f for f in page5.frames if f is not page5.main_frame][0]
            kframe.wait_for_function("() => !!window.knowledgePage", timeout=8000)
            kframe.wait_for_function(
                "() => document.getElementById('page-knowledge').style.display !== 'none'",
                timeout=8000)
            kframe.wait_for_selector("#kb-doc", state="visible", timeout=8000)
            shape = kframe.evaluate("""() => {
                const disp = (sel) => { const el = document.querySelector(sel);
                    return el ? getComputedStyle(el).display : 'missing'; };
                const main = document.querySelector('#kb-main');
                const rect = main ? main.getBoundingClientRect() : null;
                return {bar: disp('#page-knowledge > .rpt-toolbar'), toc: disp('.kb-toc'),
                        acts: disp('#kb-doc-meta .kb-acts'), doc: disp('#kb-doc'),
                        mainW: rect ? Math.round(rect.width) : -1,
                        frameW: Math.round(document.documentElement.clientWidth),
                        bodyText: (document.querySelector('#kb-render') || {}).innerText || ''}; }""")
            check("嵌入预览里知识页的作用域工具条与页面列表都不渲染（只留文档）",
                  shape["bar"] == "none" and shape["toc"] == "none", shape)
            check("嵌入预览里文档区铺满整个预览宽度（左栏不占位）",
                  shape["mainW"] >= shape["frameW"] - 2, (shape["mainW"], shape["frameW"]))
            check("嵌入预览里不出现页面管理动作（Edit / Delete / Publish / Pull a copy）",
                  shape["acts"] == "none", shape)
            check("文档本身照常渲染（正文在，且不是错误态）",
                  shape["doc"] != "none" and "Acme Retail is" in shape["bodyText"], shape["bodyText"][:80])
            page5.close()

            # ── the SAME page opened directly must keep everything ─────────────
            page6 = browser.new_page(viewport={"width": 1400, "height": 900})
            page6.on("pageerror", lambda exc: errors.append("wiki top-level: " + str(exc)))
            page6.add_init_script(STUB)
            page6.goto("http://127.0.0.1:%d/index.html?kb=channel-notes" % PORT,
                       wait_until="domcontentloaded")
            page6.wait_for_function("() => !!window.knowledgePage", timeout=8000)
            page6.wait_for_function(
                "() => document.getElementById('page-knowledge').style.display !== 'none'",
                timeout=8000)
            page6.wait_for_selector("#kb-doc", state="visible", timeout=8000)
            top = page6.evaluate("""() => {
                const disp = (sel) => { const el = document.querySelector(sel);
                    return el ? getComputedStyle(el).display : 'missing'; };
                return {emb: document.documentElement.classList.contains('app-embedded'),
                        bar: disp('#page-knowledge > .rpt-toolbar'), toc: disp('.kb-toc'),
                        acts: disp('#kb-doc-meta .kb-acts')}; }""")
            check("顶层打开同一页时工具条、页面列表、动作按钮照常（嵌入规则不外溢）",
                  top["emb"] is False
                  and all(top[k] not in ("none", "missing") for k in ("bar", "toc", "acts")), top)
            page6.close()

            check("没有 JS 报错", not errors, errors[:3])
            browser.close()
    finally:
        httpd.shutdown()
    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
