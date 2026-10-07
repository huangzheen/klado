"""Calendar UI — the grid, the bars, and the overlay.

What it pins down (each of these is a way a gantt calendar silently lies):

* **one row per month, one cell per day, 31 columns always** — the whole point of 31 is that
  day N sits at the same x in every row, so a bar in September lines up with September's
  weekdays in the row below. A 28/30/31-column grid looks fine and misleads;
* **the window opens on last month, four months deep** (last / this / next / the one after)
  — an event that has not finished yet is what people come back to, and four rows are sized to
  the viewport so they fill the page (with one month of overflow kept alive, so the wheel can
  scroll into the rest);
* **bars are packed into lanes**: two events sharing a day must not be drawn over each other,
  and one event keeps one lane for the whole month so its bar reads as one object;
* a bar's geometry matches its dates, not the row it happens to be in;
* **double-click opens the event's own 16:9 page** in the overlay, at the same address a
  shared link uses (`/e/<slug>`);
* the row menu and the hover card follow the wiki's anatomy, separator spacing included;
* **the overlay's export posts the page the browser rendered** (like the wiki's export), so the
  file carries the CURRENT slide only — not the hidden language variant, not the deck's other
  pages, not the page's script;
* **the editor never sends `body`** — the server keeps the stored 16:9 page when a field is
  absent, which is what makes editing a deadline safe;
* **a reader is offered none of it**: no Edit, no Cover, and the panel does not open;
* **an error document is never exported**: if the viewer got a 401/404 body instead of the
  page, the export refuses (and says so) rather than shipping `{"detail": …}` as the file.

Needs playwright (in `.venv312`) and a local Chrome.

    * **the editor beside the page** — the to-do rows are the one place a page and the panel can
  disagree. A page that writes its own `<li>`s shows lines the event does not know about, so
  the editor reads them back (`page_todos`) and shows them as rows; saving then stores them
  as to-dos instead of silently replacing lines the owner was never shown (2026-09-30).
  The row's own widths are measured too: the panel-wide `.cal-fld input { width:100% }` rule
  out-ranked them and crushed the owner field to ~18px — an "empty square" that read as "a
  to-do has no responsible field";

    .venv312/bin/python api/tests/verify_calendar_ui.py
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
# ⚠️ Pick a port nothing else forwards. This suite used 8797, which OrbStack forwards on some
# dev machines — the bind then fails with EADDRINUSE before a single assertion runs, and it
# reads as "the suite is broken" rather than "the port is taken". Current neighbours:
# 8791 verify_admin_agent_mail_ui · 8795 verify_knowledge_wiki_ui ·
# 8796 verify_report_bilingual_summary_ui · 8801 verify_workspace_scopes_ui ·
# 8802 verify_inbox_ui · 8803 verify_shell_nav_ui · 8804 verify_sidebar_ui +
# verify_calendar_channels_ui · 8806 verify_monthly_roadmap_ui + verify_nav_ticker_ui ·
# 8808 verify_colleague_picker_ui · 8821 verify_event_page_template_ui ·
# 8825 verify_dialog_ui · 8837 verify_event_todo_ui.
PORT = 8810


def _iso(y, m, d):
    return "%04d-%02d-%02d" % (y, m, d)


def _shift(months):
    """The same 'last month … +4' window the page computes."""
    today = date.today()
    index = today.year * 12 + (today.month - 1) + months
    return index // 12, index % 12 + 1


Y0, M0 = _shift(-1)          # the first row
Y1, M1 = _shift(0)
Y2, M2 = _shift(1)           # the third row of the default window
Y3, M3 = _shift(2)           # the fourth
EVENT_START = _iso(Y0, M0, 5)      # q3-review's own dates, asserted against the form
EVENT_DEADLINE = _iso(Y0, M0, 8)

EVENTS = [
    {"slug": "q3-review", "title": "Q3 渠道复盘会", "start_date": _iso(Y0, M0, 5),
     "end_date": _iso(Y0, M0, 9), "deadline": _iso(Y0, M0, 8), "kind": "review",
     "status": "published", "visibility": "private", "owner_email": "owner@example.com",
     "can_manage": True, "category": "渠道", "tags": ["q3"],
     "summary_en": "Q3 channel review in one line.", "summary_zh": "Q3 渠道复盘，一句话。",
     "summary": "", "partners": ["mate@example.com", "other@example.com"],
     "attachments": [{"kind": "report", "slug": "q3-deck", "title": "Q3 Deck"}],
     "created_at": "", "updated_at": "2026-09-29T09:00:00",
     # No to-dos of its own, and a page that writes them by hand: exactly the shape the
     # editor used to be blind to (see `own-todos` below).
     "todos": [], "page_todos": ["Alpha line", "Beta line"],
     # Same story for the project description: the field is empty, the PAGE carries the prose,
     # and `page_desc_*` is what lets the panel show the reader's text instead of an empty box
     # (the field itself did not exist until 2026-09-30 — that was the whole complaint).
     "description_en": "", "description_zh": "",
     "page_desc_en": "The page writes this by hand today.",
     "page_desc_zh": "这段文字目前写在页面里。",
     # A cover, shaped exactly as `_shape` reports one: the list carries the metadata and the
     # bytes come from /api/calendar/events/<slug>/cover.
     "has_cover": True, "cover_w": 1280, "cover_h": 720,
     "cover_url": "/api/calendar/events/q3-review/cover"},
    # Starts on the same days as the review: it must land in a DIFFERENT lane.
    {"slug": "launch-sync", "title": "上市同步", "start_date": _iso(Y0, M0, 7),
     "end_date": _iso(Y0, M0, 11), "deadline": "", "kind": "launch",
     "status": "published", "visibility": "private", "owner_email": "owner@example.com",
     "can_manage": True, "category": "产品", "tags": [],
     "summary_en": "Launch sync.", "summary_zh": "上市同步。", "summary": "",
     "partners": [], "attachments": [], "created_at": "", "updated_at": ""},
    # A milestone is a POINT: no bar, a diamond, and the title beside it.
    {"slug": "cd90-launch", "title": "CD90 上市", "start_date": _iso(Y1, M1, 8),
     "end_date": _iso(Y1, M1, 8), "deadline": "", "kind": "milestone",
     "status": "published", "visibility": "private", "owner_email": "owner@example.com",
     "can_manage": True, "category": "产品", "tags": [],
     "summary_en": "Ship date.", "summary_zh": "上市日。", "summary": "",
     "partners": [], "attachments": [], "created_at": "", "updated_at": ""},
    # No overlap with either: it must share lane 0 with the review.
    {"slug": "later-review", "title": "月度回顾", "start_date": _iso(Y1, M1, 3),
     "end_date": _iso(Y1, M1, 3), "deadline": "", "kind": "event",
     "status": "published", "visibility": "private", "owner_email": "owner@example.com",
     "can_manage": True, "category": "", "tags": [],
     "summary_en": "Monthly check.", "summary_zh": "月度检查。", "summary": "",
     "partners": [], "attachments": [], "created_at": "", "updated_at": ""},
    # Only used to drive "the viewer got an error document" (see _Handler): it lives in the
    # fourth default month, where nothing else is asserted.
    {"slug": "denied-review", "title": "打不开的页面", "start_date": _iso(Y3, M3, 6),
     "end_date": _iso(Y3, M3, 6), "deadline": "", "kind": "event",
     "status": "published", "visibility": "private", "owner_email": "owner@example.com",
     "can_manage": True, "category": "", "tags": [],
     "summary_en": "Unreachable page.", "summary_zh": "打不开的页面。", "summary": "",
     "partners": [], "attachments": [], "created_at": "", "updated_at": ""},
    # Someone else's event, shared with me: readable, and that is all. It must offer no Edit
    # and no Cover — not a button that fails when pressed.
    {"slug": "shared-plan", "title": "别人的排期", "start_date": _iso(Y2, M2, 4),
     "end_date": _iso(Y2, M2, 6), "deadline": "", "kind": "event",
     "status": "published", "visibility": "private", "owner_email": "mate@example.com",
     "can_manage": False, "category": "", "tags": [],
     "summary_en": "Someone else's plan.", "summary_zh": "别人的排期。", "summary": "",
     "partners": ["owner@example.com"], "attachments": [], "created_at": "", "updated_at": "",
     # ⚠️ A due date that has NOT arrived, so the fixture renders the WHITE triangle. It was
     # added 2026-10-01 with that colour ("当没有到时间也没有完成的时候改成白色") — without a
     # future due in the window, the rule existed in the code and nowhere on the page, which
     # is exactly the kind of state a colour change can silently break.
     "todos": [{"text": "Not due yet", "assignee": "mate@example.com",
                "due": _iso(Y2, M2, 20), "done": False}]},
    # The two to-do shapes the editor has to tell apart (reported 2026-09-30):
    #
    #   * a page that HAND-WRITES its to-do list carries no `todos` of its own. The page still
    #     shows those lines, so the editor has to show them too (`page_todos`) — otherwise the
    #     owner adds one row and silently replaces lines they were never shown.
    #   * an event WITH its own to-dos shows those, and must not claim they came from a page.
    #
    # Dated far outside the default window on purpose: this one is only ever opened in the
    # editor, so it draws no bar and cannot disturb the grid assertions.
    {"slug": "own-todos", "title": "自己的待办", "start_date": _iso(Y3 + 1, 3, 4),
     "end_date": _iso(Y3 + 1, 3, 6), "deadline": "", "kind": "event",
     "status": "published", "visibility": "private", "owner_email": "owner@example.com",
     "can_manage": True, "category": "", "tags": [],
     "summary_en": "Own to-dos.", "summary_zh": "自己的待办。", "summary": "",
     "partners": [], "attachments": [], "created_at": "", "updated_at": "",
     "todos": [{"text": "Real to-do", "assignee": "mate@example.com",
                "due": _iso(Y3 + 1, 3, 9), "done": False}],
     "page_todos": []},
]

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


# `/e/<slug>` is assembled by the API from the event's stored 16:9 HTML. The export posts the
# page **the browser rendered**, so the test needs one: a two-slide deck (one slide visible,
# one not), a hidden language variant, and a script that must not survive into the file.
EVENT_PAGE = ('<!doctype html><html><body><div class="deck">'
              '<section class="slide is-current" data-lang="en"><h1>Q3 review</h1></section>'
              '<section class="slide" data-lang="zh"><h1>第 3 季度复盘</h1></section>'
              '</div><script>window.__page_script_ran = 1;</script></body></html>')


class _Handler(http.server.SimpleHTTPRequestHandler):
    """`frontend/out`, plus that stand-in event page."""

    def do_GET(self):
        if self.path.startswith("/e/denied"):
            # What a viewer got while `/e/` was missing from the middleware's identity list:
            # the API's 401 body rendered as the page. The export must refuse to ship it.
            payload = b'{"detail":"sign in to access Workspace"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/e/"):
            payload = EVENT_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, *args):        # the 404s for /api/* are expected and only noise
        pass


def serve():
    httpd = _Server(("127.0.0.1", PORT), functools.partial(_Handler, directory=ROOT))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


STUB = """
window.__sent = [];
window.__put = null;
window.__put_url = '';
window.__export = null;
window.__export_url = '';
window.__cover = '';
window.fetch = function (url, init) {
  const u = String(url);
  const method = ((init || {}).method || 'GET').toUpperCase();
  window.__sent.push(method + ' ' + u);
  const reply = (payload) => Promise.resolve({
    ok: true, status: 200, headers: {get: () => 'application/json'},
    json: () => Promise.resolve(payload), blob: () => Promise.resolve(new Blob(['exported']))});
  if (u.indexOf('/export/') >= 0) {
    window.__export_url = u;
    try { window.__export = JSON.parse(init.body); } catch (e) { window.__export = null; }
    return reply({});
  }
  if (u.indexOf('/cover') >= 0) {
    // Cover generation and removal: the event record comes back either way, exactly as the
    // API does it, so the header thumbnail is repainted from the response.
    window.__cover = method === 'DELETE' ? 'deleted' : 'generated';
    return reply(method === 'DELETE' ? COVERLESS : WITH_COVER);
  }
  // ⚠️ `indexOf` not a prefix test: these arrive through appAbsUrl, so the path can be
  // either relative or fully absolute depending on how the caller built it.
  if (u.indexOf('/api/auth/colleagues') >= 0) {
    // The to-do owner field is a colleague picker; without this the dropdown renders empty
    // and "I cannot pick a responsible" would look like the field's own fault.
    return reply({colleagues: [{email: 'mate@example.com', name: 'Mate Wang'}]});
  }
  if (u.indexOf('/api/reports') >= 0) return reply({items: PICK_REPORTS});
  if (u.indexOf('/api/knowledge/items') >= 0) return reply({items: PICK_PAGES});
  if (u.indexOf('/data-center/files/stage') >= 0) {
    window.__upload = init && init.body ? 'form-data' : '';
    return reply({object_name: 'calendar-attachments/20260929_120000_q3.pdf',
                  filename: 'q3.pdf', size_bytes: 12});
  }
  if (method === 'PUT') {
    window.__put_url = u;
    try { window.__put = JSON.parse(init.body); } catch (e) { window.__put = null; }
    return reply(WITH_COVER);
  }
  const single = u.match(/\\/api\\/calendar\\/events\\/([^?]+)$/);
  if (single) {
    const found = EVENTS.find((e) => e.slug === decodeURIComponent(single[1]));
    return reply(Object.assign({}, found, {body: '<div class="deck">x</div>'}));
  }
  return reply({events: EVENTS, count: EVENTS.length, scope: 'mine'});
};
const EVENTS = %s;
const WITH_COVER = Object.assign({}, EVENTS[0], {has_cover: true, cover_w: 1280, cover_h: 720,
  cover_url: '/api/calendar/events/q3-review/cover', updated_at: '2026-09-29T09:00:00'});
const COVERLESS = Object.assign({}, EVENTS[0], {has_cover: false, cover_url: ''});
const PICK_REPORTS = [{slug: 'q3-deck', title: 'Q3 Deck', updated_at: '2026-09-20'}];
const PICK_PAGES = [{slug: 'channel-notes', title: '渠道笔记', updated_at: '2026-09-19'}];
""" % json.dumps(EVENTS)


def stub_with(events):
    """The same fetch stub, pointed at a different set of events.

    `STUB` is one string whose only data input is `const EVENTS = %s;`, so swapping that line
    is enough to render a purpose-built set on its own page — which is how a case that would
    disturb the fixture's own expectations gets tested without touching them.
    """
    return STUB.replace("const EVENTS = %s;" % json.dumps(EVENTS),
                        "const EVENTS = %s;" % json.dumps(events), 1)


def main():
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.add_init_script(STUB)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.calendarPage")

            tab = page.evaluate("""() => {
              const tab = document.getElementById('nav-tab-calendar');
              if (!tab) return null;
              const img = tab.querySelector('img');
              const rect = tab.getBoundingClientRect();
              // The label must sit on the SAME line as the icon. `.workspace-mark` is
              // display:block, so a tab whose display is not inline-flex drops the icon
              // onto its own line and pushes the label down — that shipped once
              // (tab height 52.8px vs 34.0px for its neighbours, 23px apart).
              let labelTop = null;
              for (const node of tab.childNodes) {
                if (node.nodeType === 3 && node.nodeValue.trim()) {
                  const r = document.createRange();
                  r.selectNodeContents(node);
                  labelTop = +r.getBoundingClientRect().top.toFixed(1);
                }
              }
              const iconTop = img ? +img.getBoundingClientRect().top.toFixed(1) : null;
              const ws = document.getElementById('nav-tab-reports');
              const kb = document.getElementById('nav-tab-knowledge');
              return {label: (tab.textContent || '').trim(), in_nav: !!tab.closest('.nav-center'),
                      icon: img ? img.getAttribute('src') : '',
                      icon_w: img ? Math.round(img.getBoundingClientRect().width) : 0,
                      display: getComputedStyle(tab).display,
                      height: +rect.height.toFixed(1),
                      pair_heights: [ws, kb].filter(Boolean).map((t) => +t.getBoundingClientRect().height.toFixed(1)),
                      icon_top: iconTop, label_top: labelTop};
            }""")
            check("主导航栏里有 Calendar 入口", tab is not None)
            check("入口在主导航栏内、带自己的图标（22px）",
                  bool(tab) and tab["in_nav"] and tab["icon"] == "calendar-icon.png"
                  and tab["icon_w"] == 22, tab)
            # ⚠️ Regression guard: the icon and the label must share one line, and the tab
            # must be as tall as the other icon-bearing tabs.
            check("图标与文字在同一行（没有折成两行）",
                  bool(tab) and tab["icon_top"] is not None and tab["label_top"] is not None
                  and abs(tab["icon_top"] - tab["label_top"]) < 8, tab)
            check("tab 高度与相邻的图标 tab 一致（未被撑高）",
                  bool(tab) and tab["display"] == "flex" and tab["pair_heights"]
                  and all(abs(tab["height"] - h) < 1 for h in tab["pair_heights"]), tab)

            page.click("#nav-tab-calendar")
            page.wait_for_function(
                "() => getComputedStyle(document.getElementById('page-calendar')).display === 'flex'")
            page.wait_for_selector("#cal-grid .cal-month", state="visible")
            check("点入口后日历页显示并渲染出月份行", True)

            # ⚠️ Asked for 2026-09-30: "Calendar 的标题栏也参照 workspace 一样减少一行". The
            # shell's 44px breadcrumb row sits above the calendar's own toolbar (which already
            # opens with the area tabs), so it is a second title row — the same call the
            # Workspace / Knowledge / Inbox pages already made. Hidden, not absent: the
            # breadcrumb still says where you are for the pages that want it.
            chrome = page.evaluate("""() => {
              const shell = document.querySelector('.workspace');
              const bar = shell.querySelector('.workspace-bar');
              const tool = document.querySelector('#page-calendar .rpt-toolbar');
              const grid = document.getElementById('cal-wrap');
              return {compact: shell.classList.contains('workspace-page-compact'),
                      bar_display: bar ? getComputedStyle(bar).display : null,
                      bar_h: bar ? Math.round(bar.getBoundingClientRect().height) : null,
                      toolbar_top: tool ? Math.round(tool.getBoundingClientRect().top) : null,
                      toolbar_h: tool ? Math.round(tool.getBoundingClientRect().height) : null,
                      grid_top: grid ? Math.round(grid.getBoundingClientRect().top) : null,
                      breadcrumb: (document.getElementById('breadcrumb-text') || {}).textContent || ''};
            }""")
            check("日历页收起面包屑标题栏（与 Workspace 页同样处理）",
                  chrome["compact"] and chrome["bar_display"] == "none", chrome)
            check("★ 日历的内容确实上移了一行（工具栏紧贴顶部、不再是第二行标题）",
                  chrome["toolbar_top"] is not None and chrome["toolbar_top"] <= 56
                  and chrome["grid_top"] == chrome["toolbar_top"] + chrome["toolbar_h"],
                  chrome)

            # ── the grid ─────────────────────────────────────────────────────────
            grid = page.evaluate("""() => {
              const rows = Array.from(document.querySelectorAll('#cal-grid .cal-month'));
              const first = rows[0];
              return {
                rows: rows.length,
                labels: rows.map((r) => r.querySelector('.cal-mlabel b').textContent.trim()),
                cellsPerRow: rows.map((r) => r.querySelectorAll('.cal-cell').length),
                offPerRow: rows.map((r) => r.querySelectorAll('.cal-cell.off').length),
              };
            }""")
            check("是「每月一行」的排布", grid["rows"] >= 4, grid["rows"])
            # ⚠️ The first FOUR rows are the default window — this is the assertion that the
            # window is four months and starts on last month (the rest come from the auto-fill
            # that keeps the timeline scrollable).
            check("默认窗口是上个月起 4 个月（上月/当月/下月/下下月）",
                  grid["labels"][:4] == ["%04d-%02d" % (Y0, M0), "%04d-%02d" % (Y1, M1),
                                         "%04d-%02d" % (Y2, M2), "%04d-%02d" % (Y3, M3)],
                  grid["labels"][:5])
            check("第一行是上个月", grid["labels"][0] == "%04d-%02d" % (Y0, M0), grid["labels"][:3])
            # ⚠️ 31 columns for every month is what makes day N line up across rows.
            check("每行都是 31 格（短月用 off 格补齐）",
                  set(grid["cellsPerRow"]) == {31} and any(n > 0 for n in grid["offPerRow"]), grid)

            alignment = page.evaluate("""() => {
              const rows = Array.from(document.querySelectorAll('#cal-grid .cal-month'));
              const x = (row, day) => row.querySelectorAll('.cal-cell')[day - 1].getBoundingClientRect().left;
              return {a: Math.round(x(rows[0], 12)), b: Math.round(x(rows[1], 12)),
                      c: Math.round(x(rows[0], 1))};
            }""")
            check("同一天在不同月份行里 x 对齐（第 12 天）",
                  abs(alignment["a"] - alignment["b"]) <= 1 and alignment["a"] > alignment["c"], alignment)

            # ── the rows use the page, and the timeline therefore scrolls ────────
            viewport = page.evaluate("""() => {
              const wrap = document.getElementById('cal-wrap');
              const grid = document.getElementById('cal-grid');
              const rows = Array.from(grid.querySelectorAll('.cal-month'));
              return {wrap_h: Math.round(wrap.clientHeight), grid_h: Math.round(grid.scrollHeight),
                      row_h: rows.map((r) => Math.round(r.getBoundingClientRect().height)),
                      scrollable: grid.scrollHeight > wrap.clientHeight + 4, months: rows.length};
            }""")
            # A row that only wraps its own content leaves the bottom half of the page empty —
            # the rows are sized from the viewport instead (clamped, see rowHeightBase()).
            check("月份行按页面高度放大（不是只按内容撑开）",
                  bool(viewport["row_h"]) and min(viewport["row_h"]) >= 78, viewport["row_h"][:3])
            # Four rows instead of six is the whole point of the change: the same page height
            # spread over fewer rows, so each one is taller.
            check("默认只有 4 个月 ⇒ 每行明显更高（>= 170px）",
                  bool(viewport["row_h"]) and min(viewport["row_h"]) >= 170, viewport["row_h"][:4])
            # ⚠️ A window that EXACTLY fills the viewport cannot scroll, so the months behind
            # it are unreachable by the wheel — that is how this shipped. Auto-fill keeps one
            # month of overflow alive so scrolling always has somewhere to go.
            check("时间线可滚动（滚轮能滑到窗口之外的月份）",
                  viewport["scrollable"], {k: viewport[k] for k in ("grid_h", "wrap_h", "months")})

            # ── bars ─────────────────────────────────────────────────────────────
            bars = page.evaluate("""() => {
              const rows = Array.from(document.querySelectorAll('#cal-grid .cal-month'));
              const collect = (row) => Array.from(row.querySelectorAll('.cal-bar')).map((b) => {
                const r = b.getBoundingClientRect(), days = r.width;
                return {slug: b.dataset.slug, text: b.textContent.trim(),
                        top: Math.round(r.top - row.getBoundingClientRect().top),
                        left: b.style.left, width: b.style.width, day_width: days};
              });
              return {first: collect(rows[0]), second: collect(rows[1])};
            }""")
            first = {b["slug"]: b for b in bars["first"]}
            check("两个事件都画在第一行", set(first) == {"q3-review", "launch-sync"}, list(first))
            check("重叠的事件分到不同泳道（不叠在一起）",
                  first["q3-review"]["top"] != first["launch-sync"]["top"],
                  {k: v["top"] for k, v in first.items()})
            # day 5 → day 9 of 31 columns
            check("条的位置与日期一致（5→9 号 = 4/31 起、5/31 宽）",
                  first["q3-review"]["left"].startswith("12.903")
                  and first["q3-review"]["width"].startswith("16.129"), first["q3-review"])
            check("跨月/另一行的事件落在它自己的行里（第 2 行）",
                  sorted(b["slug"] for b in bars["second"]) == ["cd90-launch", "later-review"],
                  bars["second"])
            # ⚠️ Row-local on purpose: the lane block is centred per row now, so lane 0 sits
            # at a different y in a 1-lane row than in a 2-lane one — comparing lanes across
            # rows with different lane counts would be comparing row heights, not lanes.
            check("不重叠的事件共用同一泳道（同一行里两个条的 top 相同）",
                  len(bars["second"]) == 2 and len({b["top"] for b in bars["second"]}) == 1,
                  bars["second"])
            # The lane block is centred in the row, so with spare height a bar does not cling
            # to the day-number band at the top.
            check("泳道块在行内垂直居中（条不贴着顶部的日期行）",
                  min(v["top"] for v in first.values()) > 20, {k: v["top"] for k, v in first.items()})

            # ── the deadline is the same mark as a due date now: a vertical line ──
            # ⚠️ It used to be a red diamond pinned 9px into its own bar. Asked for 2026-10-01:
            # "我要的是甘特图里面的菱形换成竖线" — and the shape has to stay inside the band of
            # the bar that carries it, or a mark reads as floating over unrelated lanes.
            deadline_mark = page.evaluate("""() => {
              const row = document.querySelectorAll('#cal-grid .cal-month')[0];
              const bar = row.querySelector('.cal-bar[data-slug="q3-review"]');
              const mark = row.querySelector('.cal-ms-tri.is-deadline');
              if (!bar || !mark) return null;
              const b = bar.getBoundingClientRect(), m = mark.getBoundingClientRect();
              const st = getComputedStyle(mark);
              return {bar: [Math.round(b.top), Math.round(b.bottom)],
                      mark: [Math.round(m.top), Math.round(m.bottom)],
                      height: Math.round(m.height), width: +m.width.toFixed(2),
                      base_vs_its_bar: Math.round(m.bottom - b.bottom),
                      inside_bar: m.top >= b.top - 1 && m.bottom <= b.bottom + 1,
                      colour: st.backgroundColor,
                      pointer_events: st.pointerEvents, shape: st.clipPath.slice(0, 12)};
            }""")
            # ⚠️ Was a red diamond in the bar, then a 1px line, now a small triangle sitting on
            # the chart's bottom edge with the STATUS as its colour (asked for 2026-10-01).
            # ⚠️ 2026-10-01: the open state became WHITE ("那个三角，当没有到时间也没有完成的时候
            # 改成白色"), so this no longer names two colours — the rule is asserted below over
            # EVERY triangle on the page, which is a stronger statement than a colour list.
            check("★ deadline 是贴着它自己那条 bar 底边的三角，颜色是状态",
                  bool(deadline_mark)
                  and deadline_mark["height"] == 5
                  and abs(deadline_mark["width"] - 5.77) < 0.6
                  and deadline_mark["base_vs_its_bar"] == 0
                  and deadline_mark["inside_bar"] is True
                  and deadline_mark["shape"] == "polygon(50% "
                  and deadline_mark["pointer_events"] == "none"
                  # This event's deadline is in the past in the fixture, and a deadline has no
                  # "done", so it is red — never green, never white.
                  and deadline_mark["colour"] == "rgb(182, 47, 47)",
                  deadline_mark)

            # ── the triangle's colour IS its state, for every mark on the page ────
            states = page.evaluate("""() => {
              const open = getComputedStyle(document.documentElement);
              return [...document.querySelectorAll('#cal-grid .cal-ms-tri')].map((m) => ({
                cls: m.className.replace('cal-ms-tri', '').trim(),
                colour: getComputedStyle(m).backgroundColor,
                edge: getComputedStyle(m).filter,
              }));
            }""")
            GREEN, RED, WHITE = "rgb(31, 122, 74)", "rgb(182, 47, 47)", "rgb(255, 255, 255)"
            want = {"is-done": GREEN, "is-late": RED, "": WHITE,
                    "is-deadline": WHITE, "is-deadline is-late": RED}
            bad = [s for s in states if want.get(s["cls"]) != s["colour"]]
            check("★ 每个三角的颜色都等于它的状态（绿=已完成 / 红=已过期 / 白=未到期未完成）",
                  states and not bad, bad[:3] or {"checked": len(states)})
            check("★ 未到期未完成的三角是白色（演示数据里有未来的 due）",
                  any(s["colour"] == WHITE for s in states),
                  sorted({s["cls"] for s in states}))
            check("★ 白三角自带一道描边，否则它在浅色条上看不见（1.6:1）",
                  all("drop-shadow" in s["edge"] for s in states if s["colour"] == WHITE)
                  and all("drop-shadow" not in s["edge"]
                          for s in states if s["colour"] in (GREEN, RED)),
                  {s["cls"]: s["edge"] for s in states})

            # ── milestones are points, not one-day bars ──────────────────────────
            milestone = page.evaluate("""() => {
              const rows = Array.from(document.querySelectorAll('#cal-grid .cal-month'));
              const bar = rows[1].querySelector('.cal-bar.milestone');
              if (!bar) return null;
              const dot = bar.querySelector('.cal-ms-dot');
              const label = bar.querySelector('.cal-ms-label');
              return {slug: bar.dataset.slug, hasDot: !!dot, text: (label || {}).textContent || '',
                      width: bar.style.width, box: Math.round(bar.getBoundingClientRect().width),
                      dotW: dot ? Math.round(dot.getBoundingClientRect().width) : 0};
            }""")
            check("milestone 画成菱形（不是一天的条）",
                  bool(milestone) and milestone["hasDot"] and milestone["slug"] == "cd90-launch",
                  milestone)
            check("milestone 没有按天算的宽度（点是点，不是跨度）",
                  bool(milestone) and not milestone["width"] and milestone["box"] < 200, milestone)
            check("milestone 自带标题标签", bool(milestone) and milestone["text"] == "CD90 上市", milestone)
            page.dblclick('#cal-grid .cal-bar.milestone')
            page.wait_for_selector("#cal-modal:not([hidden])", state="visible")
            check("双击 milestone 也能打开事件页",
                  "/e/cd90-launch" in page.evaluate(
                      "() => document.getElementById('cal-frame').getAttribute('src')"))
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('cal-modal').hidden")

            # ── hover card and menu ──────────────────────────────────────────────
            page.hover('#cal-grid .cal-bar[data-slug="q3-review"]')
            page.wait_for_function(
                "() => getComputedStyle(document.getElementById('cal-tip')).opacity === '1'", timeout=4000)
            tip = page.evaluate("""() => {
              const t = document.getElementById('cal-tip');
              return {title: (t.querySelector('.cal-tip-t') || {}).textContent,
                      when: (t.querySelector('.cal-tip-d') || {}).textContent,
                      lines: Array.from(t.querySelectorAll('.cal-tip-s')).map((e) => e.textContent),
                      foot: (t.querySelector('.cal-tip-f') || {}).innerText || ''};
            }""")
            check("悬停卡片给出标题 / 排期 / 两行摘要 / 人数",
                  tip["title"] == "Q3 渠道复盘会" and "deadline" in tip["when"]
                  and tip["lines"] == ["Q3 channel review in one line.", "Q3 渠道复盘，一句话。"]
                  and "2 partners" in tip["foot"], tip)

            # ── where the card LANDS (reported 2026-09-30) ───────────────────────
            # "在底部的事件，悬停上去的时候 tooltip 会挡住" — the card was placed below the bar
            # and then clamped into the window, so for a bar near the bottom the clamp pushed it
            # UP over its own anchor (measured: a 145px card over a 20px bar, covering it whole).
            # ⚠️ Checked for EVERY bar: this is a placement algorithm, and a single fixture
            # position would only prove one branch of it.
            slugs = page.evaluate("""() => [...document.querySelectorAll('#cal-grid .cal-bar')]
              .map((b) => b.dataset.slug).filter(Boolean)""")
            placements = []
            for slug in slugs:
                page.hover('#cal-grid .cal-bar[data-slug="%s"]' % slug)
                page.wait_for_timeout(120)
                placements.append(page.evaluate("""(slug) => {
                  const tip = document.getElementById('cal-tip');
                  const bar = document.querySelector('#cal-grid .cal-bar[data-slug="' + slug + '"]');
                  if (!tip || !bar || !tip.classList.contains('on')) return {slug: slug, shown: false};
                  const t = tip.getBoundingClientRect(), b = bar.getBoundingClientRect();
                  return {slug: slug, shown: true,
                          covers: t.left < b.right && t.right > b.left
                                  && t.top < b.bottom && t.bottom > b.top,
                          inside: t.top >= -1 && t.bottom <= window.innerHeight + 1
                                  && t.left >= -1 && t.right <= window.innerWidth + 1,
                          bar: [Math.round(b.top), Math.round(b.bottom)],
                          tip: [Math.round(t.top), Math.round(t.bottom)],
                          window_h: window.innerHeight};
                }""", slug))
            bad_cover = [p for p in placements if not p["shown"] or p["covers"]]
            check("★ 悬停卡片永不盖住它描述的那条（窗口底部的事件也一样）",
                  len(placements) == len(slugs) and not bad_cover, bad_cover or placements)
            check("悬停卡片整个留在窗口内（贴边的条也不把卡片挤出去）",
                  all(p["shown"] and p["inside"] for p in placements),
                  [p for p in placements if p["shown"] and not p["inside"]])

            # ── the kind filter: tiles in the toolbar, all on by default ─────────
            # The user's ask (2026-09-30): "把所有的事件类型在标题栏里面显示，用 tile 的方式
            # 作为多选器，默认全选" — so it is one control per type, it starts as a no-op, and
            # it has to be a VIEW: filtering must not re-query the API.
            tiles = page.evaluate("""() => {
              const box = document.getElementById('cal-kinds');
              if (!box) return {exists: false};
              const all = [...box.querySelectorAll('.cal-kind')];
              return {exists: true,
                      labels: all.map((b) => b.textContent.trim()),
                      on: all.filter((b) => b.classList.contains('on')).map((b) => b.dataset.kind),
                      roles: all.map((b) => b.getAttribute('role') + ':' + b.getAttribute('aria-checked')),
                      borders: all.map((b) => getComputedStyle(b).borderTopColor),
                      fills: all.map((b) => getComputedStyle(b).getPropertyValue('--kind')),
                      inks: all.map((b) => getComputedStyle(b).getPropertyValue('--kind-ink')),
                      edges: all.map((b) => getComputedStyle(b).getPropertyValue('--kind-edge')),
                      tints: all.map((b) => getComputedStyle(b).backgroundColor)};
            }""")
            check("★ 标题栏里有每一种事件类型的 tile，且默认全选",
                  tiles["exists"] and tiles["labels"] == ['Meeting', 'Review', 'Launch', 'Promotion', 'Campaign', 'Media', 'Content', 'Offline', 'Training', 'Research', 'Planning', 'Other']
                  and tiles["on"] == ['meeting', 'review', 'launch', 'promotion', 'campaign', 'media', 'content', 'offline', 'training', 'research', 'planning', 'other'], tiles)
            check("tile 是开关语义（role=switch + aria-checked）",
                  all(r.startswith("switch:true") for r in tiles["roles"]), tiles["roles"])
            # ⚠️ Asserted against the inline `--kind-ink` the page wrote, not a colour list:
            # a hardcoded list of hexes says nothing about WIRING (which variable reached the
            # border) and has to be re-typed every time the palette moves. What matters is that
            # the ON tile draws the type's dark ink while its wash is the type's light fill.
            def _hex2rgb(value):
                return "rgb(%d, %d, %d)" % tuple(int(value.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

            def _lum(hex_value):
                parts = [int(hex_value.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
                parts = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4) for c in parts]
                return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

            # ⚠️ The tile's BORDER is the type's `edge` — the same colour as the bar's own 1px
            # edge — while the glyph and label are the type's `ink`. A legend whose border is
            # darker than the bars it stands for reads as a third colour.
            check("★ tile 的边框就是该类型 bar 的描边色，文字是深色 ink（12 组各不相同）",
                  tiles["borders"] == [_hex2rgb(v) for v in tiles["edges"]]
                  and len(set(tiles["fills"])) == 12 and len(set(tiles["inks"])) == 12
                  and all(_lum(i) < _lum(e) < _lum(f)
                          for i, e, f in zip(tiles["inks"], tiles["edges"], tiles["fills"])),
                  {"borders": tiles["borders"][:3], "edges": tiles["edges"][:3]})
            check("★ ON tile 的底色是该类型 fill 的淡洗（洗出来不是白的）",
                  len(set(tiles["tints"])) == 12
                  and all(t != "rgba(0, 0, 0, 0)" for t in tiles["tints"]),
                  tiles["tints"][:3])
            # ⚠️ The palette is deliberately muted ("换一套含蓄低调的彩色"), and the metric has
            # to be CHROMA — the spread between the strongest and weakest channel — not
            # `(max-min)/max`, which is a *ratio* and reads 0.50 for a dark olive that is
            # plainly not a loud colour. Chroma is what "how colourful is this" actually means.
            # ⚠️ 2026-10-01: measured on the BARS' fill, not on the tile's border. The tile's
            # border is now the type's dark `ink` (a 1px border in a light fill is invisible on
            # white), so reading it here would measure near-black and pass no matter how loud
            # the bars got. The bars are what the reader actually looks at.
            # ⚠️ 2026-10-06: the cap moved 0.30 → 0.50, and this is the one place a guard was
            # changed to match the requirement rather than the reverse. 0.30 encoded
            # "换一套含蓄低调的彩色" (asked for 2026-09-30) and the reader has now rejected
            # exactly that set ("这套色系看起来不令人愉悦"). What the guard is really for is
            # unchanged and still enforced: NO bar may be so chromatic that it reads as an
            # alert rather than as one of twelve types. The check that actually holds the
            # palette together is the pairwise ΔE below, and that one does not move.
            # ⚠️⚠️ REPLACED 2026-10-06, and the reason matters. This capped the fill's RGB
            # chroma at 0.50, which was the right guard for a palette of saturated blocks and
            # is now VACUOUS: a light tint cannot exceed it, so the check can no longer fail.
            # The replacement measures the thing the complaint was actually about — how far
            # the bar's body sits from the surface behind it. The rejected set sat at
            # 2.74-3.63:1 and read as a WALL OF COLOUR; a tint sits quietly under its label.
            #
            # The backdrop is resolved by walking up until a non-transparent background is
            # found, rather than assuming `#cal-grid` paints one: an assumption here would
            # compare against rgba(0,0,0,0) and report every bar as infinitely loud.
            loud = page.evaluate('''() => {
              const lin = (c) => { c /= 255;
                return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
              const L = (m) => 0.2126 * lin(m[0]) + 0.7152 * lin(m[1]) + 0.0722 * lin(m[2]);
              const cr = (a, b) => { const x = L(a), y = L(b);
                return +(((Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05))).toFixed(2); };
              const rgb = (s) => (s.match(/\\d+/g) || []).map(Number);
              const backdrop = (el) => {
                for (let n = el; n; n = n.parentElement) {
                  const c = getComputedStyle(n).backgroundColor;
                  if (c && !/rgba\\([^)]*,\\s*0\\s*\\)|transparent/.test(c)) return rgb(c);
                }
                return [255, 255, 255];
              };
              const page_bg = backdrop(document.getElementById('cal-grid'));
              return [...document.querySelectorAll('#cal-grid .cal-bar')]
                .filter((b) => !b.classList.contains('milestone'))
                .map((b) => ({slug: b.dataset.slug,
                              ratio: cr(rgb(getComputedStyle(b).backgroundColor), page_bg)}))
                .filter((x) => x.ratio > 1.8);
            }''')
            check("★ bar 的底色是淡染不是实块（在卡片上 ≤1.8:1，否则又变回一堵色墙）",
                  loud == [], loud)

            # The complementary half, and the one that replaced the ink complaint: with a
            # tint body the TEXT is what identifies the type, so the text has to be a colour
            # and not a grey. Measured on the rendered bars, in this theme.
            inks_painted = page.evaluate('''() => {
              const lin = (c) => { c /= 255;
                return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
              const oklab = (rgb) => {
                const r = lin(rgb[0]), g = lin(rgb[1]), b = lin(rgb[2]);
                const l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b;
                const m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b;
                const s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b;
                const l_ = Math.cbrt(l), m_ = Math.cbrt(m), s_ = Math.cbrt(s);
                return [0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
                        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
                        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_];
              };
              const seen = new Map();
              document.querySelectorAll('#cal-grid .cal-bar').forEach((b) => {
                if (b.classList.contains('milestone') || seen.has(b.dataset.kind)) return;
                seen.set(b.dataset.kind, oklab(getComputedStyle(b).color.match(/\\d+/g).map(Number)));
              });
              // ⚠️ `other` is EXCLUDED and it must stay excluded: it is deliberately the
              // neutral that says "not one of the eleven", so its ink has no hue by design
              // and including it drags the median down for a reason that is not a defect.
              // This is the same exclusion `test_calendar_palette.py` makes, and it is the
              // difference between "the inks are grey" and "one of the inks is grey on
              // purpose" — a distinction the number cannot make on its own.
              const chromas = [];
              for (const [k, c] of seen) { if (k !== 'other') chromas.push(Math.hypot(c[1], c[2])); }
              chromas.sort((x, y) => x - y);
              const names = Array.from(seen.keys());
              let min = Infinity, pair = null;
              for (let i = 0; i < names.length; i += 1) {
                for (let j = i + 1; j < names.length; j += 1) {
                  const a = seen.get(names[i]), c = seen.get(names[j]);
                  const d = Math.hypot(a[0] - c[0], a[1] - c[1], a[2] - c[2]);
                  if (d < min) { min = d; pair = [names[i], names[j]]; }
                }
              }
              return {kinds: names.length,
                      realKinds: chromas.length,
                      medianChroma: +chromas[Math.floor(chromas.length / 2)].toFixed(4),
                      minDe: +min.toFixed(4), pair};
            }''')
            # ⚠️ 0.09, against the set that measured 0.018: twelve near-black inks inside
            # L 0.219-0.308. That is "不同甘特图底色上文本颜色一样" stated as a number, and it is
            # exactly what a saturated-looking palette still fails if the fill stays a block.
            # `kinds` counts the REAL types on screen, `other` excluded: the median is
            # meaningless over one or two values, and this fixture puts only a few up.
            check("★ bar 上的文字真的是颜色而不是黑灰色（中位彩度 ≥0.09，不含中性的 other）",
                  inks_painted["realKinds"] >= 2
                  and inks_painted["medianChroma"] >= 0.09, inks_painted)
            check("★ 同屏不同类型的文字两两可分（OKLab ΔE ≥0.035）",
                  inks_painted["minDe"] >= 0.035, inks_painted)

            # ⚠️⚠️ THE metric, added 2026-10-06 after "这套色系看起来不令人愉悦". Chroma and
            # contrast can both be perfect while the palette is unusable: the old set had a
            # closest pair of fills ΔE 0.016 apart in OKLab — meeting and research were the
            # SAME PAINT, and four types read as one grey-blue. No other check here could see
            # it, because all twelve were individually valid. What makes a categorical palette
            # work is the MINIMUM distance between any two of its members, not how pretty or
            # quiet each one is on its own.
            spread = page.evaluate('''() => {
              const lin = (c) => { c /= 255;
                return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
              const oklab = (rgb) => {
                const r = lin(rgb[0]), g = lin(rgb[1]), b = lin(rgb[2]);
                const l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b;
                const m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b;
                const s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b;
                const l_ = Math.cbrt(l), m_ = Math.cbrt(m), s_ = Math.cbrt(s);
                return [0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
                        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
                        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_];
              };
              const seen = new Map();
              document.querySelectorAll('#cal-grid .cal-bar').forEach((b) => {
                if (b.classList.contains('milestone')) return;
                const m = getComputedStyle(b).backgroundColor.match(/\\d+/g).map(Number);
                seen.set(b.dataset.kind, oklab(m));
              });
              const names = Array.from(seen.keys());
              let min = Infinity, pair = null;
              for (let i = 0; i < names.length; i += 1) {
                for (let j = i + 1; j < names.length; j += 1) {
                  const a = seen.get(names[i]), c = seen.get(names[j]);
                  const d = Math.hypot(a[0] - c[0], a[1] - c[1], a[2] - c[2]);
                  if (d < min) { min = d; pair = [names[i], names[j]]; }
                }
              }
              return {kinds: names.length, min: +min.toFixed(4), pair: pair};
            }''')
            # ⚠️ Keyed on KIND, not on bar. Two events of the same type MUST share a colour —
            # that is the palette working, not failing, and measuring per bar reported the
            # fixture's two `kind: "event"` rows as a ΔE of 0 and flagged the palette as
            # broken. This is also the weaker of the two ΔE checks, because the fixture only
            # puts five kinds on screen; `test_calendar_palette.py` covers all twelve.
            # ⚠️ The floor DROPPED from 0.035 to 0.015, and that is a change of requirement
            # rather than a guard loosened until it passed. Light tints are SUPPOSED to be
            # similar — that is what "quiet" means — and with a tint body the identity of a
            # type is carried by its TEXT, which is measured directly in the two checks above.
            # The floor that did not move is the one on the inks: that is the complaint that
            # was actually made, and it is now the stronger of the pair.
            check("★ 同屏不同类型的底色仍可分：最近的一对在 OKLab 里 ΔE ≥ 0.015",
                  spread["kinds"] >= 3 and spread["min"] >= 0.015, spread)

            # ── a light bar has to have an outline, or it dissolves into the page ──
            # ⚠️ This is the regression guard for the FIRST attempt at "改浅一点": the bars
            # kept their light fill but lost their edge, and a screenshot (not a number) showed
            # them melting into the white grid. The fill/white contrast of a light bar is only
            # ~1.9:1, so the 1px edge is what makes it a shape — assert the edge, and assert it
            # is strong enough to see.
            edges = page.evaluate("""() => [...document.querySelectorAll('#cal-grid .cal-bar')]
              .filter((b) => !b.classList.contains('milestone'))
              .map((b) => { const s = getComputedStyle(b);
                            return {slug: b.dataset.slug, width: s.borderTopWidth,
                                    edge: s.borderTopColor, fill: s.backgroundColor}; })""")
            def _rel_lum(rgb):
                parts = [int(x) / 255 for x in rgb.replace("rgb(", "").replace(")", "").split(",")[:3]]
                parts = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4) for c in parts]
                return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

            def _ratio(a, b):
                la, lb = _rel_lum(a), _rel_lum(b)
                return (max(la, lb) + .05) / (min(la, lb) + .05)

            weak = [e for e in edges if _ratio(e["edge"], "rgb(255, 255, 255)") < 2.5]
            noedge = [e for e in edges if e["width"] != "1px"]
            check("★ 每条 bar 都有 1px 同色系描边，且在白底上看得见（≥2.5:1）",
                  edges and not noedge and not weak,
                  {"no_edge": noedge[:2], "weak": [(w["slug"], round(_ratio(w["edge"], "rgb(255, 255, 255)"), 2)) for w in weak[:3]]})
            check("★ bar 的文字色比它的底更深（浅底上必须是深字）",
                  all(_rel_lum(e["fill"]) > _rel_lum(
                      page.evaluate("(s) => getComputedStyle(document.querySelector('#cal-grid .cal-bar[data-slug=\"' + s + '\"]')).color", e["slug"]))
                      for e in edges), edges[:2])

            seen_before = len(page.evaluate("() => window.__sent"))
            bars_before = page.evaluate("""() => ({n: document.querySelectorAll('#cal-grid .cal-bar').length,
              slugs: [...document.querySelectorAll('#cal-grid .cal-bar')].map((b) => b.dataset.slug)})""")
            page.click('#cal-kinds .cal-kind[data-kind="review"]')
            page.wait_for_timeout(250)
            off = page.evaluate("""() => ({
              n: document.querySelectorAll('#cal-grid .cal-bar').length,
              slugs: [...document.querySelectorAll('#cal-grid .cal-bar')].map((b) => b.dataset.slug),
              status: document.getElementById('cal-status').textContent,
              sent: window.__sent.length,
              review_on: document.querySelector('#cal-kinds .cal-kind[data-kind="review"]')
                          .classList.contains('on')})""")
            check("★ 关掉一个类型，它的条立刻消失 —— 而且没有重新请求接口",
                  off["n"] < bars_before["n"] and "q3-review" not in off["slugs"]
                  and off["sent"] == seen_before and not off["review_on"], off)
            check("其它类型的条没被牵连",
                  "launch-sync" in off["slugs"] and "cd90-launch" in off["slugs"], off["slugs"])
            check("状态行说明筛掉之后还剩几条（不是只报总数）",
                  "of" in off["status"] and "events" in off["status"], off["status"])

            page.click('#cal-kinds .cal-kind[data-kind="review"]')
            page.wait_for_timeout(250)
            back = page.evaluate("""() => ({
              slugs: [...document.querySelectorAll('#cal-grid .cal-bar')].map((b) => b.dataset.slug),
              on: document.querySelector('#cal-kinds .cal-kind[data-kind="review"]')
                    .classList.contains('on')})""")
            check("再点一次恢复（多选器是可逆的，不是一次性筛子）",
                  back["on"] and "q3-review" in back["slugs"]
                  and sorted(back["slugs"]) == sorted(bars_before["slugs"]), back)

            # ── two lane-packing faults the seeded stress data exposed (2026-09-30) ──────
            # Both were invisible from the fixture, which has no cross-month event and no two
            # milestones on adjacent days. Rendered from a purpose-built set on its own page so
            # the assertions above keep their fixture untouched.
            edge = [
                # ⚠️ 1. `dayOf()` reads the day out of the ISO string, so a bar that leaves the
                # month gave `end = 3` against `start = 25` — a negative width CSS drops, and the
                # bar silently became a content-sized chip at the wrong end of the month.
                {"slug": "edge-cross-in", "title": "Started last month", "kind": "event",
                 "status": "published", "start_date": _iso(Y0, M0, 28), "end_date": _iso(Y1, M1, 4),
                 "deadline": "", "summary_en": "Enters on the 1st.", "partners": [],
                 "todos": [], "can_manage": True},
                {"slug": "edge-cross-out", "title": "Ends next month", "kind": "launch",
                 "status": "published", "start_date": _iso(Y1, M1, 25), "end_date": _iso(Y2, M2, 3),
                 "deadline": "", "summary_en": "Leaves at the end.", "partners": [],
                 "todos": [], "can_manage": True},
                # ⚠️ 2. A milestone is a diamond PLUS a title beside it, so its box is wider than
                # its day; packed by day span alone, three consecutive ones landed in one lane
                # and printed over each other.
                {"slug": "edge-ms-a", "title": "MS on the 10th", "kind": "milestone",
                 "status": "published", "start_date": _iso(Y1, M1, 10), "end_date": "",
                 "deadline": "", "summary_en": "A point.", "partners": [], "todos": [],
                 "can_manage": True},
                {"slug": "edge-ms-b", "title": "MS on the 11th", "kind": "milestone",
                 "status": "published", "start_date": _iso(Y1, M1, 11), "end_date": "",
                 "deadline": "", "summary_en": "The day after.", "partners": [], "todos": [],
                 "can_manage": True},
                {"slug": "edge-ms-c", "title": "MS on the 12th", "kind": "milestone",
                 "status": "published", "start_date": _iso(Y1, M1, 12), "end_date": "",
                 "deadline": "", "summary_en": "And the day after that.", "partners": [],
                 "todos": [], "can_manage": True},
            ]
            edge_stub = stub_with(edge)
            edge_page = browser.new_page(viewport={"width": 1600, "height": 1000})
            edge_page.add_init_script(edge_stub)
            edge_page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            edge_page.wait_for_function("() => !!window.calendarPage")
            edge_page.click("#nav-tab-calendar")
            edge_page.wait_for_function(
                "() => getComputedStyle(document.getElementById('page-calendar'))"
                ".display === 'flex'")
            edge_page.wait_for_selector("#cal-grid .cal-month", state="visible")
            edge_page.wait_for_timeout(700)
            geom = edge_page.evaluate("""() => {
              const rows = [...document.querySelectorAll('#cal-grid .cal-month')];
              let overlaps = 0, pairs = [];
              const all = [];
              rows.forEach((m) => {
                const month = (m.querySelector('.cal-mlabel b') || {}).textContent || '';
                const days = m.querySelector('.cal-days').getBoundingClientRect();
                const dayW = days.width / 31;
                const bars = [...m.querySelectorAll('.cal-bar')].map((b) => {
                  const r = b.getBoundingClientRect();
                  return {month: month, slug: b.dataset.slug,
                          top: Math.round(r.top), bottom: Math.round(r.bottom),
                          left: Math.round(r.left), right: Math.round(r.right),
                          days: Math.round((r.width / dayW) * 10) / 10,
                          fromLeft: Math.round(((r.left - days.left) / dayW) * 10) / 10,
                          ms: b.className.indexOf('milestone') >= 0};
                });
                bars.forEach((b) => all.push(b));
                for (let i = 0; i < bars.length; i += 1) {
                  for (let j = i + 1; j < bars.length; j += 1) {
                    const a = bars[i], c = bars[j];
                    if (a.top < c.bottom && c.top < a.bottom && a.left < c.right && c.left < a.right) {
                      overlaps += 1; pairs.push([a.slug, c.slug]);
                    }
                  }
                }
              });
              // ⚠️ Per ROW, keyed by month: a bar that spans two months is drawn in both rows,
              // and a flat slug → bar map silently kept only the last one (which is how the
              // first version of this check measured the NEXT month's stub of the same event).
              return {rows: rows.length, overlaps: overlaps, pairs: pairs, all: all};
            }""")
            edge_page.close()
            this_month = _iso(Y1, M1, 1)[:7]
            in_row = [b for b in geom["all"] if b["month"] == this_month]
            cross_in = [b for b in in_row if b["slug"] == "edge-cross-in"]
            cross_out = [b for b in in_row if b["slug"] == "edge-cross-out"]
            ms_here = [b for b in in_row if b["slug"].startswith("edge-ms-")]
            check("★ 任何一个月行里，两条 bar 都不重叠（跨月、挤同一天的都不许压在一起）",
                  geom["overlaps"] == 0 and geom["rows"] > 0, geom["pairs"] or geom["all"][:3])
            check("★ 从上月开始、本月结束的条从第 1 天画起（不是从 28 号）",
                  len(cross_in) == 1 and cross_in[0]["fromLeft"] < 1.5
                  and cross_in[0]["days"] >= 4, cross_in)
            check("★ 本月开始、下月结束的条一直画到本月最后一天（不是缩成一个小块）",
                  len(cross_out) == 1 and cross_out[0]["fromLeft"] >= 24
                  and cross_out[0]["days"] >= 3, cross_out)
            check("★ 相邻三天的三个 milestone 各占一条泳道（标签不再互相盖住）",
                  len(ms_here) == 3 and len({m["top"] for m in ms_here}) == 3, ms_here)

            # ── a retired type must survive a save ─────────────────────────────────
            # ⚠️ `milestone` and `event` are no longer offered (2026-09-30: the milestone the
            # product wants lives INSIDE an event, as a to-do's `due`). But a `<select>` that
            # does not contain the value an event already has shows its FIRST option instead —
            # and Save sends what the dropdown shows. So opening the panel on an older event
            # and saving would silently rewrite its type. The retired value is therefore kept
            # as an option, labelled, and this asserts it round-trips.
            legacy_page = browser.new_page(viewport={"width": 1600, "height": 1000})
            legacy_page.add_init_script(stub_with([{
                "slug": "legacy-ms", "title": "Old milestone event", "kind": "milestone",
                "status": "published", "start_date": _iso(Y0, M0, 12), "end_date": "",
                "deadline": "", "summary_en": "Written before the type was retired.",
                "partners": [], "todos": [], "can_manage": True, "has_cover": False,
                "cover_url": "", "category": "", "tags": []}]))
            legacy_page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            legacy_page.wait_for_function("() => !!window.calendarPage")
            legacy_page.click("#nav-tab-calendar")
            legacy_page.wait_for_function(
                "() => getComputedStyle(document.getElementById('page-calendar'))"
                ".display === 'flex'")
            legacy_page.wait_for_selector("#cal-grid .cal-month", state="visible")
            legacy_page.wait_for_timeout(500)
            legacy_page.evaluate("() => window.calendarPage.openEvent('legacy-ms')")
            legacy_page.wait_for_function("() => !document.getElementById('cal-modal').hidden")
            legacy_page.evaluate("() => window.calendarPage.toggleEditor()")
            legacy_page.wait_for_function("() => !document.getElementById('cal-edit').hidden")
            legacy_page.wait_for_timeout(200)
            pick = legacy_page.evaluate("""() => {
              const sel = document.getElementById('cal-f-kind');
              return {value: sel.value,
                      options: [...sel.options].map((o) => o.value),
                      labels: [...sel.options].map((o) => o.textContent)};
            }""")
            check("★ 已退休的类型仍作为一个选项出现（不是被下拉框顶成第一个选项）",
                  pick["value"] == "milestone" and "milestone" in pick["options"]
                  and any("legacy" in l for l in pick["labels"]), pick)
            legacy_page.evaluate("() => { window.__put = null; }")
            legacy_page.click(".cal-edit-save")
            legacy_page.wait_for_function("() => !!window.__put", timeout=10000)
            check("★ 保存时原样送回该类型（不会被静默改成别的）",
                  legacy_page.evaluate("() => window.__put.kind") == "milestone",
                  legacy_page.evaluate("() => window.__put.kind"))
            legacy_page.close()

            page.evaluate("""() => {
              const bar = document.querySelector('#cal-grid .cal-bar[data-slug="q3-review"]');
              const box = bar.getBoundingClientRect();
              bar.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
                clientX: box.left + 8, clientY: box.top + 8}));
            }""")
            menu = page.evaluate("""() => {
              const m = document.getElementById('cal-ctx');
              return {shown: !m.hidden,
                      items: Array.from(m.querySelectorAll('button')).map((b) => b.textContent.trim()),
                      hrMargins: Array.from(m.querySelectorAll('hr')).map((h) =>
                        Math.round(parseFloat(getComputedStyle(h).marginTop)))};
            }""")
            # ⚠️ The overlay's own actions (edit / export / cover) live in this same menu,
            # so this expectation grows with them on purpose.
            check("右键菜单与 Workspace 的菜单同一套（含编辑 / 导出 / 封面）",
                  menu["items"] == ["Open", "Open in new tab", "Copy link", "Edit details…",
                                    "Partners…", "Export to Word", "Export to PDF",
                                    "Export to Picture", "Regenerate the cover",
                                    "Remove the cover", "Publish to public", "Delete"], menu["items"])
            check("菜单分隔线不撑空白（Tabler 的 hr 外边距已压掉）",
                  bool(menu["hrMargins"]) and all(m <= 8 for m in menu["hrMargins"]), menu["hrMargins"])

            # ── double-click opens the event's own page ──────────────────────────
            page.evaluate("() => document.getElementById('cal-ctx').hidden = true")
            page.dblclick('#cal-grid .cal-bar[data-slug="q3-review"]')
            page.wait_for_selector("#cal-modal:not([hidden])", state="visible")
            overlay = page.evaluate("""() => ({
              title: (document.getElementById('cal-modal-title') || {}).textContent,
              dates: (document.getElementById('cal-modal-dates') || {}).textContent,
              src: document.getElementById('cal-frame').getAttribute('src'),
              fetched: window.__sent.filter((u) => u.indexOf('/api/calendar/events/') >= 0),
            })""")
            check("双击打开事件页浮层", overlay["title"] == "Q3 渠道复盘会", overlay["title"])
            check("浮层用 /e/<slug> 这个地址（与分享链接同一个）",
                  "/e/q3-review" in overlay["src"], overlay["src"])
            check("浮层抬头给出排期 / 截止 / 参与人数",
                  "deadline" in overlay["dates"] and "partners" in overlay["dates"], overlay["dates"])

            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('cal-modal').hidden")
            check("Esc 关闭浮层", True)

            # ── the overlay's own controls: Edit / Export / Cover ────────────────
            page.evaluate("() => window.calendarPage.openEvent('q3-review')")
            page.wait_for_function("() => !document.getElementById('cal-modal').hidden")
            tools = page.evaluate("""() => {
              const shown = (id) => { const el = document.getElementById(id); return el ? !el.hidden : null; };
              const img = document.getElementById('cal-cover-thumb');
              return {edit: shown('cal-edit-btn'), cover: shown('cal-cover-btn'),
                      labels: Array.from(document.querySelectorAll('.cal-modal-tools button'))
                                .map((b) => b.textContent.trim()).filter(Boolean),
                      cover_src: img ? (img.getAttribute('src') || '') : ''};
            }""")
            check("浮层工具栏给所有者 Edit / Export / Cover",
                  bool(tools["edit"]) and bool(tools["cover"])
                  and "Edit" in tools["labels"] and "Export" in tools["labels"]
                  and "Cover" in tools["labels"], tools["labels"])
            check("有封面时抬头显示缩略图，指向该事件的 cover 端点",
                  "/api/calendar/events/q3-review/cover" in tools["cover_src"], tools["cover_src"])

            # ── export: the page the browser rendered, posted like the wiki's ─────
            page.evaluate("() => window.calendarPage.exportMenu({stopPropagation(){}, clientX: 60, clientY: 60})")
            export_items = page.evaluate(
                "() => Array.from(document.querySelectorAll('#cal-ctx button')).map((b) => b.dataset.action)")
            check("导出菜单给出三种格式",
                  export_items == ["export-word", "export-pdf", "export-picture"], export_items)
            page.click('#cal-ctx button[data-action="export-picture"]')
            page.wait_for_function("() => !!window.__export", timeout=10000)
            exported = page.evaluate("() => ({url: window.__export_url, payload: window.__export})")
            check("导出 POST 到 /events/<slug>/export/<kind>",
                  exported["url"].endswith("/api/calendar/events/q3-review/export/picture"),
                  exported["url"])
            html = (exported["payload"] or {}).get("html") or ""
            # ⚠️ A deck shows ONE slide at a time: exporting the body as-is would put every
            # page of the event into the file as if it were a single screen.
            check("导出只有当前那一页 slide，且脚本与其他语言都没跟过去",
                  "Q3 review" in html and "第 3 季度复盘" not in html
                  and "page_script_ran" not in html, html[:140])
            check("导出带上封面，且用的是相对地址（不会落到平台网关根）",
                  'src="api/calendar/events/q3-review/cover"' in html, html[:80])
            check("导出 meta 里有排期 / 截止 / 参与人",
                  "deadline" in exported["payload"]["meta"] and "@example.com" in exported["payload"]["meta"],
                  exported["payload"]["meta"])

            # ── an error document in the viewer must not become a "successful" export ──
            # ⚠️ This is the regression for the bug a user hit: `/e/<slug>` was missing from
            # the middleware's identity list, so the overlay rendered the 401 JSON *as the
            # page* — and the export happily posted that JSON.
            page.evaluate("() => window.calendarPage.openEvent('denied-review')")
            page.wait_for_timeout(500)
            page.evaluate("() => { window.__export = null; window.__export_url = ''; }")
            # Through the real path (toolbar menu → action), not by calling the internal
            # helper: the menu is what a user clicks.
            page.evaluate("() => window.calendarPage.exportMenu"
                          "({stopPropagation(){}, clientX: 60, clientY: 60})")
            page.click('#cal-ctx button[data-action="export-pdf"]')
            page.wait_for_function(
                "() => document.getElementById('cal-status').textContent.indexOf('error page') >= 0",
                timeout=8000)
            refused = page.evaluate("() => ({url: window.__export_url, payload: window.__export,"
                                    " status: document.getElementById('cal-status').textContent})")
            check("事件页是错误文档时导出直接报错，且**不发**导出请求",
                  refused["url"] == "" and refused["payload"] is None
                  and "Export failed" in refused["status"], refused["status"])
            page.evaluate("() => window.calendarPage.closeEvent()")

            # ── cover: generate / remove, the Workspace's generator ───────────────
            # (the previous block closed the overlay, and the toolbar menus belong to the
            # event that is open — so open it again)
            page.evaluate("() => window.calendarPage.openEvent('q3-review')")
            page.wait_for_timeout(400)
            page.evaluate("() => window.calendarPage.coverMenu({stopPropagation(){}, clientX: 60, clientY: 60})")
            cover_items = page.evaluate(
                "() => Array.from(document.querySelectorAll('#cal-ctx button')).map((b) => b.dataset.action)")
            check("有封面时封面菜单给出「重新生成 / 移除」",
                  cover_items == ["cover-generate", "cover-clear"], cover_items)
            page.click('#cal-ctx button[data-action="cover-generate"]')
            page.wait_for_function("() => window.__cover === 'generated'", timeout=10000)
            check("生成封面走该事件的 cover 端点",
                  any("/api/calendar/events/q3-review/cover" in s for s in
                      page.evaluate("() => window.__sent")), True)

            # ── the editor: the event's own fields, beside its own page ───────────
            page.click("#cal-edit-btn")
            page.wait_for_function("() => !document.getElementById('cal-edit').hidden")
            form = page.evaluate("""() => {
              const v = (id) => { const el = document.getElementById(id); return el ? el.value : null; };
              return {title: v('cal-f-title'), start: v('cal-f-start'), end: v('cal-f-end'),
                      deadline: v('cal-f-deadline'),
                      kind_options: Array.from((document.getElementById('cal-f-kind') || {}).options || [])
                                      .map((o) => o.value),
                      add: Array.from(document.querySelectorAll('.cal-attach-add button'))
                             .map((b) => b.textContent.trim()),
                      upload_input: !!document.getElementById('cal-f-file')};
            }""")
            check("Edit 打开右侧面板，字段按该事件预填",
                  form["title"] == "Q3 渠道复盘会" and form["start"] == EVENT_START
                  and form["deadline"] == EVENT_DEADLINE, form)
            check("可以上传文档，也可以从 Workspace / Knowledge base 里选",
                  form["upload_input"] and form["add"] == ["Upload a file", "From Workspace",
                                                           "From Knowledge base"], form["add"])

            page.click(".cal-attach-add button:nth-child(2)")
            page.wait_for_selector(".cal-pick-row", timeout=5000)
            picked = page.evaluate(
                "() => Array.from(document.querySelectorAll('.cal-pick-row b')).map((b) => b.textContent)")
            check("附件选择器列出来自 Workspace 的报告", picked == ["Q3 Deck"], picked)
            page.click(".cal-pick-row")
            page.click(".cal-attach-add button:nth-child(3)")
            page.wait_for_function(
                "() => { const r = document.querySelector('.cal-pick-row b');"
                " return !!r && r.textContent === '渠道笔记'; }", timeout=5000)
            page.click(".cal-pick-row")
            attached = page.evaluate(
                "() => Array.from(document.querySelectorAll('#cal-f-attach .cal-attach-row span'))"
                ".map((s) => s.getAttribute('title'))")
            check("report 与 knowledge 两份附件都进了列表",
                  attached == ["report: q3-deck", "knowledge: channel-notes"], attached)

            page.click(".cal-edit-save")
            page.wait_for_function("() => !!window.__put", timeout=10000)
            sent = page.evaluate("() => ({url: window.__put_url, payload: window.__put})")
            check("保存 PUT 到该事件", sent["url"].endswith("/api/calendar/events/q3-review"), sent["url"])
            # ⚠️ This is the whole reason PUT merges: without it the editor would have to
            # carry the event's 16:9 page, or destroy it by not carrying it.
            check("保存不带 body —— 服务端保留已存的页面",
                  bool(sent["payload"]) and "body" not in sent["payload"], sorted(sent["payload"] or {}))
            check("保存带上 partner / 附件 / 截止",
                  sent["payload"]["attachments"] == [
                      {"kind": "report", "slug": "q3-deck", "title": "Q3 Deck"},
                      {"kind": "knowledge", "slug": "channel-notes", "title": "渠道笔记"}]
                  and sent["payload"]["deadline"] == EVENT_DEADLINE, sent["payload"])

            # ── the to-do editor, and what the PAGE already shows ────────────────
            # Reported 2026-09-30, four separate faults behind one complaint ("the calendar's
            # event writer seems wrong"): the owner field was an 18px square nobody could type
            # into; a pick never reached the draft; those lines were invisible in the editor;
            # and saving one row therefore replaced lines the owner had never been shown.
            page.evaluate("() => { window.__put = null; }")
            page.evaluate("() => window.calendarPage.openEvent('q3-review')")
            page.wait_for_function("() => !document.getElementById('cal-modal').hidden")
            page.evaluate("() => window.calendarPage.toggleEditor()")
            page.wait_for_function("() => !document.getElementById('cal-edit').hidden")
            seed = page.evaluate("""() => {
              const rows = Array.from(document.querySelectorAll('#cal-f-todos .cal-todo-row'));
              const who = rows[0] && rows[0].querySelector('.cal-todo-who');
              const due = rows[0] && rows[0].querySelector('.cal-todo-due');
              const w = (el) => el ? Math.round(el.getBoundingClientRect().width) : 0;
              return {texts: rows.map((r) => r.querySelector('.cal-todo-text').value),
                      who: w(who), due: w(due),
                      whoPlaceholder: who ? who.placeholder : '',
                      note: Array.from(document.querySelectorAll('.cal-fld-note'))
                              .map((n) => n.textContent.trim())
                              .filter((t) => t.indexOf('from the page') >= 0)};
            }""")
            check("★ 页面手写的 to-do 行在编辑器里就是可编辑的行（不再是看不见的）",
                  seed["texts"] == ["Alpha line", "Beta line"], seed["texts"])
            # ⚠️ The panel-wide `.cal-fld input[type="text"|"date"] { width:100% }` rule is
            # (0,2,1); the to-do row's own rules were (0,1,0), so the date input took the whole
            # line and the owner field — the only one that shrinks — was crushed to ~18px.
            check("★ 负责人字段宽度可用（不是被日期框挤成小方块）",
                  seed["who"] >= 120 and seed["due"] == 132, seed)
            check("负责人字段说明自己要填什么", "Owner" in seed["whoPlaceholder"],
                  seed["whoPlaceholder"])
            check("面板明说这些行来自页面", len(seed["note"]) == 1, seed["note"])

            # ── the dates a to-do may carry (reported 2026-09-30) ────────────────
            # "在编辑事件的 milestone 的时候，所选日期需要做限制，我刚才成功选了一个在这个事件
            # 开始之前的日期" — a `due` is what the calendar draws as THIS event's milestone, so
            # one before the event starts cannot be drawn as anything.
            bounds = page.evaluate("""() => {
              const start = document.getElementById('cal-f-start');
              const end = document.getElementById('cal-f-end');
              const dues = [...document.querySelectorAll('#cal-f-todos .cal-todo-due')];
              return {start: start.value, end_min: end.getAttribute('min'),
                      due_mins: dues.map((d) => d.getAttribute('min')), due_count: dues.length};
            }""")
            check("★ to-do 的 due 下界 = 事件开始日（选择器里选不出更早的日期）",
                  bounds["due_count"] > 0 and bounds["start"]
                  and all(m == bounds["start"] for m in bounds["due_mins"]), bounds)
            check("结束日的下界 = 开始日（服务端会 400，别等提交才知道）",
                  bounds["end_min"] == bounds["start"], bounds)
            later = _iso(Y0, M0, 20)
            page.fill('#cal-f-start', later)
            page.wait_for_timeout(150)
            moved = page.evaluate("""() => ({
              end_min: document.getElementById('cal-f-end').getAttribute('min'),
              due_mins: [...document.querySelectorAll('#cal-f-todos .cal-todo-due')]
                          .map((d) => d.getAttribute('min'))})""")
            check("改了开始日，两个下界跟着走（不是只在打开面板那一刻算一次）",
                  moved["end_min"] == later
                  and all(m == later for m in moved["due_mins"]), moved)
            page.fill('#cal-f-start', bounds["start"])
            page.wait_for_timeout(150)

            # ⚠️ `min` is not enforcement: it does not stop typing, and an agent writing through
            # the API never sees the picker at all. This is the gate that actually holds — and
            # it must leave the edit intact (a silent drop would lose the rest of the save).
            page.evaluate("() => { window.__put = null; }")
            page.evaluate("""() => {
              const input = document.querySelector('#cal-f-todos .cal-todo-due');
              input.value = '2020-01-01';
              input.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
            page.click(".cal-edit-save")
            page.wait_for_timeout(500)
            refused = page.evaluate("""() => ({
              put: window.__put,
              status: document.getElementById('cal-status').textContent,
              focused: document.activeElement
                       ? (document.activeElement.className || document.activeElement.tagName) : ''})""")
            check("★ 手打一个开始之前的 due，保存被拦住并点名（不发 PUT）",
                  refused["put"] is None and "before this event" in refused["status"]
                  and "2020-01-01" in refused["status"], refused)
            check("拦住时焦点落在出问题的那一格（不用自己去找）",
                  "cal-todo-due" in refused["focused"], refused["focused"])
            page.evaluate("""() => {
              const input = document.querySelector('#cal-f-todos .cal-todo-due');
              input.value = '';
              input.dispatchEvent(new Event('change', {bubbles: true}));
            }""")

            # ── the project description is editable at all ───────────────────────
            # Reported 2026-09-30 while looking at the page: "这段文本在哪里可以编辑？" — the box
            # had no field anywhere in the app; it could only be authored into the page body.
            desc = page.evaluate("""() => {
              const en = document.getElementById('cal-f-desc-en');
              const zh = document.getElementById('cal-f-desc-zh');
              return {en: en ? en.value : null, zh: zh ? zh.value : null,
                      enTag: en ? en.tagName : null, zhTag: zh ? zh.tagName : null,
                      // A single-line input cannot wrap: a 4-line description was cut off.
                      wraps: en ? getComputedStyle(en).whiteSpace !== 'nowrap' : false,
                      // The panel says which text it adopted; key on the stable half of that
                      // sentence (it was shortened twice in the 2026-09-30 redesigns).
                      note: Array.from(document.querySelectorAll('.cal-fld-note'))
                              .filter((n) => n.textContent.indexOf('From the page') >= 0).length};
            }""")
            check("★ 描述有可编辑字段，且预填了页面上那段手写文本",
                  desc["en"] == "The page writes this by hand today."
                  and desc["zh"] == "这段文字目前写在页面里。", desc)
            check("描述是多行框（能换行，不是被截断的单行 input）",
                  desc["enTag"] == "TEXTAREA" and desc["zhTag"] == "TEXTAREA" and desc["wraps"], desc)
            check("面板说明这段文本目前写在页面里", desc["note"] == 1, desc)

            page.fill('#cal-f-todos .cal-todo-who', 'ma')
            page.wait_for_selector(".colleague-pop:not([hidden]) .cp-row", timeout=5000)
            check("输入两个字就给出注册同事", page.evaluate(
                "() => document.querySelector('.colleague-pop:not([hidden]) .cp-row').textContent"
                ".indexOf('mate@example.com') >= 0"))
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
            check("选中后字段里是地址，不是半截的输入",
                  page.evaluate("() => document.querySelector('#cal-f-todos .cal-todo-who').value")
                  == "mate@example.com",
                  page.evaluate("() => document.querySelector('#cal-f-todos .cal-todo-who').value"))
            # A pick writes the field PROGRAMMATICALLY, so no `input` event fires: without the
            # picker's `onChange` hook the draft keeps the half-typed text, and re-rendering the
            # rows (add/remove) repaints the field from that stale draft — undoing the pick.
            page.evaluate("() => window.calendarPage.addTodo()")
            page.evaluate("() => window.calendarPage.removeTodo(2)")
            page.wait_for_timeout(200)
            check("★ 行重渲染后选中的负责人还在（草稿跟着字段走）",
                  page.evaluate("() => document.querySelector('#cal-f-todos .cal-todo-who').value")
                  == "mate@example.com",
                  page.evaluate("() => document.querySelector('#cal-f-todos .cal-todo-who').value"))
            page.click(".cal-edit-save")
            page.wait_for_function("() => !!window.__put", timeout=10000)
            saved = page.evaluate("() => window.__put.todos")
            check("★ 保存把页面的行写成 to-dos 并带上刚选的负责人（不再静默丢行）",
                  [t["text"] for t in saved] == ["Alpha line", "Beta line"]
                  and saved[0]["assignee"] == "mate@example.com"
                  and saved[1]["assignee"] == "", saved)
            check("★ 保存也带上两个描述字段（页面上那段手写文本变成事件自己的描述）",
                  page.evaluate("() => window.__put.description_en")
                  == "The page writes this by hand today."
                  and page.evaluate("() => window.__put.description_zh")
                  == "这段文字目前写在页面里。",
                  page.evaluate("() => [window.__put.description_en, window.__put.description_zh]"))

            page.evaluate("() => window.calendarPage.openEvent('own-todos')")
            page.wait_for_function("() => document.getElementById('cal-modal-title')"
                                   ".textContent.indexOf('自己的待办') >= 0")
            page.evaluate("() => window.calendarPage.toggleEditor()")
            page.wait_for_function("() => !document.getElementById('cal-edit').hidden")
            own = page.evaluate("""() => ({
              texts: Array.from(document.querySelectorAll('#cal-f-todos .cal-todo-text')).map((i) => i.value),
              who: (document.querySelector('#cal-f-todos .cal-todo-who') || {}).value,
              note: Array.from(document.querySelectorAll('.cal-fld-note'))
                      .filter((n) => n.textContent.indexOf('from the page itself') >= 0).length})""")
            check("事件有自己的 to-do 时显示它自己的行，且不冒充页面的行",
                  own["texts"] == ["Real to-do"] and own["who"] == "mate@example.com"
                  and own["note"] == 0, own)
            page.evaluate("() => window.calendarPage.closeEvent()")

            # ── a reader gets none of it ─────────────────────────────────────────
            page.evaluate("() => window.calendarPage.openEvent('shared-plan')")
            page.wait_for_function(
                "() => document.getElementById('cal-modal-title').textContent.indexOf('排期') >= 0")
            reader = page.evaluate("""() => ({
              edit: document.getElementById('cal-edit-btn').hidden,
              cover: document.getElementById('cal-cover-btn').hidden,
              panel: document.getElementById('cal-edit').hidden})""")
            check("别人共享给我的事件：没有 Edit / Cover，面板也不开",
                  bool(reader["edit"]) and bool(reader["cover"]) and bool(reader["panel"]), reader)
            page.evaluate("() => window.calendarPage.toggleEditor()")
            check("读者点编辑也不开面板（不是点下去才报错）",
                  page.evaluate("() => document.getElementById('cal-edit').hidden"), True)
            page.evaluate("() => window.calendarPage.closeEvent()")

            # ── scrolling shows more months ──────────────────────────────────────
            before = page.evaluate("() => document.querySelectorAll('#cal-grid .cal-month').length")
            page.evaluate("""() => {
              const wrap = document.getElementById('cal-wrap');
              wrap.scrollTop = wrap.scrollHeight;
              wrap.dispatchEvent(new Event('scroll'));
            }""")
            page.wait_for_function(
                "() => document.querySelectorAll('#cal-grid .cal-month').length > %d" % before,
                timeout=5000)
            after = page.evaluate("() => document.querySelectorAll('#cal-grid .cal-month').length")
            check("滚到底部会加载更多月份", after > before, {before, after})

            check("整页没有 JS 报错", errors == [], errors[:3])
            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())