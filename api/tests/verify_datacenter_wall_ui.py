"""Drive the new Data Center in a real browser: wall → folder → table.

The static tests in `test_datacenter_wall.py` check the file. They cannot check the
page. Every assertion below is one where "the markup is right" and "the page works"
come apart, and each has a specific way of coming apart:

**`hidden` that does not hide.** `.dc-fb-body` is a flex row, so `display: flex`
beats the browser's `[hidden] { display: none }` default. If the `[hidden]` rule is
missing, `showWall()` runs, sets the attribute, throws no error, and the directory
stays exactly where it was — on top of the cards. Every element is in the DOM, every
computed style is "correct", and the reader sees two pages at once. Nothing short of
`elementFromPoint` catches this, which is why the "what is actually on screen" checks
below ask about bounding boxes rather than about the `hidden` property.

**A card that is a `<div>`.** The cards are `<button>`s, so they are focusable and
Enter works — that is asserted, because "looks like a card" and "is a button" are
different claims and only one of them is in the CSS.

**A dataset filed in the wrong folder, or in none.** The rows are grouped by
`source_file_path`, and the count on a card comes from the same key. Asserted by
clicking a card whose count says N and finding N rows, because the counts and the
grouping are two different code paths and they can disagree.

**A table that never arrives.** The right pane is filled by a fetch. A page that
renders both panes and leaves the right one empty is the exact failure a static
assertion waves through, so the pane is checked for actual `<td>` content.

Both themes, because every new rule is token-based and a rule that only works in one
of them is invisible in the other until someone reads the page in it.

    ../.venv312/bin/python tests/verify_datacenter_wall_ui.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, data_center_db as db                 # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-dc-wall-shots")

PASS = 0
FAIL = 0
FAILURES = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def make_user(email, pw):
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, 'DC wall', 'admin')", (email, auth_store._hash(pw)))
        conn.commit()


def seed_datasets(email, tag):
    """Two tables, both filed under one source file path.

    ⚠️ `source_file_path` is what the client groups by, and `create_pg_table` takes
    `source_file` (a display name) — so the path is set afterwards. Without this the
    fixture produces datasets with no folder, the folder list comes up empty, and the
    test passes by asserting on an empty page.
    """
    made = []
    for i in range(2):
        name = f"{tag}_ds_{i}"
        db.create_pg_table(name, [("region", "text"), ("amount", "numeric")],
                           f"{tag}fp{i}", f"销售数据 {i}", "seed.xlsx", "Sheet1", ["core"],
                           owner_email=email)
        db.insert_pg_data(name, [{"region": f"R{j}", "amount": j} for j in range(25)])
        db.update_source_file_path(name, f"2026-10/seed_{i}.xlsx")
        made.append(name)
    return made


def cleanup(email, tag, names):
    from core.db import connect_main
    for n in names:
        try:
            db.delete_pg_table(n)
        except Exception:                                        # noqa: BLE001
            pass
    with connect_main() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM _import_registry WHERE table_name LIKE %s", (f"{tag}%",))
            cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        c.commit()


def main():
    stamp = int(time.time())
    tag = f"dcwall{stamp}"
    email = f"dc-wall-{stamp}@example.test"
    pw = "Dc-Wall-Probe-1!"

    make_user(email, pw)
    names = seed_datasets(email, tag)
    os.makedirs(SHOTS, exist_ok=True)
    print(f"seeded {len(names)} datasets, user {email}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
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
                page.evaluate("document.documentElement.setAttribute('data-theme','%s')" % theme)
                page.click("#nav-tab-datacenter")
                page.wait_for_timeout(3500)

                # ── 1. the page opens on the wall ──────────────────────────────
                on_wall = page.evaluate("""() => {
                  const w = document.getElementById('dc-wall');
                  const b = document.getElementById('dc-body');
                  if (!w || !b) return {missing: true};
                  return {
                    wallHidden: w.hasAttribute('hidden'),
                    bodyHidden: b.hasAttribute('hidden'),
                    wallDisplay: getComputedStyle(w).display,
                    bodyDisplay: getComputedStyle(b).display,
                    wallTop: Math.round(w.getBoundingClientRect().top),
                    bodyTop: Math.round(b.getBoundingClientRect().top),
                    wallCards: w.querySelectorAll('.dc-folder').length,
                  };
                }""")
                check(f"[{theme}] the wall is on screen", on_wall.get("wallDisplay") != "none",
                      f"wall display={on_wall.get('wallDisplay')}")
                check(f"[{theme}] the directory view is off screen",
                      on_wall.get("bodyDisplay") == "none",
                      f"#dc-body display={on_wall.get('bodyDisplay')} — the two views are "
                      "both up, or the attribute is not beating the flex rule")
                check(f"[{theme}] the wall has cards", on_wall.get("wallCards", 0) >= 1,
                      f"{on_wall.get('wallCards')} cards")

                # The cards are the way in. A card that is a div looks identical and
                # cannot be tabbed to.
                focusable = page.evaluate("""() => {
                  const c = document.querySelector('#dc-wall .dc-folder');
                  if (!c) return null;
                  c.focus();
                  return {tag: c.tagName, focused: document.activeElement === c,
                          tabindex: c.getAttribute('tabindex')};
                }""")
                if focusable:
                    check(f"[{theme}] a folder card is a focusable button",
                          focusable["tag"] == "BUTTON" and focusable["focused"],
                          f"tag={focusable['tag']} focused={focusable['focused']}")

                page.screenshot(path=os.path.join(SHOTS, f"dc-wall-{theme}.png"), full_page=True)

                # ── 2. clicking a card opens the folder ─────────────────────────
                # The ROOT card, deliberately. This fixture files its datasets under
                # a source folder that does not exist on disk, which is the orphan
                # case: a real table whose uploaded file was deleted. Orphans are
                # filed in the root and labelled, so the root is where this fixture's
                # datasets live. Picking "the first card with a count" instead would
                # open somebody else's leftover folder and assert on the wrong page.
                card = page.evaluate("""() => {
                  const c = document.querySelector('#dc-wall .dc-folder');
                  if (!c) return null;
                  const r = c.getBoundingClientRect();
                  return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
                          title: c.querySelector('.dc-folder-title').textContent,
                          sub: c.querySelector('.dc-folder-sub').textContent};
                }""")
                check(f"[{theme}] a card is clickable on screen", card is not None)
                if card:
                    # elementFromPoint: the only proof the thing on screen is the card
                    # and not something covering it.
                    hit = page.evaluate("""([x, y]) => {
                      const el = document.elementFromPoint(x, y);
                      if (!el) return 'null';
                      const card = el.closest('.dc-folder');
                      return card ? 'card' : el.className || el.tagName;
                    }""", [card["x"], card["y"]])
                    check(f"[{theme}] the point on the card hits the card",
                          hit == "card", f"elementFromPoint returned {hit}")
                    page.mouse.click(card["x"], card["y"])
                    page.wait_for_timeout(3000)

                in_folder = page.evaluate("""() => {
                  const w = document.getElementById('dc-wall');
                  const b = document.getElementById('dc-body');
                  const c = document.getElementById('dc-crumbs');
                  return {
                    wallDisplay: w ? getComputedStyle(w).display : 'missing',
                    bodyDisplay: b ? getComputedStyle(b).display : 'missing',
                    crumbsDisplay: c ? getComputedStyle(c).display : 'missing',
                    crumbsText: c ? c.textContent.replace(/\\s+/g,' ').trim() : '',
                    rows: document.querySelectorAll('#dc-fb-list .dc-fb-row').length,
                    tocWidth: Math.round(document.getElementById('dc-toc').getBoundingClientRect().width),
                    paneWidth: Math.round(document.getElementById('dc-fb-preview').getBoundingClientRect().width),
                  };
                }""")
                check(f"[{theme}] the wall is gone once inside a folder",
                      in_folder["wallDisplay"] == "none",
                      f"wall display={in_folder['wallDisplay']}")
                check(f"[{theme}] the directory view is on screen",
                      in_folder["bodyDisplay"] == "flex",
                      f"#dc-body display={in_folder['bodyDisplay']}")
                check(f"[{theme}] the breadcrumb says where you are",
                      bool(in_folder["crumbsText"]), f"crumbs={in_folder['crumbsText']!r}")
                check(f"[{theme}] the folder has rows", in_folder["rows"] >= 1,
                      f"{in_folder['rows']} rows")
                # The two panes have to be side by side. A `flex` that did not apply
                # stacks them, and the table ends up below a full-height list.
                check(f"[{theme}] 目录 and the table are side by side",
                      in_folder["tocWidth"] > 0 and in_folder["paneWidth"] > in_folder["tocWidth"],
                      f"toc={in_folder['tocWidth']} pane={in_folder['paneWidth']}")

                page.screenshot(path=os.path.join(SHOTS, f"dc-folder-{theme}.png"), full_page=True)

                # ── 3. a dataset row shows its table ────────────────────────────
                ds = page.evaluate("""() => {
                  const rows = [...document.querySelectorAll('#dc-fb-list .dc-fb-dataset-row')];
                  if (!rows.length) return null;
                  const r = rows[0].getBoundingClientRect();
                  return {x: Math.round(r.left + 60), y: Math.round(r.top + r.height/2),
                          name: rows[0].querySelector('.dc-fb-name').textContent};
                }""")
                check(f"[{theme}] the folder lists a dataset", ds is not None,
                      "no dataset row — datasets are not being filed into folders")
                if ds:
                    page.mouse.click(ds["x"], ds["y"])
                    page.wait_for_timeout(3000)
                    table = page.evaluate("""() => {
                      const pane = document.getElementById('dc-fb-preview');
                      const t = pane.querySelector('.dc-fb-pv-table');
                      return {
                        selected: document.querySelectorAll('#dc-fb-list .dc-fb-row.selected').length,
                        hasTable: !!t,
                        head: t ? t.querySelectorAll('th').length : 0,
                        cells: t ? t.querySelectorAll('td').length : 0,
                        bar: (pane.querySelector('.dc-fb-pv-name') || {}).textContent || '',
                        tableWidth: t ? Math.round(t.getBoundingClientRect().width) : 0,
                        paneWidth: Math.round(pane.getBoundingClientRect().width),
                      };
                    }""")
                    check(f"[{theme}] the row is marked selected", table["selected"] == 1,
                          f"{table['selected']} selected rows")
                    check(f"[{theme}] the dataset's table is rendered", table["hasTable"],
                          "the right pane has no table")
                    check(f"[{theme}] the table has a header and rows",
                          table["head"] >= 1 and table["cells"] >= 1,
                          f"{table['head']} th, {table['cells']} td")
                    check(f"[{theme}] the table bar names the dataset",
                          table["bar"].strip() != "", f"bar={table['bar']!r}")

                    # ⚠️ The row count, read in the page, in the language the page is
                    # actually in. `25 行 / rows` is a perfectly good bilingual pair
                    # and it renders as "rows" in English — the number is on the half
                    # the splitter throws away. It shipped that way, and every static
                    # check in the file passed, because the string in the source is
                    # correct and only the RENDERED one is wrong. A row that says
                    # "rows" next to a table showing 25 of them reads as a count of
                    # nothing.
                    counts = page.evaluate("""() => {
                      const num = (s) => (s.match(/\\d[\\d,.]*/) || [''])[0];
                      const row = document.querySelector('#dc-fb-list .dc-fb-dataset-row .dc-fb-meta');
                      const bar = document.querySelector('#dc-fb-preview .dc-fb-pv-size');
                      return {row: row ? row.textContent.trim() : null,
                              bar: bar ? bar.textContent.trim() : null,
                              rowsInTable: document.querySelectorAll('.dc-fb-pv-table tbody tr').length};
                    }""")
                    check(f"[{theme}] the row's count has a number in it",
                          bool(counts["row"]) and any(c.isdigit() for c in counts["row"]),
                          f"row count reads {counts['row']!r} — the number is on the "
                          "half of the pair that this language discards")
                    check(f"[{theme}] the table bar's count has a number in it",
                          bool(counts["bar"]) and any(c.isdigit() for c in counts["bar"]),
                          f"table bar reads {counts['bar']!r}")
                    # And it should be the number of rows the table is showing.
                    if counts["row"] and counts["rowsInTable"]:
                        shown = int(re.sub(r"[^\d]", "", counts["row"]) or 0)
                        check(f"[{theme}] the count agrees with the table",
                              shown >= counts["rowsInTable"],
                              f"the row claims {shown} rows, the table draws "
                              f"{counts['rowsInTable']}")

                    # ⚠️ The name is the reason a reader picks this row, so it is the
                    # one thing on it that must not be squeezed. Three chips
                    # ("from 2026-10", "Dataset", "25 rows") on a 284px pane in
                    # English once left the name two characters wide — the list became
                    # a column of badges with the identifying word missing.
                    names = page.evaluate("""() => [...document.querySelectorAll('#dc-fb-list .dc-fb-row')]
                      .map(r => {
                        const n = r.querySelector('.dc-fb-name');
                        if (!n) return null;
                        return {text: n.textContent, shown: Math.round(n.getBoundingClientRect().width),
                                needed: Math.round(n.scrollWidth)};
                      }).filter(Boolean)""")
                    squeezed = [n for n in names if n["needed"] > 0 and n["shown"] < n["needed"] * 0.55]
                    check(f"[{theme}] no row's name is squeezed out by its metadata",
                          not squeezed,
                          "; ".join("%r gets %dpx of %dpx" % (n["text"], n["shown"], n["needed"])
                                    for n in squeezed[:3]))

                    page.screenshot(path=os.path.join(SHOTS, f"dc-table-{theme}.png"),
                                    full_page=True)

                # ── 4. the breadcrumb goes back to the wall ─────────────────────
                page.click("#dc-crumbs .dc-crumb")
                page.wait_for_timeout(2500)
                back = page.evaluate("""() => ({
                  wall: getComputedStyle(document.getElementById('dc-wall')).display,
                  body: getComputedStyle(document.getElementById('dc-body')).display,
                })""")
                check(f"[{theme}] the breadcrumb returns to the wall",
                      back["wall"] != "none" and back["body"] == "none",
                      f"wall={back['wall']} body={back['body']}")

                # ── 5. the search box filters the cards ─────────────────────────
                page.fill("#dc-search", "zzz-no-such-folder")
                page.wait_for_timeout(800)
                filtered = page.evaluate("""() => ({
                  cards: document.querySelectorAll('#dc-wall .dc-folder:not([disabled])').length,
                  empty: !!document.querySelector('#dc-wall .dc-empty'),
                })""")
                check(f"[{theme}] searching for nothing empties the wall",
                      filtered["cards"] == 0 and filtered["empty"],
                      f"cards={filtered['cards']} empty={filtered['empty']}")
                page.fill("#dc-search", "")
                page.wait_for_timeout(800)

                real = [e for e in errors
                        if "401" not in e and "Unauthorized" not in e
                        and "favicon" not in e.lower()]
                check(f"[{theme}] no uncaught page errors", not real, "; ".join(real[:3]))
                ctx.close()
            browser.close()
    finally:
        cleanup(email, tag, names)

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    print(f"screenshots in {SHOTS}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
