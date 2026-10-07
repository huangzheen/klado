"""The import dialog has to fit on one screen, and this is the half that can measure.

⚠️ The requirement is the user's, verbatim (2026-10-06): 「所有的信息在一屏里面显示，不希望滑动。
信息尽量紧凑点。」 The previous version of this file asserted the OPPOSITE shape —
that the body IS a scroll container and that the buttons survive scrolling to the
bottom. That was right for a dialog that had to work on a 720px screen, and it is
the wrong target now: a dialog that is merely *scrollable* is not one screen.

So the load-bearing assertion is `scrollHeight <= clientHeight + 1` on the dialog
body, at three window sizes, in both themes, with the schedule form open as well as
closed. `overflow: auto` is still declared — it is the safety valve for a window
shorter than any of these — but nothing below asserts that it is ever *used*.

The static half is `tests/test_dc_modal_layout.py`: the cap on each list, the two
columns, the absence of a native `<select>`. Neither half can see the other's
failure, which is why both exist.

Needs a live server:  KLADO_BASE=http://127.0.0.1:<port> .venv312/bin/python tests/verify_dc_modal_layout_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, dataset_groups                       # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-modal-layout")

PASS = 0
FAIL = 0
FAILURES: list[str] = []

# ⚠️ The sizes a person actually uses, not one tall maximised window: a laptop at
# 1280x800 is the tightest of the three, and 1440x720 is the shortest. The dialog is
# 513px tall as built, so all three have real slack — which is the point, because
# slack is what "fits" means.
VIEWPORTS = [("wide", 1440, 900), ("laptop", 1280, 800), ("short", 1440, 720)]


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} :: {detail}")


MEASURE = """() => {
    const vh = window.innerHeight;
    const body = document.querySelector('#dc-ie-modal .dc-modal-pad');
    const modal = document.querySelector('#dc-ie-modal .dc-modal');
    const mb = modal.getBoundingClientRect();
    const outside = [];
    // Every control, not just the footer: the one-screen requirement is that nothing
    // is BELOW the fold, and a control that hangs off the bottom of the box is the
    // same defect as a button the footer lost.
    modal.querySelectorAll('input, button, .dc-at-go, .dc-ie-chip').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;      // a hidden one is fine
        if (r.bottom > mb.bottom + 1 || r.top < mb.top - 1) {
            outside.push((el.id || el.className || el.tagName) + ' '
                         + Math.round(r.top) + '..' + Math.round(r.bottom));
        }
    });
    const pill = document.querySelector('#dc-ie-pills .dc-ie-pill input');
    const modes = Array.from(document.querySelectorAll('#dc-ie-modal .dc-ie-modes label'))
        .map(l => Math.round(l.getBoundingClientRect().width));
    return {
        vh,
        bodyOverflows: body.scrollHeight > body.clientHeight + 1,
        bodyH: body.clientHeight, bodyScrollH: body.scrollHeight,
        modalH: Math.round(mb.height), modalW: Math.round(mb.width),
        modalFits: mb.bottom <= vh + 1 && mb.top >= -1,
        outside,
        modes,
        schedChecked: document.getElementById('dc-ie-sched').checked,
        pillDisabled: pill ? pill.disabled : null,
        pillChecked: (document.querySelector('input[name="dc-ie-interval"]:checked') || {}).value,
        formHidden: document.getElementById('dc-ie-sched-form').hidden,
        okBtn: (() => { const b = document.getElementById('dc-ie-ok').getBoundingClientRect();
                        return {top: Math.round(b.top), bottom: Math.round(b.bottom)}; })(),
    };
}"""


def measure(page):
    return page.evaluate(MEASURE)


def main():
    os.makedirs(SHOTS, exist_ok=True)
    email = f"modallay{int(time.time())}@example.test"
    pw = "ModalLayout123!"
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, 'Modal layout probe', 'admin')",
                    (email, auth_store._hash(pw)))
        conn.commit()

    slug = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for theme in ("light", "dark"):
                for label, w, h in VIEWPORTS:
                    ctx = browser.new_context(viewport={"width": w, "height": h})
                    page = ctx.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(BASE + "/", wait_until="domcontentloaded")
                    page.wait_for_selector("#auth-login-email", timeout=20000)
                    page.evaluate("t => localStorage.setItem('klado-theme', t)", theme)
                    page.fill("#auth-login-email", email)
                    page.fill("#auth-login-password", pw)
                    page.click("#auth-login-btn")
                    page.wait_for_timeout(3500)
                    page.click("#nav-tab-datacenter")
                    page.wait_for_timeout(3500)
                    if slug is None:
                        slug = dataset_groups.create_group(
                            f"modal-probe-{int(time.time())}", "layout probe", email)["slug"]
                    page.evaluate("s => dc.openImportExternal(s)", slug)
                    page.wait_for_timeout(1500)
                    tag = f"{theme}/{label} {w}x{h}"

                    m = measure(page)
                    # ── the requirement ──
                    check(f"[{tag}] ONE SCREEN: the body does not scroll",
                          not m["bodyOverflows"],
                          f"content {m['bodyScrollH']}px in a {m['bodyH']}px box — the "
                          f"dialog is taller than the screen it has to fit in")
                    check(f"[{tag}] no control hangs off the bottom of the dialog",
                          not m["outside"], f"outside={m['outside']}")
                    check(f"[{tag}] the dialog fits the window",
                          m["modalFits"], f"height={m['modalH']} vh={m['vh']}")
                    check(f"[{tag}] the Import button is on screen",
                          0 <= m["okBtn"]["top"] < m["vh"], f"rect={m['okBtn']} vh={m['vh']}")

                    # ── the controls that replaced the native ones ──
                    check(f"[{tag}] the interval pills start disabled with the switch off",
                          m["schedChecked"] is False and m["pillDisabled"] is True,
                          f"checked={m['schedChecked']} pillDisabled={m['pillDisabled']}")
                    check(f"[{tag}] the mode cards are not stretched across the dialog",
                          bool(m["modes"]) and all(w < 260 for w in m["modes"]),
                          f"widths={m['modes']} in a {m['modalW']}px dialog")

                    # ── the switch really is a switch ──
                    page.click(".dc-ie-switchrow")
                    page.wait_for_timeout(300)
                    m2 = measure(page)
                    check(f"[{tag}] clicking the switch turns it on and wakes the pills",
                          m2["schedChecked"] is True and m2["pillDisabled"] is False,
                          f"checked={m2['schedChecked']} pillDisabled={m2['pillDisabled']}")
                    # A real click on the pill face, not `check()`: the radio is
                    # visually hidden behind the span, and the label is the target a
                    # person uses.
                    page.click('#dc-ie-pills .dc-ie-pill:nth-child(2)')
                    page.wait_for_timeout(200)
                    m3 = measure(page)
                    check(f"[{tag}] a pill face selects that interval",
                          m3["pillChecked"] == "60", f"checked={m3['pillChecked']}")

                    # ── the new capability: a schedule for a copy that already exists ──
                    page.click("#dc-ie-sched-new")
                    page.wait_for_timeout(400)
                    m4 = measure(page)
                    check(f"[{tag}] the schedule-an-existing-copy form opens",
                          m4["formHidden"] is False, "the form stayed hidden")
                    # ⚠️ The pills are shared by BOTH ways of making a schedule, so
                    # opening the standalone form has to wake them. Gating them on the
                    # import switch alone left this form with every interval disabled —
                    # a control that looks like a choice and cannot be made.
                    check(f"[{tag}] the interval pills are live while that form is open",
                          m4["pillDisabled"] is False,
                          "the standalone schedule has no way to choose an interval")
                    check(f"[{tag}] ONE SCREEN with that form open too",
                          not m4["bodyOverflows"] and not m4["outside"],
                          f"content {m4['bodyScrollH']}px in {m4['bodyH']}px, "
                          f"outside={m4['outside']}")
                    page.screenshot(path=os.path.join(SHOTS, f"new-{theme}-{label}.png"))

                    # A cancel must really cancel, or the form is a trap: the next
                    # person to open this dialog for another container would find
                    # yesterday's source table filled in.
                    page.click("#dc-ie-sched-cancel")
                    page.wait_for_timeout(300)
                    m5 = measure(page)
                    check(f"[{tag}] the form closes again",
                          m5["formHidden"] is True, "cancel left the form open")
                    check(f"[{tag}] no uncaught error", not errors, "; ".join(errors[:2]))
                    ctx.close()
            # ⚠️ Inside the `with`. A `browser.close()` after it lets the event loop
            # shut down first and raises "Event loop is closed" — which turned a fully
            # green run into a traceback and a non-zero exit.
            browser.close()
    finally:
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
            conn.commit()
        if slug:
            try:
                dataset_groups.delete_group(slug, email)
            except Exception:  # noqa: BLE001
                pass
        print("cleaned up")

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
