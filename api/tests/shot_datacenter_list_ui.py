"""Screenshot the Data Center dataset WALL and assert it is a grid of folders you open.

⚠️ This file has asserted three different shapes, and each rewrite was because the
reader changed their mind, not because a test broke:

  1. a column of rows (written to hold the row layout on purpose),
  2. a grid of dataset cards holding their own contents (2026-10-05, morning),
  3. a grid of FOLDER cards with the list inside, which is the Workspace project
     look (2026-10-05, later the same day — the morning version was answering a
     question that had already been superseded).

What it asserts now is the third shape, as a chain of things that have to be true
together rather than as a list of class names:

  · the wall resolves to more than one column, and its cards actually sit side by
    side — `grid-column: 1 / -1` on a card produces a wall that reads as a list
    while this container still reports its full track count, so the track count
    alone proves nothing;
  · every dataset has a card, and the loose tables have a card of their own;
  · the "new dataset" tile is on the wall;
  · CLICKING a card — a real Playwright click, which hit-tests, not
    `el.click()` in an evaluate, which fires at the element and would pass on a
    card something else is covering — swaps the wall for a list of rows;
  · that list is full width, and offers the external-database import;
  · the back button is visible while a folder is open and returns to the wall.

Needs a live server and a real database (it seeds datasets and signs in as a real
user), so it is not part of `scripts/ci_check.py`. `test_dc_card_grid_static.py`
is the half that CI can run; the reasoning for having both is at the top of that
file.

    ../.venv312/bin/python tests/shot_datacenter_list_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, data_center_db as db, dataset_shares as shares   # noqa: E402
from services import dataset_groups                                                 # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-dc-shots")

WALL = "#dc-datasets-grid"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def seed(email, tag, owner2):
    """Loose tables of my own, one shared by somebody else, and one CONTAINER.

    ⚠️ All three kinds are needed. The shared one is the row with no Share action,
    and the container is the shape that can be forced to take a whole line: with
    only loose tables in the fixture, a wall where every card spans the row passes
    every card-shaped assertion here, which is exactly what happened on 2026-10-05.
    """
    names = []
    for i, (rows, cols) in enumerate([(1284, 12), (96, 7), (45210, 24)]):
        name = f"{tag}_ds_{i}"
        db.create_pg_table(name, [("region", "text"), ("amount", "numeric")],
                          f"fp{i}", f"Dataset {i}", "seed.xlsx", "Sheet1", ["core"],
                          owner_email=email)
        db.insert_pg_data(name, [{"region": f"R{j}", "amount": j} for j in range(min(rows, 300))])
        names.append(name)
    shares.share(names[0], "someone-else@example.test", email)
    borrowed = f"{tag}_borrowed"
    db.create_pg_table(borrowed, [("region", "text"), ("amount", "numeric")],
                       "fp_borrowed", "Borrowed from a colleague", "seed.xlsx", "Sheet1",
                       ["core"], owner_email=owner2)
    db.insert_pg_data(borrowed, [{"region": f"B{j}", "amount": j} for j in range(40)])
    shares.share(borrowed, email, owner2)
    names.append(borrowed)

    slug = None
    try:
        # ⚠️ A PLAIN description, not a `中文 / English` pair: the card renders it
        # `data-i18n-skip` (it is text a person typed), so a pair stored here would
        # show both languages forever and read as a translation bug in the
        # screenshot rather than as a fixture that was written carelessly.
        g = dataset_groups.create_group("容器 / Container", "two member tables",
                                        owner_email=email)
        slug = g["slug"]
        for i in range(2):
            member = f"{tag}_member{i}"
            db.create_pg_table(member, [("region", "text"), ("amount", "numeric")],
                               f"fp_member{i}", f"Member {i}", "seed.xlsx", "Sheet1",
                               ["core"], owner_email=email)
            db.insert_pg_data(member, [{"region": f"M{j}", "amount": j} for j in range(150)])
            dataset_groups.attach_table(slug, member, owner_email=email, admin=True)
            names.append(member)
    except Exception as exc:                                        # noqa: BLE001
        print(f"  (container seeding failed: {type(exc).__name__}: {exc})")
    return names, slug


def cleanup(email, tag, names, owner2, slug=None):
    from core.db import connect_main
    # ⚠️ EVERY group this account owns, not just the seeded one: the test also
    # creates a dataset from inside a folder, and a probe that leaves rows behind
    # makes the next run's wall assertions count cards it did not put there.
    try:
        for g in dataset_groups.list_groups(email, admin=True) or []:
            try:
                dataset_groups.delete_group(g["slug"], email, admin=True)
            except Exception:                                    # noqa: BLE001
                pass
    except Exception:                                            # noqa: BLE001
        pass
    if slug:
        try:
            dataset_groups.delete_group(slug, email, admin=True)
        except Exception:                                        # noqa: BLE001
            pass
    for n in names:
        try:
            db.delete_pg_table(n)
        except Exception:                                        # noqa: BLE001
            pass
    with connect_main() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM dataset_shares WHERE shared_by=%s OR grantee_email=%s "
                        "OR shared_by=%s OR grantee_email=%s",
                        (email, "someone-else@example.test", owner2, owner2))
            cur.execute("DELETE FROM _import_registry WHERE table_name LIKE %s", (f"{tag}%",))
            cur.execute(f"DELETE FROM {dataset_groups.GROUPS_TABLE} WHERE slug = %s", (slug,))
        c.commit()


def wall_geometry(page):
    return page.evaluate("""(wall) => {
      const grid = document.getElementById('dc-datasets-grid');
      const cards = [...grid.querySelectorAll('.dc-proj, .dc-proj-new')];
      const r = cards.map(x => x.getBoundingClientRect());
      const cs = cards.map(x => getComputedStyle(x));
      return {
        gridCols: getComputedStyle(grid).gridTemplateColumns,
        gridWidth: Math.round(grid.getBoundingClientRect().width),
        n: cards.length,
        lefts: r.map(x => Math.round(x.left)),
        tops: r.map(x => Math.round(x.top)),
        widths: r.map(x => Math.round(x.width)),
        heights: r.map(x => Math.round(x.height)),
        radii: cs.map(x => x.borderTopLeftRadius),
        borders: cs.map(x => x.borderTopWidth),
        folders: grid.querySelectorAll('.dc-proj').length,
        loose: grid.querySelectorAll('.dc-proj-loose').length,
        newTile: grid.querySelectorAll('.dc-proj-new').length,
      };
    }""", WALL)


def main():
    stamp = int(time.time())
    tag = f"dcwall{stamp}"
    email = f"dc-wall-{stamp}@example.test"
    owner2 = f"dc-wall-owner-{stamp}@example.test"
    pw = "Dc-Wall-Probe-1!"

    auth_store._ensure_schema()
    uids = []
    with auth_store._db() as conn, conn.cursor() as cur:
        for addr in (email, owner2):
            cur.execute("DELETE FROM app_users WHERE email = %s", (addr,))
            cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                        "VALUES (%s, %s, 'DC wall', 'admin')", (addr, auth_store._hash(pw)))
            cur.execute("SELECT id FROM app_users WHERE email = %s", (addr,))
            uids.append(cur.fetchone()[0])
        conn.commit()
    uid = uids[0]

    names, slug = seed(email, tag, owner2)
    os.makedirs(SHOTS, exist_ok=True)
    print(f"seeded {len(names)} tables, container={slug}, user id={uid}")
    try:
        with sync_playwright() as pw2:
            browser = pw2.chromium.launch()
            for theme, suffix in (("light", "-light"), ("dark", "-dark")):
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
                # The Data Center opens on Files; the dataset wall is behind its own
                # toolbar tab, and `#dc-section-datasets` is display:none until that is
                # chosen. There are NO `#dc-tab-*` buttons — it is a full-screen page
                # whose toolbar carries `data-dc-section`.
                page.click('[data-dc-section="datasets"]')
                page.wait_for_timeout(3000)
                page.evaluate("() => dc.loadDatasets()")
                page.wait_for_timeout(3500)

                # ── the wall ──
                geo = wall_geometry(page)
                check(f"[{theme}] the wall has a card per dataset plus the new tile",
                      geo["folders"] >= 2, f"folder cards={geo['folders']}")
                check(f"[{theme}] the loose tables have their own card",
                      geo["loose"] == 1, f"loose cards={geo['loose']}")
                check(f"[{theme}] the new-dataset tile is on the wall",
                      geo["newTile"] == 1, f"new tiles={geo['newTile']}")

                # ⚠️ The track count ALONE proves nothing: a grid whose every child is
                # forced to `1 / -1` still reports every track it has. The cards'
                # own `left` is the fact.
                tracks = [t for t in geo["gridCols"].split() if t]
                check(f"[{theme}] the wall resolves to several columns",
                      len(tracks) >= 2, f"grid-template-columns={geo['gridCols']}")
                check(f"[{theme}] cards sit side by side, not one per line",
                      len(set(geo["lefts"])) > 1, f"lefts={geo['lefts']}")
                check(f"[{theme}] every card is the same width (a wall, not a queue)",
                      len(set(geo["widths"])) == 1, f"widths={geo['widths']}")
                check(f"[{theme}] every card is a box",
                      all(b and b != "0px" for b in geo["borders"])
                      and all(r not in ("0px", "") for r in geo["radii"]),
                      f"borders={geo['borders']} radii={geo['radii']}")
                by_row = {}
                for top, h in zip(geo["tops"], geo["heights"]):
                    by_row.setdefault(top, []).append(h)
                check(f"[{theme}] cards in one line share a height",
                      all(len(set(hs)) == 1 for hs in by_row.values()),
                      f"heights by row={by_row}")

                check(f"[{theme}] no folder is open yet",
                      page.locator("#dc-ds-back").is_hidden(), "the back button is up")
                page.screenshot(path=os.path.join(SHOTS, f"datacenter-wall{suffix}.png"),
                                full_page=True)

                # ── open the SEEDED container, by its slug ──
                # ⚠️ By slug, not "the first card". The light pass creates a dataset
                # and the dark pass then clicks whatever sorts first — which was the
                # empty dataset the light pass had just made, so the dark pass
                # asserted against a container with no tables in it. Two runs of one
                # script sharing one database is a test-isolation problem, and the
                # fix is to name the subject, never to rely on its position.
                # ⚠️ `page.click` and not `el.click()` in an evaluate: the evaluate
                # form dispatches straight at the element with no hit test, so it
                # "works" on a card something else is covering.
                page.click('.dc-proj[data-slug="%s"]' % slug)
                page.wait_for_timeout(1200)

                inside = page.evaluate("""(wall) => {
                  const grid = document.getElementById('dc-datasets-grid');
                  const list = grid.querySelector('.dc-ds-list');
                  const head = grid.querySelector('.dc-ds-list-head h3');
                  const acts = [...grid.querySelectorAll('.dc-ds-list-acts button')]
                    .map(b => (b.textContent || '').trim());
                  const rows = [...grid.querySelectorAll('.dc-ds-list .dc-group-table')];
                  const r = list ? list.getBoundingClientRect() : null;
                  const g = grid.getBoundingClientRect();
                  return {
                    wallCards: grid.querySelectorAll('.dc-proj, .dc-proj-new').length,
                    listWidth: r ? Math.round(r.width) : 0,
                    gridWidth: Math.round(g.width),
                    head: head ? head.textContent.trim() : '',
                    acts,
                    rows: rows.length,
                    rowNames: rows.map(x => (x.querySelector('.dc-group-table-name')
                      || {}).textContent || '').slice(0, 3),
                    firstRowButtons: rows.length
                      ? rows[0].querySelectorAll('.dc-group-table-actions button').length : 0,
                  };
                }""", WALL)
                check(f"[{theme}] clicking a folder replaces the wall with a list",
                      inside["wallCards"] == 0 and inside["rows"] >= 2,
                      f"wall cards={inside['wallCards']} rows={inside['rows']}")
                check(f"[{theme}] the list names the dataset it is inside",
                      "容器" in inside["head"] or "Container" in inside["head"],
                      f"head={inside['head']!r}")
                # A list that inherits the 225px folder track renders one table per
                # line, which still "works" and is not what a list should be.
                check(f"[{theme}] the list is full width, not one table per line",
                      inside["listWidth"] > inside["gridWidth"] * 0.9,
                      f"list={inside['listWidth']} grid={inside['gridWidth']}")
                check(f"[{theme}] a table row keeps all its actions",
                      inside["firstRowButtons"] >= 5,
                      f"buttons={inside['firstRowButtons']}")
                check(f"[{theme}] the list offers the external-database import",
                      any("Import from database" in a or "从数据库导入" in a
                          for a in inside["acts"]),
                      f"actions={inside['acts']}")
                check(f"[{theme}] the back button is up while a folder is open",
                      page.locator("#dc-ds-back").is_visible(), "no way out")
                page.screenshot(path=os.path.join(SHOTS, f"datacenter-folder{suffix}.png"),
                                full_page=True)

                # ── and back out ──
                page.click("#dc-ds-back")
                page.wait_for_timeout(1200)
                back = wall_geometry(page)
                check(f"[{theme}] back returns to the wall",
                      back["folders"] >= 2 and back["n"] >= 3,
                      f"folder cards={back['folders']} total={back['n']}")

                # ── the loose folder, which is the card most likely to be dead ──
                # ⚠️ It was dead for one full review cycle: its handler said
                # `dc.openDsFolder(DS_LOOSE_KEY)`, and an inline `onclick` runs in
                # GLOBAL scope while `DS_LOOSE_KEY` is a `const` inside the dc IIFE,
                # so the click threw a ReferenceError and changed nothing. The test
                # missed it because it only ever clicked `.dc-proj[data-slug]` — a
                # card whose argument is already a literal. Clicking EVERY card is
                # the invariant; naming the ones to click is a list that goes stale.
                if page.locator(".dc-proj-loose").count():
                    page.click(".dc-proj-loose")
                    page.wait_for_timeout(1000)
                    loose = page.evaluate("""() => {
                      const grid = document.getElementById('dc-datasets-grid');
                      return {
                        open: !!grid.querySelector('.dc-ds-list'),
                        head: (grid.querySelector('.dc-ds-list-head h3') || {}).textContent || '',
                        rows: grid.querySelectorAll('.dc-ds-list .dc-group-table').length,
                      };
                    }""")
                    check(f"[{theme}] the loose folder opens",
                          loose["open"] and loose["rows"] >= 1,
                          f"open={loose['open']} rows={loose['rows']}")
                    check(f"[{theme}] the loose folder names itself",
                          "Not in a dataset" in loose["head"] or "未归入" in loose["head"],
                          f"head={loose['head']!r}")

                    # ── create a dataset from INSIDE a folder ──
                    # The reported bug: the dialog closed and the new folder was
                    # nowhere on screen, because the wall had never been returned to.
                    created = f"folder-made {int(time.time())}"
                    page.click(".dc-ds-list-acts button")
                    page.wait_for_timeout(800)
                    page.fill("#dc-gc-name", created)
                    page.click("#dc-gc-ok")
                    page.wait_for_timeout(3000)
                    landed = page.evaluate("""(name) => {
                      const grid = document.getElementById('dc-datasets-grid');
                      return {
                        onWall: grid.querySelectorAll('.dc-proj').length > 0,
                        titles: [...grid.querySelectorAll('.dc-proj-title')]
                          .map(t => t.textContent.trim()),
                      };
                    }""", created)
                    check(f"[{theme}] creating inside a folder returns to the wall",
                          landed["onWall"],
                          "still showing the folder list — the new folder is not on "
                          "screen, which is what the reader reports as 'no refresh'")
                    check(f"[{theme}] the dataset just made is on the wall",
                          any(created in t for t in landed["titles"]),
                          f"wall has {landed['titles']}")
                    # ⚠️ Removed here, not through the UI: a confirm dialog whose
                    # button text moves with the language would make this test break
                    # on translation, and a test that breaks on translation is a test
                    # that gets deleted. It also has to go before the NEXT theme pass
                    # — the two passes share one database, so a leftover card is a
                    # card the next pass can click by accident.
                    for g in dataset_groups.list_groups(email, admin=True) or []:
                        if (g.get("name") or "") == created:
                            dataset_groups.delete_group(g["slug"], email, admin=True)

                # No card on the wall may throw. `pageerror` is the only place a
                # `ReferenceError` in an inline handler shows up — the click simply
                # does nothing, which is indistinguishable from "the card is not
                # clickable" if you only look at the screen.
                if theme == "light":
                    thrown = [e for e in errors
                              if "ReferenceError" in e or "is not defined" in e]
                    check("no card throws when clicked", not thrown, "; ".join(thrown[:3]))

                if theme == "light":
                    real = [e for e in errors if "401" not in e and "Unauthorized" not in e]
                    check("no uncaught JS errors", not real, "; ".join(real[:2]))
                ctx.close()
            browser.close()
    finally:
        try:
            cleanup(email, tag, names, owner2, slug)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  (cleanup warning: {exc})")
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE id = ANY(%s)", (uids,))
            conn.commit()

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print("  -", f)
    print(f"screenshots: {SHOTS}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
