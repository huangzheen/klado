"""Data Center: a whole local folder uploads with its structure, and a dataset's
card can be acted on from the wall.

⚠️ Two features, one file, because they share the thing that makes them easy to
get wrong: both are about a path, and both fail SILENTLY when the path is wrong.

1. FOLDER UPLOAD. A folder picker is an input with `webkitdirectory`. Without
   that attribute it is a plain file input: the reader picks a folder, one file
   from it goes up, and nothing anywhere says an error. So the attribute is
   asserted as a fact about the element, not inferred from the upload working.
   The structure itself is `webkitRelativePath` — "Q3/华东/销售.xlsx" — and the
   trap is the FIRST segment: that is the folder the reader chose in the dialog,
   so it must be dropped, or every import lands one level deeper than they asked
   and the wall looks like it simply ignored them.

2. THE DATASET CARD MENU. "Import from a database" existed and worked, but only
   from the list header — visible only after opening the dataset, which is a
   click you make when you already know what is inside. The wall card is the
   thing you can act on, so the door moved onto it.

⚠️ How the folder upload is exercised: Playwright cannot drive the OS folder
dialog, and `set_input_files` on a `webkitdirectory` input does not populate
`webkitRelativePath` — it would test a FileList the browser never produces. So
the test builds a `DataTransfer` with a read-only `webkitRelativePath` defined
per file and calls the REAL `dc.fbUploadFolder()`. The function under test is
the production one; only the browser's file dialog is simulated.

⚠️ The reader must be on the WALL for the upload assertion, not inside a folder.
An upload test that only ever runs inside a folder passes while the wall is
broken — that is the same trap as the mkdir handler's.

Needs a live server and the object store, so it is not in
`scripts/ci_check.py`. `test_dc_folder_upload.py` is the half CI can run.

    ../.venv312/bin/python tests/verify_dc_folder_import_ui.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright                        # noqa: E402

from services import auth_store, dataset_groups                       # noqa: E402

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_SHOTS", "/tmp/klado-dc-folder-shots")

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
            "VALUES (%s, %s, 'DC folder probe', 'admin')", (email, auth_store._hash(pw)))
        conn.commit()


def drop_user(email):
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        conn.commit()


def wall_titles(page):
    return page.evaluate("""() => [...document.querySelectorAll('#dc-wall .dc-folder-title')]
      .map(e => e.textContent.trim())""")


def ctx_open(page):
    return page.evaluate(
        "() => { const m = document.getElementById('dc-ds-ctx');"
        " return !!m && m.style.display !== 'none' && m.getBoundingClientRect().height > 0; }")


def main():
    stamp = int(time.time())
    tag = f"dcimport{stamp}"
    email = f"{tag}@example.test"
    pw = "Dc-Folder-Probe-1!"
    ds_name = f"{tag}-ds"
    top = f"{tag}-folder"
    sub = "华东"

    make_user(email, pw)
    # ⚠️ A PLAIN name, not a `中文 / English` pair: it is rendered as data a
    # person typed, so a pair would show both languages in the card title.
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
            page.wait_for_timeout(3500)

            # ── 1. the picker really is a FOLDER picker ──
            # ⚠️ Asserted as a property of the element. An upload that works is
            # NOT evidence for this: without the attribute the input still uploads
            # — one file, silently, from the folder the reader picked.
            attrs = page.evaluate("""() => {
              const el = document.getElementById('dc-fb-folder-input');
              return el ? {webkitdirectory: el.hasAttribute('webkitdirectory'),
                           directory: el.hasAttribute('directory'),
                           multiple: el.hasAttribute('multiple'),
                           type: el.type} : null;
            }""")
            check("there is a folder-picker input", attrs is not None, "no #dc-fb-folder-input")
            if attrs:
                check("the input is a DIRECTORY picker (webkitdirectory)",
                      attrs["webkitdirectory"], f"attrs={attrs}")
                check("the input is a directory picker (standards spelling)",
                      attrs["directory"], f"attrs={attrs}")
                check("the input takes many files at once", attrs["multiple"], f"attrs={attrs}")
            btn = page.locator('#dc-tools-files button[onclick="dc.fbUploadFolderClick()"]')
            check("the toolbar has an Upload folder button", btn.count() == 1,
                  f"count={btn.count()}")

            # ── 2. upload a folder's worth of files, from the WALL ──
            n_err = len(errors)
            before = set(wall_titles(page))
            page.evaluate("""async (arg) => {
              const mk = (rel, name, body) => {
                const f = new File([body], name, {type: 'text/csv'});
                // ⚠️ Read-only in the real browser. Defining it is the only way to
                // produce the FileList a folder selection actually makes, since
                // Playwright's set_input_files leaves it empty.
                Object.defineProperty(f, 'webkitRelativePath',
                  {value: rel, enumerable: true});
                return f;
              };
              const dt = new DataTransfer();
              dt.items.add(mk(arg.top + '/a.csv', 'a.csv', 'x\\n1\\n'));
              dt.items.add(mk(arg.top + '/' + arg.sub + '/b.csv', 'b.csv', 'x\\n2\\n'));
              dt.items.add(mk(arg.top + '/' + arg.sub + '/deep/c.csv', 'c.csv', 'x\\n3\\n'));
              await dc.fbUploadFolder(dt.files);
            }""", {"top": top, "sub": sub})
            page.wait_for_timeout(2500)

            s = page.evaluate("""() => {
              const vis = (id) => { const e = document.getElementById(id);
                if (!e || e.hidden) return false;
                if (getComputedStyle(e).display === 'none') return false;
                return e.getBoundingClientRect().height > 0; };
              return {wall: vis('dc-wall'), body: vis('dc-body'),
                      wallTitles: [...document.querySelectorAll('#dc-wall .dc-folder-title')]
                        .map(e => e.textContent.trim())};
            }""")
            after = set(s["wallTitles"])
            # The picked folder KEEPS ITS NAME. Dropping the first segment of
            # webkitRelativePath is the tempting reading — it is the folder they
            # chose — and it scatters the contents while losing the name. A
            # browser run caught it: the wall came back with the SUBFOLDER on it
            # and not the folder. `cp -r` and every cloud drive agree.
            check("the picked folder lands on the wall under its own name",
                  top in after, f"wall={sorted(after)}")
            check("only the picked folder is added, not each of its subfolders",
                  sub not in after, f"wall={sorted(after)} — a subfolder leaked to the top")
            check("the wall REPAINTED — the reader is looking at the wall, not a list",
                  s["wall"] and not s["body"], f"wall={s['wall']} body={s['body']}")
            check("uploading a folder threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            check("no error toast after a folder upload",
                  not page.evaluate("() => [...document.querySelectorAll('.dc-toast.error')]"
                                    ".map(e => e.textContent.trim())"),
                  "an error toast is up")
            page.screenshot(path=os.path.join(SHOTS, "wall-after-folder-upload.png"),
                            full_page=True)

            # ── 3. go INTO the folder and prove the tree survived ──
            page.locator("#dc-wall .dc-folder", has_text=top).first.click()
            page.wait_for_timeout(2500)
            rows = page.evaluate("""() => [...document.querySelectorAll('#dc-fb-list .dc-fb-name')]
              .map(e => e.textContent.trim())""")
            check("the files are inside the folder that was picked",
                  "a.csv" in rows, f"rows={rows}")
            check("its subfolder is listed as a folder row, not flattened",
                  sub in rows, f"rows={rows}")
            page.screenshot(path=os.path.join(SHOTS, "inside-imported-folder.png"),
                            full_page=True)
            page.click("#dc-crumbs button.dc-crumb")
            page.wait_for_timeout(1200)

            # ── 4. the dataset card's menu ──
            n_err = len(errors)
            page.click('[data-dc-section="datasets"]')
            page.wait_for_timeout(3000)
            page.evaluate("() => dc.loadDatasets()")
            page.wait_for_timeout(3000)

            card = page.locator("#dc-datasets-grid .dc-proj[data-slug]", has_text=ds_name)
            check("the dataset has a card on the wall", card.count() == 1,
                  f"count={card.count()}")
            card.first.click(button="right")
            page.wait_for_timeout(700)
            check("right-clicking a dataset card opens ITS menu", ctx_open(page),
                  "#dc-ds-ctx is not open")
            title = page.evaluate(
                "() => { const t = document.querySelector('#dc-ds-ctx .dc-ds-ctx-title');"
                " return t ? t.textContent.trim() : null; }")
            # ⚠️ A menu on a card called "华东 2026" that does not SAY so makes the
            # reader hunt the wall to find out which card is armed — and a menu that
            # silently opened on the wrong card is the worst kind of ambiguous.
            check("the menu names the card it acts on", title == ds_name,
                  f"title={title!r} card={ds_name!r}")
            items = page.evaluate(
                "() => [...document.querySelectorAll('#dc-ds-ctx .dc-fb-ctx-item')]"
                ".map(e => e.getAttribute('onclick'))")
            # ⚠️ By HANDLER, never by label. These are `中文 / English` pairs and the
            # i18n walker rewrites them to the current language, so a headless
            # Chromium only ever shows the English half — locating by the Chinese
            # text waits out the full 30s and takes the rest of the run with it.
            for want in ("dc.dsCtxOpen()", "dc.dsCtxAddTable()",
                         "dc.dsCtxImportExternal()", "dc.dsCtxDelete()"):
                check(f"the menu offers {want}", want in items, f"handlers={items}")
            check("the menu has no handler-less item", all(items), f"handlers={items}")
            page.screenshot(path=os.path.join(SHOTS, "dataset-card-menu.png"), full_page=True)

            page.locator("#dc-ds-ctx .dc-fb-ctx-item",
                         has_text="Import").first.click()
            page.wait_for_timeout(900)
            ie = page.evaluate("""() => {
              const m = document.getElementById('dc-ie-modal');
              return {open: !!m && m.classList.contains('open'),
                      slug: (document.getElementById('dc-ie-slug') || {}).textContent};
            }""")
            check("'Import from a database' opens the external-import dialog", ie["open"],
                  f"modal={ie}")
            check("it is pointed at THIS dataset", ie["slug"] == slug,
                  f"slug={ie['slug']!r} expected={slug!r}")
            check("the menu closed behind the dialog", not ctx_open(page),
                  "#dc-ds-ctx is still open")
            check("right-clicking a card threw nothing", len(errors) == n_err,
                  f"errors={errors[n_err:]}")
            page.screenshot(path=os.path.join(SHOTS, "external-import-dialog.png"),
                            full_page=True)

            # ── cleanup, while the page's session is still alive ──
            page.evaluate("""async (paths) => {
              for (const p of paths) {
                await fetch('/api/data-center/files/by-path?path=' + encodeURIComponent(p)
                            + '&is_folder=true', {method: 'DELETE'});
              }
            }""", [f"{top}/{sub}/deep", f"{top}/{sub}", top])
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