"""项目 / 文件夹 — the knowledge base's containers, driven in a real browser.

Why a browser and not an HTTP script
------------------------------------
Every claim this file makes is a claim about **what is on screen**: that the wall
lists 未归档 first, that opening a project shows its folders, that a right-click
menu item exists and moves the page where it says it will. A request-level test
would pass with a wall that never rendered, because it never asked the wall to
render.

What is actually asserted
-------------------------
* the wall shows one card per project, 未归档 first, and a card opens
* inside a project: the folder rail, 未归档 selected, and the pages of that folder
* **a page's right-click menu has 移动到文件夹, and using it really moves the page**
  — verified by re-reading the API, not by trusting a toast
* a page filed into a folder shared with a colleague is **visible to that
  colleague** — the cascade, which is the whole point of sharing a folder
* a second page is searched for across the account, not only the open folder
* a new folder can be created and a page moved into it
* the Workspace card menu has 转成知识库文档, and the clipboard really gets the
  prompt with the document's URL in it

Run
---
    cd api && KLADO_BASE=http://127.0.0.1:8000 ../.venv312/bin/python tests/verify_knowledge_projects_ui.py
"""
from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOTS = os.environ.get("KLADO_KB_SHOTS", "/tmp/klado-kb-projects")

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from playwright.sync_api import sync_playwright          # noqa: E402

from services import auth_store                            # noqa: E402

OWNER = "kbproj-owner@example.test"
MATE = "kbproj-mate@example.test"
PASSWORD = "kb-projects-probe-pw"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")
    return bool(ok)


# ── a session the script can also talk to with plain HTTP ────────────────────

class Session:
    """A logged-in cookie jar, so the test can assert against the API directly.

    Asserting a move by re-reading the API rather than by reading the screen is
    deliberate: the screen is what the **user** sees, and the API is what the
    **agent** sees, and a feature that filed the page correctly in one and not the
    other would pass a screen-only test and still be broken.
    """

    def __init__(self, email: str, password: str):
        self.email = email
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self._post("/api/auth/login", {"email": email, "password": password})

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = __import__("json").dumps(payload).encode()
        req = urllib.request.Request(BASE + path, data=body,
                                     headers={"Content-Type": "application/json"})
        return self._send(req)

    def _send(self, req) -> tuple[int, dict]:
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json_loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            return exc.code, (json_loads(raw) if raw else {})

    def get(self, path: str) -> tuple[int, dict]:
        return self._send(urllib.request.Request(BASE + path))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        return self._post(path, payload)

    def delete(self, path: str) -> tuple[int, dict]:
        return self._send(urllib.request.Request(BASE + path, method="DELETE"))


def json_loads(raw: str):
    import json
    try:
        return json.loads(raw)
    except Exception:
        return {}


def mk_user(email: str, role: str = "user") -> None:
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (email, auth_store._hash(PASSWORD), "probe", role))
        conn.commit()


def clear(email: str) -> None:
    """Start from a known state. The knowledge tables are keyed by owner, so an
    aborted earlier run would otherwise leave projects behind and the wall's
    assertions would be about somebody else's leftovers."""
    from core.db import connect_main
    with connect_main() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai_knowledge_projects WHERE owner_email = %s", (email,))
        cur.execute("DELETE FROM ai_knowledge_folder_shares WHERE recipient_email = %s", (email,))
        cur.execute("DELETE FROM ai_knowledge_items WHERE owner_email = %s", (email,))
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        conn.commit()


# The older spelling some throwaway probes still call.
wipe = clear


def seed_page(session: Session, slug: str, title: str, body: str = "## 内容") -> None:
    # ⚠️ The slug is sent explicitly. `POST /api/knowledge/items` derives one from the
    # title when it is absent, and a Chinese title derives a Chinese slug — so the
    # later assertions, which address pages by slug, would be addressing rows that do
    # not exist under the name they were seeded with.
    status, data = session.post("/api/knowledge/items",
                                {"slug": slug, "title": title, "body": body,
                                 "document_type": "note"})
    if status >= 300:
        raise SystemExit(f"could not seed {slug}: {status} {data}")


def page_placement(session: Session, slug: str) -> tuple[str, object]:
    _, data = session.get("/api/knowledge/items?scope=mine")
    for item in data.get("items", []):
        if item["slug"] == slug:
            return item.get("project_slug", ""), item.get("folder_id")
    return "", None


def login(page, email: str) -> None:
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#auth-login-email", timeout=20000)
    page.fill("#auth-login-email", email)
    page.fill("#auth-login-password", PASSWORD)
    page.click("#auth-login-btn")
    # ⚠️ Signing in **reloads the document**. Waiting for `.nav` is not enough: the
    # pre-login document already has a nav behind the auth gate, so the wait resolves
    # on the OLD page and the next click lands on a document about to be thrown
    # away. The symptom is a nav click that appears to do nothing at all — navTo
    # runs, and the page modules it dispatches to are not defined yet. Waiting for
    # the app's own global is the thing that is actually true afterwards.
    page.wait_for_function(
        "() => typeof window.knowledgePageInit === 'function'"
        " && !document.body.classList.contains('auth-gate-open')",
        timeout=30000)


def wait_idle(page, timeout: int = 20000) -> None:
    """Wait until the knowledge page's status line has stopped saying "Loading".

    ⚠️ Every view here renders in two steps: the container is un-hidden first, then
    `load()` fills it. Waiting on the container therefore resolves **before** the
    data arrives, and an assertion about a list reads an empty one — a test that
    fails on a feature that works. The status line is the one element that is
    written last in both paths.
    """
    page.wait_for_function(
        "() => { const s = document.getElementById('kb-status');"
        " return !!s && !/Loading/i.test(s.textContent || ''); }",
        timeout=timeout)


def open_knowledge(page) -> None:
    page.wait_for_selector("#nav-tab-knowledge", timeout=20000)
    page.click("#nav-tab-knowledge")
    page.wait_for_selector("#kb-wall:not([hidden])", timeout=20000)


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    wipe(OWNER)
    wipe(MATE)
    mk_user(OWNER)
    mk_user(MATE)

    owner = Session(OWNER, PASSWORD)
    mate = Session(MATE, PASSWORD)

    print("seeding…")
    seed_page(owner, "kbproj-page-a", "渠道口径 A")
    seed_page(owner, "kbproj-page-b", "渠道口径 B")
    # The catch-all is created on the server, so its slug is a fact to read rather
    # than a value to assert — the test is about where it is SHOWN, not what it is called.
    _, wall = owner.get("/api/knowledge/projects")
    unfiled_slug = next((p["slug"] for p in wall.get("projects", []) if p.get("system")), "")
    print(f"  unfiled project = {unfiled_slug!r}")
    # A coverless project of the owner's own, so the wall has something to tell the
    # drawn system cover apart from. Without it "every card is drawn" would pass.
    status, made = owner.post("/api/knowledge/projects", {"title": "临时项目"})
    plain_slug = made.get("slug", "")
    check(status == 201 and bool(plain_slug), "a coverless project of its own exists", f"status={status} slug={plain_slug!r}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        login(page, OWNER)
        open_knowledge(page)

        print("the wall")
        cards = page.eval_on_selector_all(".kb-proj", "els => els.map(e => e.dataset.proj)")
        check(bool(cards), "the wall renders at least one project card", f"cards={cards}")
        check(bool(unfiled_slug) and cards and cards[0] == unfiled_slug,
              "未归档 is the first card", f"first={cards[:1]} expected={unfiled_slug}")
        # The reserved name is stored as the PAIR "未归档 / Unfiled", and this browser
        # is running in English. A card that shows both at once is the bilingual
        # defect the i18n convention exists to prevent — on the one card every reader
        # is guaranteed to look at. A screenshot cannot catch it; this can.
        card_text = page.eval_on_selector_all(".kb-proj-title", "els => els.map(e => e.textContent)")
        check(all(" / " not in name for name in card_text),
              "no card title shows both languages at once", f"titles={card_text}")
        check(page.locator(".kb-proj-new").count() == 1,
              "the wall offers 新建项目", "no .kb-proj-new tile")

        # 未归档's cover is drawn by the app (systemCoverSvg), and it is drawn only
        # for a system project. Both halves matter: a cover on every card would mean
        # the owner's own coverless project quietly lost its "no cover" state.
        sys_card = '.kb-proj[data-proj="' + unfiled_slug + '"]'
        own_card = '.kb-proj[data-proj="' + plain_slug + '"]'
        check(page.locator(sys_card + " .kb-proj-cover-sys").count() == 1,
              "未归档 carries the drawn cover", "no .kb-proj-cover-sys on the system card")
        check(page.locator(sys_card + " .kb-proj-cover-ph").count() == 0,
              "未归档 no longer shows the No cover placeholder", "placeholder still on the system card")
        check(page.locator(own_card + " .kb-proj-cover-ph").count() == 1,
              "a coverless project of the owner's own still shows the placeholder",
              "the drawn cover leaked onto a user project")
        # "In the DOM" is not "on screen": an SVG that never painted passes every
        # count above. Measure the box the cover actually occupies.
        drawn_box = page.eval_on_selector(
            sys_card + " .kb-proj-cover-sys",
            "el => { const r = el.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }")
        check(drawn_box[0] > 100 and drawn_box[1] > 50,
              "the drawn cover is painted at card size", f"box={drawn_box}")
        # And it must be a THEME token, not baked pixels: the slot's fill has to
        # equal this theme's --accent-fill, and change when the theme changes.
        # A literal would look right in one theme and be wrong (or invisible) in the other.
        # ⚠️ Compare RESOLVED colours. `getComputedStyle(el).fill` is already an
        # `rgb(...)` while the custom property still reads `#066fd1`, so comparing the
        # two texts is not a colour comparison at all — it would fail on a correct
        # page. A throwaway probe resolves the token the same way the SVG does.
        PROBE = ("() => { const p = document.createElement('span');"
                 " p.style.color = 'var(--accent-fill)'; document.body.appendChild(p);"
                 " const v = getComputedStyle(p).color; p.remove(); return v; }")
        light_fill = page.eval_on_selector(sys_card + " .kb-sys-slot",
                                           "el => getComputedStyle(el).fill")
        token = page.evaluate(PROBE)
        check(bool(token) and light_fill == token,
              "the drawn cover is painted with the theme's --accent-fill",
              f"fill={light_fill!r} token={token!r}")
        page.eval_on_selector("html", "el => { el.dataset.theme = 'dark'; }")
        dark_fill = page.eval_on_selector(sys_card + " .kb-sys-slot", "el => getComputedStyle(el).fill")
        page.eval_on_selector("html", "el => { el.dataset.theme = 'light'; }")
        check(dark_fill and dark_fill != light_fill,
              "the drawn cover follows the dark theme", f"light={light_fill!r} dark={dark_fill!r}")
        page.screenshot(path=os.path.join(SHOTS, "01-wall.png"))

        print("inside a project")
        page.click('.kb-proj[data-proj="' + unfiled_slug + '"]')
        page.wait_for_selector("#kb-body:not([hidden])", timeout=20000)
        page.wait_for_selector("#kb-folders:not([hidden])", timeout=20000)
        wait_idle(page)
        folders = page.eval_on_selector_all("#kb-folders .kb-folder[data-folder]",
                                            "els => els.map(e => e.textContent.trim())")
        check(bool(folders), "the project page shows a folder rail", f"folders={folders}")
        check(any(f.startswith("未归档") or f.startswith("Unfiled") for f in folders),
              "未归档 is in the rail", f"folders={folders}")
        check(all(" / " not in f for f in folders),
              "no folder name shows both languages at once", f"folders={folders}")
        pages = page.eval_on_selector_all("#kb-toc-list .kb-link", "els => els.length")
        check(pages == 2, "both unfiled pages are listed", f"listed={pages}")
        # ⚠️ There is no breadcrumb row on the knowledge page any more (the old
        # `Projects › <项目名>` `#kb-crumbs` was deleted). Assert it is GONE and stays
        # gone — a re-added row would otherwise be invisible to this file, because
        # nothing here looks for it any more. `#kb-crumbs` is checked by absence, not
        # "is not visible": a rule that forgot `[hidden]` would hide it without
        # removing it, and that is exactly the defect this guards.
        check(page.locator("#kb-crumbs").count() == 0,
              "the breadcrumb row is gone from the knowledge page",
              "kb-crumbs is back in the DOM")
        check(page.eval_on_selector_all("body *", "els => els.some(e => e.classList.contains('kb-crumb'))") is False,
              "no leftover .kb-crumb chip anywhere", "a .kb-crumb is still rendered")
        page.screenshot(path=os.path.join(SHOTS, "02-project.png"))

        print("a new folder, and a page filed into it")
        target = None
        page.click("#kb-folders .kb-folder:not([data-folder])")
        page.wait_for_selector("#kld-dlg-input", timeout=10000)
        page.fill("#kld-dlg-input", "定价")
        page.click("#kld-dlg-ok")
        page.wait_for_timeout(1500)
        wait_idle(page)
        _, data = owner.get("/api/knowledge/projects/" + urllib.parse.quote(unfiled_slug) + "/folders")
        made = [f for f in data.get("folders", []) if f["title"] == "定价"]
        check(len(made) == 1, "a folder named 定价 was created", f"folders={data.get('folders')}")
        if made:
            target = made[0]
        if target:
            # Creating a folder opens it — that is what the click means ("show me this
            # new thing"). It is empty, so go back to 未归档 before moving anything.
            check(page.locator("#kb-toc-list .kb-link").count() == 0,
                  "the folder just created is the one now open, and it is empty",
                  "the new folder is not the open one")
            page.click('#kb-folders .kb-folder[data-folder]:not([data-folder="' + str(target["id"]) + '"])')
            wait_idle(page)

            # The move picker: right-click a page row, take 移动到文件夹.
            page.click('#kb-toc-list .kb-link[data-slug="kbproj-page-a"]', button="right")
            page.wait_for_selector("#kb-ctx:not([hidden])", timeout=10000)
            labels = page.eval_on_selector_all("#kb-ctx button", "els => els.map(e => e.textContent.trim())")
            # Either language: the menu renders the pair and the i18n walker keeps one
            # side, so the assertion has to accept whichever this browser is showing.
            check(any(("移动到文件夹" in l) or ("Move to a folder" in l) for l in labels),
                  "the page menu has 移动到文件夹", f"menu={labels}")
            page.screenshot(path=os.path.join(SHOTS, "03-menu.png"))
            page.click('#kb-ctx button[data-action="move"]')
            page.wait_for_selector("#kb-move-sel", timeout=10000)
            options = page.eval_on_selector_all("#kb-move-sel option",
                                                "els => els.map(e => e.textContent.trim())")
            check(any(o.startswith("定价") for o in options),
                  "the picker offers the new folder", f"options={options}")
            # By **value**, not by label. The value is `project:folder_id`, which the
            # test already knows, and it is the only handle that cannot be confused by
            # two folders whose labels read the same.
            page.select_option("#kb-move-sel", value=unfiled_slug + ":" + str(target["id"]))
            chosen = page.eval_on_selector("#kb-move-sel", "e => e.value")
            check(chosen == unfiled_slug + ":" + str(target["id"]),
                  "the picker really switched to 定价", f"select holds {chosen!r}")
            # ⚠️ Wait for the **response**, not for the page to look settled. `movePage`
            # is async and sets no "Loading" of its own, so `wait_idle` passes against
            # the status line left by the previous load — and the test then reads the
            # page's placement before the write has even been sent. That is a test that
            # fails on a feature that works, and it fails on a TIMING, which is the
            # kind that gets "fixed" by a longer sleep instead of by being fixed.
            with page.expect_response(lambda r: "/move" in r.url, timeout=20000) as done:
                page.click("#kld-dlg-ok")
            answer = done.value
            check(answer.status == 200, "the move was accepted",
                  f"HTTP {answer.status}")
            wait_idle(page)

            slug_at, folder_at = page_placement(owner, "kbproj-page-a")
            check(slug_at == unfiled_slug and folder_at == target["id"],
                  "the API agrees the page moved into 定价",
                  f"project={slug_at} folder={folder_at} expected={target['id']}")
            listed = page.eval_on_selector_all("#kb-toc-list .kb-link", "els => els.length")
            check(listed == 1, "the open folder now lists one page", f"listed={listed}")
            page.screenshot(path=os.path.join(SHOTS, "04-folder.png"))

            print("the folder share cascade")
            status, _ = owner.post(f"/api/knowledge/folders/{target['id']}/shares", {"emails": [MATE]})
            check(status == 200, "the folder was shared with a colleague", f"HTTP {status}")
            # ⚠️ Only the COLLEAGUE's view is asserted here. The owner's own page is
            # `mine`, never `shared` — the shared tab is by definition somebody else's
            # content — so checking the owner's copy of it tests nothing and fails.
            _, shared = mate.get("/api/knowledge/items?scope=shared")
            mate_slugs = [i["slug"] for i in shared.get("items", [])]
            check("kbproj-page-a" in mate_slugs,
                  "the colleague can read the page through the folder share",
                  f"mate sees {mate_slugs}")
            check("kbproj-page-b" not in mate_slugs,
                  "and only the pages actually in that folder",
                  f"mate sees {mate_slugs}")
            # The cascade: a page filed into the folder AFTER the share inherits it.
            seed_page(owner, "kbproj-page-c", "渠道口径 C")
            _, moved = owner.post("/api/knowledge/items/kbproj-page-c/move",
                                  {"project_slug": unfiled_slug, "folder_id": target["id"]})
            check(moved.get("folder_id") == target["id"], "the third page filed into 定价",
                  f"resp={moved}")
            _, shared2 = mate.get("/api/knowledge/items?scope=shared")
            check("kbproj-page-c" in [i["slug"] for i in shared2.get("items", [])],
                  "a page filed into an already-shared folder inherits the grant",
                  f"mate sees {[i['slug'] for i in shared2.get('items', [])]}")

            print("revoking the folder share")
            status, _ = owner.delete(
                f"/api/knowledge/folders/{target['id']}/shares?email={urllib.parse.quote(MATE)}")
            check(status == 200, "the folder share was revoked", f"HTTP {status}")
            _, shared3 = mate.get("/api/knowledge/items?scope=shared")
            check("kbproj-page-a" not in [i["slug"] for i in shared3.get("items", [])],
                  "one revoke took back every page in the folder",
                  f"mate still sees {[i['slug'] for i in shared3.get('items', [])]}")

        print("deleting a folder does not delete pages")
        if target:
            status, data = owner.delete(f"/api/knowledge/folders/{target['id']}")
            check(status == 200, "the folder was deleted", f"HTTP {status} {data}")
            check(data.get("count", 0) >= 1, "it reported the pages it re-filed", f"{data}")
            _, still = owner.get("/api/knowledge/items?scope=mine")
            slugs = [i["slug"] for i in still.get("items", [])]
            check("kbproj-page-a" in slugs and "kbproj-page-c" in slugs,
                  "the pages are still there", f"pages={slugs}")
            slug_at, folder_at = page_placement(owner, "kbproj-page-a")
            check(slug_at == unfiled_slug, "they went back to 未归档", f"project={slug_at}")

        print("search spans the account, not the open folder")
        page.fill("#kb-search", "渠道口径 C")
        # ⚠️ `#kb-search` debounces by 350ms before it loads, so `wait_idle` alone can
        # pass against the *previous* result — the status line is not "Loading" yet.
        # Wait out the debounce first, then for the status to settle.
        page.wait_for_timeout(900)
        wait_idle(page)
        found = page.eval_on_selector_all("#kb-toc-list .kb-link", "els => els.length")
        check(found == 1, "a search finds a page outside the open folder", f"found={found}")
        page.fill("#kb-search", "")
        page.wait_for_timeout(900)
        wait_idle(page)

        print("the wall again, and 未归档's guard rails")
        # ⚠️ PREREQUISITE, before anything that claims the wall came back: assert we
        # are actually INSIDE a project first. The wall not being visible is also what
        # a broken precondition looks like, so without this the next check is
        # vacuously true — exactly the failure mode this file already hit once.
        check(page.locator("#kb-body:not([hidden])").count() == 1,
              "（前置）we are inside a project before we try to leave it",
              "kb-body was already hidden")
        # ⚠️ The old way back was the `Projects` crumb in `#kb-crumbs`, which is gone.
        # This walks the path that replaced it, and it is the load-bearing claim of
        # the whole deletion: the top nav tab is a FRONT DOOR, so it must return to
        # the project wall. If this ever stops holding, deleting the crumb turned a
        # project into a dead end and this assertion is what says so.
        page.click("#nav-tab-knowledge")
        page.wait_for_selector("#kb-wall:not([hidden])", timeout=20000)
        wait_idle(page)
        check(page.locator("#kb-body[hidden]").count() == 1,
              "the top nav tab takes a reader back to the project wall",
              "kb-body still visible after clicking the tab")
        # A second project called 未归档 would put two identically named cards on the
        # wall and make "put it in 未归档" ambiguous. The server refuses it; this
        # checks the refusal, and then checks it left nothing behind.
        status, data = owner.post("/api/knowledge/projects", {"title": "未归档"})
        check(status == 400, "「未归档」cannot be created as a second project",
              f"HTTP {status} {data}")
        _, after = owner.get("/api/knowledge/projects")
        titles = [p["title"] for p in after.get("projects", [])]
        check(titles.count("未归档 / Unfiled") == 1,
              "the wall has exactly one 未归档", f"titles={titles}")

        print("the tab is the front door, not a history control")
        # ⚠️ The reported defect, exactly as it was reported: enter a project, leave the
        # module, come back — and the tab re-entered the project, because the project
        # and folder were never cleared on the way out. The assertion is about the wall
        # being VISIBLE and the directory being hidden, not about a variable's value.
        page.click('.kb-proj[data-proj="' + unfiled_slug + '"]')
        page.wait_for_selector("#kb-body:not([hidden])", timeout=20000)
        page.click("#nav-tab-calendar")
        page.wait_for_selector("#page-calendar", timeout=20000)
        page.wait_for_timeout(300)
        page.click("#nav-tab-knowledge")
        page.wait_for_selector("#kb-wall:not([hidden])", timeout=20000)
        wait_idle(page)
        check(page.locator("#kb-body[hidden]").count() == 1,
              "coming back to the tab shows the wall, not the folder left open",
              "the directory view survived the trip through another module")
        check(page.locator("#kb-folders[hidden]").count() == 1,
              "the folder rail is gone with the directory view", "kb-folders still visible")
        # Same instruction, same place: the rail row must not be a second, different
        # way in. (It is the one entry that still went through a bare navTo.)
        page.click('.kb-proj[data-proj="' + unfiled_slug + '"]')
        page.wait_for_selector("#kb-body:not([hidden])", timeout=20000)
        # The rail is off-canvas until the pointer reaches the hot zone, so open it the
        # way a reader does. `force` skips only Playwright's pre-check — the pointer
        # still lands on the strip and the app's own `mouseenter` opens the panel; what
        # is skipped is a hit-test that a *later* section of this run invalidates,
        # because the scrim (z-index 8699) covers the hot zone (8600) while the rail is
        # out. If the rail is already open, the row is simply clickable.
        if page.locator("body.rail-open").count() == 0:
            page.hover("#rail-hotzone", force=True)
            page.wait_for_selector("body.rail-open", timeout=20000)
        rail_knowledge = page.locator('.rail-mod[data-rail-mod="knowledge"]')
        check(rail_knowledge.count() == 1,
              "the rail has its Knowledge base row", f"count={rail_knowledge.count()}")
        rail_knowledge.first.click()
        page.wait_for_timeout(500)
        wait_idle(page)
        check(page.locator("#kb-wall:not([hidden])").count() == 1,
              "the rail's Knowledge base row lands on the wall too",
              "the rail restored the directory view")

        # ⚠️ … and none of that may cost the deep link. A pasted `?kb=` is a fresh page
        # load, and it has to open the page. This is the assertion that keeps
        # "always the wall" from quietly becoming "the wall even when asked for a page".
        #
        # `reload()` is not redundant: going from `/` to `/?kb=<slug>` changes only the
        # query, which the browser treats as a SAME-DOCUMENT navigation — nothing
        # re-runs, `bootstrapKnowledgeDeepLink` never fires, and the page stays on the
        # wall. A reader pasting a link gets a real load (different origin, or the app
        # was not already open), so the reload is what makes this a real one.
        page.goto(BASE + "/?kb=" + urllib.parse.quote("kbproj-page-a"))
        page.reload()
        # ⚠️ `:not([hidden])` alone is the wrong assertion here, and it hid a real bug:
        # `#kb-doc` was un-hidden inside a `display:none` `#kb-body`, so a deep link
        # opened the document where nobody could see it and the reader got the wall
        # instead. Waiting for the element to be VISIBLE is what forces the whole chain
        # up — the same reason `verify_nav_always_ui.py` probes with elementFromPoint.
        page.wait_for_selector("#kb-doc", state="visible", timeout=20000)
        box = page.eval_on_selector("#kb-doc", "el => { const r = el.getBoundingClientRect();"
                                              " return [Math.round(r.width), Math.round(r.height)]; }")
        check(box[0] > 200 and box[1] > 200,
              "a deep-linked page is really on screen, not merely un-hidden",
              f"doc box={box}")
        check(page.locator("#kb-wall[hidden]").count() == 1,
              "the deep link does not leave the card wall sitting on top of it",
              "the wall is still on screen")
        wait_idle(page)
        deep_title = page.eval_on_selector("#kb-doc", "el => el.textContent")
        check("渠道口径 A" in deep_title,
              "a fresh load of ?kb=<slug> still opens that page", f"doc={deep_title[:80]!r}")
        # … and from there, the tab is still the wall.
        page.click("#nav-tab-calendar")
        page.wait_for_timeout(300)
        page.click("#nav-tab-knowledge")
        page.wait_for_selector("#kb-wall:not([hidden])", timeout=20000)
        wait_idle(page)
        check(page.locator("#kb-body[hidden]").count() == 1,
              "the tab after a deep link lands on the wall as well",
              "the deep-linked page was restored by the tab")

        print("Workspace → 转成知识库文档")
        _, made = owner.post("/api/reports",
                             {"title": "转存用报告", "html": "<p>x</p>", "kind": "static"})
        report_slug = made.get("slug", "")
        page.click("#nav-tab-reports")
        # ⚠️ 「我的工作台」opens on the WALL OF PROJECTS (2026-10-03) and a report is a
        # ROW in `#wr-list` once its project is open — the card grid (`#rpt-grid`) is
        # gone from this page. The seeded report was never filed, so it is listed under
        # the account's own 未归档 project; its slug is READ from the server, the same
        # way the knowledge catch-all's is above, because a project slug is a fact to
        # look up rather than a value to assume.
        page.wait_for_selector("#wr-wall .wr-proj", timeout=20000)
        _, wr_wall = owner.get("/api/reports/projects")
        wr_unfiled = next((p["slug"] for p in wr_wall.get("projects", []) if p.get("system")), "")
        project = page.locator('#wr-wall .wr-proj[data-proj="' + wr_unfiled + '"]')
        # ⚠️ By the slug the server RETURNED, never by the title: an HTML report's slug
        # is generated (`report-<id>-<date>-…`), so a locator built from the title
        # matches nothing and the test reports "row missing" for a row that is
        # plainly on screen.
        if check(project.count() == 1, "the report's project is on the Workspace wall",
                 f"no project card for {wr_unfiled!r}"):
            project.click()
            page.wait_for_selector("#wr-list .wr-row", timeout=20000)
            row = page.locator('#wr-list .wr-row[data-slug="' + report_slug + '"]')
            if check(row.count() == 1, "the seeded report is listed in its project",
                     f"no row for slug {report_slug!r}"):
                row.click(button="right")
                page.wait_for_selector("#rpt-menu:not([hidden])", timeout=10000)
                labels = page.eval_on_selector_all("#rpt-menu button", "els => els.map(e => e.textContent.trim())")
                check(any(("转成知识库文档" in l) or ("Turn into a knowledge page" in l) for l in labels),
                      "the Workspace row menu has 转成知识库文档", f"menu={labels}")
                page.click('#rpt-menu button[data-action="to-knowledge"]')
                page.wait_for_timeout(800)
                clip = page.evaluate("() => navigator.clipboard.readText()")
                check("转成知识库文档" not in clip and "Turn this Workspace document" in clip,
                      "the clipboard got the prompt", f"clipboard head={clip[:80]!r}")
                check("report=" + urllib.parse.quote("转存用报告") in clip or "转存用报告" in clip,
                      "the prompt names the document", f"clipboard has no title")
                check("/api/reports/" in clip and "/raw" in clip,
                      "the prompt points the agent at the raw source", f"clipboard has no raw url")
                page.screenshot(path=os.path.join(SHOTS, "05-prompt.png"))

        check(not errors, "no uncaught JS error on the page", "; ".join(errors[:3]))
        browser.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    for line in FAILURES:
        print("  - " + line)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
