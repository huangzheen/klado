#!/usr/bin/env python3
"""Screenshot `settings/admin` in a real browser, in both themes, as BOTH audiences.

Why a browser and not assertions on the CSS
--------------------------------------------
P6 replaced this page wholesale — four cards, two of them new — and the defects worth
catching are exactly the ones no stylesheet check sees:

* **A page that renders but is unreadable in dark mode.** Every colour is a token, and a
  token that resolves to the light value in dark mode is invisible to a static check: it
  is a correctly-formatted `var()`. Only `data-theme="dark"` + `getComputedStyle` knows.
* **A control whose states are indistinguishable.** The module matrix has THREE states
  and this app ships no icon font, so the cells had to be drawn with text. The old
  `<i class="ri-…">` glyphs rendered as nothing at all, which made "off" and "inherit"
  two identical empty squares — a three-state control that showed two. That is a
  `textContent` assertion, not a CSS one.
* **Layout that has collapsed into something else.** A card that is `display: flex` with
  no `gap` reads as fine in a screenshot until you measure the boxes.
* **A bilingual string that did not split.** The copy is `中文 / English`, and a page can
  render perfectly while showing the reader both languages at once.

And the discipline from the rest of this project: **a screenshot that is only looked at
proves nothing.** So every screenshot is paired with an assertion, and each assertion
names the failure it would have caught.

⚠️ It creates a real company with two accounts and one publication request, signs in as
the company owner AND as a platform operator, and removes everything afterwards —
including on failure.

    cd api && ../.venv312/bin/python tests/shot_settings_admin_ui.py
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from playwright.sync_api import sync_playwright            # noqa: E402

from klado_shared import accounts, orgs                    # noqa: E402
from klado_shared.db import connect_main                   # noqa: E402

APP = os.environ.get("KLADO_APP_URL", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SETTINGS_SHOTS", "/tmp/klado-settings-shots")
PASSWORD = "Settings-Shot-Probe-1!"

#: 1280 is the narrowest a laptop gets in practice, and a four-column table inside a
#: card inside a scroller is exactly where a grid silently collapses.
VIEWPORTS = [(1440, 900), (1280, 800)]

PASS, FAIL = 0, []
RESULTS: list[str] = []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        RESULTS.append(f"  ok   {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        RESULTS.append(f"  FAIL {name}  {detail}")
    print(RESULTS[-1])
    return ok


def rgb(css: str) -> tuple:
    return tuple(int(float(n)) for n in re.findall(r"[\d.]+", css)[:3])


def _lum(c: tuple) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _lum(rgb(a)), _lum(rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def sign_in(page, email, password, tag=""):
    page.goto(APP + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#auth-login-email", timeout=20000)
    page.fill("#auth-login-email", email)
    page.fill("#auth-login-password", password)
    page.click("#auth-login-btn")
    page.wait_for_timeout(2500)
    if page.locator("#auth-gate").is_visible():
        page.screenshot(path=os.path.join(SHOTS, f"FAILED-signin-{tag}.png"))
        raise SystemExit(f"could not sign in as {email}; screenshot written")


def open_settings(page, tag):
    """Drive the real path: click the tab, because a hidden page times out on fill()."""
    page.click("#nav-tab-settings")
    page.wait_for_selector("#adm-users-body", timeout=20000)
    page.wait_for_timeout(2500)


def _marked_presets(page):
    """Which preset buttons the card is currently claiming, and which it is caching."""
    return page.evaluate("""async () => {
      const pressed = Array.from(
        document.querySelectorAll('#adm-scope-presets button'))
        .filter(b => b.getAttribute('aria-pressed') === 'true')
        .map(b => b.dataset.preset);
      const me = window.kladoOrg.get();
      // The server's own answer, read past the page's cache. `loadOrg()` re-reads from
      // `window.kladoOrg` after every save, so the cache is the ONLY thing under test —
      // comparing the page to a fresh fetch is what makes this an anti-staleness check
      // rather than a snapshot of whatever the page believes.
      const r = await fetch('/api/org/me', {credentials: 'same-origin'});
      const fresh = (await r.json()).org;
      return {pressed, cached: me.org.preset, cachedTopLevel: me.preset,
              fresh: fresh.preset, freshShare: fresh.share_scope,
              freshPublic: fresh.public_scope,
              cachedShare: me.org.share_scope, cachedPublic: me.org.public_scope};
    }""")


def press_a_preset_and_require_the_page_to_follow(page, p):
    """⚠️ Press a preset and require the card to show what is TRUE, not what was clicked.

    The assertion that used to stand here — "exactly one preset is marked" — cannot see
    this bug, and that is the whole reason it shipped. A card that went stale still marked
    exactly one button: the WRONG one. So the invariant has to name the button, and has to
    be measured against the server rather than against the page's own earlier render.

    Two writes, not one: the click moves off the seeded preset, and putting it back leaves
    the probe company as this run found it, so the next audience/viewport in the loop
    starts from the same state instead of inheriting the last one's.

    The write depth matters as much as the write. `setPreset()` once stored the preset at
    the top level (`me.preset`) while `loadOrg()` reads `me.org.preset`, so the PUT
    succeeded, the `saved` badge appeared, and the card re-rendered itself unchanged — a
    control that reports success and does nothing. Hence the `cachedTopLevel` assertion:
    nothing may write a preset outside `org` again.
    """
    start = _marked_presets(page)
    check(f"{p} the card and the server agree BEFORE any press",
          start["cached"] == start["fresh"] and start["pressed"] == [start["fresh"]],
          f"cached={start['cached']!r} fresh={start['fresh']!r} "
          f"pressed={start['pressed']}")

    # Any preset that is not the current one. `internal` is the fallback for the case the
    # company holds a combination that is not a preset at all (`preset == ""`).
    others = [b for b in ("external", "global") if b != start["fresh"]]
    target = others[0] if others else "internal"
    btn = page.locator(f'#adm-scope-presets button[data-preset="{target}"]')
    btn.scroll_into_view_if_needed()
    box = btn.bounding_box()
    # ⚠️ A real mouse click on coordinates, not `.click()`. `.click()` dispatches straight
    # to the element and does no hit test, which would let this pass against a button that
    # something is covering — the same reason `verify_shell_slim_ui.py` is careful about it.
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(2000)

    after = _marked_presets(page)
    check(f"{p} ⚠️ pressing '{target}' reached the server",
          after["fresh"] == target,
          f"fresh={after['fresh']!r} after clicking {target!r} "
          f"(the PUT was refused, or answered something else)")
    check(f"{p} ⚠️ the card followed: '{target}' is the one marked current",
          after["pressed"] == [target],
          f"marked={after['pressed']} expected [{target!r}] — the page is showing "
          f"a preset the server has already replaced")
    check(f"{p} ⚠️ the preset is cached where loadOrg() reads it",
          after["cached"] == target,
          f"kladoOrg.get().org.preset={after['cached']!r}, expected {target!r} "
          f"(setPreset wrote it somewhere loadOrg cannot see)")
    check(f"{p} no stray preset is left outside the org",
          after["cachedTopLevel"] is None,
          f"kladoOrg.get().preset={after['cachedTopLevel']!r} — `me` is the server's own "
          f"object and carries no preset beside the org; a key written there is one "
          f"nothing reads")
    check(f"{p} ⚠️ both columns followed too",
          (after["cachedShare"], after["cachedPublic"])
          == (after["freshShare"], after["freshPublic"]),
          f"cached=({after['cachedShare']},{after['cachedPublic']}) "
          f"server=({after['freshShare']},{after['freshPublic']})")
    check(f"{p} the press reported itself as saved",
          "saved" in page.inner_text("#adm-org-status").lower()
          or "已保存" in page.inner_text("#adm-org-status"),
          page.inner_text("#adm-org-status").strip())

    # Put it back, so the next audience/viewport in this run starts where this one did.
    back = page.locator(f'#adm-scope-presets button[data-preset="{start["fresh"]}"]')
    back.scroll_into_view_if_needed()
    back.click()
    page.wait_for_timeout(1800)
    restored = _marked_presets(page)
    check(f"{p} the probe preset was put back",
          restored["fresh"] == start["fresh"] and restored["pressed"] == [start["fresh"]],
          f"fresh={restored['fresh']!r} marked={restored['pressed']} "
          f"(wanted {start['fresh']!r})")


def check_saved_badge_is_aligned_with_save(page, p):
    """The `saved` badge must share the Save button's centre line.

    ⚠️ Measured in pixels, because the defect is 4px of misalignment: too small to see in a
    screenshot and too small for any stylesheet check, and it was reported by a person
    looking at the page. `.adm-invite-status` carries `margin-top: 8px` for its two STACKED
    users; the save strip puts the same class inside an `align-items: center` flex row,
    where a margin still applies before alignment — so the badge sat 8px low, 4.0px off
    the button's centre. The badge must be measured WITH content in it; empty it is
    18px of min-height and the arithmetic means nothing.
    """
    check(f"{p} the status line has something in it to measure",
          bool(page.inner_text("#adm-org-status").strip()),
          page.inner_text("#adm-org-status").strip())
    m = page.evaluate("""() => {
      const b = document.querySelector('#adm-org-save');
      const s = document.querySelector('#adm-org-status');
      const rb = b.getBoundingClientRect(), rs = s.getBoundingClientRect();
      return {d: (rs.top + rs.bottom) / 2 - (rb.top + rb.bottom) / 2,
              btnH: rb.height, badgeH: rs.height};
    }""")
    check(f"{p} ⚠️ the 'saved' badge is centred on the Save button ({m['d']:+.1f}px)",
          abs(m["d"]) <= 1.5,
          f"badge centre is {m['d']:+.1f}px off the button centre "
          f"(button {m['btnH']:.0f}px, badge {m['badgeH']:.1f}px)")


#: Applied ONLY for the screenshot, and reverted immediately after.
#:
#: ⚠️ The clipping chain is FIVE DEEP, and every rung has to go:
#:     body(overflow:hidden, 100vh) → .shell(hidden) → .workspace(hidden)
#:       → #page-system-settings(auto) → #ss-panel-admin(auto)
#: `full_page=True` sizes the image from `document.documentElement.scrollHeight`, and with
#: `body { overflow: hidden }` at viewport height that number is exactly one screen — so
#: the image is one screen tall, silently, with no error. The first version of this
#: script unrolled only the two innermost nodes, measured the cards against the PANEL's
#: own box (which really did contain them), passed 232 assertions, and produced images of
#: two cards out of four. A vision model reading the picture independently reported that
#: the modules card and the approvals card "did not exist".
#:
#: ⚠️ That is the failure to keep: the assertion and the image were measuring DIFFERENT
#: things, and the assertion was the one that was reassuring. The check below is
#: therefore written against `document.documentElement.scrollHeight` — the number
#: Playwright actually uses — and compares it with the real height of the file that was
#: written.
_UNROLL = """
(() => {
  const saved = [];
  let node = document.getElementById('ss-panel-admin');
  while (node) {
    saved.push([node, node.getAttribute('style') || '']);
    const cs = getComputedStyle(node);
    if (cs.overflow !== 'visible' || cs.overflowY !== 'visible') {
      node.style.overflow = 'visible';
      node.style.overflowY = 'visible';
      node.style.overflowX = 'visible';
    }
    // A flex child with `flex: 1 1 0%` and no intrinsic height collapses to 0 once its
    // parent stops being a fixed-height flex box, which is its own silent failure.
    if (cs.height !== 'auto') node.style.height = 'auto';
    if (cs.maxHeight && cs.maxHeight !== 'none') node.style.maxHeight = 'none';
    if (cs.flex && cs.flex !== '0 0 auto' && cs.flex !== 'none') {
      node.style.flex = '0 0 auto';
    }
    node = node.parentElement;
  }
  window.__p6ShotSaved = saved;
  return saved.length;
})()
"""

_RESTORE = """
(() => {
  (window.__p6ShotSaved || []).forEach(([node, css]) => { node.setAttribute('style', css); });
  window.__p6ShotSaved = null;
  return true;
})()
"""


def shoot_full_panel(page, path):
    """Capture the WHOLE settings panel, not just the first screenful."""
    if not page.evaluate(_UNROLL):
        return False
    try:
        page.wait_for_timeout(500)
        page.screenshot(path=path, full_page=True)
    finally:
        page.evaluate(_RESTORE)
        page.wait_for_timeout(300)
    return True


#: One probe for the page's chrome. Read as a whole because the checks below are
#: judgements about RELATIONSHIPS (gone vs still-live, this line vs that line, here vs
#: covered) and splitting it into three round trips would let the page move between them.
_SETTINGS_CHROME = """
() => {
  const pageEl = document.getElementById('page-system-settings');
  const btn = document.querySelector('#page-actions-bar .ws-action-btn');
  const bar = document.querySelector('.workspace-bar');
  const crumb = document.getElementById('breadcrumb-text');
  const actions = document.getElementById('page-actions-bar');
  const out = {
    // ⚠️ The positive control for the row check. A renamed selector and a removed
    // element both answer 0, so "0 toolbars on this page" only means anything while
    // this number proves the class is still matched somewhere in this document.
    toolbarsOnPage: pageEl ? pageEl.querySelectorAll('.rpt-toolbar').length : -1,
    toolbarsAnywhere: document.querySelectorAll('.rpt-toolbar').length,
    oldTitle: document.querySelectorAll('#ss-title').length,
    oldSummary: document.querySelectorAll('#ss-summary').length,
    crumb: crumb ? crumb.textContent.trim() : null,
    actionsDisplay: actions ? getComputedStyle(actions).display : null,
    found: !!btn,
  };
  if (btn) {
    const r = btn.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    out.cx = cx;
    out.cy = cy;
    out.w = r.width;
    out.h = r.height;
    out.barTop = bar ? bar.getBoundingClientRect().top : null;
    out.barBottom = bar ? bar.getBoundingClientRect().bottom : null;
    // ⚠️ The hit test, not `.click()`. `.click()` dispatches straight at the element and
    // never hit-tests, so it is green against a button something is sitting on top of.
    out.hitIsButton = !!(hit && (hit === btn || btn.contains(hit)));
    out.hit = hit ? (hit.tagName.toLowerCase() + '.' + (hit.className || '')) : null;
    out.glyph = !!btn.querySelector('i.ri-refresh-line');
  }
  return out;
}
"""


def check_the_page_owns_no_row_and_the_shell_owns_the_refresh(page, p):
    """The Settings page has no toolbar of its own; the shell bar above owns the Refresh.

    ⚠️ The click is a REAL mouse click on the button's own coordinates, and its effect is
    checked by emptying the roster first. `locator.click()` would dispatch at the element
    without ever hitting it, and "the handler is attached" is not "the control works" —
    which is the whole difference between a button and a decoration.

    ⚠️ The 760px leg is not decoration either. The bar carries a `max-width: 768px` rule
    that hides `.workspace-actions` outright; that rule was free while the bar was empty
    on every page, and it would have taken this page's ONLY refresh away on exactly the
    screens that can least afford one. Nothing above 768px can see that, and both
    viewports in VIEWPORTS are above it.
    """
    was = page.viewport_size
    try:
        m = page.evaluate(_SETTINGS_CHROME)
        check(f"{p} the toolbar class is still a live selector (control for the row check)",
              m["toolbarsAnywhere"] >= 1, f".rpt-toolbar matched {m['toolbarsAnywhere']}")
        check(f"{p} ⚠️ the page carries no toolbar row of its own",
              m["toolbarsOnPage"] == 0,
              f"{m['toolbarsOnPage']} .rpt-toolbar inside #page-system-settings")
        check(f"{p} ⚠️ and its title and queue count left the document with it",
              m["oldTitle"] == 0 and m["oldSummary"] == 0,
              f"#ss-title={m['oldTitle']} #ss-summary={m['oldSummary']}")
        if not check(f"{p} the Refresh is in the shell's actions bar", m["found"],
                     "no .ws-action-btn inside #page-actions-bar"):
            return
        check(f"{p} the bar above the page still names it", bool(m["crumb"]), repr(m["crumb"]))
        check(f"{p} ⚠️ the button is the hit at its own centre", m["hitIsButton"],
              f"elementFromPoint returned {m['hit']}")
        check(f"{p} ⚠️ it sits on the same line as the page name, not on a row below it",
              m["barTop"] is not None and m["barTop"] <= m["cy"] <= m["barBottom"],
              f"button centre y={m['cy']:.0f}, the bar spans "
              f"{m['barTop']}..{m['barBottom']}")
        check(f"{p} it is a square icon button, and the glyph is there",
              abs(m["w"] - m["h"]) <= 2 and m["w"] >= 24 and m["glyph"],
              f"{m['w']:.0f}x{m['h']:.0f} glyph={m['glyph']}")

        # ⚠️ The roster is emptied to give the click something observable to do — and it
        # is REBUILT here, unconditionally, in a `finally`. Leaving the page in the state
        # my own probe created would make every later assertion in the run report on a
        # page this check broke: the M2 mutation (Refresh wired to nothing) red the click
        # AND seven unrelated roster checks, which buries the one that matters and makes
        # a guard that reds more than its own behaviour look like it is testing more.
        try:
            page.evaluate("document.getElementById('adm-users-body').innerHTML = ''")
            page.wait_for_timeout(300)
            blanked = page.evaluate(
                "() => document.getElementById('adm-users-body').innerHTML.trim() === ''")
            check(f"{p} the roster was emptied first (control for the click below)", blanked,
                  "the blanking did not take, so a repaint afterwards would prove nothing")
            page.mouse.click(m["cx"], m["cy"])
            page.wait_for_timeout(3000)
            tables = page.locator("#adm-users-body table").count()
            check(f"{p} ⚠️ a real click on it reloads the page", tables == 1,
                  f"{tables} tables in the roster after the click — the click reached "
                  f"nothing, or the handler no longer repaints this card")
        finally:
            page.evaluate("window.adminPage.loadMembers()")
            page.wait_for_timeout(2000)

        page.set_viewport_size({"width": 760, "height": 900})
        page.wait_for_timeout(900)
        n = page.evaluate(_SETTINGS_CHROME)
        check(f"{p} ⚠️ it survives 760px, where the bar's own rule hides that slot",
              n["found"] and n["actionsDisplay"] != "none" and n["hitIsButton"],
              f"found={n['found']} actions display={n['actionsDisplay']} "
              f"hit={n.get('hit')}")
    finally:
        page.set_viewport_size(was)


def _audience_checks(page, audience, theme, mate_email):
    """Everything asserted about the page, for one audience in one theme."""
    p = f"[{audience}/{theme}]"

    # ⚠️ An operator with no company does not get this page AT ALL. The page administers
    # one company, the operator's console is a separate application on its own port, and
    # showing an operator a roster of accounts they have no relationship to is the bug
    # this rule exists to prevent. So the operator run asserts the REFUSAL rather than
    # the contents — which also means the two audiences below no longer differ by "which
    # cards are hidden" but by "is there a page".
    if audience == "operator":
        check(f"{p} the Settings tab is not offered to an operator with no company",
              not page.locator("#nav-tab-settings").is_visible(),
              "an operator was given the enterprise admin page")
        # ⚠️ The tab being hidden is a display rule. The page can still be OPENED by
        # anything that calls `navTo()` directly, and a hidden tab is not a permission
        # check — so the gate itself is asserted, by attempting the navigation and
        # measuring where the user ended up. `navTo` bounces to home for this audience.
        landed = page.evaluate("""() => {
          navTo(null, 'system-settings', 'Settings');
          const el = document.getElementById('page-system-settings');
          const cs = el ? getComputedStyle(el) : null;
          return {settings: cs ? cs.display !== 'none' : false,
                  breadcrumb: (document.getElementById('breadcrumb-text') || {}).textContent};
        }""")
        check(f"{p} ⚠️ and navTo() refuses to open the page for them",
              landed["settings"] is False,
              f"the page opened anyway ({landed})")
        return

    # ── the page's own chrome: no row of its own, one control in the shell bar ──
    check_the_page_owns_no_row_and_the_shell_owns_the_refresh(page, p)

    # ── the four cards ────────────────────────────────────────────────────────
    for card in ("adm-card-members", "adm-card-approvals"):
        check(f"{p} {card} is on screen", page.locator("#" + card).is_visible(),
              "hidden or missing")
    # ⚠️ The company name lives in the card header, next to the count. It used to be
    # appended to the table as a bare "· Northwind" line, which reads as a list bullet
    # and as a rendering leftover.
    check(f"{p} the company name is in the card header, not under the table",
          page.locator("#adm-members-scope").count() == 1
          # ⚠️ `indexOf`, not a regular expression: a literal bullet in a JS regex
          # inside a Python raw string is two more escaping layers to get wrong, and the
          # failure is a SyntaxError from `page.evaluate` that says nothing about the
          # thing being asserted.
          and not page.evaluate(
              "() => ['\u00b7', '\u2022', '-', '\u2013'].some("
              "  (m) => (document.querySelector('#adm-users-body').innerText"
              "    .split('\\n').find((l) => l.trim() && !l.includes('@')) || '')"
              "    .trim().startsWith(m))"),
          "a bare line is sitting under the table")
    # ⚠️ The company card is the one that differs by audience, and both directions are
    # wrong in a different way: showing it to an operator offers a form for a company
    # they do not have, and hiding it from an owner removes the only place the policy
    # dials live.
    org_visible = page.locator("#adm-card-org").is_visible()
    check(f"{p} the enterprise settings card matches the audience",
          org_visible == (audience == "owner"),
          f"visible={org_visible} for {audience}")

    # ── the roster ────────────────────────────────────────────────────────────
    check(f"{p} the roster rendered a table", page.locator("#adm-users-body table").count() == 1,
          "no table")
    # ⚠️ A header row must be internally consistent. `DICT.zh` can map the English
    # literal `Email` to `邮箱`, and nothing maps `邮箱` back — so a table whose headers
    # are bare English literals renders as `邮箱 | ROLE | 状态 | JOINED`: half the row in
    # one language, half in the other, on the same line. A vision model flagged this
    # before any assertion did. Measured, not eyeballed: the header row has to be
    # all-CJK or all-not.
    headers = page.evaluate("""() => Array.from(
        document.querySelectorAll('#adm-users-body th')).map(e => e.innerText.trim())""")
    cjk = [h for h in headers if re.search(r'[\u4e00-\u9fff]', h)]
    check(f"{p} ⚠️ the header row is one language, not half-translated",
          len(cjk) in (0, len(headers)), f"{cjk} of {headers}")
    rows = page.locator("#adm-users-body tbody tr").count()
    check(f"{p} the roster has rows", rows >= 2, f"{rows} rows")
    if audience == "owner":
        check(f"{p} a member row offers a role select",
              page.locator("#adm-users-body select.adm-role").count() >= 2,
              f"{page.locator('#adm-users-body select.adm-role').count()} selects")
        # ⚠️ The role control is a <select>, not a button, and the card's own comment
        # says every control in that Actions cell is 26px. A select at the browser's
        # default height is 20-something px and one button in the row then looks wrong.
        h = page.evaluate("""() => {
          const s = document.querySelector('#adm-users-body select.adm-role');
          const b = document.querySelector('#adm-users-body .adm-acts .adm-btn');
          return s && b ? [s.getBoundingClientRect().height, b.getBoundingClientRect().height] : null;
        }""")
        if check(f"{p} the role select is measured against the buttons in its cell", h is not None,
                 "no select/button pair"):
            check(f"{p} ⚠️ and they are the same height", abs(h[0] - h[1]) <= 1.0,
                  f"select {h[0]:.1f}px vs button {h[1]:.1f}px")

    # ── EVERY card must be inside the capture ─────────────────────────────────
    # ⚠️ This is the assertion the first version of this script did not have, and its
    # absence is why a page with four cards was screenshotted as two. `full_page` on a
    # document that is exactly one viewport tall produces a one-screen image and no
    # error; a card below the fold is simply not in the file. So the capture is measured
    # against the panel's own box, in the same unrolled state the image is taken in.
    rungs = page.evaluate(_UNROLL)
    try:
        page.wait_for_timeout(500)
        # ⚠️ Measured against `documentElement.scrollHeight` — the number Playwright
        # uses to size a `full_page` image — and not against any container's own box.
        # The first version of this check measured the panel, which genuinely contained
        # all four cards, while the image was still one screen tall.
        measured = page.evaluate("""() => {
          const doc = document.documentElement;
          return {
            scrollHeight: doc.scrollHeight,
            viewport: doc.clientHeight,
            cards: ['adm-card-members', 'adm-card-org',
                    'adm-card-approvals'].map(id => {
              const el = document.getElementById(id);
              if (!el) return [id, null];
              return [id, Math.round(el.getBoundingClientRect().bottom
                                      + window.scrollY)];
            }),
          };
        }""")
    finally:
        page.evaluate(_RESTORE)
        page.wait_for_timeout(300)
    check(f"{p} the whole clipping chain is unrolled for the capture", (rungs or 0) >= 5,
          f"only {rungs} ancestors were neutralised")
    last_bottom = max((b for _i, b in measured["cards"] if b), default=0)
    check(f"{p} ⚠️ the document is tall enough to contain every card",
          last_bottom <= measured["scrollHeight"] + 1,
          f"the last card ends at {last_bottom} but the image is only "
          f"{measured['scrollHeight']}px tall (viewport {measured['viewport']}px)")
    missing = [i for i, b in measured["cards"] if b is None]
    check(f"{p} every card exists in the DOM being captured", not missing, f"missing {missing}")

    # ── the module dialog, on the member row ─────────────────────────────────
    # ⚠️ It used to be a nine-column matrix under the roster with three states per cell,
    # cycled by clicking. The matrix is gone; the invariants worth keeping are:
    #   · the trigger is a button ON THE ROW (where the person you are thinking about is);
    #   · the options are real checkboxes, one per module this company is licensed for;
    #   · the seeded narrowing is actually VISIBLE.
    #
    # The last one is the load-bearing check and it is not hypothetical. The operator
    # branch of the old page was built from `/api/auth/admin/users`, which carries no
    # entitlements, so every cell rendered as "follow the deployment" — a matrix that
    # was provably wrong about an account that had been narrowed, while 240 assertions
    # passed over it. A vision model reading the screenshot said, correctly, that all the
    # cells looked the same. So the seed has to be observable through the new control.
    page.evaluate("window.adminPage.loadModules()")
    page.wait_for_timeout(2000)
    modbtn = page.locator(f'#adm-users-body tr:has-text("{mate_email}") button.mod-btn')
    check(f"{p} the seeded colleague's row carries a module button", modbtn.count() == 1,
          f"{modbtn.count()} buttons")
    check(f"{p} ⚠️ and the button says something was set for that person",
          modbtn.count() == 1 and "mod-btn-set" in (modbtn.first.get_attribute("class") or ""),
          modbtn.first.get_attribute("class") if modbtn.count() else "no button")
    # ⚠️ Assert BOTH directions. "at least one row is marked" passes when the mark is on
    # the wrong row — including on the one member who was never narrowed, which is the
    # failure this whole check exists to catch in its other form.
    marks = page.evaluate("""() => Array.from(
        document.querySelectorAll('#adm-users-body tbody tr')).map(tr => ({
          email: (tr.querySelector('td') || {}).innerText || '',
          marked: !!tr.querySelector('button.mod-btn.mod-btn-set'),
        }))""")
    check(f"{p} the mark is on the seeded member and on nobody else",
          [m["email"] for m in marks if m["marked"]] == [mate_email],
          f"{[(m['email'], m['marked']) for m in marks]}")

    if modbtn.count():
        modbtn.first.click()
        page.wait_for_timeout(1200)
        dlg = page.locator("#kld-dlg")
        check(f"{p} the dialog opened", dlg.is_visible(), "#kld-dlg stayed hidden")
        boxes = page.evaluate("""() => Array.from(
            document.querySelectorAll('#kld-dlg .mod-pick-box')).map(e => ({
              key: e.dataset.key, checked: e.checked, tag: e.tagName, type: e.type,
              w: e.getBoundingClientRect().width, h: e.getBoundingClientRect().height,
              label: (e.closest('.mod-pick') || {}).innerText || '',
              icon: !!((e.closest('.mod-pick') || {}).querySelector('.mod-pick-ico i')),
            }))""")
        check(f"{p} the dialog offered checkboxes", len(boxes) >= 1, f"{len(boxes)} boxes")
        check(f"{p} they are real <input type=checkbox>",
              all(b["tag"] == "INPUT" and b["type"] == "checkbox" for b in boxes),
              f"{[(b['tag'], b['type']) for b in boxes][:2]}")
        check(f"{p} ⚠️ the SEEDED narrowing is visible: workspace is unticked",
              [b["checked"] for b in boxes if b["key"] == "workspace"] == [False],
              f"workspace={[b['checked'] for b in boxes if b['key'] == 'workspace']} of "
              f"{[(b['key'], b['checked']) for b in boxes]}")
        check(f"{p} the other offered modules are still ticked",
              all(b["checked"] for b in boxes if b["key"] != "workspace"),
              f"{[(b['key'], b['checked']) for b in boxes]}")
        check(f"{p} every row shows a module icon and a module name",
              all(b["icon"] and b["label"].strip() for b in boxes),
              f"{[(b['icon'], b['label']) for b in boxes][:2]}")
        check(f"{p} the boxes are a real size, not a collapsed box",
              all(14 <= b["w"] <= 24 and 14 <= b["h"] <= 24 for b in boxes),
              f"{boxes[0]['w']:.0f}x{boxes[0]['h']:.0f}" if boxes else "none")
        check(f"{p} the dialog offers only the company's licensed modules",
              page.evaluate("""() => {
                  const o = window.kladoOrg.get();
                  const offered = ((o && o.org) || {}).module_options || [];
                  const shown = Array.from(
                    document.querySelectorAll('#kld-dlg .mod-pick-box'))
                    .map(b => b.dataset.key).sort();
                  return JSON.stringify(shown) === JSON.stringify(offered.slice().sort());
                }"""),
              "the dialog and /api/org/me disagree about what this company may offer")
        page.screenshot(path=os.path.join(SHOTS, f"{audience}-{theme}-04-modules.png"),
                        full_page=True)
        page.click("#kld-dlg-cancel")
        page.wait_for_timeout(600)
        check(f"{p} Cancel closes it", not dlg.is_visible(), "still open")

    # ── the policy card ───────────────────────────────────────────────────────
    if audience == "owner":
        check(f"{p} the three policy presets are offered",
              page.locator("#adm-scope-presets button").count() == 3,
              f"{page.locator('#adm-scope-presets button').count()} buttons")
        marked = page.evaluate("""() => Array.from(
            document.querySelectorAll('#adm-scope-presets button'))
            .map(b => b.getAttribute('aria-pressed')).filter(v => v === 'true').length""")
        check(f"{p} exactly one preset reads as the current one", marked == 1, f"{marked} pressed")
        check(f"{p} the suffix box is filled from the server",
              bool(page.input_value("#adm-org-domains").strip()),
              page.input_value("#adm-org-domains"))
        press_a_preset_and_require_the_page_to_follow(page, p)
        check_saved_badge_is_aligned_with_save(page, p)

    # ── the approvals queue ───────────────────────────────────────────────────
    page.evaluate("window.adminPage.loadApprovals()")
    page.wait_for_timeout(2000)
    ap = page.locator("#adm-approvals-body")
    check(f"{p} the approvals card answered", ap.inner_text().strip() != "", "blank body")
    if audience == "owner":
        check(f"{p} the pending request is listed",
              page.locator("#adm-approvals-body tbody tr").count() == 1,
              f"{page.locator('#adm-approvals-body tbody tr').count()} rows")
        check(f"{p} and it offers both decisions",
              page.locator("#adm-approvals-body button").count() == 2,
              f"{page.locator('#adm-approvals-body button').count()} buttons")

    # ── contrast, in THIS theme ───────────────────────────────────────────────
    # ⚠️ Measured, not read from the stylesheet. A token that resolves to the light value
    # in dark mode is a correctly-formatted `var()` and no static check can see it.
    samples = page.evaluate("""() => {
      const out = [];
      const push = (label, sel) => {
        const el = document.querySelector(sel);
        if (!el) return;
        let bg = 'rgba(0, 0, 0, 0)', node = el;
        while (node && bg === 'rgba(0, 0, 0, 0)') {
          bg = getComputedStyle(node).backgroundColor;
          node = node.parentElement;
        }
        out.push([label, getComputedStyle(el).color, bg]);
      };
      push('card title', '.adm-hd-title');
      push('note', '.adm-note');
      push('modules note', '.kld-dlg-note');
      push('body text', '.adm-table td');
      return out;
    }""")
    check(f"{p} the contrast probes found something to measure", len(samples) >= 3,
          f"{len(samples)} samples")
    for label, fg, bg in samples:
        ratio = contrast(fg, bg)
        check(f"{p} contrast {label} {ratio}:1 >= 4.5", ratio >= 4.5, f"{fg} on {bg}")


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    stamp = int(time.time())
    domain = f"corp-{uuid.uuid4().hex[:6]}.invalid"
    owner_email = f"boss@{domain}"
    mate_email = f"mate@{domain}"
    operator_email = f"op-{uuid.uuid4().hex[:6]}@klado-verify.invalid"
    org = owner = mate = operator = None
    approval = None
    try:
        org = orgs.create_org(f"Northwind {stamp}", kind=orgs.KIND_ENTERPRISE,
                              domains=[domain])
        owner = accounts.create_user(owner_email, PASSWORD, "Bea Owner")
        mate = accounts.create_user(mate_email, PASSWORD, "Mo Member")
        operator = accounts.create_user(operator_email, PASSWORD, "Op Operator")
        for address, role in ((owner_email, "owner"), (mate_email, "member")):
            orgs.upsert_invitation(org["id"], address, role, invited_by=owner_email)
            orgs.attach_existing_account(org["id"], address)
        with connect_main() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE app_users SET role = 'admin' WHERE id = %s",
                            (operator["id"],))
                # One real narrowing, so the matrix is not a wall of identical cells and
                # the "clearing one module keeps the others" claim has something to show.
                cur.execute("INSERT INTO account_modules (user_id, module_key, enabled) "
                            "VALUES (%s, 'workspace', FALSE) "
                            "ON CONFLICT (user_id, module_key) DO UPDATE SET enabled = FALSE",
                            (mate["id"],))
            conn.commit()
        approval = orgs.request_public_approval(
            mate_email, "report", f"q3-plan-{stamp}", "anyone_link")["id"]
        print(f"company: {org['name']} ({domain})  owner={owner_email}  operator={operator_email}\n")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for audience, email in (("owner", owner_email), ("operator", operator_email)):
                    for theme in ("light", "dark"):
                        for width, height in VIEWPORTS:
                            tag = f"{audience}-{theme}-{width}"
                            ctx = browser.new_context(
                                viewport={"width": width, "height": height},
                                device_scale_factor=2)
                            # ⚠️ A stored theme, so the inline <head> bootstrap and the
                            # toggle label are both exercised the way a returning user's
                            # browser does it, not by poking `data-theme` afterwards.
                            ctx.add_init_script(
                                "try { localStorage.setItem('klado-theme', '%s');"
                                " localStorage.setItem('klado-lang', 'zh'); } catch (e) {}"
                                % theme)
                            page = ctx.new_page()
                            page_errors: list[str] = []
                            page.on("pageerror", lambda e: page_errors.append(str(e)))
                            sign_in(page, email, PASSWORD, tag)
                            if audience == "operator":
                                # ⚠️ No click, no screenshot, no "is the tab there" gate
                                # first. An operator has no Settings page, so driving them
                                # into one would be asserting against a page that is
                                # supposed to be unreachable — and `open_settings()` waits
                                # on a selector inside it, so the run would hang rather
                                # than fail if the rule ever regressed.
                                _audience_checks(page, audience, theme, mate_email)
                                check(f"[{tag}] no uncaught JS errors", not page_errors,
                                      "; ".join(page_errors[:2]))
                                ctx.close()
                                continue
                            check(f"[{tag}] the Settings tab is offered",
                                  page.locator("#nav-tab-settings").is_visible(),
                                  "hidden — the audience was refused the page")
                            open_settings(page, tag)
                            _audience_checks(page, audience, theme, mate_email)
                            shoot_full_panel(page, os.path.join(SHOTS, f"{tag}-zh.png"))
                            # And the other language, because a page can lay out correctly
                            # in Chinese and overflow in English.
                            page.evaluate("window.kladoI18n.setLang('en')")
                            page.wait_for_timeout(1200)
                            shoot_full_panel(page, os.path.join(SHOTS, f"{tag}-en.png"))
                            both = page.evaluate("""() => {
                              const el = document.querySelector('#adm-users-body');
                              if (!el) return null;
                              const t = el.innerText;
                              return /[\\u4e00-\\u9fff]/.test(t);
                            }""")
                            check(f"[{tag}] ⚠️ switching to English leaves no Chinese behind",
                                  both is False, f"still Chinese={both}")
                            check(f"[{tag}] no uncaught JS errors", not page_errors,
                                  "; ".join(page_errors[:2]))
                            ctx.close()
            finally:
                browser.close()

        print(f"\n{PASS} passed, {len(FAIL)} failed")
        for line in FAIL:
            print("  -", line)
        print(f"screenshots: {SHOTS}")
        return 1 if FAIL else 0

    finally:
        for user in (owner, mate, operator):
            if user and user.get("id"):
                try:
                    with connect_main() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM account_modules WHERE user_id = %s",
                                        (user["id"],))
                            cur.execute("DELETE FROM org_members WHERE user_id = %s",
                                        (user["id"],))
                            cur.execute("DELETE FROM access_log WHERE user_id = %s",
                                        (user["id"],))
                            cur.execute("DELETE FROM app_users WHERE id = %s", (user["id"],))
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARNING: could not remove {user.get('email')}: {exc}")
        if org and org.get("id"):
            try:
                with connect_main() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM org_public_approvals WHERE org_id = %s",
                                    (org["id"],))
                        cur.execute("DELETE FROM org_members WHERE org_id = %s", (org["id"],))
                        cur.execute("DELETE FROM orgs WHERE id = %s", (org["id"],))
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: could not remove the probe company: {exc}")
        print("cleaned up the probe company, its accounts and its request")


if __name__ == "__main__":
    sys.exit(main())
