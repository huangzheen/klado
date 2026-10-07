"""Data Center: the Files WALL must refresh what it is showing, and every way of
opening a folder out of Files must actually open that folder.

⚠️ Two bugs, one page. The second was only findable because the first was being
fixed here, which is worth stating because it is the reason this file is
narrower than the feature it covers.

1. REFRESH — the reported one. Creating a folder from the wall's own "New
   folder" tile closed the dialog and changed nothing on screen. The handler
   reloaded the list INSIDE a folder (`fbLoad()` writes `#dc-body`), and from the
   wall that element is hidden: the folder was really made and the wall still
   showed the old set. Nine handlers each carried that one-liner; they now share
   `fbRefreshVisible()`. What matters here is not that the helper exists but that
   it repaints the WALL, because a test that only refreshes a folder passes
   while the wall is broken.

2. OPEN — found while auditing (1), and pre-existing. Every folder card on the
   wall, every subfolder row in a folder, and every breadcrumb crumb called
   `dc.openDsFolder(...)`, which is the DATASET grid's opener: it sets `_dsOpen`
   and re-renders `#dc-datasets-grid`, an element inside `#dc-section-datasets`
   — hidden for as long as you are on Files. So clicking a folder did nothing
   visible at all. Three facts agree it was never intended: the module's own
   export comment says the public names are `dc.openFolder(...)`; the export
   block warns that `openFolder` is taken by the FILES view; and the subfolder
   row's `onclick` called `openDsFolder` while the SAME row's `onkeydown`
   (Enter/Space) called `openFolder` — the keyboard path worked and the mouse
   path did not.

⚠️ How both are judged, because the alternative passes on broken code:
  · `page.click`, which hit-tests. `el.click()` inside an evaluate dispatches at
    the element with no hit test, so a card fully covered by something else
    still "clicks" fine.
  · `surface()` asks what is ON SCREEN — wall vs body vs crumbs — rather than
    what the DOM contains. A hidden element being present is not a page.
  · `pageerror` must stay empty. "The handler ran and threw" and "the handler
    never ran" look identical on screen; an uncaught ReferenceError is the only
    way out tells them apart.
  · Nothing is located by Chinese text. Headless Chromium is an English
    interface, so a pair's left half is not on screen.
  · The Root card is found POSITIONALLY (the code pushes it first, always) and by
    its icon class, never by its bilingual label.

Needs a live server and the object store, so it is not in
`scripts/ci_check.py`. `test_dc_card_grid_static.py` is the half CI can run.

    ../.venv312/bin/python tests/verify_dc_wall_refresh_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store                                        # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-dc-wall-shots")

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


def make_user(email, pw):
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute(
            "INSERT INTO app_users (email, password_hash, display_name, role) "
            "VALUES (%s, %s, 'DC wall probe', 'admin')", (email, auth_store._hash(pw)))
        conn.commit()


def drop_user(email):
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        conn.commit()


def surface(page):
    """What the reader is actually looking at inside the Files section.

    ⚠️ Visibility is measured with the element's own box, not `hidden` alone: a
    hidden subtree can still report a height while a parent section is closed,
    and `#dc-wall` is exactly the element whose `hidden` flag lies (it lives
    INSIDE `#dc-section-upload`, so it is un-hidden on the Datasets tab too).
    """
    return page.evaluate("""() => {
      const box = (id) => {
        const e = document.getElementById(id);
        if (!e) return false;
        if (e.hidden) return false;
        if (getComputedStyle(e).display === 'none') return false;
        return e.getBoundingClientRect().height > 0;
      };
      const txt = (sel) => [...document.querySelectorAll(sel)]
        .map(e => e.textContent.trim()).filter(Boolean);
      return {
        wall: box('dc-wall'),
        body: box('dc-body'),
        crumbs: box('dc-crumbs'),
        crumbTexts: txt('#dc-crumbs .dc-crumb'),
        wallTitles: txt('#dc-wall .dc-folder-title'),
        rootIcons: document.querySelectorAll('#dc-wall .dc-folder i.ri-hard-drive-2-line').length,
        newTiles: document.querySelectorAll('#dc-wall .dc-folder-new').length,
        folderRows: txt('#dc-fb-list .dc-fb-folder-row .dc-fb-name'),
        listRows: document.querySelectorAll('#dc-fb-list .dc-fb-row').length,
        emptyState: !!document.querySelector('#dc-fb-list .dc-fb-empty'),
      };
    }""")


def mkdir(page, name):
    """The tile / toolbar button, the dialog, the confirm button.

    ⚠️ Every selector here is structural. The dialog's own words are plain
    English with no `中文 / English` pair, so they survive a language change, but
    there is no reason to depend on them when a class will do.
    """
    page.fill("#dc-fb-mkdir-input", name)
    page.click("#dc-fb-mkdir-modal .dc-btn-primary")
    page.wait_for_timeout(2200)


def error_toasts(page):
    """What the failed handlers said.

    ⚠️ This is not decoration. `fbConfirmMkdir` wraps its whole body in
    `catch (e) { toast('Failed: ' + e.message) }`, so a handler that throws shows
    the user a 3-second red toast and an unchanged screen — the one shape of
    broken that looks exactly like "nothing happened". Asserting the screen alone
    cannot tell "it failed loudly" from "it was never wired up", and that is how a
    `ReferenceError` on a line ABOVE the refresh went unnoticed: the mkdir was
    really created, the refresh below it never ran, and the toast expired before
    anyone looked.
    """
    return page.evaluate("""() => [...document.querySelectorAll('.dc-toast.error')]
      .map(e => e.textContent.trim())""")


def mkdir_modal_open(page):
    return page.evaluate(
        "() => document.getElementById('dc-fb-mkdir-modal').classList.contains('open')")


def goto_wall(page):
    """Back to the Files wall from anywhere, through the control a reader uses.

    ⚠️ Only click when there IS a breadcrumb to click. On a red run the folder
    never opened, so the crumbs were never drawn, and an unconditional click here
    turns one real failure into a 30-second Playwright timeout that hides every
    assertion after it. A test that reports the first true thing and stops is
    worth more than one that dies on a symptom of the first true thing.
    """
    if page.locator("#dc-crumbs button.dc-crumb").count():
        page.click("#dc-crumbs button.dc-crumb")        # "Data" — calls dc.showWall()
        page.wait_for_timeout(1200)


def delete_folders(page, paths):
    page.evaluate("""async (paths) => {
      for (const p of paths) {
        await fetch('/api/data-center/files/by-path?path=' + encodeURIComponent(p)
                    + '&is_folder=true', {method: 'DELETE'});
      }
    }""", list(paths))


def main():
    stamp = int(time.time())
    tag = f"dcwallfix{stamp}"
    email = f"{tag}@example.test"
    pw = "Dc-Wall-Probe-1!"
    parent = f"{tag}-A"
    child = f"{tag}-B"

    make_user(email, pw)
    os.makedirs(SHOTS, exist_ok=True)
    print(f"probe user {email}")

    try:
        with sync_playwright() as pw2:
            browser = pw2.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#auth-login-email", timeout=15000)
            page.fill("#auth-login-email", email)
            page.fill("#auth-login-password", pw)
            page.click("#auth-login-btn")
            page.wait_for_timeout(3500)
            page.click("#nav-tab-datacenter")
            page.wait_for_timeout(4000)

            # ── Files opens on the wall ──
            s = surface(page)
            check("Data Center opens on the Files wall", s["wall"] and not s["body"],
                  f"wall={s['wall']} body={s['body']}")
            check("the wall draws the Root card first, by its icon",
                  s["rootIcons"] == 1, f"root icons={s['rootIcons']}")
            check("the wall has a New folder tile", s["newTiles"] == 1,
                  f"tiles={s['newTiles']}")
            before = list(s["wallTitles"])
            print(f"  wall before: {before}")

            # ── 1. create from the wall; the wall must show it ──
            n_err = len(errors)
            page.click("#dc-wall .dc-folder-new")
            page.wait_for_timeout(500)
            check("the New folder tile opens the dialog", mkdir_modal_open(page),
                  "no .open on #dc-fb-mkdir-modal")
            mkdir(page, parent)
            check("the dialog closes after the folder is made", not mkdir_modal_open(page),
                  "still .open")

            s = surface(page)
            check("the new folder is ON THE WALL, not only in the folder view",
                  parent in s["wallTitles"], f"wall={s['wallTitles']}")
            check("the wall still shows the wall, not a folder list",
                  s["wall"] and not s["body"], f"wall={s['wall']} body={s['body']}")
            check("creating a folder threw nothing",
                  len(errors) == n_err, f"errors={errors[n_err:]}")
            check("creating a folder showed no error toast", not error_toasts(page),
                  f"toasts={error_toasts(page)}")
            page.screenshot(path=os.path.join(SHOTS, "wall-after-create.png"), full_page=True)

            # ── 2. opening that card must open the folder ──
            n_err = len(errors)
            page.locator("#dc-wall .dc-folder", has_text=parent).first.click()
            page.wait_for_timeout(2200)
            s = surface(page)
            check("clicking a folder card OPENS it (wall gives way to the list)",
                  s["body"] and not s["wall"], f"wall={s['wall']} body={s['body']}")
            check("the breadcrumb says which folder is open",
                  s["crumbs"] and s["crumbTexts"][-1] == parent, f"crumbs={s['crumbTexts']}")
            check("a folder just made is empty, and says so",
                  s["emptyState"] or s["listRows"] == 0,
                  f"rows={s['listRows']} empty={s['emptyState']}")
            check("opening a folder threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            page.screenshot(path=os.path.join(SHOTS, "folder-open.png"), full_page=True)

            # ── 3. the Root card ──
            n_err = len(errors)
            goto_wall(page)
            s = surface(page)
            check("the breadcrumb's first crumb returns to the wall",
                  s["wall"] and not s["body"], f"wall={s['wall']} body={s['body']}")
            page.locator("#dc-wall .dc-folder").first.click()
            page.wait_for_timeout(2200)
            s = surface(page)
            check("the Root card opens the root",
                  s["body"] and not s["wall"], f"wall={s['wall']} body={s['body']}")
            check("at the root the breadcrumb is just Data, with no folder after it",
                  len(s["crumbTexts"]) == 1, f"crumbs={s['crumbTexts']}")
            check("opening Root threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")

            # ── 4. a subfolder row inside a folder ──
            n_err = len(errors)
            goto_wall(page)
            page.locator("#dc-wall .dc-folder", has_text=parent).first.click()
            page.wait_for_timeout(2000)
            page.click("#dc-tools-files button[onclick='dc.fbMkdirCurrent()']")
            page.wait_for_timeout(500)
            mkdir(page, child)
            check("making a subfolder threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            check("making a subfolder showed no error toast", not error_toasts(page),
                  f"toasts={error_toasts(page)}")
            s = surface(page)
            check("a folder made INSIDE a folder appears in that folder's list",
                  child in s["folderRows"], f"rows={s['folderRows']}")
            check("still inside the folder afterwards (the view did not jump)",
                  s["body"] and s["crumbTexts"][-1] == parent, f"crumbs={s['crumbTexts']}")

            page.locator("#dc-fb-list .dc-fb-folder-row", has_text=child).first.click()
            page.wait_for_timeout(2200)
            s = surface(page)
            # ⚠️ The crumb is the SEGMENT, not the path. `fbRenderBreadcrumb` splits
            # the prefix on '/' and draws one crumb per part, so the last crumb of
            # `parent/child` is `child`. Asserting the joined path here fails on a
            # navigation that worked perfectly.
            check("clicking a subfolder row opens THAT subfolder",
                  s["body"] and s["crumbTexts"][-1] == child and len(s["crumbTexts"]) == 3,
                  f"crumbs={s['crumbTexts']}")
            check("navigating into a subfolder threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            page.screenshot(path=os.path.join(SHOTS, "subfolder-open.png"), full_page=True)

            # ── 5. a breadcrumb crumb goes back up ──
            n_err = len(errors)
            page.locator("#dc-crumbs .dc-crumb", has_text=parent).first.click()
            page.wait_for_timeout(2200)
            s = surface(page)
            check("a breadcrumb crumb walks back up one level",
                  s["body"] and s["crumbTexts"][-1] == parent
                  and len(s["crumbTexts"]) == 2,
                  f"crumbs={s['crumbTexts']}")
            # ⚠️ What proves we are in the PARENT is the crumb DEPTH plus the child
            # being listed again — not the child being absent, which is what this
            # assertion first said. One level up from the child IS the parent, and
            # the parent's contents include the child; a test that "proves" the
            # opposite is asserting a bug.
            check("we are looking at the parent's contents again",
                  child in s["folderRows"], f"rows={s['folderRows']}")
            check("the breadcrumb threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")

            check("no uncaught error for the whole run", not errors, f"errors={errors}")

            # ⚠️ The DB user goes first; the folders need the page's session, so they
            # go while it is still alive.
            delete_folders(page, [f"{parent}/{child}", parent])
            ctx.close()
            browser.close()
    finally:
        drop_user(email)
        print(f"cleaned up {email}")

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())