"""The dynamic-report filter rail, in a real browser, against the REAL router.

What the unit tests cannot see, and this script exists for:

* the rail is built from a published schema and sits **to the left of** the report
  frame — measured as geometry, because "the sidebar renders" says nothing about which
  side of the document it took;
* a click in the rail reaches the DOCUMENT: the real injected runtime
  (`vendor/report-filters.js`) hides the slices that no longer match, and the deck
  runtime recounts the pages. Both are asserted by reading the iframe's own DOM;
* the reader gets no way to edit the filters (the rail is values-only) and the
  document itself contains no control at all — the product rule, asserted where it
  can actually break;
* the reader's pick is persisted through the real endpoint with the mount-correct
  path, and the report is served with THAT reader's saved selection already in it;
* a document row (pptx) is a document, not a report: the row says so by type and by
  file, its menu offers a download instead of the three exports, and no rail appears.

The SPA is the real `frontend/out/index.html`, the report comes from the real
`routers.reports.render_report` (real injection order), and everything is served over
real HTTP by uvicorn with a stub database — a wrong URL or a wrong injection order
fails here instead of being papered over.

    python3 api/tests/verify_dynamic_report_ui.py
"""
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import re

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import reports                                                    # noqa: E402
from services import report_projects                                          # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "out"
SHOTS = Path("/private/tmp/dynamic-report-shots")
PORT = 18895            # the checks: must be free, and the harness says so if it is not
SERVE_PORT = 18896      # `--serve`: a different port on purpose (see `main`)
# The viewer iframe's address — matched by URL, never by index: the viewer blanks the frame
# to `about:blank` before loading, so `page.frames[1]` is a discarded stub.
REPORT_FRAME = re.compile(r"/r/q3-slice-demo")
OWNER = "zhen.huang@example.com"
SLICE_SLUG = "q3-slice-demo"
DOC_SLUG = "q3-deck-file"
FAILURES = []
SEEN = []          # (method, path, body) the SPA actually asked for

# ── where the two fixtures are filed (2026-10-03) ────────────────────────────
# ⚠️ "My workspace" opens on the WALL OF PROJECTS now, and a report is reached by
# opening the project that owns it — the card grid is gone from this page. So the
# harness has to answer the two container queries too, and it answers them with the
# account's own 未归档 project: that is where an unfiled report is listed, and
# `unfiled_slug()` is PURE (a hash of the address), so the wall gets the same one card
# the server would have created, and both fixtures are listed when it is opened.
UNFILED = report_projects.unfiled_slug(OWNER)
UNFILED_FOLDER = 1
STAMP = datetime(2026, 9, 30, tzinfo=timezone.utc)
PROJECT_ROWS = [{"id": 1, "slug": UNFILED, "title": report_projects.UNFILED_TITLE,
                 "summary": "", "system": True, "owner_email": OWNER, "has_cover": False,
                 "cover_url": "", "cover_w": None, "cover_h": None, "cover_prompt": "",
                 "cover_model": "", "created_at": STAMP, "updated_at": STAMP,
                 "report_count": 2, "folder_count": 1}]
FOLDER_ROWS = [{"id": UNFILED_FOLDER, "project_slug": UNFILED,
                "title": report_projects.UNFILED_TITLE, "system": True, "sort_order": 0,
                # ⚠️ `project_system` is a column of the JOIN, not decoration: without it
                # the folder count is taken against the unfiled project as if it were an
                # ordinary one, and 未归档 reports itself permanently empty.
                "project_system": True}]

# ── the fixtures ─────────────────────────────────────────────────────────────
# A bilingual deck whose pages are slices: the EN set is [Jun, Jul, quantity-only,
# always] and the ZH set mirrors it. `period` selects which of the first two shows,
# `sales_metric=quantity` adds a third page, and the last page is shared content.
DECK = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="report-kind" content="dynamic">
<title>Q3 slice demo</title></head><body>
<div class="deck" data-deck-title="Q3 slice demo">
  <section class="slide" data-lang="en" data-filter-when="period=2026-06"><h1>June</h1></section>
  <section class="slide" data-lang="en" data-filter-when="period=2026-07"><h1>July</h1></section>
  <section class="slide" data-lang="en" data-filter-when="sales_metric=quantity"><h1>Quantity view</h1></section>
  <section class="slide" data-lang="en"><h1>Shared closing page</h1></section>
  <section class="slide" data-lang="zh" data-filter-when="period=2026-06"><h1>六月</h1></section>
  <section class="slide" data-lang="zh" data-filter-when="period=2026-07"><h1>七月</h1></section>
  <section class="slide" data-lang="zh" data-filter-when="sales_metric=quantity"><h1>数量视图</h1></section>
  <section class="slide" data-lang="zh"><h1>共享收尾页</h1></section>
</div></body></html>"""

SCHEMA = {"version": 1, "filters": [
    {"key": "period", "type": "select", "width": "half", "label": {"en": "Period", "zh": "时间段"},
     "options": [{"value": "2026-06", "label": {"en": "Jun 2026", "zh": "2026年6月"}},
                 {"value": "2026-07", "label": {"en": "Jul 2026", "zh": "2026年7月"}}],
     "default": "2026-06"},
    {"key": "sales_metric", "type": "multi", "label": {"en": "Sales value", "zh": "销售值"},
     "options": ["gto", "spto", "quantity"], "default": ["gto"]},
    {"key": "price", "type": "range", "label": {"en": "Price band"}, "min": 0, "max": 20000,
     "step": 500, "unit": "RMB", "default": {"min": 3000, "max": 9000}},
    {"key": "window", "type": "date_range", "label": {"en": "Date range"},
     "min_date": "2026-01-01", "max_date": "2026-12-31",
     "default": {"from": "2026-06-01", "to": "2026-06-30"}},
]}


def _row(**over):
    base = dict(id=7, slug=SLICE_SLUG, title="Q3 slice demo", status="published",
                owner_email=OWNER, visibility="private", kind="dynamic", html=DECK,
                filters_json=json.dumps(SCHEMA, ensure_ascii=False),
                doc_type="", doc_object="", doc_mime="", doc_name="", doc_pages=None,
                has_filters=True, has_cover=False, cover_url="", pinned=False, accent="",
                summary="", summary_en="", summary_zh="", category="Analysis", tags="q3",
                author="agent", submitter="Zhen Huang", langs="en,zh", size_bytes=12000,
                views=3, source_report_id=None, public_slug="", has_colleague_shares=False,
                has_anyone_link=False, cover_w=None, cover_h=None, cover_prompt="",
                cover_model="",
                # ⚠️ Filed in 未归档: `project_slug` names the container, and a NULL
                # `folder_id` inside it means the report sits in that project's own
                # catch-all folder. These two ARE the list query's placement filters.
                project_slug=UNFILED, folder_id=None,
                created_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
                updated_at=datetime(2026, 9, 30, tzinfo=timezone.utc))
    base.update(over)
    return base


DYN_ROW = _row()
DOC_ROW = _row(id=8, slug=DOC_SLUG, title="Q3 channel review (editable)",
               kind="document", html="", filters_json="", doc_type="pptx",
               doc_object="q3-deck-file-20260930-2200-abcd1234.pptx",
               doc_mime=reports.DOC_MIME["pptx"], doc_name="Q3_Deck.pptx", doc_pages=14,
               size_bytes=2097152, has_filters=False, langs="", summary_en="The editable deck.",
               summary_zh="可编辑幻灯片。", category="Documents", tags="channel")


class _Cursor:
    """Answers the handful of statements these routes run, by shape of the SQL."""

    def __init__(self):
        self._row = None
        self._rows = None
        self.stored = None
        self.description = None

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split()).lower()
        params = params or ()
        self._row = None
        self._rows = None
        # ⚠️ The container queries come FIRST, and the reason is the SHAPE of the text,
        # not their importance: the wall's project statement names `FROM ai_reports`
        # inside its two count subqueries, so below's loose `elif "from ai_reports"`
        # would answer it with report rows. Each branch here names the table it owns.
        if "count(*) as n from ai_reports" in text:          # one folder's report count
            self._row = {"n": 2}
        elif "from ai_report_projects p where" in text:      # the wall's project list
            self._rows = [dict(row) for row in PROJECT_ROWS]
        elif "from ai_report_folders f join" in text:        # one project's folders
            self._rows = [dict(row) for row in FOLDER_ROWS]
        elif "from ai_report_projects where slug" in text:   # ensure_unfiled / list_folders
            self._row = {"slug": UNFILED, "owner_email": OWNER, "system": True}
        elif "from ai_report_folders where project_slug" in text:   # ensure_unfiled's folder
            self._row = {"id": UNFILED_FOLDER}
        elif "ai_report_filter_selections" in text and text.startswith("select"):
            self._row = None                                   # this reader has no saved view
        elif "insert into ai_report_filter_selections" in text:
            self.stored = params
            self._row = {"updated_at": datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)}
        elif "select distinct category" in text:
            self._row = None
        elif "order by pinned" in text:                    # the directory listing's query
            self._rows = [dict(DYN_ROW), dict(DOC_ROW)]
        elif "order by category" in text:                  # the category picker
            self._rows = [{"category": "Analysis"}, {"category": "Documents"}]
        elif "from ai_reports" in text:
            slug = str(params[0]) if params else ""
            self._row = dict(DOC_ROW if slug == DOC_SLUG else DYN_ROW)
        return self

    def fetchone(self):
        # ⚠️ The `_rows` fallback is not a convenience — it is what psycopg2 does.
        # `get_project` and the wall's project list run the **same** statement shape
        # (`FROM ai_report_projects p WHERE …`); the wall `fetchall()`s it and
        # `get_project` `fetchone()`s it. A branch that fills only `_rows` therefore
        # has to answer `fetchone()` too, or the folders request 404s on a project the
        # wall just drew, and the directory silently comes up empty.
        if self._row is not None:
            return self._row
        if self._rows:
            return self._rows[0]
        return None

    def fetchall(self):
        if self._rows is not None:
            return self._rows
        if self._row:
            return [self._row]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **_kwargs):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CURSOR = _Cursor()


def build_app() -> FastAPI:
    app = FastAPI()
    reports._ensure_table = lambda: None                       # no database in this harness
    reports._pg = lambda: _Conn(CURSOR)

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": OWNER}
        request.state.auth_kind = "browser"
        body = b""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            body = await request.body()
            SEEN.append((request.method, request.url.path, body.decode("utf-8", "replace")))
        return await call_next(request)

    @app.get("/api/auth/me")
    async def _me():
        return {"authenticated": True, "enabled": False, "user": {"email": OWNER, "display_name": "Zhen"}}

    @app.get("/api/settings/build-version")
    async def _version():
        return {"commit": "local", "image": "verify-dynamic"}

    @app.get("/r/{slug}", include_in_schema=False)
    async def _standalone(slug: str, request: Request, download: bool = False):
        # ⚠️ `download` MUST be passed. Called directly from Python, the handler's own
        # `Query(False)` default is a Query OBJECT — truthy — so the response comes back with
        # `Content-Disposition: attachment` and Chromium navigates the frame into a DOWNLOAD
        # (`Page.goto: Download is starting`, net::ERR_ABORTED inside an iframe). The real
        # main.py route has the same shape for the same reason.
        return await reports.render_report(slug, request, download=False)

    app.include_router(reports.router, prefix="/api/reports")

    @app.api_route("/api/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
    async def _anything(rest: str):
        SEEN.append(("unstubbed", "/api/" + rest, ""))
        return JSONResponse({})

    index = FRONTEND / "index.html"

    @app.get("/", include_in_schema=False)
    async def _index():
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="static")
    return app


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("::1", port)) == 0


def main() -> int:
    # ⚠️ A `--serve` demo left running on this port makes every check talk to THAT process:
    # its state is already warm, and the assertions about what the SPA sent are collected in
    # this process, so they fail for a reason that has nothing to do with the code (measured
    # 2026-09-30: 3 failures, all of them "the pick was not persisted").
    if _port_busy(PORT):
        print(f"port {PORT} is already serving something — stop it (`pkill -f verify_dynamic_report_ui`) "
              "or run the checks after the demo, then re-run.")
        return 2
    app = build_app()
    # ⚠️ Binds the IPv6 loopback for a reason that no longer exists. The SPA used to treat
    # any `127.0.0.1`/`localhost` hostname as the **local static preview** — reads were
    # rewritten to a shared test deployment and signed with a localStorage token, so this
    # harness would have talked to the internet instead of to itself (observed: every call
    # 401s and nothing renders). That preview bridge is gone; nothing in the SPA inspects
    # the hostname any more, so either loopback would work today. Left as-is because
    # changing it proves nothing.
    server = uvicorn.Server(uvicorn.Config(app, host="::1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    base = f"http://[::1]:{PORT}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        page.evaluate("reportsPage.openFromNav('mine')")
        # ⚠️ The wall renders `.wr-proj` cards and the `.wr-proj-new` tile, and the
        # report grid is gone from this page — so the old "a card appeared" wait is a
        # wait for a project card. If that is empty the wall never drew and every
        # assertion below would read a page that is not there.
        page.wait_for_selector("#wr-wall .wr-proj", timeout=10000)

        # ── the wall ─────────────────────────────────────────────────────────
        # ⚠️ 「我的」opens on the wall of PROJECTS (2026-10-03), and a report is a ROW
        # in `#wr-list` once its project is open. The old "the wall lists both cards"
        # is therefore two facts now, and both are asserted: the wall names the project
        # that owns the fixtures, and that project's listing holds a row for each.
        projects = page.eval_on_selector_all("#wr-wall .wr-proj", "els => els.map(e => e.dataset.proj)")
        check("the wall lists the project that owns both fixtures", projects == [UNFILED], projects)
        check("the wall offers the new-project tile",
              page.eval_on_selector_all("#wr-wall .wr-proj-new", "els => els.length") == 1)
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / "1-wall.png"))
        page.click(f'#wr-wall .wr-proj[data-proj="{UNFILED}"]')
        page.wait_for_selector("#wr-list .wr-row", timeout=10000)
        rows = page.eval_on_selector_all("#wr-list .wr-row", "els => els.map(e => e.dataset.slug)")
        check("the project's listing shows a row for each fixture",
              rows == [DYN_ROW["slug"], DOC_ROW["slug"]], rows)

        # ⚠️ A row carries ONE badge, where the card carried two (`Dynamic` + `Filters`).
        # So the "dynamic" half is asserted on `.wr-row-b` and the "has filters" half
        # moved to the row's ICON (`ri-filter-3-line` for a dynamic report) — the same
        # signal in the shape a row has. No second badge is invented here: the rail
        # assertions further down are what still prove the filters are there.
        #
        # ⚠️ Wait for the i18n walker BEFORE reading the badge. The row arrives through
        # `innerHTML` and the walker rewrites the `中文 / English` pair a moment later,
        # so an immediate read sometimes returns the raw pair — `'动态 / DYNAMIC'` next
        # to the expected `'Dynamic'` — and the check below fails on a badge that is
        # plainly correct. It reproduced on the clean baseline too, so it is a race this
        # test had, not one this change introduced; the badge text is a translated pair,
        # and reading a translated pair is only valid once the translation has run.
        page.wait_for_function(
            "(sel) => { const el = document.querySelector(sel);"
            " return el && el.innerText.indexOf(' / ') < 0; }",
            arg=f'#wr-list .wr-row[data-slug="{SLICE_SLUG}"] .wr-row-b', timeout=10000)
        dyn_badge = page.eval_on_selector(
            f'#wr-list .wr-row[data-slug="{SLICE_SLUG}"] .wr-row-b', "el => el.innerText.trim()")
        dyn_icon = page.eval_on_selector(
            f'#wr-list .wr-row[data-slug="{SLICE_SLUG}"] .wr-row-icon', "el => el.className")
        # ⚠️ Compared against the page's own translation, not against the English word:
        # the badge is a `中文 / English` pair the i18n walker rewrites, so the rendered
        # half depends on the language this browser starts in.
        dyn_label = page.evaluate("() => window.kladoI18n.t('动态 / Dynamic')")
        # ⚠️ Case-insensitive, and the reason is the measurement: `.wr-row-b` is a
        # badge and badges are `text-transform: uppercase`, so `innerText` returns
        # the RENDERED text. Comparing it to the translation byte-for-byte asserted
        # a CSS rule, not the word — the badge said Dynamic and the test called it a
        # failure. `innerText` is still the right source: `textContent` would also
        # return the language the i18n walker hid.
        check("dynamic row is badged Dynamic (in this browser's language)",
              dyn_badge.casefold() == dyn_label.casefold(), repr(dyn_badge))
        check("dynamic row's icon says it has filters (the badge the card's second half used to be)",
              "ri-filter-3-line" in dyn_icon, dyn_icon)
        # ⚠️ The old check was "shows a type icon instead of 'No cover'": it was about a
        # cover PLACEHOLDER, and a row has no cover to fall back from. The promise behind
        # it — a document is presented as a PowerPoint document, not as a missing image —
        # is now carried by the type icon ALONE, in two ways: which glyph, and what
        # colour.
        #
        # ⚠️ The `PPTX` text badge this used to assert is GONE (2026-10-04, commit 18c41ba),
        # and the second line that carried the filename went with it, so the icon went from
        # decoration to the only statement of the file's type on the row. `data-doctype`
        # is what the colour keys off, and asserting the computed colour is what stops
        # this from quietly becoming four grey glyphs again — the check that survived
        # the removal ("is the right icon class there") would have passed on a list of
        # perfectly unidentifiable grey icons.
        doc_icon = page.eval_on_selector(
            f'#wr-list .wr-row[data-slug="{DOC_SLUG}"] .wr-row-icon',
            "el => ({cls: el.className, type: el.dataset.doctype, color: getComputedStyle(el).color})")
        doc_size = page.eval_on_selector(
            f'#wr-list .wr-row[data-slug="{DOC_SLUG}"] .wr-row-size', "el => el.innerText.trim()")
        check("document row carries the document-type icon where the cover used to be",
              "ri-file-ppt-2-line" in doc_icon["cls"], repr(doc_icon))
        check("document row's icon declares its type for the colour to key off",
              doc_icon["type"] == "pptx", repr(doc_icon))
        check("document row's icon is coloured, not the inherited grey",
              doc_icon["color"] == "rgb(194, 65, 12)", repr(doc_icon))
        check("document row still states the size (it moved onto the one line, not off it)",
              doc_size == "2.0 MB", repr(doc_size))

        # ── the document row's menu is a download, not an export ─────────────
        # ⚠️ `.rpt-more` is gone with the card wall: the SAME `#rpt-menu` opens on a
        # RIGHT-CLICK of the row now (the listener is on `#wr-list`, delegated by
        # `closest('.wr-row')`), so the menu under test is unchanged and so is the
        # Escape that closes it.
        page.click(f'#wr-list .wr-row[data-slug="{DOC_SLUG}"]', button="right")
        page.wait_for_selector("#rpt-menu:not([hidden])", timeout=5000)
        # ⚠️ The menu is unhidden in the SAME synchronous block that writes its
        # `innerHTML`, so `wait_for_selector` can return while the i18n walker has not
        # rewritten `打开 / Open` into `Open` yet — and reading then yields the raw pair
        # for every item. Wait for the rewrite to land rather than sleeping a guessed
        # interval: if the walker never runs, this times out and the check below still
        # reports the raw text, so a real regression is not hidden by the wait.
        try:
            page.wait_for_function(
                """() => { const m = document.getElementById('rpt-menu');
                    return m && !m.hidden
                        && [...m.querySelectorAll('button')].length > 0
                        && ![...m.querySelectorAll('button')]
                             .some(b => b.innerText.includes(' / ')); }""",
                timeout=5000)
        except Exception:
            pass                      # fall through: the check reports what is on screen
        menu_items = page.eval_on_selector_all("#rpt-menu button", "els => els.map(e => e.innerText.trim())")
        check("the row menu is in one language (the i18n walker reached it)",
              not [m for m in menu_items if " / " in m], menu_items)
        check("document menu offers a download of the file",
              "Download .pptx" in menu_items, menu_items)
        check("document menu offers none of the report exports",
              not [m for m in menu_items if m in ("Export to PPT", "Export to PDF", "Export to Picture", "Download HTML")],
              menu_items)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        # ⚠️ `.rpt-cover` (the clickable cover image) is gone with the wall too: the row
        # itself opens the report, so this is the same action one element shallower.
        page.click(f'#wr-list .wr-row[data-slug="{DOC_SLUG}"]')
        page.wait_for_timeout(700)
        lang_picker = page.eval_on_selector("#rpt-langpick", "el => el.classList.contains('open')")
        check("a document row never asks which language to open", lang_picker is False, lang_picker)
        doc_rail_hidden = page.eval_on_selector("#rpt-filters", "el => el.hidden")
        doc_frame = page.eval_on_selector("#rpt-frame", "el => el.getAttribute('src')")
        doc_dl_title = page.eval_on_selector("#rpt-download", "el => el.title")
        doc_state = page.eval_on_selector("#rpt-viewer", "el => el.classList.contains('open')")
        check("opening a document row opens the viewer with NO filter rail",
              doc_state and doc_rail_hidden, {"open": doc_state, "rail_hidden": doc_rail_hidden})
        check("the document viewer frames the row's own address", f"/r/{DOC_SLUG}" in doc_frame, doc_frame)
        check("the document viewer's download button names the format",
              doc_dl_title == "Download .pptx", doc_dl_title)
        page.screenshot(path=str(SHOTS / "4-document.png"))
        page.evaluate("reportsPage.closeViewer()")
        page.wait_for_timeout(200)

        # ── the dynamic row: rail on the left, report on the right ──────────
        # The fixture deck is bilingual, so opening it asks which language first — that is
        # the real flow, and the rail has to appear on the other side of it. (Clicking
        # the ROW opens the report: the card's `.rpt-cover` click target is gone.)
        page.click(f'#wr-list .wr-row[data-slug="{SLICE_SLUG}"]')
        page.wait_for_timeout(300)
        picker = page.eval_on_selector("#rpt-langpick", "el => el.classList.contains('open')")
        check("a bilingual row asks for a language before opening", picker is True, picker)
        page.evaluate("reportsPage.pickLang('en')")
        page.wait_for_selector("#rpt-filters:not([hidden])", timeout=8000)
        page.wait_for_timeout(1200)
        geom = page.evaluate("""() => {
            const rail = document.getElementById('rpt-filters').getBoundingClientRect();
            const frame = document.getElementById('rpt-frame').getBoundingClientRect();
            const bar = document.querySelector('.rpt-viewer-bar').getBoundingClientRect();
            return {rail: {l: rail.left, r: rail.right, w: rail.width, t: rail.top},
                    frame: {l: frame.left}, bar: {b: bar.bottom}};
        }""")
        check("the rail sits LEFT of the report frame",
              geom["rail"]["r"] <= geom["frame"]["l"] + 1, geom)
        check("the rail sits below the viewer bar",
              geom["rail"]["t"] >= geom["bar"]["b"] - 1, geom)
        check("the rail has a sidebar width (not collapsed)",
              geom["rail"]["w"] >= 240, geom["rail"]["w"])

        controls = page.evaluate("""() => {
            const body = document.getElementById('rpt-filters-body');
            const rail = document.getElementById('rpt-filters');
            return {
              selects: [...body.querySelectorAll('select')].map(s => s.getAttribute('data-filter-key')),
              selectOptions: [...body.querySelectorAll('select option')].map(o => o.textContent.trim()),
              chips: [...body.querySelectorAll('.rpt-fchip')].map(c => c.textContent.trim()),
              chipsOn: [...body.querySelectorAll('.rpt-fchip.on')].map(c => c.textContent.trim()),
              numbers: body.querySelectorAll('input[type=number]').length,
              dates: body.querySelectorAll('input[type=date]').length,
              numbersValue: [...body.querySelectorAll('input[type=number]')].map(i => i.value),
              datesValue: [...body.querySelectorAll('input[type=date]')].map(i => i.value),
              railButtons: [...rail.querySelectorAll('button')].map(b => b.textContent.trim()),
              note: (body.querySelector('.rpt-filters-note') || {innerText: ''}).innerText,
              labels: [...body.querySelectorAll('.rpt-fl > label')].map(l => l.textContent.trim()),
              title: document.getElementById('rpt-filters-title').textContent,
            };
        }""")
        check("a select filter is rendered with its options",
              controls["selects"] == ["period"] and controls["selectOptions"] == ["Jun 2026", "Jul 2026"],
              controls["selects"] + controls["selectOptions"])
        check("a multi filter is rendered as chips with the default on",
              controls["chips"] == ["gto", "spto", "quantity"] and controls["chipsOn"] == ["gto"],
              [controls["chips"], controls["chipsOn"]])
        check("range and date_range render the values the schema declared",
              controls["numbers"] == 2 and controls["dates"] == 2
              and controls["numbersValue"] == ["3000", "9000"]
              and controls["datesValue"] == ["2026-06-01", "2026-06-30"],
              [controls["numbersValue"], controls["datesValue"]])
        check("the rail offers ONLY values + reset/toggle — nothing that edits the schema",
              controls["railButtons"] == ["gto", "spto", "quantity", "Reset"]
              or controls["railButtons"] == ["gto", "spto", "quantity", "Reset", ""],
              controls["railButtons"])
        check("the rail says the filters are not editable here",
              "not possible" in controls["note"], repr(controls["note"]))
        check("filter labels are shown", controls["labels"] == ["Period", "Sales value", "Price band", "Date range"],
              controls["labels"])

        # ── the document is read-only and carries the reader's selection ─────
        report = page.frame(url=REPORT_FRAME)
        check("the report loaded in its own frame", report is not None,
              [f.url for f in page.frames])
        def slices():
            """Every page, with BOTH hiding reasons kept apart: the filter layer adds
            `.report-filter-off` (+ `hidden`), the language layer adds `.deck-lang-off`.
            Conflating them would make a bilingual EN view look like a broken filter."""
            # The page's own title, not `textContent`: the deck runtime appends a footer
            # ("3 / 12", the document title) to every slide.
            return report.evaluate("""() => [...document.querySelectorAll('.slide')].map(s => ({
                text: (s.querySelector('h1') || {}).textContent || '',
                lang: s.getAttribute('data-lang'),
                filteredOff: s.classList.contains('report-filter-off'),
                langOff: s.classList.contains('deck-lang-off'),
            }))""")

        doc_facts = report.evaluate("""() => ({
            controls: document.querySelectorAll('select, input, textarea, [contenteditable]').length,
            runtime: !!window.reportFilters,
            selection: window.__reportFilters ? window.__reportFilters.selection : null,
            rootMarker: document.documentElement.getAttribute('data-filters'),
        })""")
        check("the report document contains NO filter control",
              doc_facts["controls"] == 0, doc_facts["controls"])
        check("the runtime is injected with the reader's saved selection",
              doc_facts["runtime"] and doc_facts["selection"]["period"] == "2026-06"
              and doc_facts["selection"]["sales_metric"] == ["gto"],
              doc_facts["selection"])
        initial = slices()
        check("the default view filters out the non-matching slices of every language",
              [s["text"] for s in initial if s["filteredOff"]]
              == ["July", "Quantity view", "七月", "数量视图"],
              [s["text"] for s in initial if s["filteredOff"]])
        check("the EN pages that remain are the ones the reader should see",
              [s["text"] for s in initial if s["lang"] == "en" and not s["filteredOff"]]
              == ["June", "Shared closing page"],
              [s["text"] for s in initial if s["lang"] == "en" and not s["filteredOff"]])
        check("the language layer still hides the other language's pages",
              all(s["langOff"] for s in initial if s["lang"] == "zh"), initial)
        check("the root carries the filter markers for CSS authors",
              doc_facts["rootMarker"] == "period=2026-06;sales_metric=gto;price=3000..9000;"
                                         "window=2026-06-01..2026-06-30", doc_facts["rootMarker"])
        page_text = page.eval_on_selector("#rpt-page", "el => el.textContent.trim()")
        check("the deck counted only the visible pages", page_text == "1 / 2", page_text)
        page.screenshot(path=str(SHOTS / "2-rail.png"))

        # ── changing the select reaches the document AND the server ─────────
        SEEN.clear()
        page.select_option('#rpt-filters-body select[data-filter-key="period"]', "2026-07")
        page.wait_for_timeout(900)
        after = slices()
        check("the document re-slices on the reader's pick",
              [s["text"] for s in after if s["lang"] == "en" and not s["filteredOff"]]
              == ["July", "Shared closing page"]
              and [s["text"] for s in after if s["filteredOff"]] == ["June", "Quantity view", "六月", "数量视图"],
              {"visible_en": [s["text"] for s in after if s["lang"] == "en" and not s["filteredOff"]],
               "filtered": [s["text"] for s in after if s["filteredOff"]]})
        saves = [entry for entry in SEEN if entry[0] == "PUT" and entry[1].endswith("/filters/selection")]
        body = json.loads(saves[0][2]) if saves else {}
        check("the pick is persisted to the real endpoint",
              len(saves) == 1 and body.get("selection", {}).get("period") == "2026-07",
              saves[0][1] + " " + saves[0][2] if saves else "no PUT seen")
        check("the debounce collapsed the interaction into one write", len(saves) == 1, len(saves))

        # ── a multi chip changes the page count ─────────────────────────────
        page.click('#rpt-filters-body .rpt-fchip:has-text("quantity")')
        page.wait_for_timeout(900)
        count_text = page.eval_on_selector("#rpt-page", "el => el.textContent.trim()")
        metrics = page.eval_on_selector_all("#rpt-filters-body .rpt-fchip.on", "els => els.map(e => e.innerText.trim())")
        check("a chip adds its slice and the page count follows",
              count_text == "1 / 3" and metrics == ["gto", "quantity"],
              {"page": count_text, "chips": metrics})
        page.screenshot(path=str(SHOTS / "3-rail-two-slices.png"))

        # ── removing the last metric leaves an empty (not defaulted) view ───
        page.click('#rpt-filters-body .rpt-fchip:has-text("gto")')
        page.wait_for_timeout(150)
        page.click('#rpt-filters-body .rpt-fchip:has-text("quantity")')
        page.wait_for_timeout(900)
        empty_save = [e for e in SEEN if e[0] == "PUT" and e[1].endswith("/filters/selection")]
        last = json.loads(empty_save[-1][2]) if empty_save else {}
        check("an explicitly empty multi selection is stored as empty (both sides agree)",
              last.get("selection", {}).get("sales_metric") == [],
              last.get("selection", {}).get("sales_metric"))

        # ── reset returns to the schema defaults ────────────────────────────
        page.click("#rpt-filters-reset")
        page.wait_for_timeout(700)
        reset_values = page.evaluate("""() => ({
            period: document.querySelector('#rpt-filters-body select').value,
            chips: [...document.querySelectorAll('#rpt-filters-body .rpt-fchip.on')].map(c => c.textContent.trim()),
        })""")
        check("Reset restores the schema defaults",
              reset_values["period"] == "2026-06" and reset_values["chips"] == ["gto"], reset_values)

        # ── the rail collapses and comes back ───────────────────────────────
        # ⚠️ Measured RELATIVE to the viewer, not against the viewport. The reader
        # used to be a full-screen overlay, so "the report takes the width" meant
        # `frame.left ≈ 0`. Inline in the directory it sits in the right pane, whose
        # left edge is wherever the shell put it — the absolute number says where the
        # PANE is, not whether the rail gave its space back. The promise is that the
        # frame moves onto the viewer's own left edge when the rail is hidden, and
        # that it comes back to where it was; that holds in either layout.
        page.click("#rpt-filters-toggle")
        page.wait_for_timeout(150)
        collapsed = page.eval_on_selector("#rpt-filters", "el => el.hidden")
        geom_open = page.evaluate("""() => {
            const v = document.getElementById('rpt-viewer').getBoundingClientRect();
            const f = document.getElementById('rpt-frame').getBoundingClientRect();
            return {offset: f.left - v.left, width: f.width};
        }""")
        page.click("#rpt-filters-toggle")
        page.wait_for_timeout(150)
        restored = page.eval_on_selector("#rpt-filters", "el => el.hidden")
        geom_back = page.evaluate("""() => {
            const v = document.getElementById('rpt-viewer').getBoundingClientRect();
            const f = document.getElementById('rpt-frame').getBoundingClientRect();
            return {offset: f.left - v.left, width: f.width};
        }""")
        check("the rail can be collapsed and the report takes the width",
              collapsed is True and restored is False
              and geom_open["offset"] <= 1.0
              and geom_open["width"] > geom_back["width"]
              and abs(geom_back["offset"] - geom_open["offset"]) > 1.0,
              {"collapsed": collapsed, "restored": restored,
               "frame_offset_when_collapsed": geom_open,
               "frame_offset_when_open": geom_back})

        page.evaluate("reportsPage.closeViewer()")
        page.wait_for_timeout(200)
        closed = page.evaluate("""() => ({open: document.getElementById('rpt-viewer').classList.contains('open'),
                                          rail: document.getElementById('rpt-filters').hidden})""")
        check("closing the viewer hides the rail again",
              closed["open"] is False and closed["rail"] is True, closed)
        check("no uncaught error in the SPA", not errors, errors)

        browser.close()

    print(f"\nscreenshots: {SHOTS}")
    print("\n--- requests the SPA made ---")
    for method, path, _body in SEEN:
        print(f"  {method:5} {path}")
    print(f"\n{len(FAILURES)} failure(s) out of the checks above")
    return 1 if FAILURES else 0


def serve() -> int:
    """`--serve`: keep the fixture-backed demo up so a human can click through it.

    The data is a fixture (two cards, one deck, one pptx) and nothing it does leaves this
    machine, so it is the cheap way to SEE the rail and the document cards without standing
    up a database.
    """
    import uvicorn
    app = build_app()
    print(f"dynamic-report demo (fixture data): http://[::1]:{SERVE_PORT}/", flush=True)
    print("stop it with Ctrl-C / kill; it never talks to test or production.", flush=True)
    uvicorn.run(app, host="::1", port=SERVE_PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(serve() if "--serve" in sys.argv else main())