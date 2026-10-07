"""The Workspace report as a STANDALONE document — the thing the Inbox preview frames.

Why this file exists: `/r/<slug>` (and the `/api/reports/{slug}/raw` handler it delegates
to) is what the Inbox preview pane, the card viewer and a shared permalink all load. A
report therefore has to arrive as the DOCUMENT — never wrapped in the SPA shell.

That is not theoretical. The same bug has been fixed twice already, in two places:

* **2026-09-30 (Inbox)** — the pane framed the app for knowledge pages, so the reader saw
  the whole website a second time inside the preview. Fixed by making the embedded app drop
  its own chrome (`html.app-embedded`).
* **2026-09-30 (the same day, after the first fix)** — hiding the global nav turned out not
  to be enough for the wiki, whose page toolbar and page list live *inside* the shell; those
  had to be dropped too.

A report must never need any of that. Its address is a bare document: if the shell ever
appears in a report's HTML, `app-embedded` will not save it — the chrome would be in the
document itself. So this asserts the property directly, on the composed document, for the
static / interactive / long-form shapes a report can take.

The document is composed by `render_report` from four steps (`_inject_base_tag`,
`_inject_report_context`, `_inline_deck_runtime`, `_inline_annotation_layer`); the database
is the only external dependency, so the connection is faked and the real composition runs.

    python3 -m unittest tests.test_report_standalone      (run from `api/`)
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers import reports

OWNER = "owner@example.com"
MATE = "mate@example.com"

DECK = ('<html><head><title>Q3</title></head><body><div class="deck">'
        '<section class="slide" data-lang="en">Page 1</section>'
        '<section class="slide" data-lang="zh">第 1 页</section>'
        '</div></body></html>')
LONG_FORM = ('<html><head><title>Notes</title></head><body>'
             '<h1 data-lang="en">Notes</h1><h1 data-lang="zh">备注</h1>'
             '</body></html>')

# Every one of these is in `frontend/out/index.html` — verified, not assumed. If the shell
# were ever wrapped around a report, at least one has to show up.
SHELL_MARKERS = ('<nav class="nav"', 'class="nav-brand"', 'NAV_TAB_FOR_PAGE',
                 'class="rpt-toolbar"', 'id="datacenter-page"', 'class="workspace-bar"',
                 'id="page-home"', 'id="page-inbox"', 'id="page-knowledge"',
                 'id="page-calendar"', 'id="inbox-split"')


class _Cursor:
    """Answers by statement: the report row, or the colleague-share probe.

    ⚠️ Returning the report row for the share probe would make EVERY caller a colleague —
    `_may_read` treats any row as "shared with you" — so the stranger case would silently
    pass as allowed and the test would be worth nothing.
    """

    def __init__(self, row, share):
        self._row = row
        self._share = share
        self._sql = ""
        self.statements = []

    def execute(self, sql, params=None):
        self._sql = str(sql)
        self.statements.append((self._sql, params))

    def fetchone(self):
        if "ai_report_colleague_shares" in self._sql:
            return self._share
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    """Enough of a psycopg2 connection for `_db()` + a RealDictCursor read.

    `_db()` commits on success and ALWAYS closes in `finally` — the close is the point of
    that helper (psycopg2's own context manager never closes), so the fake has to offer it
    or every request would 500 on the way out.
    """

    def __init__(self, row, share=None):
        self._row = row
        self._share = share
        self.cursors = []
        self.closed = False

    def cursor(self, **kwargs):
        cur = _Cursor(self._row, self._share)
        self.cursors.append(cur)
        return cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _row(**overrides):
    base = dict(id=1, slug="q3-deck", title="Q3 Channel Deep Dive", status="published",
                owner_email=OWNER, visibility="private", kind="static", html=DECK)
    base.update(overrides)
    return base


def _client() -> TestClient:
    """The reports router behind the same identity middleware shape main.py installs."""
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": request.headers.get("X-Test-User", OWNER)}
        request.state.auth_kind = request.headers.get("X-Test-Kind", "browser")
        return await call_next(request)

    app.include_router(reports.router, prefix="/api/reports")
    return TestClient(app, raise_server_exceptions=False)


def _fetch(row, user=OWNER, path="/api/reports/q3-deck/raw", share=None):
    conn = _Conn(row, share)
    with patch.object(reports, "_ensure_table", lambda: None), \
            patch.object(reports, "_pg", lambda: conn):
        return _client().get(path, headers={"X-Test-User": user})


class StandaloneDocumentTests(unittest.TestCase):
    def test_a_static_report_is_the_document_and_nothing_else(self):
        response = _fetch(_row())
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        # The deck itself arrived, base tag and runtime included.
        self.assertIn('<section class="slide"', response.text)
        self.assertIn('<base href=', response.text)
        for marker in SHELL_MARKERS:
            self.assertNotIn(marker, response.text, marker)

    def test_an_interactive_report_is_also_the_document(self):
        """Interactive reports inline their own annotation layer — still not the app."""
        response = _fetch(_row(kind="interactive", html=DECK.replace('class="slide"',
                                                                   'class="slide" data-x="1"')))
        self.assertEqual(response.status_code, 200)
        for marker in SHELL_MARKERS:
            self.assertNotIn(marker, response.text, marker)

    def test_a_long_form_report_is_also_the_document(self):
        """No deck class at all — the OTHER shape a report can take."""
        response = _fetch(_row(slug="notes", html=LONG_FORM), path="/api/reports/notes/raw")
        self.assertEqual(response.status_code, 200)
        self.assertIn("data-lang", response.text)
        for marker in SHELL_MARKERS:
            self.assertNotIn(marker, response.text, marker)

    def test_the_document_is_frameable_so_the_preview_can_show_it(self):
        """The Inbox pane and the card viewer load this in an iframe.

        `X-Frame-Options: DENY` here would blank both, and a blank pane looks like
        "the document failed to render" rather than a header choice.
        """
        response = _fetch(_row())
        self.assertNotIn(response.headers.get("x-frame-options", "").upper(), ("DENY", "SAMEORIGIN"))

    def test_a_stranger_gets_no_document_at_all(self):
        """No shell, and no document either — the read gate still runs first."""
        response = _fetch(_row(), user="stranger@example.com")
        self.assertEqual(response.status_code, 404)
        for marker in SHELL_MARKERS:
            self.assertNotIn(marker, response.text, marker)

    def test_a_public_report_owned_by_someone_else_is_a_bare_document_too(self):
        """The read gate passes for a public snapshot; the composition must not change."""
        response = _fetch(_row(visibility="public", owner_email=MATE), user=OWNER)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<section class="slide"', response.text)
        for marker in SHELL_MARKERS:
            self.assertNotIn(marker, response.text, marker)

    def test_the_marker_list_still_describes_the_real_shell(self):
        """A guard against a vacuous assertion.

        Every check above asserts these markers are ABSENT. If the shell were renamed —
        `.nav` becomes `.topbar`, `page-knowledge` becomes something else — the list would
        match nothing and all of them would pass for the wrong reason, forever.
        """
        shell = Path(__file__).resolve().parents[2] / "frontend" / "out" / "index.html"
        html = shell.read_text(encoding="utf-8")
        missing = [marker for marker in SHELL_MARKERS if marker not in html]
        self.assertEqual(missing, [], "these markers no longer exist in the shell: %s" % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)