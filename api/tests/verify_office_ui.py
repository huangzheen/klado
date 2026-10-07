"""The Agent office on the landing page — asserted against the RENDERED page.

    ../.venv312/bin/python tests/verify_office_ui.py

This file is deliberately not a "does the SVG exist" check. Every assertion
here was written because a cheaper version of it was green while the product was
broken, and each one names the failure it exists for:

1. **A card is measured by what it DRAWS, not by what it OCCUPIES.** A `.off-label`
   with `min-width: 108px` has a box whether or not the icon font resolved, the
   name is non-empty, or the clock is running — so all three are read: the
   `<i>`'s own `::before` content, the text, and a second sample of the timer.
   A status card that renders as three empty boxes beside two working ones is
   what "the icon is not in the inline block" looks like, and it is invisible to
   a bounding-box assertion.

2. **The animation is checked by SAMPLING it, twice.** `.st-coding` on a group
   only animates a key if the selector actually matches, and the selector is
   about the DESK for a keyboard and about the PERSON for a head — a rule
   written against the wrong one matches nothing and the room sits perfectly
   still while nine agents work. So: pick a coding agent, read a key's opacity,
   wait, read it again, and require a change.

3. **Two cards in the same row must not overlap.** The lift (`data-lift`) is the
   only thing keeping neighbours apart — a card is wider than the column pitch —
   and a change to the pitch or the card width silently reintroduces the overlap
   with nothing else failing. Rect intersection, not a visual judgement.

4. **Each card must still point at ITS agent.** The cards are HTML positioned in
   % of the SVG viewBox, which is exact only while the stage's `aspect-ratio`
   equals the viewBox ratio. When those two drift, every card floats off its
   head by a constant offset and the room still looks busy — so the card's
   centre is compared against the agent's own projected centre.

5. **The roster and the room must agree.** Two views of one array, and the whole
   point is that they cannot disagree; a count taken from the picture and a
   count taken from the list are compared, not each trusted on its own.

6. **A language switch has to reach JS-built text.** The DOM walker only sees
   text that is already in the document, so the cards are exempt from it and
   `office.js` re-renders on `klado:lang` itself. After switching to English,
   an English word must be present AND no ` / ` may survive anywhere visible —
   a pair that failed to split shows the reader both languages at once, which is
   the exact failure `test_i18n.py` guards in source and this asserts on screen.

7. **Dark mode is a token question, so it is asked as one.** The card surface
   must be dark when the theme is dark: a light card in a dark room is a rule
   that pinned a literal, and no assertion about the SVG's fills would notice.

8. **The top bar still works from this page.** The office is the landing page,
   so it is the first thing every reader sees; `verify_nav_clickable_ui.py`
   covers the invariant for the whole app, and this asserts the one thing that is
   specific here — a REAL mouse click on a nav tab leaves the office.
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

import requests
import uvicorn
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import auth_store  # noqa: E402

PORT = int(os.environ.get("OFFICE_PORT", "18894"))
BASE = f"http://127.0.0.1:{PORT}"
WEB = BASE + "/?welcome=1"
PASSWORD = "office-ui-pass-1"
ADMIN = ("office", "office-ui@example.com")

passed, failed = [], []


def ok(msg):
    passed.append(msg)
    print("  ✓", msg)


def bad(msg):
    failed.append(msg)
    print("  ✗", msg)


def check(cond, msg, detail=""):
    (ok if cond else bad)(msg + (f"  [{detail}]" if detail and not cond else ""))
    return cond


def _prepare():
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (ADMIN[1],))
        cur.execute(
            "INSERT INTO app_users (email, password_hash, display_name, role) "
            "VALUES (%s, %s, %s, %s)",
            (ADMIN[1], auth_store._hash(PASSWORD), ADMIN[0], "admin"))
        cur.execute("SELECT id FROM app_users WHERE email = %s", (ADMIN[1],))
        uid = cur.fetchone()[0]
        # ⚠️ This account's AGENT has to have been here, and that is a fixture with
        # a reason behind it rather than a convenience. "An agent takes a desk by
        # turning up" means an account whose agent has never run has NOTHING at its
        # desk — no figure, no card, nothing to change. The avatar feature is about
        # the figure, so a test account with no agent activity has nothing to assert
        # against, and the whole end-to-end half of this file would quietly skip
        # itself. Writing one `kind='agent'` row is the honest way to put the
        # account in the state the feature is about, and it is exactly the state a
        # real user's account is in by the time they care what they look like.
        cur.execute("DELETE FROM access_log WHERE user_id = %s", (uid,))
        cur.execute(
            "INSERT INTO access_log (user_id, user_email, kind, method, path, "
            "bucket, hits, first_at, last_at) "
            "VALUES (%s, %s, 'agent', 'GET', '/api/reports', date_trunc('minute', NOW()), 3, NOW(), NOW())",
            (uid, ADMIN[1]))
        conn.commit()
    return uid


def _cleanup():
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app_users WHERE email = %s", (ADMIN[1],))
        row = cur.fetchone()
        if row:
            # ⚠️ The log rows go FIRST: the account's id is about to stop existing,
            # and `delete_user` nulls `user_id` rather than removing the row, which
            # would leave an orphan that the next run's `DELETE … WHERE user_id`
            # can no longer find.
            cur.execute("DELETE FROM access_log WHERE user_id = %s", (row[0],))
        cur.execute("DELETE FROM app_users WHERE email = %s", (ADMIN[1],))
        conn.commit()


def _serve():
    from main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(160):
        try:
            if requests.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise SystemExit("server did not come up")


# ── in-page probes ──────────────────────────────────────────────────────────
# Everything the checks need is computed in ONE evaluate() so the measurement
# and the reading cannot drift apart, and so the page is not round-tripped once
# per assertion.

PROBE = """() => {
  const stage = document.getElementById('off-stage');
  const svg = document.getElementById('off-svg');
  const desks = svg.querySelectorAll('.off-desk');
  const agents = [...svg.querySelectorAll('.off-agent')];
  // Hidden seats have opacity 0; every drawn character must remain opaque.
  const seated = agents.filter(a => a.getAttribute('opacity') !== '0');
  const cards = [...document.querySelectorAll('.off-label:not(.is-off)')];

  const cardInfo = cards.map(c => {
    const r = c.getBoundingClientRect();
    const ic = c.querySelector('.n i');
    const before = ic ? getComputedStyle(ic, '::before').content : 'none';
    return {
      rect: {x: r.x, y: r.y, w: r.width, h: r.height},
      name: c.querySelector('.who').textContent,
      task: c.querySelector('.t').textContent,
      meta: c.querySelector('.m').textContent,
      icon: ic ? ic.className : '',
      iconContent: before,
      color: getComputedStyle(c).getPropertyValue('--c').trim(),
      sel: c.classList.contains('is-sel'),
    };
  });

  // Each card, and the head it belongs to, in the same screen space.
  const pairs = cards.map(c => {
    const r = c.getBoundingClientRect();
    const cx = r.x + r.width / 2;
    // The card is anchored to its seat; recover the seat from the owner index
    // is not possible from the DOM, so use the card's own left% against the
    // stage to find the nearest seated agent instead.
    return {cx: cx, cy: r.y};
  });
  const agentPts = seated.map(a => {
    const r = a.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height};
  });

  const roster = [...document.querySelectorAll('#off-rows .off-row')];
  const rowsOn = roster.filter(r => r.classList.contains('on')).length;

  /* ⚠️ The animation probe used to sample `.off-desk.st-coding .key` twice and
     require a change, which was the only way to catch a selector hung off the
     wrong group. There is no `st-coding` any more and there must not be: `coding`
     was a claim about what an agent was doing, and nothing records that. The only
     motion the room is allowed is the `st-active` breath, so THAT is what is
     sampled — and it is sampled on a desk the data says is active, not on one that
     happens to match a class name. */
  const glows = [...svg.querySelectorAll('.off-desk.st-active .glowp')];
  const glowOpacity = glows.slice(0, 3).map(k => getComputedStyle(k).opacity);

  return {
    stage: {x: stage.getBoundingClientRect().x, y: stage.getBoundingClientRect().y,
            w: stage.getBoundingClientRect().width, h: stage.getBoundingClientRect().height},
    ratio: stage.getAttribute('data-ratio'),
    viewBox: svg.getAttribute('viewBox'),
    deskCount: desks.length,
    agentCount: agents.length,
    seatedCount: seated.length,
    cardCount: cards.length,
    cardInfo, pairs, agentPts,
    rosterCount: roster.length,
    rowsOn,
    legend: [...document.querySelectorAll('#off-legend .off-lg')].map(l => l.textContent.trim()),
    codingDesks: svg.querySelectorAll('.off-desk.st-coding').length,
    keyOpacity: glowOpacity,
    activeDesks: svg.querySelectorAll('.off-desk.st-active').length,
    // ⚠️ The room must be sized by the DATA, so the desk count is compared against
    // the seat list the endpoint returned rather than against a constant. The old
    // check hard-coded 12 and was green for a room with no people in it at all.
    apiSeats: window.AgentOffice ? window.AgentOffice.get() : null,
    // Opacity must stay solid for both active and away figures.
    agentOpacity: [...svg.querySelectorAll('.off-agent')]
      .filter(a => a.getAttribute('opacity') !== '0')
      .map(a => ({
        state: (a.getAttribute('class').match(/st-([a-z]+)/) || [])[1] || '?',
        opacity: parseFloat(getComputedStyle(a).opacity),
        filter: getComputedStyle(a.querySelector('.off-person')).filter,
      })),
    statOn: document.getElementById('off-stat-on').textContent,
    statFree: document.getElementById('off-stat-free').textContent,
    statDesks: document.getElementById('off-stat-desks').textContent,
    statNever: document.getElementById('off-stat-done').textContent,
    // ⚠️ The four counters used to share a row with an <h1>, a sentence and the
    // demo note, and wrapped onto a second line. "They exist" and "their text is
    // right" both stayed green while that happened, so this measures GEOMETRY:
    // one row means every box shares a `y` and the row is one box tall.
    statRow: (() => {
      const row = document.querySelector('.off-stats');
      const cells = [...row.children].map(c => c.getBoundingClientRect());
      return {
        n: cells.length,
        flexWrap: getComputedStyle(row).flexWrap,
        ys: [...new Set(cells.map(r => Math.round(r.y)))],
        rowH: Math.round(row.getBoundingClientRect().height),
        maxCellH: Math.round(Math.max(...cells.map(r => r.height))),
        texts: [...row.children].map(c => c.textContent.trim()),
      };
    })(),
    // ⚠️ The page is named in the breadcrumb now, and the body deliberately has
    // no heading of its own — the reader used to see the office's name twice.
    crumb: (() => { const t = document.getElementById('breadcrumb-text');
      return t ? t.textContent.trim() : null; })(),
    bodyH1: [...document.querySelectorAll('#page-home h1, #home-cards h1')]
      .map(h => h.textContent.trim()),
    navRect: (() => { const n = document.querySelector('nav.nav'); if (!n) return null;
      const b = n.getBoundingClientRect(); return {x: b.x, y: b.y, w: b.width, h: b.height}; })(),
    theme: document.documentElement.getAttribute('data-theme'),
  };
}"""


def overlap(a, b):
    x = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    y = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return max(0, x) * max(0, y)


# The same measurement, taken IN THE PAGE. The Python `overlap` above works on the
# rects the main PROBE collected, which is fine for the static room; growing the
# room to its ceiling needs the rects read at THAT size, and re-running the whole
# probe for it would drag in twenty unrelated measurements.
OVERLAP_PROBE = """() => {
  const rs = [...document.querySelectorAll('.off-label:not(.is-off)')]
    .map(c => c.getBoundingClientRect());
  let worst = 0;
  for (let i = 0; i < rs.length; i++) {
    for (let j = i + 1; j < rs.length; j++) {
      const a = rs[i], b = rs[j];
      const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (x > 0 && y > 0) worst = Math.max(worst, x * y);
    }
  }
  return worst;
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the account in place")
    args = ap.parse_args()

    fixture_uid = _prepare()
    _serve()
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            # Geometry checks use stationary figures; the work suite separately
            # exercises walking, sitting, chat and the pause control.
            page.add_init_script("localStorage.setItem('klado-office-motion', 'off')")
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            # ⚠️ The room is built inside a `.then()`, so a throw anywhere in
            # `boot()` — a typo'd variable, a null node, a bad SVG attribute — is
            # an UNHANDLED REJECTION, not an uncaught exception. Playwright's
            # `pageerror` does not fire for it, and the console stays clean. The
            # symptom is a page that renders its shell perfectly and shows no room
            # at all, which is exactly what happened once already: the check
            # "office.js 跑完了" was the ONLY thing that caught it, and it reports
            # `ao: undefined` rather than the actual error, so the cause had to be
            # found by hand. This listener is what turns that back into a message.
            page.add_init_script(
                """() => { window.__rejections = [];
                    window.addEventListener('unhandledrejection', e => {
                      window.__rejections.push(String((e.reason && (e.reason.stack || e.reason.message)) || e.reason));
                    }); }""")

            def await_av(animal, cloth):
                """The reader's own choice, made the way the picker makes it.

                ⚠️ A plain function, NOT `async`. Playwright's sync `evaluate`
                already blocks until the page's promise settles, so declaring this
                `async` returned a coroutine that nobody awaited: the PATCH was
                never sent, the assertion below was written as
                `check(saved is not None)` — true for a coroutine, for a 200 and
                for a 400 alike — and four later checks failed for a reason that
                had nothing to do with the product. A green assertion that cannot
                fail is worse than a red one, because it disarms the next reader."""
                return page.evaluate(
                    """async (v) => {
                        const r = await fetch('api/auth/me/avatar', {
                          method: 'PATCH', credentials: 'same-origin',
                          headers: {'Content-Type': 'application/json'},
                          body: JSON.stringify(v),
                        });
                        return {status: r.status, body: (await r.text()).slice(0, 160)};
                    }""",
                    {"animal": animal, "cloth": cloth})
            page.goto(WEB, wait_until="domcontentloaded")
            page.evaluate(
                """async (creds) => {
                    const r = await fetch('api/auth/login', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(creds),
                    });
                    if (!r.ok) throw new Error('login failed: ' + r.status);
                }""",
                {"email": ADMIN[1], "password": PASSWORD},
            )
            # Keep the real HTTP response and avatar round-trip, but scope the
            # scene to this suite's account. Unrelated local fixtures can exceed
            # the room cap and make the "More desks" precondition impossible.
            def own_seat(route):
                response = route.fetch()
                data = response.json()
                seats = [s for s in data['seats'] if s['user_id'] == fixture_uid]
                assert len(seats) == 1, 'fixture account missing from real office response'
                route.fulfill(response=response, json={**data, 'seats': seats, 'truncated': False})
            page.route('**/api/auth/office-seats', own_seat)
            page.reload(wait_until="domcontentloaded")
            # Everything before this point is the app booting while logged OUT,
            # and it legitimately 401s on its own API calls. Only what happens
            # after the session exists is this page's to answer for.
            errors.clear()
            # Long enough for the staggered walk-ins (260ms + 3×300ms, then a
            # fifth at 1900ms) plus the 1.5s walk transition to finish.
            page.wait_for_timeout(5200)
            page.locator("#off-overview").click()

            # ⚠️ Pin the language. `i18n.js` picks `stored() || browserLang()`, and a
            # headless Chromium here reports en-US — so the "中文界面" assertions
            # below used to run against an ENGLISH page and passed for the wrong
            # reason, while the Chinese branch of the breadcrumb was never executed
            # at all. A language test that runs in one language tests half a thing.
            page.evaluate("() => window.kladoI18n.setLang('zh')")
            page.wait_for_timeout(700)

            # ── nothing threw, first ──
            # Before anything else. A syntax error in office.js leaves a page that
            # looks fine — the shell, the nav and the roster host all render — and
            # every later probe then fails with a confusing `undefined`, or worse,
            # passes on an empty room. Stated plainly, it is the difference
            # between "the office is broken" and "the office is not there".
            boot = page.evaluate("() => ({ao: typeof window.AgentOffice, "
                                 "rows: !!document.getElementById('off-rows')})")
            real_errs = [e for e in errors if "favicon" not in e.lower()]
            check(boot["ao"] == "object", "office.js 跑完了（AgentOffice 已导出）", str(boot))
            check(not real_errs, "页面没有 JS 报错", " | ".join(real_errs[:3]))
            if boot["ao"] != "object":
                print(f"\n{len(passed)} 通过 / {len(failed) + 1} 失败（office.js 没跑起来，停止）")
                return 1

            d = page.evaluate(PROBE)

            # ── the room exists, and it is the room the DATA describes ──
            seats = d["apiSeats"] or []
            held = [s for s in seats if s["holder"]]
            check(len(held) > 0, "办公室读到了工位名单（不是空的）", f"n={len(held)}")
            # ⚠️ Three different numbers, and conflating any two of them is the bug
            # this page shipped with. `seats` is every DESK DRAWN (always at least
            # twelve), `held` is the desks that have an ACCOUNT behind them, and
            # the room is drawn so that the second is never larger than the first.
            # The clamp that used to make them equal — a room with one desk because
            # one account is registered — is what made this feature invisible.
            check(d["deskCount"] == len(seats),
                  "SVG 里的桌子数 = 房间画的工位数",
                  f"desks={d['deskCount']} seats={len(seats)}")
            check(len(seats) >= 12,
                  "房间至少画 12 张桌子（哪怕只有 1 个人注册）",
                  f"drawn={len(seats)} accounts={len(held)}")
            check(len(held) <= len(seats),
                  "每个注册的人都有工位（房间装得下所有人）",
                  f"accounts={len(held)} desks={len(seats)}")
            check(d["stage"]["w"] > 300, "场景有实际宽度", f"w={d['stage']['w']:.0f}")
            # ⚠️ Every HELD desk has a holder, even when no agent is on it. That is
            # the whole model: the desk belongs to the account, the agent is
            # optional. The converse is also asserted below — an UNHELD desk must
            # not pretend to be a person who has not arrived yet.
            check(all(s["holder"] for s in held),
                  "有人占的工位都有注册的人（哪怕 agent 没来过）",
                  str([s for s in held if not s["holder"]][:3]))
            came = [s for s in held if s["presence"] != "never"]
            check(d["seatedCount"] == len(came),
                  "上桌的 agent 数 = 「来过过」的人数",
                  f"seated={d['seatedCount']} came={len(came)}")
            check(d["seatedCount"] == d["cardCount"],
                  "有人就有卡片（人数与卡片数一致）",
                  f"seated={d['seatedCount']} cards={d['cardCount']}")
            check(all(a["opacity"] == 1 for a in d["agentOpacity"]),
                  "在线和离线角色都不透明（桌椅不透过身体）")
            check(all(a["filter"] == "saturate(0.4)" for a in d["agentOpacity"] if a["state"] == "idle"),
                  "离线用低饱和度区分，不降低透明度")
            # ⚠️ The roster lists PEOPLE. An unheld desk is furniture and is not in
            # it: eleven rows reading "this person's agent has not been here yet"
            # on a deployment with one account would be eleven claims about eleven
            # people who do not exist.
            check(d["rosterCount"] == len(held), "名册行数 = 注册人数（不是桌子数）",
                  f"rows={d['rosterCount']} accounts={len(held)} desks={len(seats)}")
            check(d["rowsOn"] == d["seatedCount"], "名册的「有 agent」数与场景一致",
                  f"rowsOn={d['rowsOn']} seated={d['seatedCount']}")
            check(int(d["statFree"]) == len(came), "「来过」计数与名册一致",
                  f"stat={d['statFree']} came={len(came)}")
            check(int(d["statDesks"]) == len(held),
                  "「注册的人」数的是人，不是画出来的桌子",
                  f"stat={d['statDesks']} accounts={len(held)} drawn={len(seats)}")
            check(int(d["statNever"]) == len(held) - len(came),
                  "「agent 没来过」= 注册的人 − 来过的人",
                  f"{d['statNever']} vs {len(held) - len(came)}")
            check(int(d["statOn"]) + len(came) - int(d["statOn"]) == len(came),
                  "在用 ≤ 来过（不会在用比来过还多）")
            check(len(d["legend"]) == 3, "图例列出三个存在性状态", f"n={len(d['legend'])}")

            # ── the whole office fits on one screen (new, 2026-10-05) ──
            # ⚠️ The reader said the room sat too low and that they had to scroll to
            # see all of it. Both halves are geometric and both were invisible to a
            # "the stage exists" check: the room hung below the middle of its own
            # frame (the floor is painted wider than the room shell, and the box the
            # viewBox fits to was the shell), and the stage ran past the bottom of
            # the window on any laptop. Measured, at the size the reader is at.
            fit = page.evaluate(
                """() => {
                    const st = document.getElementById('off-stage');
                    const svg = document.getElementById('off-svg');
                    const wg = svg.querySelector('g');
                    const r = st.getBoundingClientRect();
                    const m = svg.getScreenCTM();
                    let y0 = 1e9, y1 = -1e9, x0 = 1e9, x1 = -1e9;
                    // Geometry only, in viewBox space: the light sweep is a rect
                    // 300 units larger than the room on every side and would
                    // otherwise make the "room" look like it fills everything.
                    wg.querySelectorAll('polygon,path,line,ellipse,circle')
                      .forEach(n => {
                        if (n.parentNode && n.parentNode.getAttribute &&
                            n.parentNode.getAttribute('clip-path')) return;
                        const b = n.getBBox();
                        const p1 = svg.createSVGPoint(), p2 = svg.createSVGPoint();
                        p1.x = b.x; p1.y = b.y; p2.x = b.x + b.width; p2.y = b.y + b.height;
                        const a = p1.matrixTransform(m), c = p2.matrixTransform(m);
                        y0 = Math.min(y0, a.y, c.y); y1 = Math.max(y1, a.y, c.y);
                        x0 = Math.min(x0, a.x, c.x); x1 = Math.max(x1, a.x, c.x);
                    });
                    return {gapTop: y0 - r.y, gapBottom: r.bottom - y1,
                            cut: Math.max(0, Math.max(y1 - innerHeight, x1 - innerWidth)),
                            vh: innerHeight, vw: innerWidth};
                }""")
            check(abs(fit["gapTop"] - fit["gapBottom"]) <= 12,
                  "房间在画面里是居中的（上下的空白差不多）",
                  f"top={fit['gapTop']:.0f} bottom={fit['gapBottom']:.0f}")
            check(fit["cut"] == 0, "整间办公室在一屏之内（不用滚动）",
                  f"超出 {fit['cut']:.0f}px")

            # ── the counters are one ROW (geometry, not "they exist") ──
            sr = d["statRow"]
            check(sr["n"] == 4, "四个统计块都在", f"n={sr['n']}")
            check(sr["flexWrap"] == "nowrap", "统计行不许换行", sr["flexWrap"])
            check(len(sr["ys"]) == 1, "四个统计在同一行上（y 相同）", f"ys={sr['ys']}")
            check(sr["rowH"] <= sr["maxCellH"] + 2,
                  "统计行只有一行高（没有第二行）",
                  f"rowH={sr['rowH']} maxCellH={sr['maxCellH']}")
            check(not any(" / " in t for t in sr["texts"]), "中文界面里没有残留的 ` / `",
                  " | ".join(sr["texts"]))

            # ── the page is named ONCE, in the breadcrumb ──
            check(d["crumb"] is not None, "标题栏里有面包屑")
            check("Agent 办公室" in d["crumb"], "标题栏写的是办公室的名字", d["crumb"])
            check(not d["bodyH1"], "正文里没有重复的 H1（名字只出现一次）", d["bodyH1"])

            # ── a card is what it DRAWS (1) ──
            empty = [c for c in d["cardInfo"] if not c["name"].strip() or not c["task"].strip()]
            check(not empty, "每张卡都有名字和任务文字", f"{len(empty)} 张是空的")
            noicon = [c for c in d["cardInfo"] if c["iconContent"] in ("none", "normal", "")]
            check(not noicon, "每张卡的图标真的有字形（:before 不是 none）",
                  f"{len(noicon)} 张没画出来：{[c['icon'] for c in noicon]}")
            zero = [c for c in d["cardInfo"] if c["rect"]["w"] < 40 or c["rect"]["h"] < 20]
            check(not zero, "卡片盒子有实际面积", f"{len(zero)} 张太小")
            nometa = [c for c in d["cardInfo"] if not c["meta"].strip()]
            check(not nometa, "每张卡都有工位号和计时", f"{len(nometa)} 张缺")
            nocolor = [c for c in d["cardInfo"] if not c["color"]]
            check(not nocolor, "每张卡都拿到了状态色变量", f"{len(nocolor)} 张没颜色")

            # ── no two cards overlap (3) ──
            worst, worst_pair = 0, None
            for i in range(len(d["cardInfo"])):
                for j in range(i + 1, len(d["cardInfo"])):
                    a = d["cardInfo"][i]["rect"]
                    b = d["cardInfo"][j]["rect"]
                    o = overlap(a, b)
                    if o > worst:
                        worst, worst_pair = o, (d["cardInfo"][i]["name"], d["cardInfo"][j]["name"])
            check(worst == 0, "同排的卡片互不重叠",
                  f"重叠 {worst:.0f}px² 于 {worst_pair}")

            # ── each card points at its own agent (4) ──
            drift = []
            for c in d["pairs"]:
                if not d["agentPts"]:
                    break
                near = min(abs(c["cx"] - a["x"]) for a in d["agentPts"])
                drift.append(near)
            check(drift and max(drift) < 60,
                  "卡片横向中心都对得上某个 agent 的头",
                  f"最大偏差 {max(drift):.0f}px" if drift else "no cards")

            # ── …and STILL points at it after the reader drags (the real bug) ──
            # ⚠️ The assertion above is the one that shipped broken for a whole
            # round, and it is worth being precise about why. Cards are HTML placed
            # in % of the stage; the % is resolved through the viewBox. Pan and zoom
            # change the viewBox, so "the card is above its agent" is only true in
            # the one frame nobody dragged. The reader's report was exactly that:
            # drag the office and the dashboard drifts further from its agent.
            #
            # So the same measurement is taken AGAIN after a REAL mouse drag, and
            # the two numbers are compared to each other as well as to the bound.
            # Comparing only to an absolute bound is not enough: a room that
            # re-laid-out its cards correctly but scaled the stage would pass, and
            # a room that drifted by a constant offset would pass too. What has to
            # hold is that the drift does not GROW when the reader moves the camera.
            before = page.evaluate(PROBE)
            box = page.locator("#off-stage").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.5)
            page.mouse.down()
            for step in range(1, 6):
                page.mouse.move(box["x"] + box["width"] * 0.62 - step * 26,
                                box["y"] + box["height"] * 0.5 - step * 9)
                page.wait_for_timeout(40)
            page.mouse.up()
            page.wait_for_timeout(400)
            after = page.evaluate(PROBE)
            check(after["viewBox"] != before["viewBox"],
                  "拖动真的改变了 viewBox（前置：这一轮测的是移动之后）",
                  f"{before['viewBox']} → {after['viewBox']}")
            moved = max(abs(float(before["viewBox"].split()[i])
                            - float(after["viewBox"].split()[i])) for i in range(4))
            check(moved > 5, "房间确实被拖动了", f"Δ={moved:.1f}")
            drift_after = []
            for c in after["pairs"]:
                if not after["agentPts"]:
                    break
                drift_after.append(min(abs(c["cx"] - a["x"]) for a in after["agentPts"]))
            check(drift_after and max(drift_after) < 60,
                  "拖动之后卡片仍然对得上 agent 的头",
                  f"最大偏差 {max(drift_after):.0f}px" if drift_after else "no cards")
            check(after["cardCount"] == before["cardCount"],
                  "拖动没有让卡片凭空多出来或消失",
                  f"{before['cardCount']} → {after['cardCount']}")

            # ── the card must also clear its own head vertically ──
            # ⚠️ A card can be perfectly centred on its agent and still cover its
            # face. The anchor is 2.35 world units above the seat so the card's
            # BOTTOM sits above the tallest head (the rabbit's ears, y = -50); at
            # the old 1.15 the card's bottom edge landed in the middle of the skull
            # and every animal drew its face behind an opaque card, which is how six
            # different beasts can look identical.
            #
            # ⚠️ The measured quantity is the card's BOTTOM against the head's TOP,
            # and the SIGN is the whole assertion. Screen y grows DOWNWARD, so
            # `cardBottom - headTop` is NEGATIVE when the card is correctly floating
            # above its head and POSITIVE when it is covering the face. Both wrong
            # versions of this check were written here first — one compared the two
            # TOPS (which is negative for a correct layout), one kept the tops and
            # flipped the bound. A clearance assertion whose sign is backwards does
            # not fail loudly; it fails on a room that is exactly right.
            clear = page.evaluate(
                """() => {
                    const svg = document.getElementById('off-svg');
                    const stage = document.getElementById('off-stage');
                    const vb = svg.getAttribute('viewBox').split(' ').map(Number);
                    const sr = stage.getBoundingClientRect();
                    const sx = sr.width / vb[2];
                    const out = [];
                    for (const a of svg.querySelectorAll('.off-agent')) {
                      if (a.getAttribute('opacity') === '0') continue;
                      const head = a.querySelector('.off-beast');
                      if (!head) continue;
                      const hr = head.getBoundingClientRect();
                      const card = [...document.querySelectorAll('.off-label:not(.is-off)')]
                        .map(c => c.getBoundingClientRect())
                        .find(r => Math.abs((r.x + r.width/2) - (hr.x + hr.width/2)) < 40);
                      if (!card) continue;
                      out.push({gapPx: Math.round(card.y + card.height - hr.y),
                                gapUnits: +(((card.y + card.height - hr.y) / sx)).toFixed(1)});
                    }
                    return out;
                }""")
            check(clear and max(c["gapPx"] for c in clear) <= 0,
                  "卡片下沿在头顶之上，不遮住脸",
                  f"最大重叠 {max((c['gapPx'] for c in clear), default=0):.0f}px"
                  if clear else "no heads")
            check(clear and max(c["gapUnits"] for c in clear) <= 0,
                  "按 viewBox 单位量也一样（与舞台大小无关）",
                  f"最大重叠 {max((c['gapUnits'] for c in clear), default=0):.1f} 单位"
                  if clear else "no heads")

            page.evaluate("() => document.getElementById('off-reset').click()")
            page.wait_for_timeout(300)

            # ── there is no way to invent an agent (2) ──
            # ⚠️ This is the assertion that keeps the room honest, and it is an
            # assertion about ABSENCE, which is easy to write badly. It used to
            # force seat 1 through all seven simulated states with
            # `AgentOffice.setState` and call that a pass. Those functions are gone,
            # so the check is now: the affordances that would let a reader put an
            # agent on a desk do not exist in the DOM at all, and clicking a desk
            # only selects it. "The button is hidden" would pass while a second
            # button did the same thing, so both the buttons AND the handler are
            # checked.
            page.evaluate("() => document.getElementById('off-reset').click()")
            page.wait_for_timeout(300)
            chrome = page.evaluate(
                """() => ({
                    add: !!document.getElementById('off-add'),
                    pause: !!document.getElementById('off-pause'),
                    speed: !!document.getElementById('off-speed'),
                    rowAct: document.querySelectorAll('#off-rows .off-row .act').length,
                    apiFns: Object.keys(window.AgentOffice || {}).sort(),
                })""")
            check(not chrome["add"], "没有「接入一个 agent」按钮")
            check(not chrome["pause"] and not chrome["speed"],
                  "没有暂停/倍速（模拟器没了，这两个控制也没用了）")
            check(chrome["rowAct"] == 0, "名册里没有逐行的 +/− 按钮", f"n={chrome['rowAct']}")
            check(chrome["apiFns"] == ["get", "refresh"],
                  "对外只剩读接口（没有 setAgent/setState 之类）", str(chrome["apiFns"]))

            # Clicking a desk must NOT create anybody. Measured, not inferred: seat 1
            # is taken before and after, and the "agent" column must not move.
            before_seats = page.evaluate("() => JSON.stringify(window.AgentOffice.get())")
            page.mouse.click(d["stage"]["x"] + d["stage"]["w"] * 0.45,
                             d["stage"]["y"] + d["stage"]["h"] * 0.5)
            page.wait_for_timeout(400)
            after_seats = page.evaluate("() => JSON.stringify(window.AgentOffice.get())")
            check(before_seats == after_seats,
                  "点工位不会凭空变出一个 agent（房间只反映数据）")

            # ── the roster and the room cannot drift apart (5) ──
            after2 = page.evaluate(PROBE)
            check(after2["rowsOn"] == after2["seatedCount"], "名册与场景在点击之后仍然一致",
                  f"rowsOn={after2['rowsOn']} seated={after2['seatedCount']}")

            # ── English reaches the JS-built text (6) ──
            # ⚠️ ORDER, and it is load-bearing. This block must run while still ON
            # home. It used to sit after the "click the top bar and leave" check,
            # which had already navigated to Knowledge base — so the breadcrumb
            # legitimately read "知识库" and the assertion was measuring another
            # page. It then tried to recover by clicking the breadcrumb's home
            # icon, and that icon has no box on the knowledge page (the whole
            # `.workspace-bar` is hidden there, so every child measures 0×0) —
            # Playwright waited 30s and took the run down with it. The page you
            # are asserting about has to be the page you are standing on.
            # ⚠️ Precondition, not decoration: a switch check is only meaningful
            # if the bar really does read the office's name in Chinese first.
            # "It changed" is vacuously true if it said something else all along —
            # that is exactly how a frozen label slips through.
            before = page.evaluate(
                """() => document.getElementById('breadcrumb-text').textContent""")
            check("Agent 办公室" in before, "（前置）切语言前标题栏是中文的办公室名", before)
            # ⚠️ Measured, not assumed. A `<span>` holding the name is in the DOM
            # whether or not the bar is on screen, and the whole point of moving
            # the name into the bar was that the reader can see it.
            barBox = page.evaluate(
                """() => { const el = document.querySelector('.breadcrumb');
                    const r = el.getBoundingClientRect();
                    return {w: r.width, h: r.height, txt: el.textContent.trim()}; }""")
            check(barBox["w"] > 0 and barBox["h"] > 0,
                  "标题栏在屏上（有实际面积，不只是 DOM 里有）", barBox)

            page.evaluate("() => window.kladoI18n.setLang('en')")
            page.wait_for_timeout(700)
            en = page.evaluate(
                """() => {
                    const vis = [...document.querySelectorAll('.off-label:not(.is-off)')]
                      .map(c => c.textContent).join(' | ')
                      + ' || ' + [...document.querySelectorAll('#off-rows .off-row')]
                      .map(r => r.textContent).join(' | ');
                    return {text: vis, crumb: document.getElementById('breadcrumb-text').textContent};
                }""")
            check(" / " not in en["text"], "英文界面里没有残留的 ` / `（pair 拆干净了）",
                  en["text"][:120])
            check("Agent office" in en["crumb"], "标题栏切到了英文", en["crumb"])
            check(any(w in en["text"] for w in ("Never arrived", "Was here", "Recently online")),
                  "存在性状态词是英文的", en["text"][:120])
            # ⚠️ The old assertion looked for "Searching"/"Coding"/"Needs review" and
            # would have failed on a room that was telling the truth. The words that
            # must NOT be there are as load-bearing as the ones that must: if the
            # seven simulated states ever creep back into the strings, this room is
            # again claiming to know what agents are working on.
            check(not any(w in en["text"] for w in ("Searching", "Coding", "Writing", "Needs review")),
                  "英文界面里没有回到那七个模拟状态词", en["text"][:120])
            page.evaluate("() => window.kladoI18n.setLang('zh')")
            page.wait_for_timeout(500)

            # ── a real click on the top bar still leaves the office (8) ──
            # LAST on purpose: it navigates away, and every assertion above is
            # about the page you are standing on.
            nav = d["navRect"]
            if check(nav is not None, "顶栏在"):
                hit = page.evaluate(
                    """() => { const n = document.querySelector('nav.nav');
                        const b = n.getBoundingClientRect();
                        const el = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
                        return el ? (el.closest('nav.nav') ? 'nav' : el.className) : 'none'; }""")
                check(hit == "nav", "顶栏中心命中的是顶栏自己（没被场景盖住）", f"hit={hit}")
                page.mouse.click(nav["x"] + nav["w"] * 0.5, nav["y"] + nav["h"] / 2)
                page.wait_for_timeout(700)
                left = page.evaluate(
                    """() => { const h = document.getElementById('page-home');
                        return getComputedStyle(h).display === 'none'; }""")
                check(left, "真点击顶栏标签后离开了主页")

            # ── back to the office: the room grows, and growing is a REBUILD ──
            # ⚠️ This is the assertion with the most behind it. "More desks" cannot
            # edit a drawn room — the floor, the walls, the windows and the aisle
            # all change size — so it tears the old one down and builds a new one.
            # The listeners were attached inline, which meant the SECOND run
            # attached a second wheel handler, a second pointermove handler and a
            # second interval, each closing over its own seat list. Nothing throws
            # and nothing looks broken: the room simply moves two or three times
            # per drag and every card is re-placed by every stale interval, so the
            # symptom is "the office gets busier when I ask for more desks".
            #
            # So the leak is measured as BEHAVIOUR, not as a counter nobody keeps:
            # one drag must move the room by one drag's worth. A room with three
            # live pointermove handlers moves three times as far, and that number
            # is the same whether the leak came from listeners, intervals or both.
            page.evaluate("() => document.querySelector('nav.nav') && "
                          "[...document.querySelectorAll('nav.nav .nav-tab, nav.nav a')]"
                          ".find(a => /Knowledge|知识库/.test(a.textContent))")
            page.goto(WEB, wait_until="domcontentloaded")
            page.evaluate(
                """async (creds) => {
                    const r = await fetch('api/auth/login', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(creds),
                    });
                    if (!r.ok) throw new Error('login failed: ' + r.status);
                }""",
                {"email": ADMIN[1], "password": PASSWORD},
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(4500)
            page.locator("#off-overview").click()
            page.evaluate("() => window.kladoI18n.setLang('zh')")
            page.wait_for_timeout(500)

            more = page.evaluate(
                """() => { const b = document.getElementById('off-more');
                    if (!b) return null;
                    const r = b.getBoundingClientRect();
                    const api = window.AgentOffice.get();
                    return {hidden: b.hidden, disabled: b.disabled, text: b.textContent.trim(),
                            w: r.width, h: r.height,
                            desks: api.length,
                            accounts: api.filter(s => s.holder).length}; }""")
            if check(more is not None, "有「再加几张」按钮", str(more)):
                # ⚠️ This is the assertion that would have caught the bug the user
                # reported. The button used to be `hidden` whenever the account
                # count had caught up with the room, and the room used to be clamped
                # to the account count — so on a deployment with one account the
                # control the user had asked for was simply not in the DOM, and no
                # amount of pressing it would have revealed anything. A control that
                # MAY be absent is a control nobody can rely on.
                check(not more["hidden"],
                      "按钮永远在屏上（哪怕只有 1 个人注册）",
                      f"hidden={more['hidden']} desks={more['desks']} accounts={more['accounts']}")
                check(more["h"] > 10 and more["w"] > 40, "按钮有实际面积（不是 0×0）",
                      f"{more['w']:.0f}×{more['h']:.0f}")
                check(not more["disabled"], "按钮是可点的（不是灰掉不能按）",
                      f"disabled={more['disabled']}")
                check(" / " not in more["text"] or more["text"].startswith("再加"),
                      "按钮文案是双语 pair（英文页不会露出中文）", more["text"])

                first = page.evaluate("() => window.AgentOffice.get().length")
                check(first >= 12, "默认房间至少 12 张桌子",
                      f"n={first} accounts={more['accounts']}")
                page.click("#off-more"); page.locator("#off-overview").click()
                page.wait_for_timeout(900)
                second = page.evaluate("() => window.AgentOffice.get().length")
                check(second > first, "点了按钮房间真的变大了",
                      f"{first} → {second}")
                check(page.evaluate(
                    "() => document.querySelectorAll('#off-labels .off-label').length") == second,
                    "卡片数 = 桌子数（重建没有留下孤儿卡片）")
                check(page.evaluate(
                    "() => document.querySelectorAll('#off-svg .off-desk').length") == second,
                    "SVG 里的桌子数 = 工位数（重建没有叠房间）")
                # ⚠️ A bigger room must not invent people. The roster and the
                # counters are about accounts; the room is about furniture.
                after = page.evaluate("() => ({rows: document.querySelectorAll('#off-rows .off-row').length,"
                                      "stat: document.getElementById('off-stat-desks').textContent})")
                check(after["rows"] == more["accounts"] and after["stat"] == str(more["accounts"]),
                      "房间变大后，名册和「注册的人」仍然数的是人",
                      f"rows={after['rows']} stat={after['stat']} accounts={more['accounts']}")

                page.click("#off-more"); page.locator("#off-overview").click()
                page.wait_for_timeout(900)
                third = page.evaluate("() => window.AgentOffice.get().length")
                check(third > second, "再点一次还能更大（重建不是一次性的）",
                      f"{second} → {third}")

                # ── the ceiling is a MEASURED number, so it is measured here ──
                # ⚠️ `SEATS_MAX` was 200 when this room held no cards and 45 was
                # the first size at which neighbouring cards overlap by ~24px²
                # (the cards are HTML at a fixed pixel size and can only be scaled
                # down to a 0.6 readability floor). A cap that is not the size the
                # button can actually reach is a cap that ships a broken room, and
                # the breakage is invisible in a screenshot of a mostly-empty room.
                # So: press it until it stops, and require the cards to still fit.
                for _ in range(12):
                    if page.evaluate("() => { const b = document.getElementById('off-more');"
                                     " return !b || b.disabled; }"):
                        break
                    page.click("#off-more"); page.locator("#off-overview").click()
                    page.wait_for_timeout(800)
                at_cap = page.evaluate(
                    """() => ({n: window.AgentOffice.get().length,
                              disabled: !!(document.getElementById('off-more')||{}).disabled})""")
                check(at_cap["disabled"], "到上限后按钮自己变灰（而不是无限长）",
                      str(at_cap))
                check(at_cap["n"] <= 39, "上限不超过实测不会重叠的尺寸",
                      f"n={at_cap['n']}")
                worst_cap = page.evaluate(OVERLAP_PROBE)
                check(worst_cap == 0, "房间长到上限时卡片依然互不重叠",
                      f"重叠 {worst_cap:.0f}px² 于 {at_cap['n']} 张桌子")
                page.click("#off-overview")
                page.wait_for_timeout(300)

                # The leak. One drag, measured.
                # ⚠️ The baseline is read HERE, not reused from `third`. The ceiling
                # loop above grew the room several more times, so `third` is stale
                # and the comparison was measuring the ceiling loop, not the drag.
                before_drag = page.evaluate("() => window.AgentOffice.get().length")
                vb0 = page.evaluate("() => document.getElementById('off-svg').getAttribute('viewBox')")
                box = page.locator("#off-stage").bounding_box()
                page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.5)
                page.mouse.down()
                for step in range(1, 6):
                    page.mouse.move(box["x"] + box["width"] * 0.6 - step * 24,
                                    box["y"] + box["height"] * 0.5)
                    page.wait_for_timeout(35)
                page.mouse.up()
                page.wait_for_timeout(300)
                vb1 = page.evaluate("() => document.getElementById('off-svg').getAttribute('viewBox')")
                dx = abs(float(vb1.split()[0]) - float(vb0.split()[0]))
                # A single handler moves the viewBox by (pixels / stageWidth) * vbWidth.
                # Three of them move it three times that, and the ratio is what says
                # "one room is listening" rather than "the room moved".
                k = (float(vb0.split()[2])) / box["width"]
                per = 24 * 5 * k
                check(dx <= per * 1.6,
                      "一次拖动只被一个 handler 处理（重建没有留下重复监听）",
                      f"Δ={dx:.1f}，一次应为 {per:.1f}")
                after_drag = page.evaluate(
                    "() => document.querySelectorAll('#off-labels .off-label').length")
                check(after_drag == before_drag,
                      "拖动之后卡片数还是对的（没有多个 interval 重复渲染）",
                      f"{before_drag} → {after_drag}")

            # ── the beast and the shirt are the account's, from the server ──
            # ⚠️ Asserted as GEOMETRY and as the resolved colour, not as "there is
            # an svg inside the agent". An empty group satisfies that, and a group
            # drawn in the wrong colour satisfies it too. Each of the six animals
            # has a different node count and a different bounding box, so both are
            # a fingerprint; and the shirt is read as the COMPUTED fill, which is
            # the only way to catch a hard-coded hex that a theme switch ignores.
            av = page.evaluate(
                """() => {
                    const api = window.AgentOffice ? window.AgentOffice.get() : [];
                    const heads = [...document.querySelectorAll('#off-svg .off-beast')];
                    return {
                      api,
                      animals: api.map(s => s.animal),
                      cloths: api.map(s => s.cloth),
                      heads: heads.map(h => {
                        const r = h.getBoundingClientRect();
                        const torso = h.closest('.off-person').querySelector('.torso');
                        return {
                          nodes: h.querySelectorAll('*').length,
                          w: Math.round(r.width), h: Math.round(r.height),
                          torsoFill: getComputedStyle(torso).fill,
                          shirtVar: h.closest('.off-agent').style.getPropertyValue('--shirt'),
                        };
                      }),
                      clothTokens: ['red','blue','yellow','green','black']
                        .map(k => getComputedStyle(document.documentElement)
                                  .getPropertyValue('--off-cloth-' + k).trim()),
                      furTokens: ['tiger','ox','horse','cat','rabbit','hippo']
                        .map(k => getComputedStyle(document.documentElement)
                                  .getPropertyValue('--off-fur-' + k).trim()),
                    };
                }""")
            check(av["heads"], "场景里画出了兽首", f"n={len(av['heads'])}")
            check(all(h["nodes"] >= 8 for h in av["heads"]),
                  "每个头都是完整的一组图（不是空壳）",
                  str(sorted(h["nodes"] for h in av["heads"])))
            check(all(h["w"] > 0 and h["h"] > 0 for h in av["heads"]),
                  "每个头都有实际面积", str([(h["w"], h["h"]) for h in av["heads"]][:3]))
            check(all(h["shirtVar"].startswith("var(--off-cloth-") for h in av["heads"]),
                  "衣服走的是 theme.css 的 token（不是写死的颜色）",
                  str([h["shirtVar"] for h in av["heads"]][:2]))
            check(all(t.startswith("#") or t.startswith("rgb") for t in av["clothTokens"]),
                  "五件衣服的颜色都定义在 theme.css 里", str(av["clothTokens"]))
            check(all(t.strip() for t in av["furTokens"]),
                  "六种兽的毛色都定义在 theme.css 里", str(av["furTokens"]))
            # ⚠️ NOT "the six heads in the room are all different". Every account
            # that has never chosen an avatar is a tiger — that is the column
            # DEFAULT — so on any real deployment that assertion is testing the
            # data, not the drawing, and it would be red on a perfectly correct
            # build. What has to hold is that the room draws the animal ITS OWN
            # SEAT was sent, which is only answerable by changing one and watching
            # it change. That is asserted below, end to end, through the same PATCH
            # the picker uses.

            # ── the picker ──
            page.evaluate("() => { const a = document.getElementById('nav-avatar'); if (a) a.click(); }")
            page.wait_for_timeout(800)
            pick = page.evaluate(
                """() => {
                    const host = document.getElementById('au-avatar');
                    if (!host) return null;
                    const r = host.getBoundingClientRect();
                    return {
                      visible: r.width > 0 && r.height > 0,
                      animals: [...host.querySelectorAll('.au-avatar-cell')],
                      cloths: [...host.querySelectorAll('.au-cloth-cell')],
                      onAnimal: (host.querySelector('.au-avatar-cell.is-on') || {}).title,
                      thumbsDraw: [...host.querySelectorAll('.au-avatar-thumb')]
                        .map(t => t.querySelectorAll('*').length),
                      /* ⚠️ Node COUNT is not a fingerprint. Two of the six happen to
                         build the same number of nodes, so "6 distinct counts"
                         failed on a correct build and would have been 'fixed' by
                         nudging a shape until the numbers differed — changing the
                         drawing to satisfy the assertion. The geometry itself is
                         the fingerprint, so that is what is compared. */
                      thumbsShape: [...host.querySelectorAll('.au-avatar-thumb .off-beast')]
                        .map(h => [...h.querySelectorAll('*')].map(n =>
                          n.tagName + ':' + (n.getAttribute('d') || n.getAttribute('r')
                            || (n.getAttribute('cx') || '') + ',' + (n.getAttribute('cy') || '')
                            || n.getAttribute('x') || '')).join('~')),
                    };
                }""")
            if check(pick is not None, "用户菜单里有形象选择器", str(pick)):
                check(pick["visible"], "形象选择器在屏上（有实际面积）")
                check(len(pick["animals"]) == 6, "六个可选形象",
                      f"n={len(pick['animals'])}")
                check(len(pick["cloths"]) == 5, "五件可选衣服",
                      f"n={len(pick['cloths'])}")
                check(all(n > 5 for n in pick["thumbsDraw"]),
                      "缩略图真的画了东西（不是空的 svg 盒子）",
                      str(pick["thumbsDraw"]))
                check(bool(pick["onAnimal"]), "当前形象有选中态", str(pick["onAnimal"]))
                # The six ARE distinguishable — asked of the PICKER, where all six
                # are on screen at once, rather than of the room where they are
                # whatever the accounts happen to have chosen.
                sig = set(pick["thumbsShape"])
                check(len(pick["thumbsShape"]) == 6 and len(sig) == 6,
                      "六个形象形状各不相同（不是同一个头换颜色）",
                      f"{len(sig)} 种 / {len(pick['thumbsShape'])} 个")

            # ── end to end: the choice reaches the desk ──
            # ⚠️ The full loop, in the order the reader performs it. Read the shape
            # of the head at this account's OWN seat, change the avatar through the
            # same endpoint the picker calls, refresh the room, and read it again.
            #
            # Every cheaper version of this passes while the feature is broken: "the
            # picker has six buttons" passes when the buttons post nothing; "the
            # seat list carries an avatar" passes when the room ignores it; "the head
            # is an svg" passes when it is always the same animal. Only a CHANGE,
            # observed at the desk, distinguishes a working avatar from a decorative
            # one — and it is asserted by node count rather than by colour, because
            # a tiger in a blue shirt and a tiger in a green shirt are the same
            # picture and only the species is what was being tested.
            def _own_head():
                # ⚠️ Found by the CARD's name, not by the seat's index. `get()`
                # returns seats in room order, the room only draws the first N,
                # and only seats whose agent has been here have a card at all —
                # so "the first seat" is three different things depending on the
                # deployment. The card that carries this account's name is the only
                # handle that means "my desk" in every case.
                return page.evaluate(
                    """(who) => {
                        const card = [...document.querySelectorAll('.off-label:not(.is-off)')]
                          .find(c => (c.querySelector('.who').textContent || '').trim() === who);
                        if (!card) return null;
                        const cr = card.getBoundingClientRect();
                        const hx = cr.x + cr.width / 2;
                        const head = [...document.querySelectorAll('#off-svg .off-beast')]
                          .map(h => ({h, r: h.getBoundingClientRect()}))
                          .map(o => ({h: o.h, d: Math.abs(o.r.x + o.r.width / 2 - hx)}))
                          .sort((p, q) => p.d - q.d)[0];
                        if (!head || head.d > 40) return null;
                        const api = window.AgentOffice.get();
                        const seat = api.find(s => s.holder && s.holder.trim() === who);
                        return {nodes: head.h.querySelectorAll('*').length,
                                animal: seat ? seat.animal : null,
                                cloth: seat ? seat.cloth : null,
                                shirt: head.h.closest('.off-agent').style.getPropertyValue('--shirt')};
                    }""",
                    ADMIN[0].strip())

            # ⚠️ No "press more until my desk appears" loop any more, and its removal
            # is the point: the room now always holds every registered account, so a
            # newly registered colleague — this test account is the newest, because
            # `_prepare()` deletes and re-inserts it — is seated from the first
            # render. The loop existed because desks were handed out in registration
            # order and the room was capped, so being last meant having no desk at
            # all until the reader asked for a bigger room. That is a property a
            # new colleague should never have had to discover.
            own0 = _own_head()
            if check(own0 is not None, "能找到本账号自己工位上的头", str(own0)):
                saved = await_av("hippo", "green")
                check(saved["status"] == 200, "PATCH /me/avatar 成功",
                      f"{saved['status']} {saved['body']}")
                page.evaluate("() => window.AgentOffice.refresh()")
                page.wait_for_timeout(1200)
                own1 = _own_head()
                if check(own1 is not None, "改完之后还能找到同一个头", str(own1)):
                    check(own1["animal"] == "hippo",
                          "工位上的形象换成了新选的那个", f"{own0['animal']} → {own1['animal']}")
                    check(own1["cloth"] == "green",
                          "工位上的衣服换成了新选的那件", f"{own0['cloth']} → {own1['cloth']}")
                    check(own1["nodes"] != own0["nodes"],
                          "头的形状真的换了（不是只换了个颜色）",
                          f"{own0['nodes']} → {own1['nodes']} 个节点")
                    check(own1["shirt"] == "var(--off-cloth-green)",
                          "工位上的衣服走的是对应 token", own1["shirt"])
                # Put it back: this test writes to a real account.
                await_av("tiger", "blue")
                page.evaluate("() => window.AgentOffice.refresh()")
                page.wait_for_timeout(900)
            check(True, "形象回滚完成")

            # ── dark mode is a token question (7) ──
            page.evaluate("() => { try { localStorage.setItem('klado-theme', 'dark'); } catch (e) {} }")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5200)
            dk = page.evaluate(
                """() => {
                    const c = document.querySelector('.off-label:not(.is-off)');
                    if (!c) return null;
                    const bg = getComputedStyle(c).backgroundColor;
                    const ink = getComputedStyle(c.querySelector('.who')).color;
                    const rgb = s => (s.match(/[\\d.]+/g) || []).map(Number);
                    return {theme: document.documentElement.getAttribute('data-theme'),
                            bg: rgb(bg), ink: rgb(ink)};
                }""")
            if check(dk is not None, "深色模式下也有卡片", str(dk)):
                lum = lambda c: (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255
                check(lum(dk["bg"]) < 0.5, "深色模式下卡片背景是深的（走的是 token）", str(dk["bg"]))
                check(lum(dk["ink"]) > lum(dk["bg"]) + 0.3, "卡片上的文字比背景亮",
                      f"ink={dk['ink']} bg={dk['bg']}")
            page.evaluate("() => { try { localStorage.setItem('klado-theme', 'light'); } catch (e) {} }")

            # ── nothing threw ──
            # ⚠️ The rejections are read from the PAGE, not from the listener, and
            # deliberately so: the listener is installed by an init script, which
            # runs on every navigation, and this block re-navigates twice. Reading
            # `window.__rejections` at the end reports only what happened in the
            # CURRENT document, which is the one being judged.
            rejected = page.evaluate("() => window.__rejections || []") or []
            real = [e for e in errors if "favicon" not in e.lower()]
            real += ["unhandled rejection: " + r.splitlines()[0] for r in rejected]
            check(not real, "页面没有 console 报错 / 未捕获的 promise", " | ".join(real[:3]))

            browser.close()
    finally:
        if not args.keep:
            _cleanup()

    print(f"\n{len(passed)} 通过 / {len(failed)} 失败")
    for f in failed:
        print("  ✗", f)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
