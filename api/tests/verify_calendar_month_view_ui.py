"""Calendar — the MONTH view (seven weekday columns, one row per week).

The timeline is one row per month with 31 day columns and a bar per event; this suite pins
down the other shape: one month at a time, laid out in whole weeks. Each of the ways a month
grid lies is a case here:

* **it is really 7 columns, Sunday first** — the same first-day-of-week the timeline's
  weekend shading already assumes, so Saturday is the same column in both views;
* **a month has exactly the weeks it needs** — five, or six when the month does not fit in
  five. The sweep at the end walks a year of months and checks every one against the count
  computed independently here, and asserts it saw BOTH a five-row and a six-row month: a
  grid hardcoded to five rows is exactly the bug this catches, and it only shows up in the
  months that happen to need six;
* **an event lands on the day it says** — measured, not read. For every event the suite
  works out which day cells its bars actually overlap and compares that set against the days
  the event's own dates say it should cover. One assertion catches a wrong column, a
  day off by one, a month-boundary clip that did not clip, a piece that failed to wrap into
  the next week, and a bar drawn in a lane that belongs to somebody else;
* **two events on one day do not overlap** — lane packing, again measured;
* **the kind filter reaches the grid** — a hidden type takes its bars AND its to-do marks
  with it, and the events that are still on prove the filter emptied the month rather than
  the whole page (a negative assertion with nothing counting the survivors is worthless);
* **a retired `milestone` is still a point** — it keeps its diamond instead of becoming a
  filled one-day bar indistinguishable from a one-day task;
* **the to-do and deadline marks land on their own days**, carrying the same green / red /
  white states the timeline gives them;
* **the overlay, the hover card and the right-click menu still work** — the grid reuses
  `.cal-bar`, so the wiring is shared, and that is exactly the thing worth pinning;
* **it is a VIEW** — switching refetches only the date range the new view needs, and the
  timeline's rows come back untouched when you switch back;
* **every colour is a token**, checked in both themes: a hex literal in the stylesheet is
  right in one theme and wrong in the other, and nothing about the rendering fails loudly.

Needs playwright (in `.venv312`) and a local Chrome.

    .venv312/bin/python api/tests/verify_calendar_month_view_ui.py
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
from calendar import monthrange
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
# ⚠️ 8812: a port nothing else forwards. 8810 is verify_calendar_ui, and a bind that fails
# with EADDRINUSE reads as "this suite is broken" rather than "the port is taken".
PORT = 8812

TODAY = date.today()
Y, M = TODAY.year, TODAY.month


def _iso(y, m, d):
    return "%04d-%02d-%02d" % (y, m, d)


def _prev_month(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def week_rows(y, m):
    """How many week rows a month needs. `date.weekday()` is Monday=0, so Sunday is 6."""
    lead = (date(y, m, 1).weekday() + 1) % 7
    total = monthrange(y, m)[1]
    return (lead + total + 6) // 7


def _event(slug, title, start, end, kind="meeting", **extra):
    base = {"slug": slug, "title": title, "start_date": start, "end_date": end,
            "deadline": "", "kind": kind, "status": "published", "visibility": "private",
            "owner_email": "owner@example.com", "can_manage": True, "category": "",
            "tags": [], "summary_en": "", "summary_zh": "", "summary": "",
            "partners": [], "attachments": [], "todos": [],
            "created_at": "", "updated_at": ""}
    base.update(extra)
    return base


PY, PM = _prev_month(Y, M)
LAST = monthrange(Y, M)[1]

# ⚠️ Mark days are anchored to TODAY, not to fixed numbers, because "overdue" is relative to
# the day the suite runs: a fixed day 8 is in the past on the 20th and in the future on the
# 3rd, and the assertion would then be testing the calendar's date rather than the code's
# rule. They sit on 1 / 2 / 3 / LAST so they never collide for any month length, and the
# expected states are derived from the same rule the page uses (see `mark_expectations`).
MARK_DONE, MARK_LATE, MARK_DEADLINE, MARK_PENDING = 1, 2, 3, LAST

# ⚠️ Day numbers stay at or below 22 so the fixture behaves the same in February as in
#    March — a 29th would be a different shape every month and the geometry assertions
#    would be testing the calendar, not the code.
EVENTS = [
    # Two events on ONE day: they must land on different lanes, drawn on top of each other
    # is the single most common month-grid bug.
    _event("mv-a", "Standup", _iso(Y, M, 10), _iso(Y, M, 10), "meeting"),
    _event("mv-b", "Ship it", _iso(Y, M, 10), _iso(Y, M, 10), "launch"),
    # Runs 5→18: it wraps a week boundary, so it must come back as pieces that line up.
    _event("mv-long", "Autumn campaign", _iso(Y, M, 5), _iso(Y, M, 18), "campaign"),
    # Starts in the PREVIOUS month and ends on the 3rd: the grid must clip it to 1..3, not
    # draw a bar from a day that is not in this month.
    _event("mv-span", "Carried over", _iso(PY, PM, 26), _iso(Y, M, 3), "planning"),
    # A retired type: still a POINT, and the grid must keep the diamond that says so.
    _event("mv-ms", "CD90", _iso(Y, M, 12), _iso(Y, M, 12), "milestone"),
    # Spans the whole month, so it also exercises a bar crossing every week row at once, and
    # carries the deadline plus the three to-do mark states.
    _event("mv-todo", "Review prep", _iso(Y, M, 1), _iso(Y, M, LAST), "review",
           deadline=_iso(Y, M, MARK_DEADLINE),
           todos=[{"text": "Collect numbers", "assignee": "mate@example.com",
                   "due": _iso(Y, M, MARK_DONE), "done": True},
                  {"text": "Overdue line", "assignee": "mate@example.com",
                   "due": _iso(Y, M, MARK_LATE), "done": False},
                  {"text": "Not due yet", "assignee": "mate@example.com",
                   "due": _iso(Y, M, MARK_PENDING), "done": False}]),
    # Drafts must read as quiet in the grid too, not just in the timeline.
    _event("mv-draft", "Rough idea", _iso(Y, M, 15), _iso(Y, M, 15), "research",
           status="draft"),
]


def mark_expectations():
    """What the three mark states must be TODAY, from the page's own rule.

    `done` is always green. An undone to-do and an event deadline are red once their day has
    passed and plain before it — the same `due < today` comparison the renderer makes. The
    last day of the month is never in the past, so one plain mark is guaranteed every run.
    """
    late = sorted({d for d in (MARK_LATE, MARK_DEADLINE) if d < TODAY.day})
    plain = sorted({MARK_PENDING} | {d for d in (MARK_LATE, MARK_DEADLINE) if d >= TODAY.day})
    return {"done": [MARK_DONE], "late": late, "plain": plain}

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


EVENT_PAGE = ('<!doctype html><html><body><div class="deck">'
              '<section class="slide is-current"><h1>Month view</h1></section>'
              '</div></body></html>')


class _Handler(http.server.SimpleHTTPRequestHandler):
    """`frontend/out`, plus the stand-in page `/e/<slug>` shows in the overlay."""

    def do_GET(self):
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
window.fetch = function (url, init) {
  const u = String(url);
  window.__sent.push(((init || {}).method || 'GET') + ' ' + u);
  const reply = (payload) => Promise.resolve({
    ok: true, status: 200, headers: {get: () => 'application/json'},
    json: () => Promise.resolve(payload)});
  if (String(u).indexOf('/api/auth/colleagues') >= 0) return reply({colleagues: []});
  const single = u.match(/\\/api\\/calendar\\/events\\/([^?]+)$/);
  if (single) {
    const found = EVENTS.find((e) => e.slug === decodeURIComponent(single[1]));
    return reply(Object.assign({}, found, {body: '<div class="deck">x</div>'}));
  }
  return reply({events: EVENTS, count: EVENTS.length, scope: 'mine'});
};
const EVENTS = %s;
""" % json.dumps(EVENTS)

# ── in-page probes ───────────────────────────────────────────────────────────
# Every geometry assertion is measured from the browser, not read out of the markup: a bar
# can be in the right cell in the source and the wrong column on screen, and the difference
# is exactly what this view can get wrong.
PROBES = """
window.__mv = {
  cellRects: function () {
    const out = {};
    document.querySelectorAll('.cal-mv-cell').forEach((c) => {
      const n = c.querySelector('.cal-mv-num');
      if (!n) return;
      const r = c.getBoundingClientRect();
      out[n.textContent.trim()] = {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
    });
    return out;
  },
  // Which day cells a slug's bars actually cover, measured. A bar is inset by the chip's
  // own 2px margin, so overlap is tested against the cell MINUS that margin.
  // ⚠️ Cells are scoped to the bar's OWN week row. Every week row has one cell per column at
  // the same x, so comparing a bar against all 35 cells counts every other row's day-3 and
  // day-10 too — which reads as "the bar covers 3, 10, 17, 24, 31" for a one-day event.
  daysCovered: function (slug) {
    const days = [];
    document.querySelectorAll('.cal-bar[data-slug="' + slug + '"]').forEach((bar) => {
      const week = bar.closest('.cal-mv-week');
      if (!week) return;
      const b = bar.getBoundingClientRect();
      week.querySelectorAll('.cal-mv-cell').forEach((c) => {
        const n = c.querySelector('.cal-mv-num');
        if (!n) return;
        const r = c.getBoundingClientRect();
        if (b.left < r.right - 2 && b.right > r.left + 2) days.push(Number(n.textContent.trim()));
      });
    });
    return Array.from(new Set(days)).sort(function (a, b) { return a - b; });
  },
  cellBg: function (day) {
    const cells = document.querySelectorAll('.cal-mv-cell');
    for (const c of cells) {
      const n = c.querySelector('.cal-mv-num');
      if (n && n.textContent.trim() === String(day)) return getComputedStyle(c).backgroundColor;
    }
    return '';
  },
  cardBg: function () {
    const el = document.querySelector('.cal-mv');
    return el ? getComputedStyle(el).backgroundColor : '';
  },
  barRects: function (slug) {
    return Array.from(document.querySelectorAll('.cal-bar[data-slug="' + slug + '"]'))
      .map((el) => { const r = el.getBoundingClientRect();
        return {left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                w: r.width, h: r.height, cls: el.className}; });
  },
  rowCount: function () { return document.querySelectorAll('.cal-mv-week').length; },
  // How much of the height the reader actually has the grid is using. ⚠️ Measured against
  // `.cal-wrap`'s CONTENT box (clientHeight), and the ratio is reported rather than asserted
  // here so the test can state it in the failure message — "the month view uses 37% of the
  // height" is the finding, "it does not fill" is the symptom the reader reported.
  fillRatio: function () {
    const wrap = document.getElementById('cal-wrap');
    const weeks = document.querySelectorAll('.cal-mv-week');
    if (!wrap || !weeks.length) return {ratio: 0, weeks: 0, used: 0, have: 0};
    const top = wrap.getBoundingClientRect().top;
    const cs = getComputedStyle(wrap);
    const padTop = parseFloat(cs.paddingTop) || 0;
    const last = weeks[weeks.length - 1].getBoundingClientRect();
    // The bottom padding is slack, not space the grid should eat: the timeline stops short
    // of it too, and asking the month to reach it would be asking for a scrollbar.
    const have = wrap.clientHeight - padTop;
    const used = last.bottom - top - padTop;
    return {ratio: have > 0 ? used / have : 0, weeks: weeks.length,
            used: Math.round(used), have: Math.round(have)};
  },
  // Vertical gap between every pair of bars that share a grid COLUMN (i.e. two lanes of the
  // same day) anywhere in the month, and the TIGHTEST of them.
  // ⚠️ Bars are SIBLINGS of the day cells, not children — both are grid items of
  // `.cal-mv-week`. Querying `.cal-mv-cell` for bars matches nothing and returns a null that
  // reads exactly like "the events are not stacked".
  laneSpread: function () {
    const gaps = [];
    document.querySelectorAll('.cal-mv-week').forEach((week) => {
      const byCol = {};
      week.querySelectorAll('.cal-bar').forEach((bar) => {
        const col = getComputedStyle(bar).gridColumnStart;
        (byCol[col] = byCol[col] || []).push(bar.getBoundingClientRect().top);
      });
      Object.keys(byCol).forEach((col) => {
        const tops = byCol[col].sort(function (a, b) { return a - b; });
        for (let i = 1; i < tops.length; i += 1) gaps.push(Math.round(tops[i] - tops[i - 1]));
      });
    });
    return {pairs: gaps.length, min: gaps.length ? Math.min.apply(null, gaps) : 0,
            sample: gaps.slice(0, 6)};
  },
  cellCount: function () { return document.querySelectorAll('.cal-mv-cell').length; },
  colCount: function () {
    const w = document.querySelector('.cal-mv-week');
    return w ? getComputedStyle(w).gridTemplateColumns.split(' ').length : 0;
  },
  weekLabels: function () {
    return Array.from(document.querySelectorAll('.cal-mv-wk span'))
      .map((s) => s.textContent.trim());
  },
  markDays: function (cls) {
    const out = [];
    document.querySelectorAll('.cal-mv-cell').forEach((c) => {
      if (c.querySelector('.cal-ms-tri.' + cls)) {
        const n = c.querySelector('.cal-mv-num');
        if (n) out.push(Number(n.textContent.trim()));
      }
    });
    return out.sort(function (a, b) { return a - b; });
  },
  markCount: function () { return document.querySelectorAll('.cal-mv-marks .cal-ms-tri').length; },
  todayCell: function () {
    const c = document.querySelector('.cal-mv-cell.today');
    if (!c) return null;
    const n = c.querySelector('.cal-mv-num');
    return n ? Number(n.textContent.trim()) : null;
  },
  title: function () {
    const t = document.querySelector('.cal-mv-title');
    return t ? t.textContent.trim() : '';
  },
  isMonth: function () {
    const g = document.getElementById('cal-grid');
    return !!(g && g.classList.contains('is-month'));
  },
  // Computed colours, so a token can be proven to reach the screen in BOTH themes.
  colorOf: function (sel, prop) {
    const el = document.querySelector(sel);
    return el ? getComputedStyle(el)[prop] : '';
  },
  surface: function () { return getComputedStyle(document.body).getPropertyValue('--surface').trim(); }
};
"""


def settle(page, quiet_ms=250, limit_ms=4000):
    """Wait until the page stops issuing requests.

    Needed wherever the measurement window contains a resize: the shell re-renders (and
    refetches) on `resize`, so a request count sampled before the viewport settles includes a
    load the test caused itself — which is how "scrolling fetches nothing" ends up measuring
    the harness instead of the behaviour.
    """
    waited = 0
    last = -1
    while waited < limit_ms:
        now = page.evaluate("window.__sent.length")
        if now == last:
            return now
        last, waited = now, waited + quiet_ms
        page.wait_for_timeout(quiet_ms)
    return page.evaluate("window.__sent.length")


def goto_month(page, steps):
    """Walk the month grid `steps` months forward (negative goes back)."""
    for _ in range(abs(steps)):
        page.click(".cal-mv-nav[onclick*='stepMonth(%d)']" % (1 if steps > 0 else -1))
        page.wait_for_timeout(120)
    page.wait_for_timeout(250)


def main():
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(STUB)
            page.add_init_script(PROBES)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_timeout(600)
            # ⚠️ The calendar page ships hidden; nothing in it is clickable until the nav tab
            # puts it on screen. (The button exists and `page.click` times out on it, which
            # reads as "the switcher is broken" rather than "the page is not open".)
            page.click("#nav-tab-calendar")
            page.wait_for_function(
                "() => getComputedStyle(document.getElementById('page-calendar')).display === 'flex'")
            page.wait_for_selector("#cal-grid .cal-month", state="visible")
            page.wait_for_timeout(400)

            # ── the timeline is what we started on ──
            check("opens on the timeline",
                  page.locator(".cal-month").count() >= 1
                  and page.locator(".cal-mv").count() == 0,
                  "rows=%d monthGrid=%d" % (page.locator(".cal-month").count(),
                                            page.locator(".cal-mv").count()))

            # ── switching views ──
            before_switch = settle(page)
            page.click("#cal-view-month")
            page.wait_for_timeout(900)
            switch_calls = page.evaluate("window.__sent.length") - before_switch
            # ⚠️ Exactly ONE. The month view fetches its own month and then stops — the
            # timeline's `fillViewport` loop (keep appending months until the window
            # overflows, so the wheel has somewhere to go) has no meaning for a one-month
            # grid, and running it here re-renders thirty-six times and refetches.
            check("switching view costs exactly one request", switch_calls == 1, switch_calls)
            check("Month tab is the selected one",
                  page.locator("#cal-view-month.on").count() == 1
                  and page.locator("#cal-view-timeline.on").count() == 0)
            check("month grid replaces the timeline rows",
                  page.locator(".cal-mv").count() == 1 and page.locator(".cal-month").count() == 0,
                  "grid=%d rows=%d" % (page.locator(".cal-mv").count(),
                                        page.locator(".cal-month").count()))
            check("container carries the month-view flag",
                  page.evaluate("window.__mv.isMonth()") is True)

            # ⚠️ A view, not a filter: it refetched, and it asked for ONE month rather than
            # the timeline's whole scrolling window.
            sent = page.evaluate("window.__sent.slice()")
            month_calls = [u for u in sent if u.startswith("GET /api/calendar/events?")]
            want_from, want_to = "from=%s-01" % _iso(Y, M, 1)[:7], "to=%s-%02d" % (_iso(Y, M, 1)[:7], LAST)
            check("month view refetched its own range",
                  bool(month_calls) and want_from in month_calls[-1] and want_to in month_calls[-1],
                  month_calls[-1] if month_calls else "no request")

            # ── the shape of the grid ──
            check("seven columns, in the stylesheet",
                  page.evaluate("window.__mv.colCount()") == 7,
                  page.evaluate("window.__mv.colCount()"))
            labels = page.evaluate("window.__mv.weekLabels()")
            check("seven weekday headings",
                  len(labels) == 7 and all(labels), labels)
            check("Sunday is the first column",
                  labels[0].lower().startswith("sun"), labels)
            rows = page.evaluate("window.__mv.rowCount()")
            want_rows = week_rows(Y, M)
            check("this month has exactly the weeks it needs (%d)" % want_rows,
                  rows == want_rows, "got %d" % rows)
            cells = page.evaluate("window.__mv.cellCount()")
            check("every row is a full seven days (%d cells)" % (rows * 7),
                  cells == rows * 7, "got %d" % cells)

            # ── every event lands on the days its own dates say ──
            for ev in EVENTS:
                if ev["start_date"][:7] != _iso(Y, M, 1)[:7] and ev["end_date"][:7] != _iso(Y, M, 1)[:7]:
                    continue
                total = monthrange(Y, M)[1]
                lo = max(1, int(ev["start_date"][8:10]) if ev["start_date"][:7] == _iso(Y, M, 1)[:7] else 1)
                hi = min(total, int(ev["end_date"][8:10]) if ev["end_date"][:7] == _iso(Y, M, 1)[:7] else total)
                want = list(range(lo, hi + 1))
                got = page.evaluate("slug => window.__mv.daysCovered(slug)", ev["slug"])
                check("%s covers %d-%d" % (ev["slug"], want[0], want[-1]),
                      got == want, "want %s, got %s" % (want, got))

            # A bar wrapping the week boundary must be MORE than one piece — a grid that
            # redraws it as one bar per month simply cannot draw across two week rows.
            pieces = page.evaluate("window.__mv.barRects('mv-long')")
            check("a 14-day event is one piece per week it crosses", len(pieces) >= 2,
                  "pieces=%d" % len(pieces))
            check("its pieces stack down the page, not side by side",
                  len(pieces) >= 2 and all(p["top"] > pieces[0]["top"] for p in pieces[1:]))

            # ── lane packing, measured ──
            a = page.evaluate("window.__mv.barRects('mv-a')")
            b = page.evaluate("window.__mv.barRects('mv-b')")
            stacked = (a and b and not (a[0]["top"] < b[0]["bottom"] and b[0]["top"] < a[0]["bottom"]))
            check("two events on one day get their own lanes", bool(stacked),
                  "a=%s b=%s" % ([round(x["top"]) for x in a], [round(x["top"]) for x in b]))
            # The positive control: the cells they were measured against really exist.
            check("…and both are inside the day-10 cell",
                  bool(a and b and all(r["w"] > 40 for r in a + b)),
                  "widths=%s" % [round(r["w"]) for r in a + b])

            # ── it uses the height it has ──
            # Asked for 2026-10-06 with a screenshot: "高度没有利用上，都挤在一起". The grid
            # was 286px inside an 863px window. Two separate things were wrong and they need
            # two separate checks: the CARD was not filling its container, and once it did,
            # the LANES were still bunched at the top of each now-tall cell.
            fill = page.evaluate("window.__mv.fillRatio()")
            # ⚠️ Positive control first: a ratio computed from zero matched rows is 0, and
            # "0 < 0.9" would then be a false failure while proving nothing. `weeks` must be
            # the real count for this month.
            check("the measurement found the month's real week rows",
                  fill["weeks"] == want_rows, fill)
            check("the grid fills the height the page gave it (asked for 2026-10-06)",
                  fill["ratio"] >= 0.92,
                  "uses %.0f%% (%dpx of %dpx) — the rest was a blank screen"
                  % (fill["ratio"] * 100, fill["used"], fill["have"]))
            check("…and it fits without a scrollbar",
                  page.evaluate(
                      "() => { const w = document.getElementById('cal-wrap');"
                      " return w.scrollHeight <= w.clientHeight + 2; }") is True)
            spread = page.evaluate("window.__mv.laneSpread()")
            # ⚠️ `pairs` first, always. "No stacked lanes at all" and "the lanes are bunched"
            # both make a `min` small, and only one of them is the bug.
            check("the month really does have events sharing a day", spread["pairs"] >= 1, spread)
            # The floor is `.cal-mv-lane` (18px) plus the chip's own 1px margins, so ~20px is
            # "bunched at the top of the cell". The TIGHTEST pair is the assertion: one crowded
            # cell is enough to make the month read as crammed again.
            check("stacked events spread out instead of bunching (asked for 2026-10-06)",
                  spread["pairs"] >= 1 and spread["min"] > 26, spread)
            # ⚠️ Spread alone would have passed with an 18px hairline in a 140px cell — the
            # lanes grew, the event did not ("甘特图的高度能不能调高一点", 2026-10-06). The bar
            # itself has to be a readable size on its own.
            chip_h = page.evaluate(
                "window.__mv.barRects('mv-long')[0].h")
            check("an event chip is tall enough to read, not a hairline",
                  chip_h >= 22, "chip height %spx" % chip_h)

            # ⚠️「甘特图不要紧挨着日期」— the first bar must not sit flush against the
            # day-number strip. A cell is one grid row of dates followed by one row per lane,
            # and with no `row-gap` those two were touching, which made the first bar read as
            # part of the header rather than as an event.
            #
            # ⚠️ Measured on the SMALLEST gap over all week rows, not on one convenient row:
            # a row whose cell has no events has nothing to measure, and picking the row that
            # happens to look fine is how a layout guard ends up guarding nothing.
            gap = page.evaluate("""() => {
              let min = Infinity, row = null;
              for (const wk of document.querySelectorAll('.cal-mv-week')) {
                const cell = wk.querySelector('.cal-mv-cell:not(.out)');
                const bars = [...wk.querySelectorAll('.cal-bar')];
                if (!cell || !bars.length) continue;
                const g = bars[0].getBoundingClientRect().top
                        - cell.getBoundingClientRect().bottom;
                if (g < min) { min = g; row = bars[0].dataset.slug; }
              }
              return {min: +min.toFixed(2), row,
                      rowGap: getComputedStyle(
                        document.querySelector('.cal-mv-week')).rowGap};
            }""")
            check("the first bar is not flush against the day numbers (asked for 2026-10-06)",
                  gap["min"] >= 6, gap)

            # ── a milestone is a point, not a filled one-day bar ──
            ms = page.locator(".cal-mv .cal-bar.milestone").first
            check("the retired milestone still draws in the grid", ms.count() == 1)
            # ⚠️ Compared against the CARD's own paint, not against `--surface` read as a raw token:
            # `getPropertyValue` returns `#ffffff` while `backgroundColor` returns
            # `rgb(255, 255, 255)`, and those are the same paint as two strings. Nor is the
            # DAY CELL the right reference — a plain cell has no background of its own and
            # shows the card through it, so its computed value is transparent.
            ms_bg = page.evaluate(
                "() => getComputedStyle(document.querySelector('.cal-mv .cal-bar.milestone')).backgroundColor")
            card_bg = page.evaluate("window.__mv.cardBg()")
            ms_diamond = page.locator(".cal-mv .cal-bar.milestone .cal-ms-dot").count()
            check("…as a diamond on a plain chip, not a filled bar",
                  ms_diamond == 1 and ms_bg == card_bg,
                  "bg=%s card=%s diamond=%d" % (ms_bg, card_bg, ms_diamond))
            ms_fill = page.evaluate(
                "() => getComputedStyle(document.querySelector('.cal-bar:not(.milestone)')).backgroundColor")
            check("a normal bar in the same cell keeps its type colour",
                  ms_fill not in ("rgba(0, 0, 0, 0)", ms_bg), ms_fill)

            # ── to-do and deadline marks, on their own days ──
            want_marks = mark_expectations()
            got_done = page.evaluate("window.__mv.markDays('is-done')")
            check("a completed to-do is marked done, on its own day",
                  got_done == want_marks["done"], "%s want %s" % (got_done, want_marks["done"]))
            got_late = page.evaluate("window.__mv.markDays('is-late')")
            check("an overdue to-do and an overdue deadline are marked late",
                  got_late == want_marks["late"],
                  "%s want %s (today is the %dth)"
                  % (got_late, want_marks["late"], TODAY.day))
            check("every mark is accounted for, none extra",
                  page.evaluate("window.__mv.markCount()") == 4,
                  page.evaluate("window.__mv.markCount()"))
            got_plain = page.evaluate(
                """() => {
                  const out = [];
                  document.querySelectorAll('.cal-mv-cell').forEach((c) => {
                    const n = c.querySelector('.cal-mv-num');
                    const m = c.querySelector('.cal-mv-marks .cal-ms-tri');
                    if (!n || !m) return;
                    if (m.classList.contains('is-done') || m.classList.contains('is-late')) return;
                    out.push(Number(n.textContent.trim()));
                  });
                  return out.sort(function (a, b) { return a - b; });
                }""")
            check("a to-do that is not due yet stays plain, on the last day",
                  got_plain == want_marks["plain"],
                  "%s want %s (today is the %dth)" % (got_plain, want_marks["plain"], TODAY.day))
            # The colour is what makes the three states mean anything, so assert it rather
            # than the class name alone.
            ms_colors = page.evaluate(
                """() => {
                  const pick = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? getComputedStyle(el).getPropertyValue('--ms-color').trim() : '';
                  };
                  return [pick('.cal-ms-tri.is-done'), pick('.cal-ms-tri.is-late')];
                }""")
            check("done and overdue really are different colours",
                  ms_colors[0] and ms_colors[1] and ms_colors[0] != ms_colors[1], ms_colors)

            # ── today ──
            check("today's cell is marked, and it is today's number",
                  page.evaluate("window.__mv.todayCell()") == TODAY.day,
                  page.evaluate("window.__mv.todayCell()"))

            # ── the kind filter reaches the grid ──
            before = page.locator(".cal-mv .cal-bar").count()
            page.click(".cal-kind[data-kind='review']")
            page.wait_for_timeout(400)
            after = page.locator(".cal-mv .cal-bar").count()
            hidden = page.locator(".cal-mv .cal-bar[data-slug='mv-todo']").count()
            survivors = page.locator(".cal-mv .cal-bar[data-slug='mv-a']").count()
            # ⚠️ Positive control on the SAME query: a count of 0 means nothing unless the
            # selector still matches something that is on.
            check("turning a type off empties the grid (and the rest is still drawn)",
                  before > 0 and after < before and hidden == 0 and survivors == 1,
                  "before=%d after=%d hidden=%d survivors=%d"
                  % (before, after, hidden, survivors))
            check("its to-do marks went with it",
                  page.evaluate("window.__mv.markDays('is-done')") == []
                  and page.evaluate("window.__mv.markCount()") == 0)
            page.click(".cal-kind[data-kind='review']")
            page.wait_for_timeout(400)
            check("turning it back on restores it",
                  page.locator(".cal-mv .cal-bar[data-slug='mv-todo']").count() >= 1
                  and page.evaluate("window.__mv.markDays('is-done')")
                  == want_marks["done"])

            # ── hover card ──
            bar = page.locator(".cal-mv .cal-bar[data-slug='mv-long']").first
            bar.hover()
            page.wait_for_timeout(250)
            check("hovering a grid bar opens the hover card",
                  page.locator("#cal-tip.on").count() == 1
                  and "Autumn campaign" in (page.locator("#cal-tip").inner_text() or ""))
            page.mouse.move(5, 5)
            page.wait_for_timeout(150)

            # ── double-click opens the event's page ──
            page.dblclick(".cal-mv .cal-bar[data-slug='mv-long']")
            page.wait_for_timeout(500)
            check("double-click opens the event overlay",
                  page.evaluate("() => !document.getElementById('cal-modal').hidden")
                  and "Autumn campaign" in page.locator("#cal-modal-title").inner_text())
            check("the overlay loads the event's own page",
                  "/e/mv-long" in (page.locator("#cal-frame").get_attribute("src") or ""))
            page.click("#cal-modal .cal-modal-h button[title='Close']")
            page.wait_for_timeout(250)
            check("closing it empties the frame",
                  page.locator("#cal-frame").get_attribute("src") == "about:blank")

            # ── navigation ──
            title_now = page.evaluate("window.__mv.title()")
            goto_month(page, 1)
            check("Next month moves the grid on",
                  page.evaluate("window.__mv.title()") != title_now,
                  page.evaluate("window.__mv.title()"))
            ny, nm = _next_month(Y, M)
            check("…and it lands on the month after",
                  page.evaluate("window.__mv.title()")
                  == ("%d 年 %d 月" % (ny, nm) if page.evaluate(
                      "() => window.kladoI18n.lang()") == "zh"
                      else _month_en(nm, ny)),
                  page.evaluate("window.__mv.title()"))
            check("…and that month is laid out right too",
                  page.evaluate("window.__mv.rowCount()") == week_rows(ny, nm),
                  page.evaluate("window.__mv.rowCount()"))
            check("…with seven columns again",
                  page.evaluate("window.__mv.colCount()") == 7)
            goto_month(page, -1)
            check("Previous month comes back",
                  page.evaluate("window.__mv.title()") == title_now,
                  page.evaluate("window.__mv.title()"))

            # ── the sweep: every month has exactly the weeks it needs ──
            seen = set()
            bad = []
            for _ in range(14):
                got = page.evaluate("window.__mv.rowCount()")
                want = week_rows(*_from_title(page.evaluate("window.__mv.title()")))
                seen.add(got)
                if got != want:
                    bad.append((page.evaluate("window.__mv.title()"), got, want))
                if page.evaluate("window.__mv.colCount()") != 7:
                    bad.append((page.evaluate("window.__mv.title()"), "columns", 7))
                goto_month(page, 1)
            check("fourteen months all have the right number of week rows",
                  not bad, bad[:3])
            check("…and the sweep met both a five-row and a six-row month",
                  5 in seen and 6 in seen, sorted(seen))

            page.click("button[onclick='calendarPage.resetWindow()']")
            page.wait_for_timeout(600)
            check("'Today' returns the grid to this month",
                  page.evaluate("window.__mv.title()") == title_now,
                  page.evaluate("window.__mv.title()"))

            # ── switching back ──
            page.click("#cal-view-timeline")
            page.wait_for_timeout(700)
            check("the timeline comes back intact",
                  page.locator(".cal-month").count() >= 1
                  and page.locator(".cal-mv").count() == 0
                  and page.evaluate("window.__mv.isMonth()") is False,
                  "rows=%d grid=%d" % (page.locator(".cal-month").count(),
                                        page.locator(".cal-mv").count()))
            check("its bars came back too",
                  page.locator("#cal-grid .cal-bar").count() >= 7,
                  page.locator("#cal-grid .cal-bar").count())

            # ── tokens, in both themes ──
            # ⚠️ The thing being asserted is that the stylesheet reaches for TOKENS. A colour
            # literal is right in one theme and wrong in the other and nothing fails loudly,
            # so each theme is checked separately AND the two are compared: a value that is
            # identical in light and dark is a value that is not coming from a token at all.
            cards = {}
            # ⚠️ Back to the grid: the block above proved the TIMELINE comes back, and every
            # selector below is `.cal-mv`, which no longer exists while the timeline is on.
            page.click("#cal-view-month")
            page.wait_for_timeout(700)
            for theme in ("light", "dark"):
                page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
                page.wait_for_timeout(250)
                card = page.evaluate("window.__mv.cardBg()")
                cards[theme] = card
                wk = page.evaluate("window.__mv.colorOf('.cal-mv-cell.wk', 'backgroundColor')")
                plain = page.evaluate(
                    "window.__mv.colorOf('.cal-mv-cell:not(.wk):not(.today):not(.out)', 'backgroundColor')")
                check("%s theme: weekend shading differs from an ordinary weekday" % theme,
                      bool(wk and plain and wk != plain), "%s vs %s" % (wk, plain))
                today_bg = page.evaluate("window.__mv.colorOf('.cal-mv-cell.today', 'backgroundColor')")
                check("%s theme: today is marked apart from an ordinary weekday" % theme,
                      bool(today_bg) and today_bg not in ("", plain), "%s vs %s" % (today_bg, plain))
                chip = page.evaluate(
                    "window.__mv.colorOf('.cal-mv .cal-bar:not(.milestone)', 'backgroundColor')")
                check("%s theme: an event chip keeps its own fill" % theme,
                      chip not in ("rgba(0, 0, 0, 0)", "", card), "%s on %s" % (chip, card))
                # ⚠️ The TEXT is the other half of the complaint —「不同甘特图底色上文本颜色
                # 一样也不舒服」— and it is a per-theme property now: a light tint carries a
                # dark saturated ink, a dark tint a light one. So it cannot be asserted once,
                # and it must stay off both the card colour and the chip's own fill.
                ink = page.evaluate(
                    "window.__mv.colorOf('.cal-mv .cal-bar:not(.milestone)', 'color')")
                check("%s theme: a chip's label is its own ink, not the card's text colour" % theme,
                      bool(ink) and ink not in ("", chip, card), "%s on %s" % (ink, card))
            # And the two themes must NOT agree: an ink identical in light and dark is a value
            # that is not coming from a token at all — which is exactly the bug the single
            # palette had, where one hex had to serve both surfaces.
            inks = page.evaluate("() => { const out = {}; const sel = "
                                 "'.cal-mv .cal-bar:not(.milestone)'; "
                                 "for (const t of ['light', 'dark']) { "
                                 "document.documentElement.setAttribute('data-theme', t); "
                                 "out[t] = getComputedStyle(document.querySelector(sel)).color; } "
                                 "document.documentElement.setAttribute('data-theme', 'light'); "
                                 "return out; }")
            check("★ 主题切换时 chip 的文字颜色也跟着换（不是写死一个值）",
                  bool(inks.get("light")) and bool(inks.get("dark"))
                  and inks["light"] != inks["dark"], inks)
            check("the surface really does change between themes",
                  cards["light"] and cards["dark"] and cards["light"] != cards["dark"], cards)
            page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")

            # ── the language switch reaches the month view's own copy ──
            page.evaluate("window.kladoI18n.setLang('zh')")
            page.wait_for_timeout(300)
            check("weekday headings follow the language",
                  page.evaluate("window.__mv.weekLabels()")[0] == "日",
                  page.evaluate("window.__mv.weekLabels()"))
            check("the month title follows too",
                  "年" in page.evaluate("window.__mv.title()"),
                  page.evaluate("window.__mv.title()"))
            page.evaluate("window.kladoI18n.setLang('en')")
            page.wait_for_timeout(300)
            check("…and come back with it",
                  page.evaluate("window.__mv.weekLabels()")[0] == "Sun",
                  page.evaluate("window.__mv.weekLabels()"))

            # ── a month grid is ONE month: it never reaches for the timeline's scroll-to-extend
            before_n = page.evaluate("window.__sent.length")
            # ⚠️ A short window first, and POSITIVE CONTROL on the overflow itself: with a tall
            # window the grid does not scroll, no `scroll` event fires, and the assertion
            # below would pass for the wrong reason — a guard that never runs.
            page.set_viewport_size({"width": 1600, "height": 300})
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                """() => { const w = document.getElementById('cal-wrap');
                    return {sh: w.scrollHeight, ch: w.clientHeight}; }""")
            check("a short window really does make the grid scroll",
                  overflow["sh"] > overflow["ch"] + 4, overflow)
            # ⚠️ Re-baseline AFTER the resize settles. The shell re-renders on `resize`, so a
            # count taken before it is off by exactly the load the test itself caused.
            before_n = settle(page)
            page.evaluate(
                "() => { const w = document.getElementById('cal-wrap');"
                " w.scrollTop = w.scrollHeight; w.dispatchEvent(new Event('scroll')); }")
            page.wait_for_timeout(600)
            after_n = page.evaluate("window.__sent.length")
            rows_after_scroll = page.evaluate("window.__mv.rowCount()")
            check("scrolling the grid fetches nothing and adds no weeks",
                  after_n == before_n and rows_after_scroll == want_rows,
                  "requests %d→%d, rows %d→%d" % (before_n, after_n, want_rows, rows_after_scroll))
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.wait_for_timeout(400)

            # ── a bar that wraps a week keeps its corners on the inside ──
            corners = page.evaluate(
                """() => Array.from(
                     document.querySelectorAll('.cal-mv .cal-bar[data-slug="mv-long"]'))
                   .map((el) => { const s = getComputedStyle(el); return {
                     contL: el.classList.contains('cont-l'),
                     contR: el.classList.contains('cont-r'),
                     tl: s.borderTopLeftRadius, tr: s.borderTopRightRadius}; })""")
            check("each wrapped piece carries the continuation flags",
                  len(corners) >= 2 and corners[0]["contL"] is False and corners[0]["contR"] is True
                  and corners[-1]["contL"] is True and corners[-1]["contR"] is False, corners)
            check("…and its corners are square on the inside, round on the outside",
                  corners[0]["tr"] == "0px" and corners[-1]["tl"] == "0px"
                  and corners[0]["tl"] != "0px", corners)

            check("no uncaught errors on the page", not errors, errors[:3])
            browser.close()
    finally:
        httpd.shutdown()
    if FAILURES:
        print("\n%d FAILED: %s" % (len(FAILURES), FAILURES))
        return 1
    print("\nall month-view checks passed")
    return 0


_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]


def _month_en(m, y):
    return "%s %d" % (_MONTHS_EN[m - 1], y)


def _from_title(text):
    """Read (year, month) back out of the heading the grid rendered."""
    for i, name in enumerate(_MONTHS_EN):
        if text.startswith(name):
            return int(text.split()[-1]), i + 1
    parts = text.replace("年", " ").replace("月", " ").split()
    return int(parts[0]), int(parts[1])


if __name__ == "__main__":
    sys.exit(main())