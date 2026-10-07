#!/usr/bin/env python3
"""Workspace 项目 / 文件夹 — the card wall and the directory view, in a real browser.

Why this file exists
--------------------
`test_report_projects.py` proves the statements are *shaped* right, and it stubs the
connection — so it cannot see a query PostgreSQL rejects, a selector that matches
nothing, or a reader that ends up behind the top bar. All three happened while this
feature was written. This is the half that needs a real page.

What it guards, in the order it actually broke:

1. **The wall renders, and the two views are exclusive.** A wall and a directory view
   that are both visible at once is the failure AGENTS.md records for the knowledge
   page: `getElementById` returning null makes the toggle fail *silently*, because
   `if (body)` is just false.

2. **The reader is IN the pane, and the top bar is still reachable.** ⚠️ This is the
   one the whole design rests on. `.rpt-viewer` is a `position: fixed` overlay at
   `z-index: 8000`; the directory view moves that same element into `#wr-main` and
   adds `.inline`. If the modifier is missing, the reader paints over the navigation
   and the page looks exactly like the old full-screen viewer — a *worse* result than
   not having done the work. So the assertion is not "the document opened", it is
   `elementFromPoint` on the nav's own centre, which is the only question that
   distinguishes the two.

3. **Counts and list agree.** The card says how many reports it holds; the directory
   lists them. A card that says 3 over a list showing 1 is the reader's first evidence
   that the numbers are invented.

4. **Filing survives a reload.** The placement is the only thing a reader cannot see
   on the card, so it is the easiest thing to break in a way nothing else notices.

Run it against a **source** instance, not the deployed image:

    python -m uvicorn main:app --app-dir api --host 127.0.0.1 --port 8010
    KLADO_BASE=http://127.0.0.1:8010 python api/tests/verify_report_projects_ui.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8010")
SHOTS = os.environ.get("KLADO_WR_SHOTS", "/tmp/klado-wr-projects")

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
for _p in (REPO_DIR, API_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from playwright.sync_api import sync_playwright          # noqa: E402

from services import auth_store                            # noqa: E402

OWNER = "wrproj-owner@example.test"
MATE = "wrproj-mate@example.test"
PASSWORD = "wr-projects-probe-pw"

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


def bad(message: str) -> None:
    check(False, message)


# ── a session the script can also talk to with plain HTTP ────────────────────

class Session:
    """A logged-in cookie jar, so the test can assert against the API directly.

    Asserting a move by re-reading the API rather than by reading the screen is
    deliberate: the screen is what the **user** sees and the API is what the
    **agent** sees, and a feature that filed the report correctly in one and not the
    other would pass a screen-only test and still be broken.
    """

    def __init__(self, email: str, password: str):
        self.email = email
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        code, _ = self.post("/api/auth/login", {"email": email, "password": password})
        if code != 200:
            raise SystemExit(f"login failed for {email}: HTTP {code}")

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(BASE + path, data=body,
                                     headers={"Content-Type": "application/json"})
        return self._send(req)

    def put(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(BASE + path, data=body, method="PUT",
                                     headers={"Content-Type": "application/json"})
        return self._send(req)

    def delete(self, path: str) -> tuple[int, dict]:
        return self._send(urllib.request.Request(BASE + path, method="DELETE"))

    def get(self, path: str) -> tuple[int, dict]:
        return self._send(urllib.request.Request(BASE + path))

    def _send(self, req) -> tuple[int, dict]:
        try:
            with self.opener.open(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, (json.loads(raw) if raw else {})
            except ValueError:
                return exc.code, {"raw": raw}


def mk_user(email: str) -> None:
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
        cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (email, auth_store._hash(PASSWORD), "probe", "user"))
        conn.commit()


def wipe(email: str) -> None:
    """Start from a known state.

    The container tables are keyed by owner, so an aborted earlier run would
    otherwise leave projects behind and every assertion about counts would be about
    somebody else's leftovers.
    """
    from core.db import connect_main

    with connect_main() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, slug FROM ai_reports WHERE owner_email = %s", (email,))
        ids = [row[0] for row in cur.fetchall()]
        for report_id in ids:
            cur.execute("DELETE FROM ai_reports WHERE id = %s", (report_id,))
        cur.execute("SELECT slug FROM ai_report_projects WHERE owner_email = %s", (email,))
        slugs = [row[0] for row in cur.fetchall()]
        for slug in slugs:
            cur.execute("DELETE FROM ai_report_projects WHERE slug = %s", (slug,))
        conn.commit()


REPORT_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<style>body{font-family:sans-serif;margin:0;padding:40px}"
    "h1{font-size:34px}</style></head><body>"
    "<h1>季度渠道复盘</h1><p>这是目录视图里应该能读到的正文。</p>"
    "</body></html>"
)


def seed(session: Session) -> dict:
    """One project, two folders, three reports — filed on purpose, not by accident."""
    _, project = session.post("/api/reports/projects", {"title": "渠道口径"})
    slug = project["slug"]
    _, folder = session.post(f"/api/reports/projects/{slug}/folders", {"title": "月度"})
    _, folder2 = session.post(f"/api/reports/projects/{slug}/folders", {"title": "季度"})
    made = []
    for index, (name, fid) in enumerate((("一月复盘", folder["id"]),
                                        ("二月复盘", folder["id"]),
                                        ("Q1 汇总", folder2["id"]))):
        _, report = session.post("/api/reports", {
            # Slug is ASCII and stable: it goes in a URL and in a `data-slug`
            # attribute, so the Chinese title stays in `title` where it belongs.
            "slug": f"wrproj-{fid}-{index}",
            "title": name, "summary": "季度渠道复盘", "category": "Ad-hoc",
            "tags": "渠道", "html": REPORT_HTML.replace("季度渠道复盘", name),
        })
        session.post(f"/api/reports/{report['slug']}/move",
                     {"project_slug": slug, "folder_id": fid})
        made.append(report["slug"])
    return {"project": slug, "project_title": "渠道口径",
            "folders": [folder, folder2], "reports": made}


def login(page, email: str) -> None:
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#auth-login-email", timeout=20000)
    page.fill("#auth-login-email", email)
    page.fill("#auth-login-password", PASSWORD)
    page.click("#auth-login-btn")
    # ⚠️ Signing in **reloads the document**, and `window.reportsPageInit` exists on
    # the PRE-login page too — every module is defined by its script regardless of who
    # is signed in. Waiting for that global alone resolves against the old document,
    # the next click lands on a page about to be thrown away, and every assertion
    # afterwards reads a 401'd page: no wall, no cards, no error worth reading. The
    # auth gate closing is the part that is actually true afterwards.
    page.wait_for_function(
        "() => typeof window.reportsPageInit === 'function'"
        " && !document.body.classList.contains('auth-gate-open')",
        timeout=30000)


def wait_workspace(page) -> None:
    """Wait until the view on screen has been given its final content.

    ⚠️ Every view here renders in two steps: the container is un-hidden first, then
    `load()` fills it. Waiting on the container resolves **before** the data arrives,
    and an assertion about the list then reads an empty one — a test that fails on a
    feature that works.

    ⚠️ **The sentinel is the container's content, not a status string.** The old
    version waited for `#rpt-count` to stop saying "Loading" — but nothing ever writes
    "Loading" there, and the count is `''` both before the first load and after an
    empty result. So the wait resolved instantly and the assertions read an empty
    wall. What is actually true when a view has finished is that **its container has
    children**: the wall always gets the 新建项目 tile, and `#wr-list` always gets rows
    or an empty-state block. A view that renders nothing therefore times out here
    rather than quietly passing.
    """
    page.wait_for_function(
        """() => {
          const wall = document.getElementById('wr-wall');
          const body = document.getElementById('wr-body');
          if (wall && !wall.hidden) return !!wall.querySelector('.wr-proj, .wr-proj-new');
          if (body && !body.hidden) {
            const list = document.getElementById('wr-list');
            return !!list && list.childElementCount > 0;
          }
          return false;
        }""",
        timeout=25000)


def click_folder(page, folder_id, expect_rows: int) -> list[str]:
    """Click one folder row and wait for its list to settle. Returns the slugs.

    ⚠️ Not `:not([data-folder="…"])`. That was the first version and it matched three
    elements at once — the other two folders **and** the 新建文件夹 button, which
    carries `.wr-folder` but no id — so the click landed on whichever the selector
    resolved to and the test then asserted against an empty list. A selector that
    matches more than it means is a selector whose result depends on document order.

    ⚠️ **Waiting on the row COUNT, not on "the list has children".** `#wr-list` is
    never emptied between two loads, so the previous folder's rows — or its
    empty-state block — are still there when the next click lands, and a
    has-children wait resolves instantly against the *old* view. The fixture knows
    how many rows it filed, so the wait and the assertion are the same statement.
    """
    page.click(f'#wr-folders .wr-folder[data-folder="{folder_id}"]')
    page.wait_for_function(
        """(n) => {
          const list = document.getElementById('wr-list');
          if (!list) return false;
          const rows = list.querySelectorAll('.wr-row').length;
          // An empty folder must show WHY it is empty, not a blank column — and it
          // must be the real empty state, not the `rpt-loading` placeholder that
          // shares the `.rpt-empty` class.
          if (n === 0) return rows === 0 && !!list.querySelector('.rpt-empty:not(.rpt-loading)');
          return rows === n;
        }""",
        arg=expect_rows, timeout=25000)
    return page.eval_on_selector_all("#wr-list .wr-row", "els => els.map(e => e.dataset.slug)")


def rail(page) -> list[dict]:
    """The folder rail as `{id, text}` pairs, so a test can choose by name."""
    return page.eval_on_selector_all(
        "#wr-folders .wr-folder[data-folder]",
        "els => els.map(e => ({id: e.dataset.folder,"
        " text: (e.querySelector('.wr-folder-t')||{}).innerText || ''}))")


def nav_hits_nav(page) -> dict:
    """Is the top bar actually on top? — asked with a hit test, not with the DOM.

    ⚠️ A viewer that covers the navigation leaves it **in the DOM**, still
    `display: flex`, still at y=0. Every static assertion says the nav is healthy.
    The only question that tells the two apart is what is painted at the nav's own
    centre, which is what `elementFromPoint` answers.
    """
    return page.evaluate("""() => {
      const nav = document.querySelector('.nav');
      if (!nav) return {ok: false, why: 'no .nav'};
      const r = nav.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {ok: !!hit && nav.contains(hit),
              why: hit ? (hit.id || hit.className || hit.tagName) : 'nothing'};
    }""")


def viewer_placement(page) -> dict:
    """Where the reader is, and whether it is a screen overlay or a pane child."""
    return page.evaluate("""() => {
      const v = document.getElementById('rpt-viewer');
      if (!v) return {present: false};
      const style = getComputedStyle(v);
      return {
        present: true,
        open: v.classList.contains('open'),
        inline: v.classList.contains('inline'),
        position: style.position,
        parent: v.parentElement
          ? (v.parentElement.id || v.parentElement.className || v.parentElement.tagName)
          : null,
        inPane: !!document.getElementById('wr-main') &&
                v.parentElement === document.getElementById('wr-main'),
      };
    }""")


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    wipe(OWNER)
    wipe(MATE)
    mk_user(OWNER)
    mk_user(MATE)

    session = Session(OWNER, PASSWORD)
    seed_data = seed(session)
    project_slug = seed_data["project"]
    project_title = seed_data["project_title"]
    print(f"  seeded project {project_slug}, folders "
          f"{[f['id'] for f in seed_data['folders']]}, reports {seed_data['reports']}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        console_errors: list[str] = []
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        login(page, OWNER)
        page.wait_for_selector("#nav-tab-reports", timeout=20000)
        page.click("#nav-tab-reports")

        # ── 1. the wall ────────────────────────────────────────────────────
        page.wait_for_selector("#wr-wall:not([hidden])", timeout=25000)
        wait_workspace(page)
        cards = page.eval_on_selector_all(
            "#wr-wall .wr-proj", "els => els.map(e => e.dataset.proj)")
        # The wall's card count must equal the API's project count — asserted against
        # the API rather than a literal, so adding a project to the seed does not mean
        # editing a number in two places.
        _, wall_api = session.get("/api/reports/projects")
        check(len(cards) == len(wall_api["projects"]),
              "the wall shows one card per project",
              f"{len(cards)} cards for {len(wall_api['projects'])} projects")
        check(project_slug in cards, "the project just created is on the wall", f"got {cards}")
        check(len(cards) >= 1 and cards[0].startswith("unfiled-"),
              "未归档 is the first card", f"got {cards}")
        check(page.locator("#wr-wall .wr-proj-new").count() == 1,
              "the wall offers 新建项目", "no .wr-proj-new tile")
        sys_card = '.wr-proj[data-proj="' + cards[0] + '"]'
        check(page.locator(sys_card + " .kb-proj-cover-sys").count() == 1,
              "未归档 carries the drawn cover", "no drawn cover on the system card")
        check(page.locator("#wr-body").get_attribute("hidden") is not None,
              "the directory view is hidden while the wall is up",
              "both views are on screen at once")
        check(nav_hits_nav(page)["ok"], "the top bar is reachable on the wall",
              str(nav_hits_nav(page)))

        # The count on the card must equal what the directory will list.
        counts = page.eval_on_selector_all(
            "#wr-wall .wr-proj", "els => els.map(e => ({p: e.dataset.proj,"
            " t: (e.querySelector('.wr-proj-sub')||{}).innerText || ''}))")
        target = [c for c in counts if c["p"] == project_slug][0]
        check("3" in target["t"], "the project card says how many reports it holds",
              f"card reads {target['t']!r}")
        # ⚠️ The count line is the one part of a card that is NOT `data-i18n-skip`
        # (the title is), so it has to resolve its own i18n pair. A bare Chinese
        # literal here would read fine in a Chinese screenshot and be the only wrong
        # thing on an English wall — so the assertion is on the WORDS, not the number.
        lang_probe = page.evaluate("() => ({lang: window.kladoI18n && kladoI18n.lang()})")
        has_cjk = any("\u4e00" <= c <= "\u9fff" for c in target["t"])
        check(has_cjk is (lang_probe["lang"] == "zh"),
              f"the card's count line is single-language (lang={lang_probe['lang']})",
              f"card reads {target['t']!r}")

        page.screenshot(path=os.path.join(SHOTS, "01-wall.png"))

        # ── 2. the directory view ──────────────────────────────────────────
        page.click('.wr-proj[data-proj="' + project_slug + '"]')
        page.wait_for_selector("#wr-folders:not([hidden])", timeout=25000)
        wait_workspace(page)
        folders = rail(page)
        check(len(folders) == 3,
              "the folder rail lists 未归档 plus the two folders I made",
              f"got {[f['text'] for f in folders]}")
        check(page.locator("#wr-wall").get_attribute("hidden") is not None,
              "the wall is hidden inside the directory view",
              "the wall is still on screen")
        # ⚠️ The `Projects > <name>` row is GONE (2026-10-05); the reader asked for
        # that line to go. What must survive it is the CONTROL the row carried:
        # `closeProject()` was reachable only from there, so the way back to the
        # project wall now lives in the rail as `#wr-back`, carrying the project
        # name. Two things are asserted because either alone is a trap — a visible
        # back button that does not work, and a working one the reader cannot find.
        check(page.locator("#wr-crumbs").count() == 0,
              "那一行面包屑没有了", "still on the page")
        check(page.locator("#wr-back:not([hidden])").count() == 1,
              "回项目墙的入口在侧栏里，而且看得见", "no back control in the rail")
        check(page.evaluate("() => document.getElementById('wr-back-name').textContent.trim()")
              == project_title,
              "侧栏入口带的是当前项目名（不是一个要读者猜的箭头）",
              page.evaluate("() => document.getElementById('wr-back-name').textContent"))
        back_box = page.locator("#wr-back").bounding_box()
        check(back_box is not None and back_box["width"] > 0 and back_box["height"] > 0,
              "侧栏入口真的能点到（有面积，不是 0x0）", str(back_box))
        page.click("#wr-back")
        page.wait_for_selector("#wr-wall:not([hidden])", timeout=25000)
        wait_workspace(page)
        check(page.locator("#wr-back").get_attribute("hidden") is not None,
              "回到项目墙后侧栏入口收起来了（墙上没有「回项目」这种废话）",
              "the back control is still on the wall")
        # And back in, because the rest of this test continues in the directory.
        page.click('.wr-proj[data-proj="' + project_slug + '"]')
        page.wait_for_selector("#wr-folders:not([hidden])", timeout=25000)
        wait_workspace(page)
        # The rail opens on 未归档, which nothing was filed into, so the list is empty
        # and **says** so — an empty list with no explanation reads as a broken page.
        empty_slugs = click_folder(page, folders[0]["id"], 0)
        check(empty_slugs == [],
              "未归档 is empty, so no rows are listed", f"got {empty_slugs}")
        check(page.locator("#wr-list .rpt-empty:not(.rpt-loading)").count() == 1,
              "the empty folder explains itself instead of a blank column or a stuck spinner",
              f"empty blocks: {page.locator('#wr-list .rpt-empty').count()}, "
              f"still loading: {page.locator('#wr-list .rpt-loading').count()}")
        check(nav_hits_nav(page)["ok"], "the top bar is reachable in the directory view",
              str(nav_hits_nav(page)))
        page.screenshot(path=os.path.join(SHOTS, "02-directory.png"))

        # ── 3. opening a report: the reader must be IN the pane ────────────
        monthly = [f for f in folders if "月度" in f["text"]]
        check(len(monthly) == 1, "the 月度 folder is in the rail by name",
              f"got {[f['text'] for f in folders]}")
        if not monthly:
            browser.close()
            return 1
        rows = click_folder(page, monthly[0]["id"], 2)
        check(len(rows) == 2, "the 月度 folder lists its two reports", f"got {rows}")

        if not rows:
            bad("no report row to open — the fixture filed two")
            browser.close()
            return 1
        page.click('#wr-list .wr-row[data-slug="' + rows[0] + '"]')
        page.wait_for_selector("#rpt-viewer.open", timeout=25000)
        page.wait_for_timeout(1500)          # the iframe needs a beat to paint

        place = viewer_placement(page)
        check(place["present"] and place["open"], "the reader is open", str(place))
        check(place["inPane"], "the reader is mounted INSIDE the right pane", str(place))
        check(place["inline"], "the reader carries the inline modifier", str(place))
        check(place["position"] == "static",
              "the reader is not a fixed screen overlay any more", str(place))
        check(page.locator("#wr-empty").get_attribute("hidden") is not None,
              "the empty state is gone while a document is open", "both are on screen")
        # ⚠️ THE assertion this whole design rests on.
        hit = nav_hits_nav(page)
        check(hit["ok"], "the top bar is still the top thing on screen with a document open",
              f"the nav is covered by {hit['why']}")
        # The document really rendered, not just an iframe element.
        frame_text = page.evaluate("""() => {
          const f = document.getElementById('rpt-frame');
          try { return (f.contentDocument.body.innerText || '').slice(0, 60); }
          catch (e) { return 'unreadable: ' + e.message; }
        }""")
        check("复盘" in frame_text or "unreadable" in frame_text,
              "the document body rendered inside the pane", f"frame says {frame_text!r}")
        page.screenshot(path=os.path.join(SHOTS, "03-preview.png"))

        # The reader must be sized by the pane, not by the screen.
        box = page.evaluate("""() => {
          const v = document.getElementById('rpt-viewer');
          const m = document.getElementById('wr-main');
          if (!v || !m) return null;
          const a = v.getBoundingClientRect(), b = m.getBoundingClientRect();
          return {vw: Math.round(a.width), pw: Math.round(b.width),
                  vh: Math.round(a.height), ph: Math.round(b.height)};
        }""")
        check(box and box["vw"] <= box["pw"] + 2 and box["vh"] <= box["ph"] + 2,
              "the reader fills the pane instead of covering the screen", str(box))

        # ── 4. back out, and the reader returns to being an overlay ────────
        page.click("#rpt-viewer .rpt-back")
        page.wait_for_timeout(600)
        place = viewer_placement(page)
        check(not place.get("open"), "Back closes the document", str(place))
        check(str(place.get("parent") or "").upper() == "BODY",
              "the reader is back at body level, not parked in the pane", str(place))
        check(not place.get("inline"), "the inline modifier was removed", str(place))
        check(page.locator("#wr-empty").get_attribute("hidden") is None,
              "the pane invites the reader to pick a document again",
              "the empty state did not come back")
        check(page.locator("#wr-list .wr-row").count() == 2,
              "the report list is still there after closing the reader",
              "closing the reader emptied the list")

        # ── 5. moving a report between folders, seen from the screen ───────
        page.click('#wr-list .wr-row[data-slug="' + rows[1] + '"]', button="right")
        page.wait_for_selector("#rpt-menu:not([hidden]) button[data-action='move']", timeout=8000)
        check(True, "a report row offers 移动到文件夹")
        page.click("#rpt-menu button[data-action='move']")
        page.wait_for_selector("#wr-move-sel", timeout=8000)
        # ⚠️ The dialog **preselects the folder the report is already in** (that is what
        # makes it a "move" rather than a "pick"), so selecting `monthly` here would be a
        # no-op and the assertion below would pass against a list that never moved. Move
        # it to the sibling folder instead.
        quarterly = [f for f in folders if "季度" in f["text"]]
        check(len(quarterly) == 1, "the 季度 folder is in the rail by name",
              f"got {[f['text'] for f in folders]}")
        if not quarterly:
            browser.close()
            return 1
        target_value = project_slug + ":" + quarterly[0]["id"]
        page.select_option("#wr-move-sel", target_value)
        page.click("#kld-dlg-ok")
        page.wait_for_timeout(1800)
        badges = page.eval_on_selector_all(
            "#wr-folders .wr-folder", "els => els.map(e => e.innerText.replace(/\\s+/g,' ').trim())")
        moved_row = page.eval_on_selector_all(
            "#wr-list .wr-row", "els => els.map(e => e.dataset.slug)")
        check(rows[1] not in moved_row,
              "the moved report left the folder it was in", f"{moved_row}")
        check(len(moved_row) == 1,
              "and it is the only row left in that folder", f"{moved_row}")
        # …and the API agrees, which is the half a screen-only test cannot see.
        _, listing = session.get(f"/api/reports?scope=mine&project={project_slug}"
                                 f"&folder_id={monthly[0]['id']}")
        api_slugs = [r["slug"] for r in listing.get("reports", [])]
        check(sorted(api_slugs) == sorted(moved_row),
              "the API and the screen list the same reports",
              f"api={api_slugs} screen={moved_row}")
        check(any("月度 1" in b for b in badges) and any("季度 2" in b for b in badges),
              "the folder counts moved with the report", f"{badges}")
        print(f"       folder rail now reads: {badges}")

        # ── 6. it survives a reload ────────────────────────────────────────
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => typeof window.reportsPageInit === 'function'",
                               timeout=30000)
        page.click("#nav-tab-reports")
        page.wait_for_selector("#wr-wall:not([hidden])", timeout=25000)
        wait_workspace(page)
        after = page.eval_on_selector_all(
            "#wr-wall .wr-proj", "els => els.map(e => e.dataset.proj)")
        check(project_slug in after, "the project is still on the wall after a reload",
              f"got {after}")
        page.click('.wr-proj[data-proj="' + project_slug + '"]')
        page.wait_for_selector("#wr-folders:not([hidden])", timeout=25000)
        wait_workspace(page)
        persisted = [f["text"] for f in rail(page)]
        check(len(persisted) == 3, "the folders are still there after a reload",
              f"{persisted}")

        # ── 7. the other two scopes stay flat ──────────────────────────────
        page.click("#rpt-tab-public")
        wait_workspace(page)
        check(page.locator("#wr-wall").get_attribute("hidden") is not None,
              "Public does not open a wall of projects the reader cannot open",
              "the wall is showing in the public scope")
        check(page.locator("#wr-folders").get_attribute("hidden") is not None,
              "Public shows no folder rail", "the rail is showing")
        page.click("#rpt-tab-mine")
        wait_workspace(page)
        check(page.locator("#wr-wall").get_attribute("hidden") is None,
              "coming back to My workspace returns to the wall", "it did not")

        # ── 8. the three things that are cheap to break ────────────────────
        check(not console_errors, "no uncaught JavaScript error on the page",
              "; ".join(console_errors[:3]))
        # The filter controls must not be reachable on the wall: there is nothing
        # there for them to filter.
        check(page.locator("#wr-filters").is_hidden(),
              "the search/filter controls are hidden on the wall",
              "filters are visible over a wall of cards")
        page.click('.wr-proj[data-proj="' + project_slug + '"]')
        page.wait_for_selector("#wr-folders:not([hidden])", timeout=25000)
        check(page.locator("#wr-filters").is_visible(),
              "the filters appear once there is a list to filter",
              "the filters stayed hidden in the directory view")

        browser.close()

    print()
    print(f"  {PASS} passed, {FAIL} failed")
    for failure in FAILURES:
        print(f"   · {failure}")
    print(f"  screenshots: {SHOTS}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
