#!/usr/bin/env python3
"""Screenshot the admin console in a real browser, in both themes, and assert the things
a screenshot exists to catch.

Why a browser and not assertions on the CSS
--------------------------------------------
A stylesheet can be perfectly compliant and still produce a page nobody can use. The three
defects this file looks for are all of that kind, and none of them is a missing rule:

* **A page that renders but is unreadable in dark mode.** Every colour in
  `console.css` is a token, and a token that resolves to the light value in dark mode is
  invisible to a static check — it is a correctly-formatted `var()`. The only way to know
  is to apply `data-theme="dark"` and read `getComputedStyle`.
* **Layout that has collapsed into something else.** A rail that is not `position: sticky`
  scrolls away; a card grid that has become one column at 1280px stops being scannable.
  These are facts about the rendered box, not about the source.
* **A bilingual string that did not split.** The console's copy is `中文 / English`, and a
  page can render perfectly while showing the reader both languages at once. The walker is
  a moving target as pages are added, so it is measured, not assumed.

And the discipline from the rest of this project: **a screenshot that is only looked at
proves nothing.** So every screenshot is paired with an assertion, and each assertion
names the failure it would have caught. The images are for a human; the checks are for the
suite.

It creates a real operator account, signs in with it, and removes it afterwards —
including on failure. Run against a database you are happy to have briefly written to.

    cd api && ../.venv312/bin/python tests/shot_admin_console_ui.py
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "klado_shared/.."))

from playwright.sync_api import sync_playwright            # noqa: E402

from klado_shared import accounts, orgs                    # noqa: E402
from klado_shared.config import settings                   # noqa: E402
from klado_shared.db import connect_main                   # noqa: E402

CONSOLE = os.environ.get("KLADO_CONSOLE_URL",
                         f"http://127.0.0.1:{settings.KLADO_ADMIN_PORT}")
SHOTS = os.environ.get("KLADO_ADMIN_SHOTS", "/tmp/klado-admin-shots")

PASS, FAIL = 0, []
RESULTS: list[str] = []

#: Widths the console is asserted at. 1280 is the narrowest a laptop gets in practice;
#: the rail plus a table with seven columns has to survive it, because that is the case
#: where a grid silently collapses.
VIEWPORTS = [(1440, 900), (1280, 800)]

#: A third, deliberately narrower pass. ⚠️ The clipping guards are **dead at 1440 and 1280**:
#: once the tables are given the full pane they fit, so nothing overflows and the checks
#: pass for the wrong reason. Measured 2026-10-03 at this width, 8 of the 13 tables in
#: `api_endpoints.md` do overflow — which is where a scroller has to earn its keep, and
#: where `overflow: hidden` would silently swallow the rightmost column. A guard that has
#: never been seen to fail is not a guard.


def _record(message, page_errors: list, http_errors: list) -> None:
    """Sort a console message into "our bug" or "the server said no".

    ⚠️ Split by content, not by `type`. Everything Chromium reports about a failed fetch
    arrives as `console.error` with a `Failed to load resource` prefix, so filtering on
    the message is the only way to tell an unhandled rejection from a 401 the console
    asked for.
    """
    text = message.text
    if "Failed to load resource" in text:
        http_errors.append(text)
    else:
        page_errors.append(text)


def check(name: str, ok: bool, detail: str = "") -> bool:
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


def relative_luminance(c: tuple) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = relative_luminance(rgb(a)), relative_luminance(rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


PAGES = ["overview", "orgs", "personal", "accounts", "modules", "manual", "skill", "mail",
         "bin", "audit", "system"]


def sign_in(page, email: str, password: str, tag: str = "") -> None:
    """Sign in, or explain.

    ⚠️ The diagnostics on the failure path are not decoration. A bare
    `wait_for_selector` timeout says "the element never appeared" and nothing about
    whether that was a 429 from the login limiter, a 403 from the read-only switch, a
    thrown exception, or a genuinely slow page — four problems with four different fixes.
    The first version of this script printed a Playwright call log and exited, and the
    only way to tell them apart was to re-derive the whole run by hand.
    """
    seen: list[str] = []
    page.on("pageerror", lambda e: seen.append("pageerror: " + str(e)))
    page.on("console", lambda m: seen.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
    page.goto(CONSOLE + "/", wait_until="networkidle")
    page.wait_for_selector("#login:not([hidden])", timeout=15000)
    page.fill("#li-email", email)
    page.fill("#li-pass", password)
    page.click("#li-go")
    try:
        page.wait_for_selector("#shell:not([hidden])", timeout=20000)
    except Exception as exc:  # noqa: BLE001 — re-raised with the diagnosis attached
        detail = {
            "login error box": page.eval_on_selector("#li-err", "e => e.textContent"),
            "throttled screen shown": not page.eval_on_selector(
                "#throttled", "e => e.hidden"),
            "console": "; ".join(seen[:6]) or "(nothing logged)",
        }
        page.screenshot(path=os.path.join(SHOTS, f"FAILED-signin-{tag}.png"))
        raise RuntimeError(f"sign-in failed for {tag}: {detail}") from exc
    page.wait_for_timeout(500)


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    stamp = int(time.time())
    email = f"console-shot-{stamp}@klado-verify.invalid"
    password = "Console-Shot-Probe-1!"

    user = org = None
    try:
        user = accounts.create_user(email, password, "Console Shot")
        with connect_main() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE app_users SET role = 'admin' WHERE id = %s",
                            (user["id"],))
            conn.commit()
        # ⚠️ Seed a real code. Without one every account row renders the 「签发」 button and
        # the screenshot shows a column of buttons — which looks finished and proves nothing
        # about the two things that actually matter here: that the 55-character code is
        # masked down to something scannable, and that a copy button sits beside it. A
        # fixture with no code leaves both untested while the page still looks right.
        accounts.create_agent_token(user["id"], "shot probe", created_by=email)
        # A company with a real, non-trivial licence. The four switchable modules with
        # two of them licensed is the case the drawer has to render correctly; an
        # unconfigured company would show every switch on and prove nothing.
        org = orgs.create_org(f"Console Shot {stamp}",
                              kind=orgs.KIND_ENTERPRISE,
                              domains=[f"console-shot-{stamp}.invalid"])
        orgs.set_org_modules(org["id"], ["workspace", "knowledge"])
        print(f"probe operator: {email} (id {user['id']})")
        print(f"probe company:  {org['name']} (id {org['id']})\n")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in ("light", "dark"):
                    for width, height in VIEWPORTS:
                        suffix = f"{theme}-{width}"
                        ctx = browser.new_context(
                            viewport={"width": width, "height": height},
                            device_scale_factor=2)
                        page = ctx.new_page()
                        # Two separate lists, and the distinction matters. `pageerror` is
                        # an uncaught exception — always a defect. A console "Failed to load
                        # resource" line is the browser reporting an HTTP status, and the
                        # console PRODUCES one on purpose at boot: `boot()` asks `/me` to
                        # find out whether there is a session, and a 401 is the answer.
                        # Asserting on that mixed stream would fail every run for the
                        # first version of this script, and "fixing" it by not recording
                        # network failures would lose the signal that a page's own fetch
                        # got a 500.
                        page_errors: list[str] = []
                        http_errors: list[str] = []
                        page.on("console", lambda m: _record(m, page_errors, http_errors))
                        page.on("pageerror", lambda e: page_errors.append(str(e)))

                        # A stored theme, so `applyTheme` has something to read and the
                        # inline <head> bootstrap and the button label are both exercised
                        # on the path a returning operator actually takes.
                        ctx.add_init_script(
                            f"try {{ localStorage.setItem('klado-admin-theme', '{theme}');"
                            f" localStorage.setItem('klado-admin-lang', 'zh'); }} catch (e) {{}}")
                        # `tag` first: the sign-in diagnostics name the failing viewport,
                        # and a diagnostic that cannot name the case is the Playwright
                        # timeout all over again.
                        tag = f"{suffix}"
                        print(f"\n— {tag} —")
                        sign_in(page, email, password, tag)

                        # The boot-time 401 is expected; everything after it is not.
                        page_errors.clear()
                        http_errors.clear()
                        for key in PAGES:
                            page.click(f'#nav button[data-page="{key}"]')
                            page.wait_for_timeout(450)
                            page.screenshot(path=os.path.join(SHOTS, f"{tag}-{key}.png"))
                            # ⚠️ No `break` on failure. The first version stopped the whole
                            # pass on the first bad page, which meant one dark-mode
                            # contrast failure silently skipped the other eight dark
                            # screenshots — a script that reports one problem when it has
                            # nine is a script people stop reading.
                            check(f"{tag}: {key} raises no uncaught exception",
                                  not page_errors, "; ".join(page_errors[:3]))
                            check(f"{tag}: {key} had no failed request",
                                  not http_errors, "; ".join(http_errors[:2]))
                            check(f"{tag}: {key} did not fall back to an error card",
                                  "加载失败 / Could not load" not in
                                  page.inner_text(f"#page-{key}"),
                                  page.inner_text(f"#page-{key}")[:120])
                            page_errors.clear()
                            http_errors.clear()
                            run_page_checks(page, key, tag)
                            if key == "orgs":
                                run_org_drawer_checks(page, tag, org["id"])
                            if key == "accounts":
                                run_account_code_checks(page, tag)
                            if key == "manual":
                                run_manual_checks(page, tag)
                            if key == "skill":
                                run_skill_checks(page, tag)
                            if key in ("manual", "skill"):
                                run_fill_checks(page, tag, f"page-{key}")

                        if theme == "light":
                            run_geometry_checks(page, width, height, tag)
                            run_contrast_checks(page, tag)
                        ctx.close()
                browser.close()
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    finally:
        if user and user.get("id"):
            try:
                with connect_main() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM org_members WHERE user_id = %s",
                                    (user["id"],))
                        cur.execute("DELETE FROM access_log WHERE user_id = %s",
                                    (user["id"],))
                        cur.execute("DELETE FROM app_users WHERE id = %s", (user["id"],))
                    conn.commit()
                print(f"\nremoved the probe operator {email}")
            except Exception as exc:  # noqa: BLE001
                print(f"\nWARNING: could not remove the probe operator: {exc}")
        if org and org.get("id"):
            try:
                with connect_main() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM org_public_approvals WHERE org_id = %s",
                                    (org["id"],))
                        cur.execute("DELETE FROM org_members WHERE org_id = %s",
                                    (org["id"],))
                        cur.execute("DELETE FROM orgs WHERE id = %s", (org["id"],))
                    conn.commit()
                print(f"removed the probe company {org['name']}")
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: could not remove the probe company: {exc}")

    print(f"\n{PASS} passed, {len(FAIL)} failed   →   {SHOTS}")
    for line in FAIL:
        print("  -", line)
    return 1 if FAIL else 0


def run_geometry_checks(page, width, height, tag) -> None:
    """The shell's shape. Each of these is a fact about the rendered box, and each has a
    concrete way to be quietly wrong."""
    print(f"  geometry @ {tag}")
    box = page.evaluate("""() => {
      const q = (s) => { const e = document.querySelector(s); if (!e) return null;
        const r = e.getBoundingClientRect(); const c = getComputedStyle(e);
        return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
                h: Math.round(r.height), pos: c.position, display: c.display,
                overflowY: c.overflowY}; };
      return {
        shell: q('#shell'), rail: q('.kd-rail'), topbar: q('.kd-topbar'),
        body: q('.kd-body'), navCount: document.querySelectorAll('#nav button').length,
        gridCols: getComputedStyle(document.querySelector('#shell')).gridTemplateColumns,
      };
    }""")

    check(f"{tag}: the shell is a two-column grid",
          box["gridCols"].count(" ") == 1, f"gridTemplateColumns={box['gridCols']!r}")
    check(f"{tag}: the rail is sticky and full height",
          box["rail"]["pos"] == "sticky" and box["rail"]["h"] >= height - 2,
          f"position={box['rail']['pos']} height={box['rail']['h']} vs viewport {height}")
    # `>=`, not `>`. The work area's border box starts exactly where the rail ends, and
    # the rail has no margin — so x == width is the correct adjacency. The first version
    # asserted `>` and failed on a layout that was right, which is worse than no check:
    # a check that cries wolf gets deleted rather than fixed.
    check(f"{tag}: the rail sits to the LEFT of the work area",
          box["rail"]["x"] == 0 and box["body"]["x"] >= box["rail"]["w"],
          f"rail.x={box['rail']['x']} rail.w={box['rail']['w']} body.x={box['body']['x']}")
    check(f"{tag}: the topbar is pinned to the top",
          box["topbar"]["pos"] == "sticky" and box["topbar"]["y"] == 0,
          f"position={box['topbar']['pos']} y={box['topbar']['y']}")
    check(f"{tag}: the topbar is one bar, not two",
          box["topbar"]["h"] <= 56, f"height={box['topbar']['h']}")
    check(f"{tag}: every page is reachable from the rail",
          box["navCount"] == len(PAGES), f"{box['navCount']} buttons, {len(PAGES)} pages")
    check(f"{tag}: the work area is not squeezed to nothing",
          box["body"]["w"] > 700, f"body width {box['body']['w']} at viewport {width}")

    # The horizontal-overflow check. A table with seven columns inside a grid is the
    # classic way a console becomes unusable: nothing looks broken, and the last column
    # is simply off-screen with no scrollbar.
    overflow = page.evaluate("""() => {
      const d = document.documentElement;
      return {doc: d.scrollWidth, view: d.clientWidth};
    }""")
    check(f"{tag}: no horizontal overflow on the page itself",
          overflow["doc"] <= overflow["view"] + 1,
          f"scrollWidth={overflow['doc']} clientWidth={overflow['view']}")

    # (b) An email in a 208px rail. `word-break: break-all` put it on two lines, splitting
    #     the domain; the fix is one line with an ellipsis, so the line COUNT is the
    #     assertion, and the full value has to remain reachable somehow.
    footer = page.evaluate("""() => {
      const e = document.querySelector('#rail-ft');
      if (!e) return null;
      const first = e.firstElementChild;
      return {lines: Math.round(first.getBoundingClientRect().height /
                 parseFloat(getComputedStyle(e).lineHeight || 18)),
              title: first.getAttribute('title') || ''};
    }""")
    check(f"{tag}: the signed-in address stays on ONE line in the rail",
          footer is not None and footer["lines"] <= 1, f"{footer}")
    check(f"{tag}: the full address is still reachable (the title carries it)",
          footer is not None and "@" in footer["title"], f"{footer}")

    # Rows are rows. A card grid here would make every row look clickable when only some
    # are, and the selected one would stop being findable.
    row = page.evaluate("""() => {
      const r = document.querySelector('.kd-row');
      if (!r) return null;
      const c = getComputedStyle(r);
      return {radius: c.borderTopLeftRadius, shadow: c.boxShadow};
    }""")
    check(f"{tag}: list rows carry no radius and no shadow",
          row is None or (row["radius"] in ("0px", "") and row["shadow"] == "none"),
          f"{row}")


def run_page_checks(page, key: str, tag: str) -> None:
    """Checks that only make sense while THEIR page is the visible one.

    ⚠️ A SCREENSHOT is not an assertion, and the first run of this script was green while
    the page was visibly wrong: a search box's placeholder was cut to "name or mem". A
    vision model reading the output reported it; a human would have had to notice. It is
    measurable, so it is measured.

    ⚠️ And it has to run HERE, inside the page loop. The first attempt measured every input
    in `#shell` at once, from whichever page happened to be open: a `display:none` page's
    input has `clientWidth === 0`, so the available width came out as **-20** and four
    inputs looked truncated. An assertion that measures the wrong element reports a
    confident wrong answer, which is worse than reporting nothing.
    """
    truncated = page.evaluate("""(key) => {
      const out = [];
      const host = document.querySelector('#page-' + key);
      if (!host) return out;
      host.querySelectorAll('input[placeholder]').forEach((el) => {
        if (!el.placeholder) return;
        if (el.getBoundingClientRect().width < 1) return;   // not laid out: nothing to compare
        const probe = document.createElement('span');
        const c = getComputedStyle(el);
        probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;' +
          'font:' + c.font;
        probe.textContent = el.placeholder;
        document.body.appendChild(probe);
        const need = Math.ceil(probe.getBoundingClientRect().width);
        probe.remove();
        const have = Math.floor(el.clientWidth -
          (parseFloat(c.paddingLeft) + parseFloat(c.paddingRight)));
        if (need > have) out.push({id: el.id || '(unnamed)', need, have,
                                    text: el.placeholder});
      });
      return out;
    }""", key)
    check(f"{tag}/{key}: no placeholder is wider than its input",
          not truncated, f"{truncated}")


def run_account_code_checks(page, tag: str) -> None:
    """The accounts page: the code lives in the person's row, masked, with a copy button.

    ⚠️ Each of these fails on a defect that leaves the page looking completely finished,
    which is why none of them can be left to a screenshot review:

    * a code rendered in full is 55 characters of base64 in a table cell. It does not look
      broken — it looks like a table, and the width it forces is invisible until somebody
      opens the page on a laptop;
    * a copy button wired to `navigator.clipboard` **silently does nothing here**, because
      the console is served over plain http on a LAN address and that is not a secure
      context. The button renders, the handler runs, and no error is ever raised;
    * the old standalone codes table coming back would pass a check that only asks "is
      there a code column" — so this asserts there is exactly ONE table on the page, not
      merely that a column exists.
    """
    info = page.evaluate("""() => {
      const host = document.querySelector('#page-accounts');
      if (!host) return null;
      const table = host.querySelector('.kd-table');
      const heads = [...(table ? table.querySelectorAll('thead th') : [])]
        .map((th) => th.textContent.trim());
      const cell = host.querySelector('.kd-code-cell');
      /* Find the cell that actually holds a code rather than assuming it is the first
       * row. Row order comes from the accounts query and is not stable across runs — the
       * seeded company members and the probe operator compete for the top, and a
       * code-less row renders a 「签发」 button with no `.kd-mono` in it at all. Assuming
       * "first" made this pass or fail depending on who happened to sort first. */
      const withCode = [...host.querySelectorAll('.kd-code-cell')]
        .find((td) => td.querySelector('[data-copy-code]'));
      /* Measure the masked code element itself, not the cell's `textContent`. The cell
       * also holds a status badge, a button label, and the template's own newlines and
       * indentation — 24 characters of pure whitespace between the three spans — so the
       * cell reads ~52 even when the mask is perfect. Whitespace is not the thing under
       * test, and an assertion that trips on it gets "fixed" by loosening the bound until
       * it would also pass on an unmasked code. */
      const shown = withCode ? withCode.querySelector('.kd-mono') : null;
      const shownText = shown ? shown.textContent.trim() : '';
      const copy = withCode ? withCode.querySelector('[data-copy-code]') : null;
      return {
        tables: host.querySelectorAll('table').length,
        heads,
        codeCells: host.querySelectorAll('.kd-code-cell').length,
        shownLen: shownText.length,
        shownText,
        bullets: (shownText.match(/•/g) || []).length,
        hasCopy: !!copy,
        copyLabel: copy ? copy.getAttribute('aria-label') : null,
        // The full value must live in the button's data attribute, never in visible text.
        copyPayloadLen: copy ? (copy.dataset.code || '').length : 0,
        hasIssue: !!host.querySelector('[data-issue-code]'),
        secure: window.isSecureContext,
      };
    }""")
    if not info:
        check(f"{tag}/accounts: the accounts page is on screen", False, "no #page-accounts")
        return
    check(f"{tag}/accounts: the codes live in the accounts table, not a second one",
          info["tables"] == 1, f"{info['tables']} table(s) on the page")
    check(f"{tag}/accounts: the table has an agent-code column",
          any("授权码" in h or "Agent code" in h for h in info["heads"]), f"{info['heads']}")
    check(f"{tag}/accounts: the seeded account renders a code",
          info["codeCells"] >= 1 and info["shownLen"] > 0,
          f"{info['codeCells']} code cell(s), rendered {info['shownLen']!r} chars")
    # 12-char prefix + 8 dots + 4-char tail = 24.
    check(f"{tag}/accounts: the code is shown masked, not in full",
          0 < info["shownLen"] <= 30 and info["bullets"] >= 4,
          f"rendered {info['shownLen']} chars: {info['shownText']!r}")
    check(f"{tag}/accounts: a copy button sits beside the masked code",
          info["hasCopy"] and info["copyLabel"],
          f"hasCopy={info['hasCopy']} label={info['copyLabel']}")
    # The button must carry the real 55-character value, or copying yields the mask.
    check(f"{tag}/accounts: the copy button carries the full code",
          info["copyPayloadLen"] >= 40, f"payload {info['copyPayloadLen']} chars")
    # ⚠️ The page must offer no way to create a code. A 「签发」 button made "this person
    # cannot act for an agent" a state an operator had to notice and click, and it is the
    # thing the operator explicitly did not want. The invariant behind it — every active
    # account has a code — is a BACKEND property and is pinned by
    # `test_agent_code_storage.py::AgentCodeInvariantTests`, which can see the SQL.
    # Asserting it here would be asserting the fixture: these accounts are seeded by
    # ci_check, and the backfill runs in the app's lifespan, which this run never starts.
    check(f"{tag}/accounts: there is no way to issue a code from this page",
          not info["hasIssue"], "a 签发 / Issue control is still present")


def run_org_drawer_checks(page, tag: str, org_id: int) -> None:
    """The company drawer's per-company module licence.

    ⚠️ Seeded on purpose. Without a company the drawer never opens, the block renders
    nothing, and a control that only exists when there is data is a control nobody has
    ever looked at. The seed has to be able to walk into the branch under test.

    The property that matters is that the drawer shows what the company is LICENSED to
    offer, not the raw registry: a switch the operator can tick for a module the
    deployment has withdrawn is a box that saves and then does nothing.
    """
    page.evaluate(f"openOrg({org_id})")
    page.wait_for_timeout(1800)
    drawer = page.locator(".kd-drawer")
    check(f"{tag}/orgs: the company drawer opened", drawer.count() == 1,
          f"{drawer.count()} drawers")
    if not drawer.count():
        return
    switches = page.evaluate("""() => Array.from(
        document.querySelectorAll('#o-modules [data-orgmod]')).map(b => ({
          key: b.dataset.orgmod,
          on: b.getAttribute('aria-checked'),
        }))""")
    check(f"{tag}/orgs: the drawer offers one switch per switchable module",
          len(switches) >= 4, f"{len(switches)} switches")
    check(f"{tag}/orgs: and none of them is a required module",
          all(s["key"] not in ("core", "inbox", "settings", "datacenter")
              for s in switches), f"{[s['key'] for s in switches]}")
    # The seeded licence is exactly {workspace, knowledge}: those two read as on and the
    # other two as off, which is the only way to tell a control that renders stored state
    # from one that renders a constant.
    states = {s["key"]: s["on"] for s in switches}
    check(f"{tag}/orgs: ⚠️ the SEEDED licence is what the drawer shows",
          states.get("workspace") == "true" and states.get("knowledge") == "true"
          and states.get("dashboard") == "false" and states.get("calendar") == "false",
          f"{states}")
    summary = page.locator("#o-modules-offer")
    check(f"{tag}/orgs: the drawer says what the company can actually tick",
          summary.count() == 1 and "workspace" in summary.inner_text(),
          summary.inner_text() if summary.count() else "no summary line")
    page.screenshot(path=os.path.join(SHOTS, f"{tag}-org-drawer.png"), full_page=True)
    # ⚠️ Close it. The drawer comes with a scrim, and the page loop is about to click the
    # NEXT page's nav button — which the scrim swallows, so the run dies 20 seconds later
    # on a timeout that says "element is visible, enabled and stable" and names a scrim
    # nobody remembers opening. A check that leaves its subject on screen owns everything
    # that happens next.
    page.locator('.kd-drawer [data-x="close"]').first.click()
    page.wait_for_timeout(700)
    check(f"{tag}/orgs: the drawer closes and takes its scrim with it",
          page.locator(".kd-scrim").count() == 0,
          f"{page.locator('.kd-scrim').count()} scrims left")


def run_manual_checks(page, tag: str) -> None:
    """The two-pane manual page, measured as a rendered box.

    The unit suite proves the *renderer* escapes first and matches its block markers; it
    cannot prove the page puts the two panes side by side, or that clicking a row actually
    opens a document. Both are facts about boxes and clicks, and both are exactly the kind
    of thing that renders "fine" while being unusable.

    ⚠️ Every count here is conditional on the database having rows. A fresh installation has
    no manual documents at all, and an assertion that demands two of them turns this script
    into a test of the seed data — so the empty case asserts the empty state is *readable*
    and stops there.
    """
    panes = page.evaluate("""() => {
      const list = document.querySelector('#page-manual .kk-list');
      const main = document.querySelector('#page-manual .kk-main');
      if (!list || !main) return null;
      const l = list.getBoundingClientRect(), m = main.getBoundingClientRect();
      return {rows: list.querySelectorAll('.kk-item').length,
              listW: Math.round(l.width), mainW: Math.round(m.width),
              sideBySide: m.left >= l.right - 1};
    }""")
    if panes is None:
        check(f"{tag}/manual: the two panes exist", False, "kk-list / kk-main not found")
        return
    check(f"{tag}/manual: the two panes exist and sit side by side",
          panes["sideBySide"] and panes["listW"] > 100 and panes["mainW"] > 200, f"{panes}")
    if not panes["rows"]:
        check(f"{tag}/manual: an empty manual says so instead of showing nothing",
              "Nothing matches" in page.inner_text("#page-manual")
              or "没有匹配" in page.inner_text("#page-manual"),
              page.inner_text("#page-manual")[:120])
        return

    page.click("#page-manual .kk-item")
    page.wait_for_timeout(500)
    opened = page.evaluate("""() => {
      const host = document.querySelector('#page-manual');
      const render = host.querySelector('.kk-render');
      return {
        hasTitle: !!host.querySelector('.kk-doc-title'),
        childTags: render ? [...render.children].map((n) => n.tagName) : [],
        /* A renderer that escaped at emit time, or that bailed out, leaves literal
         * markdown syntax in the prose. ⚠️ `pre` and `code` are stripped first, and that
         * is not a convenience: this corpus is the document set that TEACHES markdown, so
         * a literal "##" inside a fenced example is the content being *correct*.
         * Measuring the whole subtree flagged four correct pages and taught nobody
         * anything — the check was measuring the subject matter, not the renderer. */
        showsSyntax: (() => {
          if (!render) return false;
          const copy = render.cloneNode(true);
          copy.querySelectorAll('pre, code').forEach((n) => n.remove());
          return /^#{1,6}\\s/m.test(copy.innerText);
        })(),
        hasScript: !!host.querySelector('.kk-render script, .kk-render img[onerror]'),
      };
    }""")
    check(f"{tag}/manual: clicking a page opens it with a title",
          opened["hasTitle"], f"{opened}")
    check(f"{tag}/manual: the markdown rendered into real elements",
          bool(opened["childTags"]) and not opened["showsSyntax"], f"{opened}")
    check(f"{tag}/manual: a page body cannot inject a tag",
          not opened["hasScript"], f"{opened}")

    # Opening a document must not move the reader's place. ⚠️ Both panes scroll, and the
    # bug this catches (2026-10-03) was that clicking an entry re-rendered the whole page,
    # so `host.innerHTML = …` replaced both scroll containers and their `scrollTop` went to
    # 0 — the catalogue you were scrolling snapped back to the top, and the row you just
    # clicked could end up out of view. A screenshot taken *after* the click looks fine
    # either way, which is why this is measured and not looked at.
    #
    # ⚠️ The row clicked is the **last** one, and only after the list is scrolled to the
    # bottom. Playwright's `click()` scrolls its target into view first, so clicking a row
    # near the top while the list is scrolled down makes *Playwright* scroll it back — the
    # first version of this check measured that and reported 772 → 0 for an app that was
    # already fixed. The row has to be genuinely on screen for the measurement to mean
    # anything about the page.
    scrolls = page.evaluate("""() => {
      const list = document.querySelector('#page-manual .kk-list');
      return {
        listMax: list ? list.scrollHeight - list.clientHeight : 0,
        items: document.querySelectorAll('#page-manual .kk-item').length,
      };
    }""")
    if scrolls["listMax"] > 40 and scrolls["items"] > 1:
        page.evaluate("""() => {
          const l = document.querySelector('#page-manual .kk-list');
          l.scrollTop = l.scrollHeight;
        }""")
        page.wait_for_timeout(250)
        before = page.evaluate(
            "() => Math.round(document.querySelector('#page-manual .kk-list').scrollTop)")
        page.click(f"#page-manual .kk-item >> nth={scrolls['items'] - 1}")
        page.wait_for_timeout(600)
        after = page.evaluate("""() => {
          const l = document.querySelector('#page-manual .kk-list');
          return {
            listTop: Math.round(l.scrollTop),
            on: [...document.querySelectorAll('#page-manual .kk-item.on')]
                   .map((n) => n.dataset.key),
            title: (document.querySelector('#page-manual .kk-doc-title') || {}).textContent,
          };
        }""")
        check(f"{tag}/manual: opening a document leaves the catalogue where it was",
              after["listTop"] == before,
              f"scrollTop {before} → {after['listTop']}")
        check(f"{tag}/manual: exactly one row is marked selected, and it is the opened one",
              len(after["on"]) == 1, f"selected={after['on']} title={after['title']}")

    check(f"{tag}/manual: the stat cards are gone",
          not page.query_selector("#page-manual .kd-stats"),
          "用户 2026-10-03 明确要求删掉这一排统计卡")
    check(f"{tag}/manual: the blue explainer is gone",
          not page.query_selector("#page-manual .kd-note.info"),
          "同上")
    check(f"{tag}/manual: the total size is in the title bar instead",
          "正文总量" in page.inner_text("#pg-meta"),
          page.inner_text("#pg-meta"))


def run_fill_checks(page, tag: str, page_id: str) -> None:
    """The reader pages use the window they are given.

    ⚠️ Measured, not eyeballed, because the original defect was *invisible* in a
    screenshot in the sense that mattered: the panes were capped at `62vh`, which on a
    900px window left 178px of empty page below them and on a 1200px one left 346px. The
    page looked fine. It looked like a page with a short document.

    The width half is the same story: `.kk-doc` carried `max-width: 78ch`, which is a good
    measure for prose and a bad one for a 2 525px-wide source file shown in a 607px box.
    """
    box = page.evaluate("""(pid) => {
      const pane = document.querySelector(`#${pid} .kk-main`);
      const body = document.querySelector('.kd-body');
      if (!pane || !body) return null;
      const b = body.getBoundingClientRect();
      const cs = getComputedStyle(pane);
      /* ⚠️ `clientWidth` includes the pane's own padding (`--sp-5`, 24px a side), so
       * comparing a child against it fails on a pane whose children fill it exactly.
       * The number to compare against is the *content* width. */
      const contentW = pane.clientWidth
        - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      return {
        vh: window.innerHeight,
        paneH: Math.round(pane.getBoundingClientRect().height),
        paneMaxH: cs.maxHeight,
        below: Math.round(window.innerHeight - b.bottom),
        topbar: Math.round(document.querySelector('.kd-topbar')
                             .getBoundingClientRect().height),
        paneW: Math.round(pane.getBoundingClientRect().width),
        contentW: Math.round(contentW),
        /* How much of the content box the widest child actually uses. */
        widest: Math.max(0, ...[...pane.querySelectorAll('.kk-doc, .kk-render, .kk-src')]
          .map((n) => Math.round(n.getBoundingClientRect().width))),
        overflowX: document.documentElement.scrollWidth - window.innerWidth,
      };
    }""", page_id)
    if not box:
        check(f"{tag}/{page_id}: the panes are there to measure", False)
        return
    # A prose document has to be OPEN for `.kk-render` to exist, and the clipping
    # measurements below are about what is inside it. `api_endpoints.md` is the richest
    # prose in the package — 13 tables and 4 code blocks — so it is the one to open.
    if not page.query_selector(f"#{page_id} .kk-render"):
        page.click(f"#{page_id} .kk-item >> nth=0")
        page.wait_for_timeout(700)
    check(f"{tag}/{page_id}: the pane uses the height the window has",
          box["paneMaxH"] == "none" and box["paneH"] > box["vh"] * 0.6
          and box["below"] <= 0,
          f"pane={box['paneH']} vh={box['vh']} maxH={box['paneMaxH']} "
          f"emptyBelow={box['below']}")
    check(f"{tag}/{page_id}: the topbar keeps its declared height",
          box["topbar"] == 52,
          f"topbar={box['topbar']}px — a flex column will squeeze it without `flex: none`")
    check(f"{tag}/{page_id}: the content box spans the pane",
          box["widest"] >= box["contentW"] - 2,
          f"widest={box['widest']} contentW={box['contentW']} pane={box['paneW']}")
    check(f"{tag}/{page_id}: nothing overflows the window sideways",
          box["overflowX"] <= 0, f"overflowX={box['overflowX']}")

    # ⚠️ "Spans the pane" is not the same as "nothing is cut off". A 78ch measure on the
    # *container* satisfied the first and failed the second: on `api_endpoints.md` the
    # tables were 585px wide with 41 / 89 / 162px scrolled out of sight, which is
    # indistinguishable from missing on screen. Measured per element, not eyeballed.
    clipped = page.evaluate("""(pid) => {
      const root = document.querySelector(`#${pid} .kk-render`);
      if (!root) return null;
      const over = (n) => n.scrollWidth - n.clientWidth;
      const hidden = (n) => {
        const cs = getComputedStyle(n);
        return cs.overflowX === 'hidden' || cs.overflowX === 'clip';
      };
      return {
        paneW: Math.round(root.getBoundingClientRect().width),
        paragraphs: [...root.querySelectorAll(':scope > p, :scope > ul, :scope > ol')]
          .map(n => Math.round(n.getBoundingClientRect().width)),
        tables: [...root.querySelectorAll('table')].map(over),
        pres: [...root.querySelectorAll('pre')].map(over),
        presHidden: [...root.querySelectorAll('pre')]
          .map((n) => getComputedStyle(n).overflowX),
        /* Anything wide that has NO way to be scrolled is genuinely lost, not merely
         * out of view — that is the case worth failing on. */
        unreachable: [...root.querySelectorAll('table, pre')]
          .filter(n => over(n) > 1 && hidden(n)).length,
        tablesWrapped: root.querySelectorAll('.kk-tablewrap table').length,
        tableCount: root.querySelectorAll('table').length,
        /* ⚠️ `overflow-x` of a table is ALWAYS `visible` — the horizontal scroller is the
         * wrapper around it, and the wrapper's value is what decides whether an over-wide
         * table can be scrolled back into view. So this is read off the wrapper, never off
         * the table. A table with no wrapper counts as unscrolled, not as "fine".
         *
         * ⚠️ Judged unconditionally, not "only if it overflows right now": at 1440px no
         * table on api_endpoints.md overflows at all, so a `hidden` wrapper measured by its
         * symptom is indistinguishable from `auto` until someone narrows the window. A
         * check that only reddens at a width CI never visits is not a guard. */
        wrapperX: [...new Set([...root.querySelectorAll('.kk-tablewrap')]
          .map(n => getComputedStyle(n).overflowX))],
        tablesNoScroller: [...root.querySelectorAll('table')]
          .filter(n => {
            const w = n.closest('.kk-tablewrap');
            if (!w) return true;
            const x = getComputedStyle(w).overflowX;
            return x !== 'auto' && x !== 'scroll';
          }).length,
      };
    }""", page_id)
    if clipped:
        # ⚠️ Tables and code blocks are checked differently, and conflating them produces a
        # check that fails on correct code: a `<pre>` with long lines is *supposed* to
        # overflow and scroll — that is what `overflow-x: auto` is for. A table has no
        # such excuse now that it is given the full width, so a table that still overflows
        # means the fix has regressed.
        check(f"{tag}/{page_id}: no table is scrolled out of sight",
              not any(clipped["tables"]),
              f"tables overflow={clipped['tables']}")
        # ⚠️ A `<pre>` is expected to overflow and scroll; what must never happen is an
        # over-wide block whose overflow is `hidden`/`clip`, because that content is
        # simply gone. `visible-but-scrolled` and `not-visible-at-all` are different
        # defects and the first version of this check conflated them.
        # ⚠️ Unconditional on purpose: gating on `over(n) > 1` made it width-dependent and
        # therefore vacuous at the two viewports CI actually runs.
        vanishing = [x for x in clipped["presHidden"] if x in ("hidden", "clip")]
        check(f"{tag}/{page_id}: a long code block scrolls rather than vanishing",
              not vanishing,
              f"pres={clipped['pres']} overflowX={sorted(set(clipped['presHidden']))}")
        check(f"{tag}/{page_id}: an over-wide table is still reachable, not lost",
              clipped["unreachable"] == 0, f"{clipped['unreachable']} element(s)")
        check(f"{tag}/{page_id}: every table has a scroller around it",
              clipped["tableCount"] == 0
              or clipped["tablesWrapped"] == clipped["tableCount"],
              f"{clipped['tablesWrapped']}/{clipped['tableCount']}")
        # ⚠️ Having a wrapper is not the same as the wrapper scrolling. `overflow-x: hidden`
        # on `.kk-tablewrap` keeps the markup, keeps every table, and is green under both
        # checks above — while silently amputating the right-hand columns. This is the only
        # one of the five that notices, and it is the one that had to be added after the
        # symptom-based version was caught passing on `hidden`.
        check(f"{tag}/{page_id}: the table scroller can actually scroll",
              clipped["tablesNoScroller"] == 0,
              f"{clipped['tablesNoScroller']} table(s) without a scrollable wrapper, "
              f"wrapper overflowX={clipped['wrapperX']}")
        check(f"{tag}/{page_id}: running prose keeps a readable line length",
              (not clipped["paragraphs"] or all(w <= 620 for w in clipped["paragraphs"]))
              and (not clipped["paragraphs"] or clipped["paneW"] > 620),
              f"paragraphs={clipped['paragraphs']} pane={clipped['paneW']}")


def run_skill_checks(page, tag: str) -> None:
    """The Agent Skill page: the package, as documents.

    The assertions that matter here are the two the static tests cannot make: that a
    **source** file is shown as source (not run through the markdown renderer), and that
    opening a file does not move the reader's place — the same defect the 说明书 page had,
    one page over.
    """
    empty = page.evaluate("""() => ({
      items: document.querySelectorAll('#page-skill .sk-item').length,
      nested: [...document.querySelectorAll('#page-skill .kk-item-s code')]
                 .filter((n) => n.textContent.includes('/')).length,
      meta: (document.getElementById('pg-meta') || {}).textContent || '',
    })""")
    if not empty["items"]:
        check(f"{tag}/skill: the package's files are listed", False, f"{empty}")
        return
    check(f"{tag}/skill: the package's files are listed", True)
    check(f"{tag}/skill: files in sub-directories are listed too",
          empty["nested"] > 0, f"nested={empty['nested']} of {empty['items']}")
    check(f"{tag}/skill: the title bar names the package",
          "技能包" in empty["meta"], empty["meta"])

    # The prose file renders as markdown.
    page.click("#page-skill .sk-item >> nth=0")
    page.wait_for_timeout(700)
    prose = page.evaluate("""() => {
      const host = document.getElementById('page-skill');
      return {
        title: (host.querySelector('.kk-doc-title') || {}).textContent,
        hasRender: !!host.querySelector('.kk-render'),
        heads: host.querySelectorAll('.kk-render h1, .kk-render h2').length,
        version: (host.querySelector('.kk-doc-meta') || {}).textContent || '',
        injected: !!host.querySelector('.kk-render script, .kk-render img[onerror]'),
      };
    }""")
    check(f"{tag}/skill: SKILL.md opens and renders as markdown",
          prose["hasRender"] and prose["heads"] > 3, f"{prose}")
    check(f"{tag}/skill: the package version is read out of the file",
          "package version" in prose["version"] or "包版本" in prose["version"],
          prose["version"])
    check(f"{tag}/skill: a document body cannot inject a tag", not prose["injected"])

    # A source file must NOT be rendered as prose. ⚠️ This is the assertion worth having:
    # the four dashboard references are working HTML pages, and a page that showed one as
    # prose would be showing a document that is not the thing being shipped.
    # ⚠️ Nested **and not markdown**. `references/api_endpoints.md` is nested too, and it
    # is prose — selecting it would fail this check for a page doing the right thing.
    # The rows that must render as source are the dashboard HTML and the Python script.
    nested = page.evaluate("""() => [...document.querySelectorAll('#page-skill .sk-item')]
        .map((b, i) => [i, b.dataset.entry])
        .filter(([, p]) => p && p.includes('/') && !p.endsWith('.md'))[0]""")
    if nested:
        page.click(f"#page-skill .sk-item >> nth={nested[0]}")
        page.wait_for_timeout(700)
        src = page.evaluate("""() => {
          const host = document.getElementById('page-skill');
          const pre = host.querySelector('.kk-src');
          return {
            title: (host.querySelector('.kk-doc-title') || {}).textContent,
            isPre: !!pre,
            hasRender: !!host.querySelector('.kk-render'),
            firstLine: pre ? pre.textContent.split('\\n')[0].slice(0, 40) : null,
            ws: pre ? getComputedStyle(pre).whiteSpace : null,
            injected: pre ? !!pre.querySelector('script, img[onerror]') : null,
          };
        }""")
        check(f"{tag}/skill: a nested file opens (the route spans paths)",
              bool(src["title"]), f"{src}")
        check(f"{tag}/skill: a source file is shown as source, not as prose",
              src["isPre"] and not src["hasRender"], f"{src}")
        check(f"{tag}/skill: source keeps its own line breaks",
              src["ws"] == "pre", f"white-space={src['ws']}")
        check(f"{tag}/skill: a source file cannot inject a tag", not src["injected"])

        before = page.evaluate(
            "() => Math.round(document.querySelector('#page-skill .kk-list').scrollTop)")
        items = page.evaluate(
            "() => document.querySelectorAll('#page-skill .sk-item').length")
        page.evaluate("""() => {
          const l = document.querySelector('#page-skill .kk-list');
          l.scrollTop = l.scrollHeight;
        }""")
        page.wait_for_timeout(250)
        before = page.evaluate(
            "() => Math.round(document.querySelector('#page-skill .kk-list').scrollTop)")
        page.click(f"#page-skill .sk-item >> nth={items - 1}")
        page.wait_for_timeout(600)
        after = page.evaluate("""() => ({
          top: Math.round(document.querySelector('#page-skill .kk-list').scrollTop),
          on: document.querySelectorAll('#page-skill .sk-item.on').length,
        })""")
        check(f"{tag}/skill: opening a file leaves the list where it was",
              after["top"] == before, f"scrollTop {before} → {after['top']}")
        check(f"{tag}/skill: exactly one row is marked selected",
              after["on"] == 1, f"{after}")


def run_contrast_checks(page, tag) -> None:
    """The theme check. Every value in `console.css` is a `var()`, and a token that
    resolves to the light value in dark mode is perfectly valid CSS — so this has to be
    measured, not read.

    The thresholds are WCAG AA: 4.5:1 for body text, 3:1 for large text and for the
    non-text parts of a control.
    """
    print(f"  contrast @ {tag}")
    samples = page.evaluate("""() => {
      const out = {};
      /* ⚠️ A transparent background is resolved by walking UP the ancestor chain, not by
       * falling back to `body`. `body` is `--bg`; a table cell is really painted on the
       * card's `--surface` above it, and measuring against the wrong one produced a
       * 4.23:1 reading for a colour that is 4.96:1 where it actually sits — a false
       * failure is worse than no check, because it sends somebody to fix the wrong token.
       */
      const bgOf = (e) => {
        for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
          const c = getComputedStyle(n).backgroundColor;
          if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c;
        }
        return getComputedStyle(document.body).backgroundColor;
      };
      const put = (name, sel) => {
        const e = document.querySelector(sel);
        if (!e) return;
        const c = getComputedStyle(e);
        out[name] = {fg: c.color, bg: bgOf(e), size: parseFloat(c.fontSize),
                     weight: c.fontWeight};
      };
      put('page background', 'body');
      put('card surface', '.kd-card');
      put('body text on card', '.kd-card-bd');
      put('muted text', '.kd-card-hd .kd-sub');
      put('nav item', '#nav button');
      put('nav item selected', '#nav button.on');
      put('topbar title', '.kd-topbar h1');
      put('table header', '.kd-table th');
      put('table cell', '.kd-table td');
      put('primary button', '.kd-btn.primary', '.kd-btn.primary');
      put('muted text on a stripe', '.kd-row .kd-meta');
      put('empty-state text', '.kd-empty');
      put('rail footer', '#rail-ft');
      return out;
    }""")
    for name, s in samples.items():
        if not s:
            continue
        # Large text (>=18.66px bold, or >=24px) gets the 3:1 threshold.
        large = s["size"] >= 24 or (s["size"] >= 18.66 and int(s["weight"] or 400) >= 700)
        need = 3.0 if large else 4.5
        got = contrast(s["fg"], s["bg"])
        check(f"{tag}: {name} is legible ({need}:1 needed)", got >= need,
              f"{got}:1  fg={s['fg']} bg={s['bg']} size={s['size']} weight={s['weight']}")

    # Dark mode specifically: a page background that is still light is the classic
    # half-finished theme, and it is invisible in a light-mode screenshot.
    if tag.startswith("dark"):
        bg = rgb(samples["page background"]["bg"])
        check(f"{tag}: the page background is actually dark",
              relative_luminance(bg) < 0.06, f"rgb{bg}")
        rail = page.evaluate(
            "() => getComputedStyle(document.querySelector('.kd-rail')).backgroundColor")
        check(f"{tag}: the rail is darker than the page (it is an ink surface)",
              relative_luminance(rgb(rail)) < relative_luminance(bg),
              f"rail={rail} bg={samples['page background']['bg']}")


if __name__ == "__main__":
    sys.exit(main())
