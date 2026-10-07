"""Data Center: a wall folder card has a menu, and "Add table" can read a local file.

⚠️ Two user reports, and both are "the affordance is missing", which is the shape
that no assertion in this repo could have found on its own.

1. THE WALL CARD MENU. A card you cannot right-click is not a card with fewer
   options, it reads as a card that does not do anything.

   ⚠️ Only five items, and that is a decision with a reason, not a short list.
   Rename / Move / Publish / download-as-zip are ABSENT because the backend cannot
   do them to a folder: `rename_file` and `move_file` copy one MinIO object key,
   and a "folder" is only a shared key PREFIX with no object of its own — calling
   them returns 200 and moves nothing. So the guard asserts that every item on
   this menu HAS a handler that works, and does not assert the four that would
   be lies. A visible item that does nothing is indistinguishable from a bug.

2. "ADD TABLE" COULD NOT PICK A LOCAL FILE. The `File` control in that dialog is
   a `<select>` of the file LIBRARY, and a dataset's first file is by definition
   not in it yet — the dialog was unfinishable on a fresh account. The fix stages
   the picked file through the same `/files/stage` the Files view uses and then
   selects it in that dropdown, so sheet / name / overwrite are unchanged.

⚠️ Judged by CLICKING (`page.click` / `locator.click(button="right")`, both of
which hit-test), plus `pageerror` empty and no error toast. Screen alone cannot
tell a dead menu item from an absent one.

⚠️ The upload-elsewhere items are the subtle part: from the WALL, `_fbPrefix` is
'' , so an implementation that ignored the chosen folder would upload to the root
and report success. The assertions therefore open the card's menu, pick a file,
and then check the file is inside THAT folder.

Needs a live server and the object store, so it is not in `scripts/ci_check.py`.
`test_dc_ctx_menus.py` is the half CI can run.

    ../.venv312/bin/python tests/verify_dc_ctx_menus_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, dataset_groups                       # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-dc-ctx-shots")

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
            "VALUES (%s, %s, 'DC ctx probe', 'admin')", (email, auth_store._hash(pw)))
        conn.commit()


def drop_user(email):
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        conn.commit()


def menu_open(page, menu_id):
    return page.evaluate(
        "id => { const m = document.getElementById(id);"
        " return !!m && m.style.display !== 'none' && m.getBoundingClientRect().height > 0; }",
        menu_id)


def menu_handlers(page, menu_id):
    return page.evaluate(
        "id => [...document.querySelectorAll('#' + id + ' .dc-fb-ctx-item')]"
        ".map(e => e.getAttribute('onclick'))", menu_id)


def wall_titles(page):
    return page.evaluate("""() => [...document.querySelectorAll('#dc-wall .dc-folder-title')]
      .map(e => e.textContent.trim())""")


def stage_file(page, name, rel, body):
    """Hand `dc.fbUploadFiles` a File the way a picker would."""
    page.evaluate("""async (arg) => {
      const f = new File([arg.body], arg.name, {type: 'text/csv'});
      const dt = new DataTransfer();
      dt.items.add(f);
      await dc.fbUploadFiles(dt.files);
    }""", {"name": name, "rel": rel, "body": body})


def main():
    stamp = int(time.time())
    tag = f"dcctx{stamp}"
    email = f"{tag}@example.test"
    pw = "Dc-Ctx-Probe-1!"
    folder = f"{tag}-folder"
    ds_name = f"{tag}-ds"

    make_user(email, pw)
    g = dataset_groups.create_group(ds_name, "container for the menu probe", owner_email=email)
    slug = g["slug"]
    os.makedirs(SHOTS, exist_ok=True)
    print(f"probe user {email}, dataset {slug}")

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

            # a folder to hang the menu off
            page.evaluate("() => dc.fbMkdirRoot()")
            page.wait_for_timeout(600)
            page.fill("#dc-fb-mkdir-input", folder)
            page.click("#dc-fb-mkdir-modal .dc-btn-primary")
            page.wait_for_timeout(2500)
            check("the fixture folder is on the wall", folder in wall_titles(page),
                  f"wall={wall_titles(page)}")

            # ── 1. the wall card's menu ──
            n_err = len(errors)
            card = page.locator("#dc-wall .dc-folder", has_text=folder).first
            card.click(button="right")
            page.wait_for_timeout(700)
            check("right-clicking a wall folder card opens a menu",
                  menu_open(page, "dc-wall-ctx"), "#dc-wall-ctx is not open")
            title = page.evaluate(
                "() => { const t = document.querySelector('#dc-wall-ctx .dc-ds-ctx-title');"
                " return t ? t.textContent.trim() : null; }")
            check("the menu names the folder it acts on", title == folder,
                  f"title={title!r} folder={folder!r}")
            handlers = menu_handlers(page, "dc-wall-ctx")
            for want in ("dc.wallCtxOpen()", "dc.wallCtxNewDir()", "dc.wallCtxUpload()",
                         "dc.wallCtxUploadFolder()", "dc.wallCtxDelete()"):
                check(f"the menu offers {want}", want in handlers, f"handlers={handlers}")
            # ⚠️ The four the backend cannot do are deliberately absent. Asserting
            # their ABSENCE is what stops someone "completing the menu" by adding
            # an item that returns 200 and moves nothing.
            for absent in ("dc.fbFileCtxRename(", "dc.fbFileCtxMove(",
                           "dc.fbFileCtxPublish("):
                check(f"the menu does NOT offer a dead item ({absent})",
                      not any(absent in (h or "") for h in handlers),
                      f"handlers={handlers}")
            page.screenshot(path=os.path.join(SHOTS, "wall-card-menu.png"), full_page=True)

            # ── 2. the Root card's menu must not offer to delete everything ──
            page.locator("#dc-wall .dc-folder").first.click(button="right")
            page.wait_for_timeout(600)
            del_hidden = page.evaluate(
                "() => getComputedStyle(document.getElementById('dc-wall-ctx-del')).display === 'none'")
            check("the Root card's menu hides Delete", del_hidden,
                  "one stray right-click could offer to delete the whole of Files")
            page.click("body", position={"x": 5, "y": 5})
            page.wait_for_timeout(400)
            check("an outside click closes the menu", not menu_open(page, "dc-wall-ctx"),
                  "#dc-wall-ctx is still open")
            check("right-clicking a card threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")

            # ── 3. "upload files here" really goes to THAT folder ──
            # ⚠️ From the wall `_fbPrefix` is '', so an implementation that
            # ignored the chosen folder would put it in the root and succeed.
            n_err = len(errors)
            card.click(button="right")
            page.wait_for_timeout(600)
            page.locator("#dc-wall-ctx .dc-fb-ctx-item",
                         has_text="Upload files here").first.click()
            page.wait_for_timeout(500)
            stage_file(page, "ctx-a.csv", folder, "x\n1\n")
            page.wait_for_timeout(2500)

            page.locator("#dc-wall .dc-folder", has_text=folder).first.click()
            page.wait_for_timeout(2500)
            rows = page.evaluate("""() => [...document.querySelectorAll('#dc-fb-list .dc-fb-name')]
              .map(e => e.textContent.trim())""")
            check("'Upload files here' put the file in THAT folder, not the root",
                  "ctx-a.csv" in rows, f"rows={rows}")
            check("the upload threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            check("no error toast after uploading from a menu",
                  not page.evaluate("() => [...document.querySelectorAll('.dc-toast.error')]"
                                    ".map(e => e.textContent.trim())"), "an error toast is up")
            page.screenshot(path=os.path.join(SHOTS, "inside-after-menu-upload.png"),
                            full_page=True)
            page.click("#dc-crumbs button.dc-crumb")
            page.wait_for_timeout(1200)

            # ── 4. "new subfolder here" makes it INSIDE that folder ──
            # ⚠️ Judged by going IN and looking, not by looking at the wall. The
            # wall reads the ROOT of the store and draws top-level folders only,
            # so a correctly nested subfolder never appears on it — an assertion
            # that looked for it there would have "failed" a working feature.
            child = folder + "-child"
            card.click(button="right")
            page.wait_for_timeout(600)
            page.locator("#dc-wall-ctx .dc-fb-ctx-item",
                         has_text="New subfolder here").first.click()
            page.wait_for_timeout(600)
            page.fill("#dc-fb-mkdir-input", child)
            page.click("#dc-fb-mkdir-modal .dc-btn-primary")
            page.wait_for_timeout(2500)
            page.locator("#dc-wall .dc-folder", has_text=folder).first.click()
            page.wait_for_timeout(2500)
            rows = page.evaluate("""() => [...document.querySelectorAll('#dc-fb-list .dc-fb-name')]
              .map(e => e.textContent.trim())""")
            check("'New subfolder here' nests under the chosen folder",
                  child in rows, f"rows={rows}")
            crumbs = page.evaluate(
                "() => [...document.querySelectorAll('#dc-crumbs .dc-crumb')]"
                ".map(e => e.textContent.trim())")
            check("and we are still inside the parent", crumbs and crumbs[-1] == folder,
                  f"crumbs={crumbs}")
            page.click("#dc-crumbs button.dc-crumb")
            page.wait_for_timeout(1200)
            check("'New subfolder here' did not also create it at the top level",
                  child not in wall_titles(page), f"wall={wall_titles(page)}")

            # ── 5. Add table can read a file from this computer ──
            n_err = len(errors)
            page.click('[data-dc-section="datasets"]')
            page.wait_for_timeout(3000)
            page.evaluate("() => dc.loadDatasets()")
            page.wait_for_timeout(3000)
            page.locator("#dc-datasets-grid .dc-proj[data-slug]", has_text=ds_name).first \
                .click(button="right")
            page.wait_for_timeout(600)
            page.locator("#dc-ds-ctx .dc-fb-ctx-item", has_text="Add table").first.click()
            page.wait_for_timeout(1500)
            at_open = page.evaluate(
                "() => document.getElementById('dc-at-modal').classList.contains('open')")
            check("Add table opens", at_open, "dc-at-modal is not open")

            picker = page.evaluate("""() => {
              const el = document.getElementById('dc-at-local-input');
              return el ? {exists: true, type: el.type, multiple: el.hasAttribute('multiple'),
                           accept: el.getAttribute('accept') || ''} : {exists: false};
            }""")
            check("Add table has a local file picker", picker.get("exists"),
                  "no #dc-at-local-input — the dialog can only list what is already uploaded")
            if picker.get("exists"):
                check("the picker takes spreadsheets",
                      all(e in picker["accept"] for e in (".xlsx", ".csv")),
                      f"accept={picker['accept']!r}")

            before = page.evaluate("""() => [...document.getElementById('dc-at-file').options]
              .map(o => o.textContent.trim())""")
            print(f"  library dropdown before: {before}")
            # ⚠️ Creating a folder writes an EMPTY `.keep` object so the folder
            # exists in a store with no real directories, and it is registered in
            # the file library — `browse_files` drops a folder with no visible
            # object under it, so the row is load-bearing. But it is a marker, not
            # a document: listing it beside real files, with no sheets under it, is
            # what made this dialog look broken. Asserted by CONTENT, because a
            # count would have passed with the markers swapped for another file.
            check("the library dropdown holds only this account's own file",
                  before == ["ctx-a.csv"], f"options={before}")
            check("no folder placeholder is offered as a source file",
                  not any(".keep" in o for o in before), f"options={before}")

            page.evaluate("""async () => {
              // A CSV that parses to one sheet with one row.
              const f = new File(['region,amount\\nNorth,12\\n'], 'local-pick.csv',
                                 {type: 'text/csv'});
              const dt = new DataTransfer();
              dt.items.add(f);
              await dc.dcAtUploadLocal(dt.files);
            }""")
            page.wait_for_timeout(3000)
            state = page.evaluate("""() => {
              const sel = document.getElementById('dc-at-file');
              const chosen = sel.options[sel.selectedIndex];
              return {
                options: sel.options.length,
                value: sel.value,
                chosenLabel: chosen ? chosen.textContent.trim() : null,
                sheets: [...document.getElementById('dc-at-sheet').options]
                  .map(o => o.textContent.trim()),
                hint: (document.getElementById('dc-at-local-hint').textContent || '').trim(),
              };
            }""")
            check("the picked file entered the library", state["options"] > len(before),
                  f"options {before} -> {state['options']}")
            check("the picked file is SELECTED, not merely added", bool(state["value"]),
                  f"value={state['value']!r} label={state['chosenLabel']!r}")
            check("the selected file's sheets are offered",
                  len(state["sheets"]) >= 1, f"sheets={state['sheets']}")
            # ⚠️ The reader has to be told. Without a line saying what happened, a
            # failed upload is a dialog that looks exactly like an idle one.
            check("the dialog says what happened", bool(state["hint"]), f"hint={state['hint']!r}")
            check("picking a local file threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            page.screenshot(path=os.path.join(SHOTS, "add-table-local-file.png"), full_page=True)

            # ── cleanup, while the page's session is alive ──
            page.evaluate("""async (paths) => {
              for (const p of paths) {
                await fetch('/api/data-center/files/by-path?path=' + encodeURIComponent(p)
                            + '&is_folder=true', {method: 'DELETE'});
              }
            }""", [f"{folder}/{child}", folder])
            ctx.close()
            browser.close()
    finally:
        try:
            dataset_groups.delete_group(slug, email, admin=True)
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (group cleanup failed: {exc})")
        drop_user(email)
        print(f"cleaned up {email}")

    print(f"\n{PASS} passed, {FAIL} failed")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())