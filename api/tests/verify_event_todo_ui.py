"""The event to-do list: a line carries who and by-when, and the page shows it.

The user's request (2026-09-30) was four things at once, and they are one feature:

1. **「把左边一列全部用来放 to-do」** — the left column of the 16:9 event page is the to-do
   list and nothing else, so the list gets the whole column;
2. **「Summary 移到右边区」** — the project description moves to the right column, above the
   three cards;
3. **「每一行要有人名和 due date … check box 也要可编辑 … 可以在前端删除和添加」** — the
   list stopped being prose in the page and became a field (`{text, assignee, due, done}`),
   which is the only way a checkbox can survive a reload and a line can be edited;
4. **「每一条 to-do 就是一条 milestone」** — a line with a `due` is drawn on that day in the
   calendar.

What this suite refuses to take on trust:

* the description really is in the RIGHT column and does **not** overlap the first card —
  ⚠️ `.ev-desc` is authored inside `.ev-main` (§3.1) and moved by CSS, so "the stylesheet says
  so" is not evidence; the rectangles are;
* the to-do list really does FILL the left column (bottom edge == the column's);
* a page that still hard-codes its `<li>`s is untouched by the new slot;
* the editor's rows round-trip through the **PUT body** (a checkbox that only changes in the
  DOM is not "editable"), an empty row is dropped rather than refused, and removing a row
  removes it from the payload;
* the Partners box is the **colleague picker** — two letters must call
  `/api/auth/colleagues` and offer a row you can click, not just be a text field;
* the calendar draws one diamond per `due` date, inside its own strip at the bottom of the
  month row.

    .venv312/bin/python api/tests/verify_event_todo_ui.py
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.reports import _inject_base_tag, _inline_deck_runtime   # noqa: E402
from services.ai import calendar_page                                # noqa: E402
from services.ai.calendar_events import Attachment, CalendarEvent, Todo   # noqa: E402

PORT = 8837
FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


def _iso(y, m, d):
    return "%04d-%02d-%02d" % (y, m, d)


def _shift(months):
    today = date.today()
    index = today.year * 12 + (today.month - 1) + months
    return index // 12, index % 12 + 1


Y0, M0 = _shift(-1)     # the first row of the window
Y1, M1 = _shift(0)

DESC = ("Line up sell-in / sell-out / inventory for six channels, read YoY and MoM per channel, "
        "and attribute the outliers. The meeting ends with an owner and a date on every action.")

# The page the author writes: the doc's §3.1 skeleton, with the empty `data-ev="todo"` slot.
PAGE = f"""<!doctype html><html><head><title>event</title></head><body>
<div class="deck">
  <section class="slide ev-page" data-lang="en">
    <div class="ev-head">
      <span class="ev-kicker" data-ev="kicker"></span>
      <span class="ev-when" data-ev="schedule"></span>
    </div>
    <h1 class="ev-title" data-ev="title"></h1>
    <div class="ev-cols">
      <div class="ev-main">
        <p class="ev-desc">{DESC}</p>
        <h2 class="ev-block-title">To-do</h2>
        <ul class="ev-todo" data-ev="todo"></ul>
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

# A page that writes its own list: the new slot must not touch it.
PAGE_AUTHORED = ('<!doctype html><html><head></head><body><div class="deck">'
                 '<section class="slide ev-page" data-lang="en">'
                 '<div class="ev-cols"><div class="ev-main">'
                 '<ul class="ev-todo"><li>手写的这一条</li></ul>'
                 '</div><aside class="ev-side"></aside></div>'
                 '</section></div></body></html>')

# Dues chosen so one to-do lands in the FIRST month of the window and the other two also
# have a home; the third has no date at all and must therefore have no diamond anywhere.
TODOS = [
    Todo("Align sell-in / sell-out / inventory for 6 channels", "zhen.huang@example.com",
         _iso(Y0, M0, 6), False),
    Todo("Attribute any YoY anomaly channel", "li.ming@example.com", _iso(Y0, M0, 3), False),
    Todo("Assign action items to owners", "", "", True),
    # done AND dated: the one row that proves a finished line still gets a (hollow) diamond
    Todo("Sign off the action list", "other@example.com", _iso(Y0, M0, 8), True),
]

EVENT = CalendarEvent(
    id=1, slug="q3-review", title="Q3 渠道复盘会", kind="review", category="渠道",
    start_date=_iso(Y0, M0, 2), end_date=_iso(Y0, M0, 12), deadline=_iso(Y0, M0, 10),
    summary_en="Q3 channel review in one line.", summary_zh="Q3 渠道复盘，一句话。",
    partners=["mate@example.com", "other@example.com"],
    attachments=[Attachment("report", "q3-deck", "Q3 Deck")],
    todos=TODOS, body=PAGE, owner_email="owner@example.com", visibility="private",
    status="published")

# What the API hands the SPA (`_shape`): the SPA reads `todos` off this, and the editor's
# PUT must carry the same shape back.
EVENT_JSON = {
    "slug": "q3-review", "title": "Q3 渠道复盘会",
    "start_date": _iso(Y0, M0, 2), "end_date": _iso(Y0, M0, 12), "deadline": _iso(Y0, M0, 10),
    "kind": "review", "status": "published", "visibility": "private",
    "owner_email": "owner@example.com", "can_manage": True, "category": "渠道", "tags": ["q3"],
    "summary": "", "summary_en": "Q3 channel review in one line.",
    "summary_zh": "Q3 渠道复盘，一句话。",
    "partners": ["mate@example.com", "other@example.com"],
    "attachments": [{"kind": "report", "slug": "q3-deck", "title": "Q3 Deck"}],
    "todos": [{"text": t.text, "assignee": t.assignee, "due": t.due, "done": t.done} for t in TODOS],
    "has_cover": False, "cover_url": "", "created_at": "", "updated_at": "2026-09-30T09:00:00",
}

# A second event in the NEXT month with no to-dos at all: the to-do strip must not appear
# (and therefore must not shrink the bars) for months that have none.
PLAIN = {
    "slug": "no-todos", "title": "没有待办的事件", "start_date": _iso(Y1, M1, 4),
    "end_date": _iso(Y1, M1, 6), "deadline": "", "kind": "event", "status": "published",
    "visibility": "private", "owner_email": "owner@example.com", "can_manage": True,
    "category": "", "tags": [], "summary": "", "summary_en": "Plain.", "summary_zh": "普通。",
    "partners": [], "attachments": [], "todos": [],
    "has_cover": False, "cover_url": "", "created_at": "", "updated_at": "",
}


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


class _Handler(http.server.SimpleHTTPRequestHandler):
    """`frontend/out`, plus `/e/<slug>` assembled exactly the way `render_event` does it."""

    def do_GET(self):
        if self.path.startswith("/e/"):
            slug = self.path[3:].split("?")[0]
            event = EVENT if slug == "q3-review" else None
            if event is None:
                self.send_error(404)
                return
            body = _inline_deck_runtime(_inject_base_tag(event.body, "/"))
            body = calendar_page.render(body, event, base_href="/")
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def serve():
    httpd = _Server(("127.0.0.1", PORT), functools.partial(_Handler, directory=ROOT))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


STUB = """
window.__sent = [];
window.__put = null;
window.__put_url = '';
window.__colleagues_q = '';
window.fetch = function (url, init) {
  const u = String(url);
  const method = ((init || {}).method || 'GET').toUpperCase();
  window.__sent.push(method + ' ' + u);
  const reply = (payload) => Promise.resolve({
    ok: true, status: 200, headers: {get: () => 'application/json'},
    json: () => Promise.resolve(payload), blob: () => Promise.resolve(new Blob(['x']))});
  if (u.indexOf('/api/auth/colleagues') >= 0) {
    const m = u.match(/[?&]q=([^&]*)/);
    window.__colleagues_q = m ? decodeURIComponent(m[1]) : '';
    return reply({colleagues: [{email: 'zhen.huang@example.com', display_name: 'Zhen Huang',
                                role: 'admin'},
                               {email: 'li.ming@example.com', display_name: 'Li Ming',
                                role: 'member'}], domain: 'example.com'});
  }
  if (method === 'PUT') {
    window.__put_url = u;
    try { window.__put = JSON.parse(init.body); } catch (e) { window.__put = null; }
    return reply(EVENTS[0]);
  }
  const single = u.match(/\\/api\\/calendar\\/events\\/([^?]+)$/);
  if (single) {
    const found = EVENTS.find((e) => e.slug === decodeURIComponent(single[1]));
    return reply(Object.assign({}, found, {body: '<div class="deck">x</div>'}));
  }
  if (u.indexOf('/api/calendar/events') >= 0) {
    return reply({events: EVENTS, count: EVENTS.length, scope: 'mine'});
  }
  return reply({});
};
const EVENTS = %s;
""" % json.dumps([EVENT_JSON, PLAIN])


PAGE_PROBE = """() => {
  const box = (sel) => { const el = document.querySelector(sel); if (!el) return null;
    const r = el.getBoundingClientRect();
    return {left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right),
            bottom: Math.round(r.bottom), w: Math.round(r.width), h: Math.round(r.height)}; };
  return {
    desc: box('.ev-desc'), main: box('.ev-main'), side: box('.ev-side'),
    todo: box('.ev-todo'), firstCard: box('.ev-card'),
    rows: Array.from(document.querySelectorAll('.ev-todo li')).map((li) => ({
      cls: li.className, text: (li.querySelector('.ev-todo-text') || {}).textContent || li.textContent,
      who: (li.querySelector('.ev-todo-meta b') || {}).textContent || '',
      due: (li.querySelector('.ev-todo-meta time') || {}).textContent || '',
      tick_bg: getComputedStyle(li, '::before').backgroundColor,
      tick_border: getComputedStyle(li, '::before').borderTopColor,
      mark: getComputedStyle(li, '::after').content,
      mark_border: getComputedStyle(li, '::after').borderRightColor})),
    cards: Array.from(document.querySelectorAll('.ev-card')).map((c) => {
      const r = c.getBoundingClientRect();
      return {top: Math.round(r.top), h: Math.round(r.height), bottom: Math.round(r.bottom),
              border: getComputedStyle(c).borderTopWidth}; }),
    canvas: (() => { const p = document.querySelector('.ev-page'); if (!p) return null;
      const r = p.getBoundingClientRect();
      return {bottom: Math.round(r.bottom), h: Math.round(r.height)}; })(),
    side_bottom: document.querySelector('.ev-side')
      ? Math.round(document.querySelector('.ev-side').getBoundingClientRect().bottom) : null,
  };
}"""

EDITOR_PROBE = """() => {
  const rows = Array.from(document.querySelectorAll('#cal-f-todos .cal-todo-row'));
  return {
    count: rows.length,
    texts: rows.map((r) => r.querySelector('.cal-todo-text').value),
    owners: rows.map((r) => r.querySelector('.cal-todo-who').value),
    dues: rows.map((r) => r.querySelector('.cal-todo-due').value),
    done: rows.map((r) => r.querySelector('.cal-todo-done').checked),
    empty: !!document.querySelector('#cal-f-todos .cal-attach-empty'),
    partners_tag: (document.getElementById('cal-f-partners') || {}).tagName || '',
    partners_value: (document.getElementById('cal-f-partners') || {}).value || '',
  };
}"""


httpd = serve()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ── 1. the page: left column is the to-do list, the description is on the right ──
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(f"http://127.0.0.1:{PORT}/e/q3-review", wait_until="load")
        page.wait_for_timeout(600)
        page.evaluate("() => { window.__errors = []; }")
        view = page.evaluate(PAGE_PROBE)

        check("★ 左栏里只有 to-do，描述已经不在左栏",
              view["desc"] and view["side"] and view["desc"]["left"] >= view["side"]["left"]
              - 2 and view["desc"]["right"] <= 1600, view["desc"])
        check("★ 描述在右栏最上面，且与第一张卡不重叠",
              view["desc"] and view["firstCard"]
              and view["desc"]["bottom"] <= view["firstCard"]["top"],
              {"desc_bottom": view["desc"]["bottom"], "card_top": view["firstCard"]["top"]})
        check("★ to-do 列表填满整个左栏",
              view["todo"] and view["main"]
              and abs(view["todo"]["bottom"] - view["main"]["bottom"]) <= 2
              and view["todo"]["top"] - view["main"]["top"] < 60,
              {"todo": view["todo"], "main": view["main"]})
        check("服务端把每一条 to-do 都填进了空槽（不是靠作者写）",
              len(view["rows"]) == len(TODOS)
              and view["rows"][0]["text"].startswith("Align sell-in"),
              [r["text"][:24] for r in view["rows"]])
        check("★ 每一行都带人名和 due date",
              view["rows"][0]["who"] == "Zhen Huang" and view["rows"][0]["due"] == _iso(Y0, M0, 6)
              and view["rows"][1]["due"] == _iso(Y0, M0, 3), view["rows"][:2])
        check("没有 due 的那一行只写文字（不是空一截日期）",
              view["rows"][2]["due"] == "" and view["rows"][2]["text"].startswith("Assign"),
              view["rows"][2])
        # ⚠️ The mark used to be the checkbox filling solid teal (`::before` background). Since the
        # 2026-09-30 redesign it is a real TICK drawn in CSS on `::after` — a filled square read
        # as a colour swatch, not as "this line is done". So assert the tick, not the fill:
        # the done row's `::after` is generated with a teal border, and the open row has none.
        done_row, open_row = view["rows"][2], view["rows"][0]
        check("★ done 的那一行被标出来（打出对勾，空行没有）",
              "is-done" in done_row["cls"] and "is-done" not in open_row["cls"]
              and done_row["mark"] != "none"          # `content: ""` — generated, i.e. the tick exists
              and open_row["mark"] == "none"
              and done_row["mark_border"] != open_row["mark_border"]
              and done_row["tick_border"] != open_row["tick_border"],
              {"done": {k: done_row[k] for k in ("mark", "mark_border", "tick_border")},
               "open": {k: open_row[k] for k in ("mark", "mark_border", "tick_border")}})
        # ⚠️ Rewritten with the same pass: the three "cards" were filled rounded rectangles
        # stretched to equal heights so they filled the column exactly. They are now
        # content-sized spec entries with a hairline on top — the contract worth pinning is
        # that they read as ONE list (a rule each, stacked in order, inside the canvas), not
        # that they are three equal widgets.
        check("右侧三块事实条目：各带发丝线、依次下落、都在画布内",
              len(view["cards"]) == 3
              and all(c["border"] == "1px" for c in view["cards"])
              and all(b["top"] >= a["bottom"] - 1
                      for a, b in zip(view["cards"], view["cards"][1:]))
              and all(c["bottom"] <= view["canvas"]["bottom"] for c in view["cards"]),
              {"cards": view["cards"], "canvas": view["canvas"]})
        page.close()

        # A page that writes its own list keeps it.
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.route("**/e/authored", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body=_inline_deck_runtime(calendar_page.render(PAGE_AUTHORED, EVENT, base_href="/"))))
        page.goto(f"http://127.0.0.1:{PORT}/e/authored", wait_until="load")
        page.wait_for_timeout(400)
        authored = page.evaluate("() => Array.from(document.querySelectorAll('.ev-todo li'))"
                                 ".map((li) => li.textContent.trim())")
        # ⚠️ The one box where the event's field beats what the author wrote: without this,
        # an event published before `todos` existed would show its old prose for ever and the
        # owner's edit in the panel would appear to do nothing.
        check("★ 事件有 todos 时，页面里手写的待办会被事件自己的替换",
              authored and len(authored) == len(TODOS) and authored[0].startswith("Align sell-in"),
              authored)
        page.close()

        # ── 2. the editor ─────────────────────────────────────────────────────────
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.add_init_script(STUB)
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{PORT}/index.html", wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.calendarPage")
        page.click("#nav-tab-calendar")
        page.wait_for_selector("#cal-grid .cal-month", timeout=8000)
        page.wait_for_timeout(400)

        # ── 3. each due date is a small triangle on the chart's bottom edge ───────
        # ⚠️ Fourth iteration of this mark (2026-10-01): "把细线换成等边小三角。三角形的下面的
        # 边与甘特图的底边重合。高度是3px" + a STATUS colour. The history is worth keeping in
        # mind before changing it again — every earlier version was wrong in a different way:
        # one shared band at the bottom of the month (detached), a full-height line through the
        # row (crossed unrelated lanes), a line inside its own bar (correct, but the user wanted
        # triangles), and a lane-height line (2px taller than its bar).
        marks = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#cal-grid .cal-month')];
          const out = [];
          rows.forEach((m) => {
            const days = m.querySelector('.cal-days').getBoundingClientRect();
            m.querySelectorAll('.cal-ms-tri').forEach((t) => {
              const r = t.getBoundingClientRect(), st = getComputedStyle(t);
              const dayW = days.width / 31, cx = r.left + r.width / 2;
              const dayIndex = Math.round((cx - days.left) / dayW + 0.5);
              // ⚠️ Compared against ITS OWN BAR's bottom edge, not the chart's: putting every
              // mark on one rail along the bottom of the month detached a mark from an event
              // whose bar sat in lane 0 of a thirteen-lane row ("这个小三角又脱离事件的甘特图了").
              const bars = [...m.querySelectorAll('.cal-bar[data-slug="' + t.dataset.slug + '"]')]
                .map((x) => x.getBoundingClientRect())
                .sort((a, b) => Math.abs(a.bottom - r.bottom) - Math.abs(b.bottom - r.bottom));
              const bar = bars[0] || null;
              out.push({slug: t.dataset.slug, cls: t.className,
                        colour: st.backgroundColor, width: +r.width.toFixed(2),
                        height: +r.height.toFixed(2), pe: st.pointerEvents,
                        base_vs_its_bar: bar ? Math.round(r.bottom - bar.bottom) : null,
                        inside_own_bar: bar ? r.top >= bar.top - 1 && r.bottom <= bar.bottom + 1 : null,
                        centred: Math.abs(cx - (days.left + (dayIndex - 0.5) * dayW)) < 1.3});
            });
          });
          return out;
        }""")
        dues = [m for m in marks if "is-deadline" not in m["cls"]]
        deadlines = [m for m in marks if "is-deadline" in m["cls"]]
        check("★ 每条带 due 的 to-do 都是一个三角，且标着它所属的事件",
              len(dues) == len([t for t in TODOS if t.due]) and all(m["slug"] for m in dues), dues)
        check("★ 三角形是等边的（高 5px、底 5×2/√3≈5.77px）",
              all(abs(m["height"] - 5) < 0.6 and abs(m["width"] - 5.77) < 0.6 for m in marks),
              [{"w": m["width"], "h": m["height"]} for m in marks])
        # ⚠️ "三角形的下面的边与甘特图的底边重合" — and that bottom edge is the one belonging to
        # the event's OWN bar. Measured against the bar, never against a constant: a constant is
        # what put the base 3px below the line it was meant to sit on.
        check("★ 三角形的底边压在它自己那条 bar 的底边上，且整个在 bar 内",
              all(m["base_vs_its_bar"] == 0 and m["inside_own_bar"] is True for m in marks),
              [(m["slug"], m["base_vs_its_bar"], m["inside_own_bar"]) for m in marks])
        check("★ 三角形按它那一天的列居中（不是一个边贴在日期上）",
              all(m["centred"] for m in marks), marks)
        check("★ 三角形不吃鼠标事件（否则会挡住条的点击/双击）",
              all(m["pe"] == "none" for m in marks), [m["pe"] for m in marks])
        check("★ 不再有任何竖线（细线已经被三角取代）",
              page.evaluate("() => document.querySelectorAll('.cal-ms-line').length") == 0)
        # ⚠️ 2026-10-01: the open state became WHITE ("那个三角，当没有到时间也没有完成的时候改成
        # 白色"), so this maps every mark's colour to its state instead of naming two colours —
        # and the mapping is checked on the mark's own classes, which is the rule itself.
        want = {"is-done": "rgb(31, 122, 74)", "is-late": "rgb(182, 47, 47)",
                "": "rgb(255, 255, 255)"}
        check("状态色：已完成=绿 / 已过期=红 / 未到期未完成=白（不是类型色，也不是黄色）",
              all(want.get(m["cls"].replace("cal-ms-tri", "").strip()) == m["colour"] for m in dues),
              [(m["cls"], m["colour"]) for m in dues])
        check("事件的 deadline 也是同一个三角，红色表示已过期",
              len(deadlines) == 1 and deadlines[0]["height"] == 5
              and deadlines[0]["colour"] == "rgb(182, 47, 47)",
              deadlines)

        # open the editor
        page.evaluate("() => window.calendarPage.openEvent('q3-review')")
        page.wait_for_timeout(400)
        page.click("#cal-edit-btn")
        page.wait_for_selector("#cal-f-todos .cal-todo-row", timeout=4000)
        form = page.evaluate(EDITOR_PROBE)
        check("Event details 里有 To-do 区，按该事件预填",
              form["count"] == len(TODOS) and form["texts"][0].startswith("Align sell-in")
              and form["owners"][0] == "zhen.huang@example.com"
              and form["dues"][0] == _iso(Y0, M0, 6) and form["done"][2] is True, form)
        check("★ Partners 换成了同事选择器（是 input，不是 textarea）",
              form["partners_tag"] == "INPUT"
              and form["partners_value"] == "mate@example.com, other@example.com",
              [form["partners_tag"], form["partners_value"]])

        # typing two letters must call the API and offer a clickable row
        page.fill("#cal-f-partners", "")
        page.type("#cal-f-partners", "zh")
        page.wait_for_selector(".colleague-pop .cp-row", timeout=4000)
        offered = page.evaluate("() => Array.from(document.querySelectorAll('.colleague-pop .cp-row'))"
                                ".map((r) => r.textContent.trim())")
        check("★ 打两个字母就弹邮箱（走 /api/auth/colleagues）",
              page.evaluate("() => window.__colleagues_q") == "zh"
              and any("zhen.huang@example.com" in row for row in offered), offered)
        page.click(".colleague-pop .cp-row")
        page.wait_for_timeout(200)
        check("点一条就把邮箱写进输入框",
              "zhen.huang@example.com" in page.evaluate("() => document.getElementById('cal-f-partners').value"),
              page.evaluate("() => document.getElementById('cal-f-partners').value"))

        # ── 4. the editor really writes the list ──────────────────────────────────
        page.check("#cal-f-todos .cal-todo-row[data-index='0'] .cal-todo-done")
        page.fill("#cal-f-todos .cal-todo-row[data-index='0'] .cal-todo-text", "对齐三个口径")
        page.fill("#cal-f-todos .cal-todo-row[data-index='1'] .cal-todo-due", _iso(Y0, M0, 9))
        page.click("text=Add to-do")
        page.wait_for_timeout(150)
        check("Add to-do 加了一行，且新行是空的",
              page.evaluate("() => document.querySelectorAll('#cal-f-todos .cal-todo-row').length")
              == len(TODOS) + 1,
              page.evaluate("() => document.querySelectorAll('#cal-f-todos .cal-todo-row').length"))
        page.click("#cal-f-todos .cal-todo-row[data-index='2'] .cal-todo-del")
        page.wait_for_timeout(150)
        after_remove = page.evaluate(EDITOR_PROBE)
        # Index 2 was the "Assign action items" row; the empty one just added takes its
        # place at the end. Removing must NOT renumber anyone else.
        check("删掉一行后，行序不乱、也没把别的行删掉",
              after_remove["count"] == len(TODOS)
              and after_remove["texts"][0] == "对齐三个口径"
              and after_remove["texts"][1].startswith("Attribute")
              and after_remove["texts"][2] == "Sign off the action list"
              and after_remove["texts"][3] == ""
              and after_remove["done"][3] is False,
              after_remove["texts"])

        page.click(".cal-edit-save")
        page.wait_for_timeout(400)
        sent = page.evaluate("() => window.__put")
        # ⚠️ 3 rows in the panel, 2 in the payload: the row that was added and never typed
        # into is dropped (a save must not fail over it) — `store.parse_todos` says the same.
        check("★ 保存把 to-do 发进了 PUT 请求体，没写字的那一行被丢掉",
              sent and len(sent.get("todos") or []) == len(TODOS) - 1
              and all(t["text"].strip() for t in sent["todos"]), (sent or {}).get("todos"))
        check("★ 勾选/文字/日期/负责人都真的进了请求体",
              sent and sent["todos"][0]["done"] is True
              and sent["todos"][0]["text"] == "对齐三个口径"
              and sent["todos"][1]["due"] == _iso(Y0, M0, 9)
              and sent["todos"][0]["assignee"] == "zhen.huang@example.com",
              sent and sent["todos"][:2])
        check("★ 选择器里的 Partners 进了请求体（不是原始文本框内容）",
              sent and sent["partners"] == ["zhen.huang@example.com"], sent and sent["partners"])
        check("★ 保存仍然不发 body（不能毁掉事件的 16:9 页面）",
              sent is not None and "body" not in sent, sorted((sent or {}).keys()))
        check("整页没有 JS 报错", errors == [], errors[:2])
        page.close()
        browser.close()
finally:
    httpd.shutdown()

print("\n%d checks failed" % len(FAILURES))
raise SystemExit(1 if FAILURES else 0)