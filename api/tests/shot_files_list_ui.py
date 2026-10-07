"""Screenshot the Data Center FILES tab and assert the panes are not cards.

The change this guards is a VISUAL one, and it is easy to undo by accident: the
panes used to paint themselves `var(--surface)` — white — on a `var(--bg)` page, so
the whole browser read as one big card you could pick up. The row rule underneath was
the sharper half of it: `var(--surface-2)`, which in the light theme is #f8fafc on a
white row, a contrast of about 1.03:1. A hairline you cannot see is not a hairline,
and the rows read as items floating loose rather than as a list.

So the assertions are about PAINTS, not about layout: every pane must carry the page
background, and the rule between rows must be the same `--border` the Inbox rows use.

    ../.venv312/bin/python tests/shot_files_list_ui.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, data_center_db as db, oss_storage   # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-files-shots")

PASS = 0
FAIL = 0
FAILURES: list[str] = []

PANES = [".dc-fb-page", ".dc-fb-tree", ".dc-fb-main", ".dc-fb-preview"]

# Measured on a page that is not Data Center, and compared with Data Center's own.
# "Looks like the other pages" is only a claim until something measures the other pages.
TOOLBAR_PROPS = ["backgroundColor", "borderBottomColor", "borderBottomWidth",
                 "paddingTop", "paddingLeft", "height"]


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def rgb(css_rgb):
    """'rgb(246, 248, 251)' -> (246, 248, 251). getComputedStyle always answers in the
    rgb()/rgba() form, so pulling the numbers out is enough and avoids a colour parser."""
    return tuple(int(float(n)) for n in re.findall(r"[\d.]+", css_rgb)[:3])


def main():
    stamp = int(time.time())
    tag = f"fileslist{stamp}"
    email = f"files-list-{stamp}@example.test"
    pw = "Files-List-Probe-1!"

    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, 'files list', 'admin')", (email, auth_store._hash(pw)))
        cur.execute("SELECT id FROM app_users WHERE email = %s", (email,))
        uid = cur.fetchone()[0]
        conn.commit()

    # Three folders plus a real uploaded file. The file is not decoration: a FOLDER row
    # navigates when clicked, so a fixture of nothing but folders leaves the selected
    # row unselectable and the selection assertion would never see a selected row at
    # all. That is the "a fake must be able to reach the branch under test" rule, in UI
    # fixture form.
    paths = [f"{tag}_{n}" for n in ("alpha", "beta", "gamma")]
    for p in paths:
        db.create_folder(p)
    blob = b"region,amount\nNorth,12\nSouth,7\n"
    # At the ROOT, not inside one of the folders: this list shows one level, and a file
    # filed under a folder would only appear after navigating in.
    file_key = f"{tag}-regional-spend.csv"
    oss_storage.put_object(db.RAW_BUCKET, file_key, blob)
    db.register_oss_file(file_key, [], len(blob), owner_email=email)
    os.makedirs(SHOTS, exist_ok=True)
    print(f"seeded {len(paths)} folders, user id={uid}")
    try:
        with sync_playwright() as pw2:
            browser = pw2.chromium.launch()
            for theme in ("light", "dark"):
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(BASE + "/", wait_until="domcontentloaded")
                page.wait_for_selector("#auth-login-email", timeout=15000)
                page.fill("#auth-login-email", email)
                page.fill("#auth-login-password", pw)
                page.click("#auth-login-btn")
                page.wait_for_timeout(3500)
                page.evaluate(
                    "document.documentElement.setAttribute('data-theme', '%s')" % theme)
                page.click("#nav-tab-datacenter")
                page.wait_for_timeout(3500)
                # Data Center opens on Files, but the list is loaded on navigation, so
                # re-ask for it the way a person would rather than trusting the timing.
                page.evaluate("window.dc && window.dc.loadFiles()")
                page.wait_for_timeout(3000)

                paint = page.evaluate("""(panes) => {
                  const root = getComputedStyle(document.documentElement);
                  const bg = root.getPropertyValue('--bg').trim();
                  const border = root.getPropertyValue('--border').trim();
                  const px = v => {
                    const d = document.createElement('div');
                    d.style.color = v; document.body.appendChild(d);
                    const c = getComputedStyle(d).color; d.remove(); return c;
                  };
                  return {
                    bg: px(bg), border: px(border),
                    panes: panes.map(sel => {
                      const el = document.querySelector(sel);
                      return el ? getComputedStyle(el).backgroundColor : null;
                    }),
                    rows: [...document.querySelectorAll('#dc-fb-list .dc-fb-row')].map(r => {
                      const cs = getComputedStyle(r);
                      return {rule: cs.borderBottomColor, shadow: cs.boxShadow,
                              radius: cs.borderTopLeftRadius, bg: cs.backgroundColor};
                    }),
                  };
                }""", PANES)

                page_bg = rgb(paint["bg"])
                for sel, got in zip(PANES, paint["panes"]):
                    check(f"[{theme}] {sel} sits on the page background, not on a card",
                          got is not None and rgb(got) == page_bg,
                          f"page={paint['bg']} pane={got}")

                rows = paint["rows"]
                check(f"[{theme}] the list has rows to divide", len(rows) >= 3,
                      f"only {len(rows)} rows")
                if len(rows) >= 3:
                    check(f"[{theme}] rows are divided by the Inbox hairline",
                          all(r["rule"] == paint["border"] for r in rows),
                          f"rule={rows[0]['rule']} --border={paint['border']}")
                    check(f"[{theme}] no row carries a card shadow",
                          all(r["shadow"] in ("none", "") for r in rows),
                          f"shadow={rows[0]['shadow']}")
                    check(f"[{theme}] rows are not rounded cards",
                          all(r["radius"] in ("0px", "") for r in rows),
                          f"radius={rows[0]['radius']}")

                # Selecting a row must be visible: the Inbox marks it with a 3px accent
                # bar, and a collapsed table will not paint a border-left on the row, so
                # that bar is an inset shadow. This catches the bar being dropped. Click
                # a FILE row (`data-path`) — a folder row navigates instead of selecting.
                page.locator("#dc-fb-list .dc-fb-row[data-path]").first.click()
                page.wait_for_timeout(600)
                sel = page.evaluate("""() => {
                  const r = document.querySelector('#dc-fb-list .dc-fb-row.selected');
                  if (!r) return null;
                  const cs = getComputedStyle(r);
                  return {bg: cs.backgroundColor, shadow: cs.boxShadow};
                }""")
                check(f"[{theme}] the selected row is marked",
                      bool(sel) and "inset" in (sel["shadow"] or ""),
                      f"selected={sel}")

                # ── It has to look like the rest of the app, not like a second app ──
                # The Data Center used to be a `position: fixed; inset: 0` overlay at
                # the same z-index as the nav and later in the document, so it painted
                # over the global navigation — the one page in the product with no nav
                # bar — and it carried its own top bar PLUS a second one per section.
                shell = page.evaluate("""() => {
                  const nav = document.querySelector('.nav');
                  const dc = document.getElementById('datacenter-page');
                  const n = nav.getBoundingClientRect(), d = dc.getBoundingClientRect();
                  const groups = {};
                  for (const [k, id] of [['files','dc-tools-files'],
                                         ['datasets','dc-tools-datasets']]) {
                    const el = document.getElementById(id);
                    groups[k] = el ? getComputedStyle(el).display : null;
                  }
                  return {
                    navVisible: getComputedStyle(nav).display !== 'none' && n.height > 0,
                    navBottom: Math.round(n.bottom), dcTop: Math.round(d.top),
                    toolbars: document.querySelectorAll(
                      '#datacenter-page > .rpt-toolbar').length,
                    groups,
                    navActive: document.querySelector(
                      '#nav-tab-datacenter').classList.contains('active'),
                    // Whether the nav actually RECEIVES clicks. While Data Center was
                    // painted over the top of the screen, the tabs were not merely
                    // hidden — the overlay swallowed every click on them, so the only
                    // way out was the page's own Back button. `elementFromPoint` at the
                    // tab's centre is the honest test, and unlike a real click it
                    // reports the answer instead of retrying for thirty seconds.
                    navTabHittable: (() => {
                      const el = document.getElementById('nav-tab-reports');
                      if (!el) return false;
                      const b = el.getBoundingClientRect();
                      const hit = document.elementFromPoint(
                        b.left + b.width / 2, b.top + b.height / 2);
                      return !!(hit && hit.closest('.nav'));
                    })(),
                    // The two things the reader sees first and calls "not lined up":
                    // an empty band between the toolbar and the panes, and the tree's
                    // FILES row sitting somewhere else than the Files tab above it.
                    // Measure the PANES, not `.dc-body`: the inset that caused the gap
                    // was padding on that element, so its own box top stays flush and
                    // would report "no gap" for as long as the gap is there.
                    gapUnderToolbar: (() => {
                      const bar = document.querySelector('#datacenter-page > .rpt-toolbar')
                        .getBoundingClientRect();
                      return ['.dc-fb-tree', '.dc-fb-main', '.dc-fb-preview']
                        .map(s => {
                          const e = document.querySelector(s);
                          return e ? Math.round(
                            e.getBoundingClientRect().top - bar.bottom) : null;
                        });
                    })(),
                    tabIcon: (() => {
                      const el = document.querySelector('#dc-tab-upload i');
                      return el ? Math.round(el.getBoundingClientRect().left) : null;
                    })(),
                    filesIcon: (() => {
                      const el = document.querySelector('.dc-fb-tree-root i');
                      return el ? Math.round(el.getBoundingClientRect().left) : null;
                    })(),
                    // The three pane headers are one band, not three: the tree's
                    // "Files", the list's column labels and the preview's file name were
                    // 31 / 27 / 40px tall and 11 / 10 / 13px of text, and the first of
                    // them also sat 6px lower than the other two. Same height, same top
                    // line, same text size — otherwise the browser reads as three
                    // panels stacked at random offsets.
                    heads: (() => {
                      const spec = [['.dc-fb-tree-root', '.dc-fb-tree-root span'],
                                    ['#dc-fb-list th', null],
                                    ['.dc-fb-preview-bar', '.dc-fb-pv-name']];
                      return spec.map(([sel, textSel]) => {
                        const e = document.querySelector(sel);
                        if (!e) return {sel, missing: true};
                        const t = document.querySelector(textSel || sel);
                        const ts = t ? getComputedStyle(t) : getComputedStyle(e);
                        const r = e.getBoundingClientRect();
                        return {sel, h: Math.round(r.height), t: Math.round(r.top),
                                fs: ts.fontSize, fw: ts.fontWeight};
                      });
                    })(),
                  };
                }""")
                check(f"[{theme}] the global nav stays on screen",
                      shell["navVisible"], "nav is hidden or has no height")
                check(f"[{theme}] the page starts below the nav, not over it",
                      abs(shell["navBottom"] - shell["dcTop"]) <= 1,
                      f"nav ends at {shell['navBottom']}, page starts at {shell['dcTop']}")
                check(f"[{theme}] the nav tabs are clickable, not covered",
                      shell["navTabHittable"],
                      "something is painted over the navigation")
                check(f"[{theme}] the panes start on the toolbar's line",
                      all(g == 0 for g in shell["gapUnderToolbar"]),
                      f"{shell['gapUnderToolbar']}px between the toolbar and tree/list/preview")
                check(f"[{theme}] FILES lines up with the Files tab",
                      shell["filesIcon"] == shell["tabIcon"],
                      f"FILES icon at {shell['filesIcon']}, tab icon at {shell['tabIcon']}")

                heads = shell["heads"]
                check(f"[{theme}] all three pane headers are the same height",
                      len({h["h"] for h in heads}) == 1,
                      f"heights={[h.get('h') for h in heads]}")
                check(f"[{theme}] all three pane headers start on one line",
                      len({h["t"] for h in heads}) == 1,
                      f"tops={[h.get('t') for h in heads]}")
                check(f"[{theme}] all three pane headers use one text size",
                      len({h["fs"] for h in heads}) == 1,
                      f"sizes={[h.get('fs') for h in heads]}")
                check(f"[{theme}] the nav knows which page you are on",
                      shell["navActive"], "Data Center tab is not highlighted")
                check(f"[{theme}] there is one page toolbar, not two",
                      shell["toolbars"] == 1, f"{shell['toolbars']} toolbars")
                check(f"[{theme}] only the active section's controls are shown",
                      shell["groups"]["files"] != "none"
                      and shell["groups"]["datasets"] == "none",
                      f"groups={shell['groups']}")

                # …and the other way round. The lookup that was broken named the Files
                # group after its section key (`upload`), so switching to Datasets left
                # the Files controls on screen next to the Datasets ones. Only clicking
                # the second tab can catch that.
                page.click("#dc-tab-datasets")
                page.wait_for_timeout(1500)
                ds = page.evaluate("""() => {
                  const g = {};
                  for (const [k, id] of [['files','dc-tools-files'],
                                         ['datasets','dc-tools-datasets']]) {
                    const el = document.getElementById(id);
                    g[k] = el ? getComputedStyle(el).display : null;
                  }
                  return g;
                }""")
                check(f"[{theme}] switching to Datasets swaps the toolbar controls",
                      ds["datasets"] != "none" and ds["files"] == "none",
                      f"groups={ds}")
                page.click("#dc-tab-upload")
                page.wait_for_timeout(1200)

                # Measure a page that is NOT Data Center, then measure this one, and
                # compare. The Data Center used to bring its own top bar and a second
                # one per section, which is why it read as a different product; the
                # only durable guard is to hold its toolbar to the same numbers.
                # Drive the reference page through JS rather than a physical click: the
                # clickability of the nav is asserted above, and letting a covered nav
                # stall a real click for thirty seconds would bury the actual failure.
                page.evaluate("document.getElementById('nav-tab-reports').click()")
                page.wait_for_timeout(1500)
                ref = page.evaluate("""(props) => {
                  const el = document.querySelector('#page-reports > .rpt-toolbar');
                  if (!el) return null;
                  const cs = getComputedStyle(el);
                  const out = {}; props.forEach(p => out[p] = cs[p]);
                  return out;
                }""", TOOLBAR_PROPS)
                page.evaluate("document.getElementById('nav-tab-datacenter').click()")
                page.wait_for_timeout(1500)
                mine = page.evaluate("""(props) => {
                  const el = document.querySelector('#datacenter-page > .rpt-toolbar');
                  if (!el) return null;
                  const cs = getComputedStyle(el);
                  const out = {}; props.forEach(p => out[p] = cs[p]);
                  return out;
                }""", TOOLBAR_PROPS)
                check(f"[{theme}] the toolbar matches the other pages", ref and mine and
                      all(ref[p] == mine[p] for p in TOOLBAR_PROPS),
                      f"reference={ref} datacenter={mine}")

                page.screenshot(path=os.path.join(SHOTS, f"files-list-{theme}.png"))

                if theme == "light":
                    real = [e for e in errors if "401" not in e and "Unauthorized" not in e]
                    check("no uncaught JS errors", not real, "; ".join(real[:2]))
                ctx.close()
            browser.close()
    finally:
        for p in paths:
            try:
                db.delete_folder_by_path(p)
            except Exception:                                       # noqa: BLE001
                pass
        try:
            oss_storage.remove_object(db.RAW_BUCKET, file_key)
        except Exception:                                           # noqa: BLE001
            pass
        from core.db import connect_main
        with connect_main() as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM _file_library WHERE object_name = %s", (file_key,))
            c.commit()
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE id = %s", (uid,))
            conn.commit()

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    print(f"screenshots: {SHOTS}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
