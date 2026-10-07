"""Editing in the browser and saving it: what the reader sees, and what is sent.

    .venv312/bin/python api/tests/verify_edit_mode_ui.py

Almost nothing here can be checked by reading the source, because every claim is
about the SCREEN or about a REQUEST:

* a document opens in edit mode, with no button to press — `editMode.on` being
  true proves nothing if the text boxes are not actually typable, so the probe
  types and measures;
* "unsaved changes" has to be a real diff against the document as it was, so a
  reader who types and then puts the original text back must be told they are
  clean again. A boolean set on `input` cannot do that;
* the save request must carry the `data-oid`s the SERVER rendered. The fixture
  serves the real markup shape — `.pv-page-wrap`, `.pv-text`, `data-oid` — because
  a hand-written fake with the wrong attributes would let a mismatched oid through
  and the failure would only appear against a real file;
* after saving, the frame is reloaded from the server, so the screen has to show
  what came back rather than what this browser happened to type.

The POST is stubbed and its body captured. Driving the actual endpoint needs a
database and a session; `api/tests/test_doc_edit.py` covers that half, round-
tripping through real .docx / .pptx / .xlsx files.
"""
import functools
import http.server
import io
import json
import re
import socketserver
import sys
import threading
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
# `doc_preview` is imported from here, so the preview this file renders is the
# real renderer's output rather than a shape somebody typed to look like it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))
PORT = 8803

FAILURES = []

PAGES = 5
PAGE_TEXT = [
    "第一页 季度定价概览",
    "第二页 客户分层与折扣",
    "第三页 成本结构拆解",
    "第四页 竞品对照表",
    "第五页 下一步行动项",
]


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def _docx_bytes():
    """A real .docx, rendered by the real `doc_preview` — never a hand-written fake.

    ⚠️ This is the whole point of the rewrite. The previous fixture answered
    `/r/<slug>` with the preview markup AS the frame's own document, and it passed
    38 checks while the real app could not put a caret in a single paragraph. The
    reason is structural: `/r/<slug>` is a WRAPPER whose body is one
    `<iframe class="dp-frame">`, so the frame's `contentDocument` holds 0
    `[data-oid]` and the document one level down holds all of them. A fixture that
    flattens the two levels does not test the thing that broke.

    So this serves the two levels the way `reports._document_page_html` does, from
    bytes `docx.Document()` produced. Nothing here is invented: the page count, the
    `data-oid`s, the `.pv-page-wrap` elements and the font sizes are whatever the
    renderer really emits."""
    import docx as _docx

    d = _docx.Document()
    for i in range(PAGES):
        d.add_heading(PAGE_TEXT[i], level=2)
        d.add_paragraph("正文内容第 %d 页" % (i + 1))
        if i < PAGES - 1:
            d.add_page_break()
    # ⚠️ A run that is ALREADY bold, and a run that is 14pt blue, on the last
    # paragraph. Both exist to be checked after a save: a round trip that rebuilds
    # runs from the paragraph's default style turns them back into plain text, and
    # nothing in a screenshot or a text assertion would notice.
    tail = d.paragraphs[-1]
    keep = tail.runs[0]
    keep.bold = True
    tail.add_run(" 尾巴").italic = True
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx_bytes():
    """A workbook with two sheets, the shape the sheet-tab work is about."""
    import openpyxl
    from openpyxl.styles import Font

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Price bands"
    for col, head in enumerate(["SKU", "Band", "List", "Floor", "Change"], start=1):
        cell = sheet.cell(row=1, column=col, value=head)
        cell.font = Font(bold=True)
    for row, values in enumerate([("BUM-100", "Entry", "1,299", "999", "-3.1%"),
                                  ("BUM-220", "Standard", "2,499", "1,999", "-2.0%")], start=2):
        for col, value in enumerate(values, start=1):
            sheet.cell(row=row, column=col, value=value)
    for col, width in zip("ABCDE", (14, 12, 10, 10, 10)):
        sheet.column_dimensions[col].width = width
    second = book.create_sheet("Notes")
    second["A1"] = "owner"
    second["B1"] = "note"
    second["A2"] = "pricing"
    second["B2"] = "Floors are net of channel rebate."
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _wrapper_html(preview_path):
    """`reports._document_page_html(embed=True)`, the two-level shape, in miniature."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<title>reader</title><style>'
        'body{margin:0;background:#f1f5f9}'
        '.dp-body{padding:14px}'
        '.dp-frame{width:100%%;height:100vh;border:1px solid #e2e8f0;border-radius:10px;'
        'background:#fff}'
        '</style></head><body><div class="dp-body">'
        '<iframe class="dp-frame" src="%s" title="Preview"></iframe>'
        '<p class="dp-hint">DOCX layout, rendered in your browser.</p>'
        "</div></body></html>" % preview_path
    )


#: `/r/<slug>` → the preview the wrapper's inner iframe points at. The workbook is
#: a DIFFERENT renderer path with a different page unit (a sheet, not a page), so
#: it needs its own entry: serving an .xlsx at the .docx preview would mean the
#: thing under test is a memo, and the outline labels — which are read out of the
#: document — would be read out of the wrong file.
PREVIEWS = {"book-price-bands": "/book.html"}


def handler_factory():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith("/r/"):
                # Level 1: the wrapper. It has no `data-oid` of its own, and that is
                # the fact the whole test now exists to keep true.
                slug = path[len("/r/"):].strip("/")
                body = _wrapper_html(PREVIEWS.get(slug, "/preview.html")).encode()
            elif path == "/preview.html":
                # Level 2: the real renderer's output.
                from services import doc_preview
                body = doc_preview.preview_html(
                    _docx_bytes(), "docx", "季度定价备忘").encode("utf-8")
            elif path == "/book.html":
                from services import doc_preview
                body = doc_preview.preview_html(
                    _xlsx_bytes(), "xlsx", "价格带原始数据").encode("utf-8")
            else:
                return super().do_GET()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


def serve():
    httpd = _Server(("127.0.0.1", PORT), handler_factory())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def doc(name, doc_type="docx", status="published", slug=None, can_manage=True,
        visibility="private", editable=True):
    return {
        "id": abs(hash(name)) % 100000,
        "slug": slug or name.rsplit(".", 1)[0].replace("_", "-"),
        "title": "季度定价备忘 " + name,
        "summary": "",
        "category": "Documents",
        "tags": [],
        "status": status,
        "doc_type": doc_type,
        "doc_name": name,
        "owner_email": "me@example.com",
        "author": "agent",
        "visibility": visibility,
        "kind": "document",
        "size_bytes": 37888,
        "can_manage": can_manage,
        # ⚠️ The SERVER answers this. A client-side `doc_type === 'docx'` would be
        # a second, drifting answer to "what can be edited" — and PDFs are exactly
        # where the two would disagree.
        "editable": editable,
        "shared_with_me": False,
        "shared": False,
        "created_at": "2026-09-29T10:00:00",
        "updated_at": "2026-10-01T10:00:00",
    }


STUB = """
window.__docs = [];
window.__calls = [];
window.__saved = [];
window.__saveFails = false;
window.__dropOids = [];
window.fetch = function (url, init) {
  const u = String(url);
  window.__calls.push(u);
  const json_ = (b) => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(b)});
  if (u.indexOf('/document/content') > -1) {
    const body = JSON.parse((init && init.body) || '{}');
    window.__saved.push(body);
    if (window.__saveFails) {
      return Promise.resolve({ok: false, status: 409,
        json: () => Promise.resolve({detail: '这些修改已经对不上当前文档了 / these edits no longer match'})});
    }
    // Mimic the server: it reports which oids it could not resolve, and it does
    // NOT pretend a dropped edit was written.
    const edits = (body.edits || []).filter(e => window.__dropOids.indexOf(e.oid) === -1);
    return json_({ok: true, slug: 'q0-pricing-memo', doc_type: 'docx',
                  applied: edits.map(e => e.oid),
                  dropped: (body.edits || []).filter(e => window.__dropOids.indexOf(e.oid) > -1)
                                    .map(e => e.oid),
                  size_bytes: 40000, pages: PAGES});
  }
  if (u.indexOf('/api/reports/projects/') > -1 && u.indexOf('/folders') > -1) {
    return json_({folders: [{id: 1, name: 'Documents', system: true, count: window.__docs.length}]});
  }
  if (u.indexOf('/api/reports/projects') > -1) {
    return json_({projects: [{slug: 'pricing', name: 'Pricing', doc_count: window.__docs.length}]});
  }
  if (u.indexOf('/api/reports') === 0) {
    return json_({reports: window.__docs.slice(), count: window.__docs.length, categories: ['Documents']});
  }
  return json_({});
};
window.PAGES = %d;
// ⚠️ The frame's src is watched directly: an iframe navigation bypasses
// `window.fetch`, so the call log above cannot see a reload.
window.__frameSrcs = [];
window.__watchFrame = setInterval(() => {
  const f = document.getElementById('rpt-frame');
  if (f && window.__frameSrcs[window.__frameSrcs.length - 1] !== f.src) {
    window.__frameSrcs.push(f.src);
  }
}, 60);
""" % PAGES


def set_docs(page, docs):
    page.evaluate("(d) => { window.__docs = d; }", docs)


# ⚠️ Two levels, deliberately. The wrapper document has no `.pv-page-wrap` and no
# `[data-oid]`, so a readiness probe written against `#rpt-frame` alone waits for a
# condition that is never true — or, worse, is written against a fake and passes.
FRAME_READY = (
    "() => { const f=document.getElementById('rpt-frame');"
    " const w=f && f.contentDocument; if(!w) return false;"
    " const inner=w.querySelector('iframe.dp-frame');"
    " const d=inner && inner.contentDocument;"
    " return !!(d && d.body && d.querySelector('[data-oid]')); }")


#: Geometry of the viewer, measured in one round trip. A layout claim cannot be
#: checked by reading a stylesheet: the toolbar shipped as a 306x754 column of the
#: document while every rule describing it as a one-row bar was still in the file,
#: and the only thing that noticed was a screenshot. So the numbers that decide it
#: — where the bar ends, where the toolbar starts, how tall it is, who its parent
#: is and which way that parent stacks — are read off the rendered box.
TOOLBAR_GEOMETRY = """() => {
  const R = (sel) => { const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height)}; };
  const tools = document.getElementById('rpt-tools');
  const parent = tools ? tools.parentElement : null;
  return {
    viewer: R('.rpt-viewer'), bar: R('.rpt-viewer-bar'), tools: R('#rpt-tools'),
    main: R('.rpt-viewer-main'), frame: R('#rpt-frame'), outline: R('#rpt-outline'),
    parentClass: parent ? parent.className : null,
    parentDir: parent ? getComputedStyle(parent).flexDirection : null,
  };
}"""


def oids(page):
    """The `data-oid`s the renderer ACTUALLY minted, in document order.

    ⚠️ Read, never written out here. The renderer numbers oids by position and a
    page-break paragraph consumes a number without producing an element, so the
    list for a five-page .docx is b0 b1 b3 b4 b6 … — a formula in the test would be
    a second, wrong answer to "what is this document's oid sequence", which is
    exactly the kind of drift this file exists to catch."""
    return in_frame(page, "Array.from(doc.querySelectorAll('[data-oid]'))"
                          ".map(e => e.dataset.oid)")


def open_doc(page, docs, slug="q0-pricing-memo"):
    """Open the folder, then one document inside it.

    ⚠️ `open()` reads the item out of `_items`, and `_items` is only filled by
    opening a FOLDER — `load()` at the Workspace root fetches the project list. So
    a test that calls `open()` directly gets an empty item and every item-derived
    affordance silently disappears, which reads as a bug in the feature.
    """
    set_docs(page, docs)
    page.evaluate("() => reportsPage.openProject('pricing')")
    page.wait_for_timeout(500)
    page.evaluate("(s) => reportsPage.open(s)", slug)
    page.wait_for_function(FRAME_READY)
    page.wait_for_timeout(500)


def visible(page, sel):
    return page.evaluate(
        """(s) => { const el = document.querySelector(s);
             if (!el) return false;
             const r = el.getBoundingClientRect();
             return r.width > 0 && r.height > 0
                 && getComputedStyle(el).display !== 'none'; }""", sel)


def in_frame(page, expr):
    return page.evaluate(
        """(e) => { const f = document.getElementById('rpt-frame');
             const w = f && f.contentDocument;
             const inner = w && w.querySelector('iframe.dp-frame');
             const d = inner && inner.contentDocument;
             if (!d || !d.body) return null;
             return new Function('doc', 'win', 'return (' + e + ');')(
                 d, inner.contentWindow);
        }""", expr)


def type_into(page, oid, text):
    """Type into a box the way a reader does — through the DOM event, not by
    assigning textContent, which would never fire `input` and so would never make
    the document dirty. A test that skips this is testing nothing."""
    page.evaluate(
        """([o, t]) => { const w = document.getElementById('rpt-frame').contentDocument;
             const inner = w.querySelector('iframe.dp-frame');
             const d = inner.contentDocument;
             const el = d.querySelector('[data-oid="' + o + '"]');
             el.focus();
             el.textContent = t;
             // ⚠️ Dispatched in the DOCUMENT, not the wrapper. An `input` event does
             // not cross an iframe boundary, so raising it on the wrapper would
             // never reach the listener `applyEditMode` attached to the real
             // document — the test would type and the document would stay clean.
             el.dispatchEvent(new d.defaultView.Event('input', {bubbles: true}));
        }""", [oid, text])


def select_word(page, oid, word):
    """Select a word inside a box and press the toolbar's B button — the way a
    reader does it, so the test exercises the same path: a real selection in the
    DOCUMENT, the button's `mousedown` not stealing it, and `execCommand` run
    against the inner document rather than the host page."""
    return page.evaluate(
        """([o, word]) => {
          const w = document.getElementById('rpt-frame').contentDocument;
          const d = w.querySelector('iframe.dp-frame').contentDocument;
          const el = d.querySelector('[data-oid="' + o + '"]');
          // ⚠️ A TreeWalker, not `el.firstChild`. A heading's runs come wrapped in
          // the renderer's own `<span style="…">`, so the first child is an
          // ELEMENT and `firstChild.nodeValue` is null — which reads as a mystifying
          // "cannot read indexOf of null" several checks later instead of "that
          // block has no bare text node".
          const walker = d.createTreeWalker(el, 4 /* NodeFilter.SHOW_TEXT */);
          const node = walker.nextNode();
          if (!node) return 'NO TEXT NODE in ' + o;
          // ⚠️ No word means the WHOLE block, which is a real reader action and the
          // case the single-run payload rule exists for. It is also a much worse
          // failure than it looks if it is not handled: `indexOf` returns -1,
          // `setStart(node, -1)` throws IndexSizeError with a nonsense offset, and
          // the run dies several checks later with nothing pointing here.
          const at = word ? node.nodeValue.indexOf(word) : 0;
          const range = d.createRange();
          range.setStart(node, at);
          range.setEnd(node, word ? at + word.length : node.nodeValue.length);
          if (at < 0) return 'NOT FOUND: ' + word;
          const sel = d.defaultView.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          return sel.toString();
        }""", [oid, word])


def press_and_read(page, selector, word):
    """Select `word` in the body block, press one toolbar control, SAVE, and
    return the edit that went out.

    ⚠️ Through the real save, on purpose. Reading the payload builder directly
    would be faster and would prove less: the claim under test is "this button
    gets into the file", and the request body is the last point at which that is
    still observable in this harness (the POST is stubbed; `test_doc_edit.py`
    covers the other half by re-opening the saved bytes).

    ⚠️ The save REPLACES the document with what the server sent, so every control
    has to start from a fresh selection — that is why this is a helper and not
    one long sequence.

    ⚠️ The enabled check is not politeness, it is the witness, and it RETURNS
    rather than raises. A control whose click changes nothing leaves Save
    DISABLED, and `page.click` on a disabled button then spins for the full 30s
    timeout and raises — a red that says "the harness hung", not "this control
    did not reach the request". Found by mutation: dropping the four optional
    keys from `editRunState` left the strike button dead and the test died on a
    TimeoutError without ever naming the control.

    Returning None is safe because every call site already does `or {}` / `or
    []`, so a dead control becomes a NAMED failure on the assertion that was
    about it, and the run carries on to the next control instead of stopping."""
    oid = oids(page)[1]
    select_word(page, oid, word)
    page.click(selector)
    page.wait_for_timeout(250)
    if page.eval_on_selector("#rpt-save-btn", "el => el.disabled"):
        return None
    page.evaluate("() => { window.__saved = []; }")
    page.click("#rpt-save-btn")
    page.wait_for_timeout(750)
    return page.evaluate(
        """() => { const s = window.__saved[window.__saved.length - 1];
                   return (s && s.edits && s.edits[0]) || null; }""")


def main():
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(STUB)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.reportsPage")
            page.evaluate("() => navTo(null, 'reports', 'Workspace')")
            page.wait_for_selector("#rpt-tab-mine", state="visible")

            # ── the fixture's SHAPE, before anything is opened ────────────────
            # ⚠️ A precondition, and here because without it this file fails in the
            # least useful way possible: `open_doc` waits for a document a
            # flattened fixture can never produce, and the run dies on a 30-second
            # timeout with no sentence saying the FIXTURE was the thing wrong. The
            # bug this file was rewritten for lived in that one nesting level, so the
            # level is asserted in a sentence of its own.
            #
            # Fetched in Python, not through `window.fetch` — the stub above
            # replaces it, and its `.text()` does not exist.
            wrapper = urllib.request.urlopen(
                "http://127.0.0.1:%d/r/whatever" % PORT, timeout=5).read().decode()
            inner_src = re.search(r'<iframe class="dp-frame" src="([^"]+)"', wrapper)
            preview = urllib.request.urlopen(
                "http://127.0.0.1:%d%s" % (PORT, inner_src.group(1) if inner_src else "/"),
                timeout=5).read().decode()
            check("夹具是两层：/r/ 是 wrapper，它自己的 iframe 指向 preview.html",
                  bool(inner_src) and inner_src.group(1) == "/preview.html",
                  "wrapper iframe src=%r" % (inner_src.group(1) if inner_src else None))
            check("第二层才是文档：wrapper 里零个 data-oid，preview 里有一整份",
                  ("data-oid" not in wrapper) and ("data-oid" in preview),
                  "wrapper=%d preview=%d" % (wrapper.count("data-oid"),
                                             preview.count("data-oid")))

            # ── opening IS edit mode: no button in between ────────────────────
            open_doc(page, [doc("q0_pricing_memo.docx")])
            check("打开文档就已经在编辑态（没有中间那一步）",
                  page.evaluate("() => document.getElementById('rpt-save-btn').hidden") is False)
            check("工具栏里没有单独的 Edit 按钮了（合并进同一行）",
                  page.evaluate("() => document.querySelectorAll('#rpt-edit-btn').length") == 0)
            # ⚠️ The two-level structure, asserted first and on its own terms: the
            # editor is only correct if it is pointed at the document, and every
            # other check below is downstream of this. A version of
            # `editFrameInner()` that stopped one level early still renders a
            # perfectly good-looking viewer — it just has nothing to type into.
            check("文档在第二层 iframe 里，wrapper 自己一个 data-oid 都没有",
                  page.evaluate("""() => { const w = document.getElementById('rpt-frame').contentDocument;
                       if (!w) return null;
                       const inner = w.querySelector('iframe.dp-frame');
                       if (!inner) return null;
                       return {wrapper: w.querySelectorAll('[data-oid]').length,
                               inner: inner.contentDocument.querySelectorAll('[data-oid]').length}; }""")
                  == {"wrapper": 0, "inner": PAGES * 2},
                  page.evaluate("""() => { const w = document.getElementById('rpt-frame').contentDocument;
                       const inner = w && w.querySelector('iframe.dp-frame');
                       return {wrapper: w.querySelectorAll('[data-oid]').length,
                               inner: inner ? inner.contentDocument.querySelectorAll('[data-oid]').length : -1}; }"""))
            check("没有 Office 形状的假 ribbon 与没接线的按钮（data-dead）",
                  page.evaluate("""() => document.querySelectorAll(
                       '#rpt-ribbon, #rpt-ribbon-tabs, [data-dead]').length""") == 0)
            # ⚠️ Only what the reader can SEE. `#rpt-tostatic` and
            # `#rpt-viewer-pull` share the bar and are hidden for a document, so
            # counting every button's text would fail for the wrong reason — and a
            # check that can fail for the wrong reason is one nobody keeps.
            # ⚠️ Only what the reader can SEE, and "can see" is measured with a
            # rect + a computed `display` rather than `offsetParent`. The viewer is
            # `position: fixed`, and `offsetParent` is null for every descendant of
            # a fixed element whether or not anything is on screen — a filter that
            # returns nothing always "passes" a check that nothing may remain.
            # ⚠️ `.rpt-viewer-bar`, a class — the element has no `id`, and a check
            # built on `#rpt-viewer-bar` selects nothing and therefore passes. That
            # is the failure mode of a "nothing should remain" assertion, and it is
            # silent: no error, no empty list, just a green tick.
            seen = page.evaluate("""() => Array.from(
                    document.querySelectorAll('.rpt-viewer-bar button'))
                .filter(b => { const r = b.getBoundingClientRect();
                  return r.width > 0 && r.height > 0
                      && getComputedStyle(b).display !== 'none'; })
                .map(b => b.textContent.trim()).filter(t => t)""")
            # ⚠️ `Back` and `Save`, and nothing else. Both are real: one leaves, one
            # keeps. The five Office tab labels that used to sit here are gone, and
            # so is the inert ribbon below them.
            # ⚠️ `seen` is the BAR's own text. The toolbar is a second element with
            # icon-only buttons, and the check below asserts it separately — mixing
            # the two would let a toolbar that rendered nothing still pass.
            check("标题栏上只有真控件：Back 和 Save（没有 Office 那五个标签）",
                  seen == ["Back", "Save"], seen)
            # ⚠️ The full inventory, not "there are some". A toolbar that lost its
            # alignment group to a rename still passes every check that only asks
            # whether the buttons work — the ones that work are still there.
            check("工具栏是调研出来的那一套常用控件，一个不少",
                  page.evaluate("""() => Array.from(
                      document.querySelectorAll('#rpt-tools .rpt-tool'))
                      .map(b => b.classList.contains('rpt-tool-color') ? 'color'
                               : (b.dataset.cmd || (b.dataset.size
                                   ? 'size' + b.dataset.size : '?')))""")
                  == ["bold", "italic", "underline", "strike", "superscript",
                      "subscript", "size1", "size-1", "color",
                      "align-left", "align-center", "align-right", "undo", "redo"],
                  page.evaluate("""() => Array.from(
                      document.querySelectorAll('#rpt-tools .rpt-tool'))
                      .map(b => b.classList.contains('rpt-tool-color') ? 'color'
                               : (b.dataset.cmd || (b.dataset.size
                                   ? 'size' + b.dataset.size : '?')))"""))
            check("工具栏按钮在屏幕上看得见（有面积，不是 0x0）",
                  page.evaluate("""() => Array.from(
                      document.querySelectorAll('#rpt-tools .rpt-tool'))
                      .every(b => { const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0; })"""))
            # ⚠️ The check above is NECESSARY AND NOT SUFFICIENT, and the difference
            # is the whole bug. A button is a 30x28 box; it has an area whether or
            # not anything is drawn inside it, so a row of three EMPTY boxes passes
            # it — which is exactly what shipped: the `-line` spellings of bold /
            # italic / underline are not in this file's inline Remix block, the
            # buttons rendered with `background: none` and no glyph, and the reader
            # saw two working icons and three blank gaps. The glyph is measured
            # here, in the same place a reader looks: the `<i>`'s own box, and the
            # `:before` content the webfont draws from.
            # ⚠️ TWO ways a button can draw, and both are checked. A webfont icon
            # proves itself by its `:before` content; a letter (X², A+) proves
            # itself by having a box and some text. Probing only the first kind
            # makes the second kind look exactly like the empty box this check was
            # written to catch.
            glyphs = page.evaluate("""() => Array.from(
                document.querySelectorAll('#rpt-tools .rpt-tool')).map(b => {
                  const i = b.querySelector('i');
                  const t = b.querySelector('.rpt-tool-txt');
                  const node = i || t;
                  const r = node ? node.getBoundingClientRect() : {width: 0, height: 0};
                  return {cmd: b.dataset.cmd || b.dataset.size || 'color',
                          kind: i ? 'icon' : (t ? 'text' : 'EMPTY'),
                          label: i ? i.className : (t ? t.textContent : ''),
                          w: Math.round(r.width), h: Math.round(r.height),
                          before: i ? getComputedStyle(i, ':before').content : 'n/a'}; })""")
            blank = [g["label"] for g in glyphs
                     if g["kind"] == "EMPTY" or g["w"] == 0 or g["h"] == 0
                     or (g["kind"] == "icon" and g["before"] in ("none", "normal", '""'))]
            check("工具栏每个按钮都真的画出了东西（不是空盒子）",
                  not blank, "blank=%s all=%s" % (blank, [(g["cmd"], g["kind"],
                                                             g["label"], g["w"], g["h"])
                                                            for g in glyphs]))
            check("那个假 ribbon 确实不在页面上（不是藏起来了）",
                  page.evaluate("() => document.querySelectorAll("
                                "'#rpt-ribbon, #rpt-ribbon-tabs').length") == 0)
            check("Save 在工具栏内且没有把按钮挤出屏幕",
                  page.evaluate("""() => { const s = document.getElementById('rpt-save-btn');
                       const bar = document.querySelector('.rpt-viewer-bar');
                       if (!s || !bar || !bar.contains(s)) return false;
                       const sr = s.getBoundingClientRect(), br = bar.getBoundingClientRect();
                       return sr.width > 0 && sr.right <= br.right + 1; }"""))

            # ── the toolbar is a BAR: geometry, not existence ──────────────────
            # ⚠️ This block exists because the toolbar shipped as a COLUMN. It was
            # the first child of `.rpt-viewer-main`, which is a flex ROW (the
            # outline and the filter rail sit beside the frame in there), so a row
            # did not stack it — it became a third column of the document. Every
            # assertion above still passed: the element was there, `hidden` was
            # false, the buttons had area. What was wrong was the box's SIZE and
            # POSITION, which is the only thing that could have told anyone.
            # Measured then: 306x754, its own `align-items: center` floating five
            # buttons in the middle of an empty white column, the document squeezed
            # from 1008 to 702. Measured now: 1216x39, directly under the bar.
            geo = page.evaluate(TOOLBAR_GEOMETRY)
            check("工具栏是贴在标题栏下面的一行，不是文档旁边的一根竖条",
                  geo["tools"] and geo["bar"] and geo["viewer"]
                  and abs(geo["tools"]["y"] - (geo["bar"]["y"] + geo["bar"]["h"])) <= 1
                  and geo["tools"]["h"] <= 60,
                  geo)
            check("工具栏横跨整个 viewer 宽度（把整行都还给了读者）",
                  geo["tools"] and geo["viewer"]
                  and geo["tools"]["x"] == geo["viewer"]["x"]
                  and geo["tools"]["w"] == geo["viewer"]["w"],
                  geo)
            check("工具栏是 .rpt-viewer 的列孩子，不是 .rpt-viewer-main 的行孩子",
                  geo["parentDir"] == "column" and "rpt-viewer" in geo["parentClass"]
                  and "rpt-viewer-main" not in geo["parentClass"],
                  "parent=%r dir=%s" % (geo["parentClass"], geo["parentDir"]))
            check("文档紧跟在目录面板右边（工具栏不在它左边占掉一列）",
                  geo["frame"] and geo["tools"] and geo["outline"] and geo["main"]
                  and geo["frame"]["y"] >= geo["tools"]["y"] + geo["tools"]["h"] - 1
                  and geo["frame"]["x"] == geo["outline"]["x"] + geo["outline"]["w"]
                  and geo["frame"]["w"] == geo["main"]["w"] - geo["outline"]["w"],
                  geo)

            # ── the boxes are really typable ─────────────────────────────────
            live = oids(page)
            O1, O3 = live[0], live[4]
            check("渲染出来的 oid 数量 = 真实块数（每页标题 + 正文）",
                  len(live) == PAGES * 2, live)
            editable = in_frame(page, "doc.querySelectorAll('[data-oid][contenteditable=\"true\"]').length")
            check("每个带 data-oid 的块都真的可输入", editable == PAGES * 2, editable)
            check("编辑态的框线样式真的作用到了 iframe 内（app.css 跨不过 iframe）",
                  in_frame(page, "getComputedStyle(doc.querySelector('[data-oid]')).outlineStyle")
                  not in (None, "none"),
                  in_frame(page, "getComputedStyle(doc.querySelector('[data-oid]')).outlineStyle"))
            check("未改动时 Save 可见但不可点（读者一眼看到「有地方存」）",
                  visible(page, "#rpt-save-btn")
                  and page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is True)
            check("未改动时不显示未保存提示",
                  not visible(page, "#rpt-dirty"))

            # ── the outline is still the document's own data ──────────────────
            check("页面目录行数 = 文档真实页数",
                  page.evaluate("() => document.querySelectorAll('#rpt-outline-list .rpt-ol').length") == PAGES)
            check("目录标签是该页真实首段文字",
                  page.evaluate("""() => Array.from(document.querySelectorAll('#rpt-outline-list .rpt-ol-t'))
                      .map(e => e.textContent)""") == PAGE_TEXT)

            # ── dirtiness is a DIFF, not a flag ───────────────────────────────
            type_into(page, O1, "改过的第一页标题")
            page.wait_for_timeout(200)
            check("改过之后 Save 可点",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is False)
            check("改过之后显示未保存提示", visible(page, "#rpt-dirty"))
            check("改过的块在文档里被标出来",
                  in_frame(page, "!!doc.querySelector('[data-oid=\"b0\"]').classList.contains('pv-dirty')"))
            check("没改过的块没有被标出来",
                  in_frame(page, "!doc.querySelector('[data-oid=\"b1\"]').classList.contains('pv-dirty')"))
            # ⚠️ The one a boolean set on `input` cannot pass.
            type_into(page, O1, PAGE_TEXT[0])
            page.wait_for_timeout(200)
            check("把文字改回原样后，Save 重新不可点（是比对，不是打标记）",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is True)
            check("把文字改回原样后，未保存提示消失", not visible(page, "#rpt-dirty"))

            # ── the save carries the server's oids ────────────────────────────
            type_into(page, O1, "改过的第一页标题")
            type_into(page, O3, "改过的第三页正文")
            page.wait_for_timeout(200)
            page.click("#rpt-save-btn")
            page.wait_for_function("() => window.__saved.length === 1", timeout=15000)
            page.wait_for_timeout(900)
            sent = page.evaluate("() => window.__saved[0]")
            check("保存请求发到了正确的端点",
          any("/document/content" in c for c in page.evaluate("() => window.__calls")),
                  [c for c in page.evaluate("() => window.__calls") if "content" in c])
            check("只发送改动过的块（不是整份文档）",
                  sorted(e["oid"] for e in sent["edits"]) == sorted([O1, O3]),
                  [e["oid"] for e in sent["edits"]])
            check("发送的是服务端给的 oid，不是渲染下标",
                  all(e["oid"].startswith("b") for e in sent["edits"]), sent["edits"])
            check("发送的文本是读者输入的内容",
                  dict((e["oid"], e["text"]) for e in sent["edits"])
                  == {O1: "改过的第一页标题", O3: "改过的第三页正文"},
                  sent["edits"])
            # ⚠️ Measured on the frame's own `src`, not on the fetch log. An iframe
            # navigation never goes through `window.fetch`, so the stub's call log
            # cannot see it — the first version of this check watched the log,
            # counted zero, and would have "passed" a save that reloaded nothing.
            check("保存后 frame 真的从服务端重新载入（src 换了）",
                  page.evaluate("() => window.__frameSrcs.length >= 2")
                  and page.evaluate("() => window.__frameSrcs[0] !== window.__frameSrcs[1]"),
                  page.evaluate("() => window.__frameSrcs"))
            check("保存后回到「没有未保存修改」",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is True
                  and not visible(page, "#rpt-dirty"))
            check("保存后仍在编辑态（不用重新进入就能继续改）",
                  in_frame(page, "doc.querySelectorAll('[data-oid][contenteditable=\"true\"]').length")
                  == PAGES * 2)
            check("保存后 baseline 被刷新（再改一次不会把旧内容发回去）",
                  page.evaluate("""(o) => { const w = document.getElementById('rpt-frame').contentDocument;
                       const d = w.querySelector('iframe.dp-frame').contentDocument;
                       return d.querySelector('[data-oid="' + o + '"]').textContent.trim(); }""",
                                O1)
                  == PAGE_TEXT[0],
                  "fixture 重载后回到原文字，属预期")

            # ── the toolbar's promise: B on a selection, and it SURVIVES ──────
            # ⚠️ The whole toolbar exists so that pressing B is not undone by
            # pressing Save. So the assertion has to be the REQUEST, not the
            # screen: the screen shows bold the moment the button is pressed, with
            # or without a working save. `runs` on the wire is the only thing that
            # proves the button and the file are connected.
            open_doc(page, [doc("q0_pricing_memo.docx")])
            # ⚠️ The PLAIN paragraph, not the heading. A heading is already bold in
            # the preview, so pressing B there turns bold OFF — which this editor
            # cannot express back into the file (see `apply_edits`), and a test that
            # used a heading would be asserting the wrong direction and passing or
            # failing for the wrong reason. "Bold a word in body text" is the case
            # the button is for.
            O1 = oids(page)[0]
            O2 = oids(page)[1]
            picked = select_word(page, O2, "正文")
            page.click('#rpt-tools .rpt-tool[data-cmd="bold"]')
            page.wait_for_timeout(300)
            check("按 B 之后按钮亮起来（它是选区状态的回显）",
                  page.evaluate("""() => document.querySelector(
                      '#rpt-tools .rpt-tool[data-cmd="bold"]').classList.contains('on')"""),
                  page.evaluate("""() => Array.from(
                      document.querySelectorAll('#rpt-tools .rpt-tool'))
                      .map(b => b.dataset.cmd + ':' + (b.classList.contains('on') ? 'on' : 'off'))"""))
            check("只加粗不改字也算「有未保存的改动」",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is False,
                  "a bold with no new characters is still an edit")
            check("未保存提示也亮了",
                  visible(page, "#rpt-dirty"))
            page.evaluate("() => { window.__saved = []; }")
            page.click("#rpt-save-btn")
            page.wait_for_timeout(700)
            sent = page.evaluate("() => (window.__saved[window.__saved.length - 1] || {}).edits || []")
            check("保存请求带的是 runs（不是一个光秃秃的 text）",
                  sent and all("runs" in e for e in sent),
                  sent)
            # ⚠️ TWO runs, not three: 正文 is the first two characters, so there is
            # nothing before it. An assertion written as "plain, bold, plain"
            # would have been a test of my own imagination of where the word sits.
            check("加粗的那一段 runs 里 b=True，其余 b=False",
                  sent and len(sent) == 1 and [r["b"] for r in sent[0]["runs"]] == [True, False]
                  and sent[0]["runs"][0]["text"] == "正文",
                  sent[0]["runs"] if sent else None)
            check("加粗的是读者选中的那几个字，不多不少",
                  sent and "".join(r["text"] for r in sent[0]["runs"] if r["b"]) == "正文",
                  [r["text"] for r in sent[0]["runs"]] if sent else None)
            check("runs 拼起来等于正文（服务端会拒绝两者不一致的载荷）",
                  sent and "".join(r["text"] for r in sent[0]["runs"]) == sent[0]["text"],
                  sent[0]["text"] if sent else None)
            # …and undo is honest: it puts the document back to clean.
            open_doc(page, [doc("q0_pricing_memo.docx")])
            O2 = oids(page)[1]
            select_word(page, O2, "正文")
            page.click('#rpt-tools .rpt-tool[data-cmd="bold"]')
            page.wait_for_timeout(250)
            page.click('#rpt-tools .rpt-tool[data-cmd="undo"]')
            page.wait_for_timeout(400)
            check("撤销之后文档回到干净（否则撤销就是假的）",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is True
                  and not visible(page, "#rpt-dirty"),
                  "save disabled=%s dirty visible=%s"
                  % (page.evaluate("() => document.getElementById('rpt-save-btn').disabled"),
                     visible(page, "#rpt-dirty")))
            check("撤销之后文字一个字都没少",
                  in_frame(page, "doc.querySelectorAll('[data-oid]')[1].textContent")
                  .startswith("正文内容第 1 页"),
                  in_frame(page, "doc.querySelectorAll('[data-oid]')[1].textContent"))

            # ── a save the server refuses must not look like a save ───────────
            page.evaluate("() => { window.__saveFails = true; }")
            type_into(page, live[1], "这次会失败")
            page.wait_for_timeout(200)
            page.click("#rpt-save-btn")
            page.wait_for_timeout(900)
            check("保存失败时给出提示（不是静默）",
                  page.evaluate("() => document.querySelectorAll('.kld-toast, .toast, [class*=toast]').length") > 0)
            check("保存失败后改动仍在（没有假装成功）",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is False)
            page.evaluate("() => { window.__saveFails = false; }")

            # ── a partially-applied save says so ─────────────────────────────
            page.evaluate("(o) => { window.__dropOids = [o]; }", O3)
            type_into(page, O3, "只改这一处")
            page.wait_for_timeout(200)
            page.click("#rpt-save-btn")
            page.wait_for_function("() => window.__saved.length === 3", timeout=15000)
            page.wait_for_timeout(700)
            check("部分失败时仍然保存成功的部分（不整批丢弃）",
                  len(page.evaluate("() => window.__saved[2].edits")) >= 1)

            # ── every NEW control, pressed for real and read out of the request ──
            # ⚠️ From a FRESH document, and with the drop list from the previous
            # block cleared. Both because a block that inherits the state the one
            # before it happened to leave behind is a test of that state: the
            # partial-save block above leaves `__dropOids` armed, and a payload
            # assertion that silently reads a DROPPED edit proves nothing.
            page.evaluate("() => { window.__dropOids = []; }")
            open_doc(page, [doc("q0_pricing_memo.docx")])
            # ⚠️ One control per save, on a document the previous save has already
            # replaced. The claim is not "the button changes the DOM" — that is
            # what a screenshot shows — it is "the attribute the reader asked for
            # is IN THE REQUEST", because an attribute drawn on screen and absent
            # from the payload is the exact failure this editor was rebuilt to
            # stop making.
            for name, selector, key, want in [
                ("删除线", '#rpt-tools .rpt-tool[data-cmd="strike"]', "s", True),
                ("上标", '#rpt-tools .rpt-tool[data-cmd="superscript"]', "va", "sup"),
                ("下标", '#rpt-tools .rpt-tool[data-cmd="subscript"]', "va", "sub"),
            ]:
                runs = (press_and_read(page, selector, "正文") or {}).get("runs") or []
                hit = [r for r in runs if r.get(key) == want]
                check("%s 进到了保存请求里（就是选中的那几个字）" % name,
                      bool(runs) and bool(hit)
                      and "".join(r["text"] for r in hit) == "正文", runs)

            def runs_of(selector):
                return (press_and_read(page, selector, "正文") or {}).get("runs") or []

            runs = runs_of('#rpt-tools .rpt-tool[data-size="1"]')
            sized = [r for r in runs if "sz" in r]
            check("增大字号把磅值写进了请求",
                  bool(sized) and "".join(r["text"] for r in sized) == "正文"
                  and all(isinstance(r["sz"], (int, float)) and r["sz"] > 0
                          for r in sized), runs)
            check("字号只落在读者选中的那几个字上",
                  all("sz" not in r for r in runs if r["text"] != "正文"), runs)
            check("没选中的 run 连这个键都没有（不是带了个 null）",
                  all("sz" not in r for r in runs if r["text"] != "正文"), runs)
            smaller = runs_of('#rpt-tools .rpt-tool[data-size="-1"]')
            check("缩小字号也写得进去（不是只有增大有用）",
                  any(isinstance(r.get("sz"), (int, float)) for r in smaller), smaller)

            # The palette: opened by a real click, and a colour chosen from it.
            # ⚠️ The selection is made HERE, not carried over from the step above:
            # every one of those steps ends in a save, and a save replaces the
            # document — so the selection a previous step used is gone, and a
            # colour button pressed without one is a button that correctly does
            # nothing. Carrying it over would have tested the wrong thing and
            # blamed the app for it.
            select_word(page, oids(page)[1], "正文")
            page.click('#rpt-tools .rpt-tool-color')
            page.wait_for_timeout(250)
            swatches = page.evaluate("""() => Array.from(
                document.querySelectorAll('#rpt-swatches .rpt-swatch')).map(b => {
                  const r = b.getBoundingClientRect();
                  return {hex: b.dataset.hex, w: Math.round(r.width),
                          h: Math.round(r.height)}; })""")
            check("点颜色按钮打开调色板，八个色块都画出来了（不是 0x0）",
                  len(swatches) == 8 and all(s["w"] > 0 and s["h"] > 0 for s in swatches),
                  swatches)
            # ⚠️ White is deliberately absent: it is the one colour that is invisible
            # on the page the text lands on, and offering it would be offering a
            # way to make a sentence disappear.
            check("调色板里没有纯白",
                  all((s["hex"] or "").upper() != "FFFFFF" for s in swatches), swatches)
            page.click('#rpt-swatches .rpt-swatch[data-hex="C0392B"]')
            page.wait_for_timeout(250)
            check("选完颜色调色板自己关掉（它盖在文档上）",
                  page.evaluate("() => document.getElementById('rpt-swatches').hidden") is True,
                  page.evaluate("() => document.getElementById('rpt-swatches').hidden"))
            # ⚠️ Saved straight from there: the colour went onto the selection the
            # reader made BEFORE opening the palette, and the panel lives inside
            # the bar so its `mousedown` is prevented too — the selection is
            # still the one they made. Re-selecting here would be a different
            # edit, and pressing another control first would save this one away.
            page.evaluate("() => { window.__saved = []; }")
            page.click("#rpt-save-btn")
            page.wait_for_timeout(750)
            runs = (page.evaluate("""() => { const s = window.__saved[window.__saved.length - 1];
                                         return (s && s.edits && s.edits[0]) || null; }""")
                    or {}).get("runs") or []
            check("选过的那个颜色进了保存请求，而且只在那几个字上",
                  any(r.get("color") == "C0392B" for r in runs)
                  and all("color" not in r for r in runs if r["text"] != "正文"), runs)

            for name, want in [("居中", "center"), ("右对齐", "right")]:
                edit = press_and_read(page,
                                      '#rpt-tools .rpt-tool[data-cmd="align-%s"]' % want,
                                      "正文")
                check("%s 把对齐写进了请求" % name, (edit or {}).get("align") == want, edit)

            # ⚠️ And the direction that is EASY to get wrong: left-aligning a
            # paragraph that is already left-aligned must NOT report a change. The
            # document came back from the server in the file's own alignment, so
            # pressing the button it already has changes nothing — and a toolbar
            # that invents an edit there would teach the reader to distrust the
            # dirty light, which is the one signal they cannot verify themselves.
            select_word(page, oids(page)[1], "正文")
            page.click('#rpt-tools .rpt-tool[data-cmd="align-left"]')
            page.wait_for_timeout(300)
            check("按了段落本来就是的那个对齐，Save 不会亮（没有凭空造出改动）",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is True
                  and not visible(page, "#rpt-dirty"),
                  "save disabled=%s" % page.evaluate(
                      "() => document.getElementById('rpt-save-btn').disabled"))
            # …and once there IS something to save, the alignment rides along with
            # it, because the two are the same edit as far as the file is concerned.
            type_into(page, oids(page)[1], "左对齐之后又改了字")
            page.click('#rpt-tools .rpt-tool[data-cmd="align-left"]')
            page.wait_for_timeout(200)
            page.evaluate("() => { window.__saved = []; }")
            page.click("#rpt-save-btn")
            page.wait_for_timeout(700)
            both = page.evaluate("""() => { const s = window.__saved[window.__saved.length - 1];
                                     return (s && s.edits && s.edits[0]) || null; }""")
            check("有别的改动时，对齐跟着那一条一起发出去（不单独占一条）",
                  bool(both) and (both.get("align") in (None, "left"))
                  and len(page.evaluate("() => window.__saved[window.__saved.length - 1].edits")) == 1,
                  both)

            # ⚠️ The case that made B a no-op on a whole paragraph, and the reason
            # the rule is "more than one run OR the run carries formatting" rather
            # than "more than one run": selecting a whole plain paragraph reads
            # back as ONE bold run, and the old length test dropped it — so the
            # reader watched their own bold disappear on save.
            whole = press_and_read(page, '#rpt-tools .rpt-tool[data-cmd="bold"]', None)
            runs = (whole or {}).get("runs") or []
            check("整段加粗也写得进去（单个 run 但有格式，不是 null）",
                  len(runs) == 1 and runs[0].get("b") is True, whole)

            # ⚠️ And the opposite claim, which is the one the whole `touched` map
            # exists for: a plain text edit must not drag the document's INHERITED
            # size and colour into the file. Every run here reads a concrete
            # inherited value out of the browser, and none of it may be sent.
            type_into(page, oids(page)[1], "改过的正文")
            page.evaluate("() => { window.__saved = []; }")
            page.click("#rpt-save-btn")
            page.wait_for_timeout(750)
            plain = page.evaluate("""() => { const s = window.__saved[window.__saved.length - 1];
                                       return (s && s.edits && s.edits[0]) || null; }""")
            leaked = [k for r in ((plain or {}).get("runs") or [])
                      for k in ("sz", "color") if k in r]
            check("只改文字时，请求里没有 size / color 这两个键",
                  bool(plain) and "align" not in plain and not leaked, plain)

            # ── formats the server says cannot be edited ──────────────────────
            open_doc(page, [doc("terms.pdf", doc_type="pdf", slug="terms",
                                editable=False)], "terms")
            check("PDF 没有 Save（服务端说不可编辑，前端就不给）",
                  not visible(page, "#rpt-save-btn"))
            check("PDF 不显示 ribbon 标签（工具栏退回普通样子）",
                  not visible(page, "#rpt-ribbon-tabs"))
            # ⚠️ Not "the document has no data-oid": the fixture serves a docx-shaped
            # page for every slug, because building a real PDF preview per test
            # would test the fixture rather than the client. What the CLIENT owes
            # us is that it did not make those blocks typable — and the server
            # refuses the save even if it did.
            check("PDF 的文字不可输入（客户端没开 contenteditable）",
                  in_frame(page, "doc.querySelectorAll('[data-oid][contenteditable]').length") == 0,
                  in_frame(page, "doc.querySelectorAll('[data-oid]').length"))
            check("PDF 的正文没有 pv-editing 标记",
                  in_frame(page, "!doc.body.classList.contains('pv-editing')"))
            open_doc(page, [doc("borrowed.docx", slug="borrowed", can_manage=False,
                                visibility="shared", editable=False)], "borrowed")
            check("借来的文档没有 Save", not visible(page, "#rpt-save-btn"))
            open_doc(page, [doc("q1_report.html", doc_type=None, slug="q1-report",
                                editable=False)], "q1-report")
            check("报告没有 Save（没有文件可回写）", not visible(page, "#rpt-save-btn"))

            # ── the panel collapses; the document does not stop being editable ──
            # ⚠️ This block replaced the old "leave edit mode asks first" one, and
            # the reason it could go is the reason it HAD to: a document opens
            # editable and stays editable, so there is no mode to leave and nothing
            # to confirm. The panel's × is a layout control now, and a layout
            # control that silently discarded the reader's typing would be data
            # loss dressed as a window control.
            open_doc(page, [doc("q0_pricing_memo.docx")])
            O1 = oids(page)[0]
            type_into(page, O1, "没保存就走的改动")
            page.wait_for_timeout(200)
            page.click(".rpt-outline-x")
            page.wait_for_timeout(400)
            check("面板的 × 收起的是面板，不是编辑态（也没有弹确认框）",
                  not visible(page, "#rpt-outline") and not visible(page, "#kld-dlg"),
                  "outline=%s dialog=%s" % (visible(page, "#rpt-outline"),
                                            visible(page, "#kld-dlg")))
            check("收起面板没有丢掉未保存的改动（Save 仍可点）",
                  page.evaluate("() => document.getElementById('rpt-save-btn').disabled") is False)
            check("收起面板后文字仍可输入（editing 标记没被摘掉）",
                  in_frame(page, "doc.querySelectorAll('[data-oid][contenteditable=\"true\"]').length")
                  == PAGES * 2,
                  in_frame(page, "doc.querySelectorAll('[data-oid][contenteditable=\"true\"]').length"))
            check("收起后未保存提示仍在（读者知道自己还没存）", visible(page, "#rpt-dirty"))
            check("工具栏上有按钮能把面板再打开（收起了回不去就是死路）",
                  page.evaluate("() => { const b = document.getElementById('rpt-outline-toggle');"
                                " return !!b && b.offsetParent !== null; }"))
            page.click("#rpt-outline-toggle")
            page.wait_for_timeout(400)
            check("再点一次面板回来了，目录还是这份文档的",
                  visible(page, "#rpt-outline")
                  and page.evaluate("() => document.querySelectorAll('#rpt-outline-list .rpt-ol').length")
                      == PAGES,
                  page.evaluate("() => { const p = document.getElementById('rpt-outline');"
                                " return p ? (p.hidden ? 'hidden' : 'shown') : 'absent'; }"))
            check("面板回来时 aria-expanded 也跟着变（读屏知道开没开）",
                  page.evaluate("() => document.getElementById('rpt-outline-toggle')"
                                ".getAttribute('aria-expanded')") == "true")

            # ── a workbook is a page per sheet, laid out from the top left ────
            # ⚠️ Measured, in the real two-level frame, at a real window size. This
            # is a layout change and layout claims are exactly the ones a source
            # read cannot check: `align-items: center` looks right in the CSS and
            # wrong on screen, and a narrow sheet centred on a wide pane still
            # passes anything that only asserts "there is a grid".
            # ⚠️ The frame is re-pointed straight at the preview route. Going
            # through `reportsPage.open()` would need a second document card, and
            # the thing under test is the WORKBOOK's layout, not the Workspace's
            # navigation.
            page.evaluate("""async () => {
              const f = document.getElementById('rpt-frame');
              f.src = '/r/settle.docx';
              await new Promise(r => setTimeout(r, 50));
              f.src = '/book.html';
            }""")
            page.wait_for_timeout(900)
            # ⚠️ "Visible" here means THE READER CAN SEE IT — measured as a rendered
            # box, never as the `hidden` attribute. `.pv-sheet-block` sets
            # `display: flex`, which beats the browser's own
            # `[hidden] { display: none }`, so a sheet can carry `hidden` and be on
            # screen at the same time. A check that read the attribute would report
            # one sheet while the reader is looking at two — which is exactly what
            # deleting the `[hidden]` rule below does, and it passed until this.
            page.evaluate("""() => {
              window.ON_SCREEN = function (d) {
                return {
                  visible: Array.from(d.querySelectorAll('.pv-sheet-block'))
                    .filter(b => { const r = b.getBoundingClientRect();
                                   return r.width > 0 && r.height > 0; })
                    .map(b => b.querySelector('.pv-caption').textContent),
                  active: Array.from(d.querySelectorAll('.pv-tab'))
                    .filter(t => t.classList.contains('is-on'))
                    .map(t => t.textContent)};
              };
            }""")
            book = page.evaluate("""() => {
              const d = document.getElementById('rpt-frame').contentDocument;
              const blocks = Array.from(d.querySelectorAll('.pv-sheet-block'));
              return {
                innerW: d.documentElement.clientWidth,
                overflow: d.documentElement.scrollWidth > d.documentElement.clientWidth + 1,
                sheets: blocks.map(b => {
                  const grid = b.querySelector('.pv-grid').getBoundingClientRect();
                  const table = b.querySelector('.pv-cells').getBoundingClientRect();
                  return {name: b.querySelector('.pv-caption').textContent,
                          left: Math.round(grid.left),
                          top: Math.round(b.getBoundingClientRect().top),
                          gridW: Math.round(grid.width),
                          tableW: Math.round(table.width)};
                }),
                tabs: Array.from(d.querySelectorAll('.pv-tab')).map(t => t.textContent),
              };
            }""")
            check("工作簿里每张 sheet 都在（两张，没丢）",
                  len(book["sheets"]) == 2, book["sheets"])
            check("屏上只显示当前这一张 sheet（和 Excel 打开一样，不是全部堆着）",
                  page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
                    return window.ON_SCREEN(d); }""")
                  == {"visible": ["Price bands"], "active": ["Price bands"]},
                  page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
                    return window.ON_SCREEN(d); }"""))
            # Clicking a tab is the whole interaction, so it is clicked, not asserted
            # in the abstract: a tab strip that is a picture of a control passes any
            # check about its existence.
            page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
              d.querySelectorAll('.pv-tab')[1].dispatchEvent(new MouseEvent('click', {bubbles: true})); }""")
            page.wait_for_timeout(400)
            after = page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
                    return window.ON_SCREEN(d); }""")
            check("点第二个标签，切到第二张 sheet，高亮跟着走",
                  after == {"visible": ["Notes"], "active": ["Notes"]}, after)
            book2 = page.evaluate("""() => {
              const d = document.getElementById('rpt-frame').contentDocument;
              const b = Array.from(d.querySelectorAll('.pv-sheet-block'))
                             .filter(x => { const r = x.getBoundingClientRect();
                             return r.width > 0 && r.height > 0; })[0];
              const grid = b.querySelector('.pv-grid').getBoundingClientRect();
              const table = b.querySelector('.pv-cells').getBoundingClientRect();
              return {left: Math.round(grid.left), top: Math.round(b.getBoundingClientRect().top),
                      gridW: Math.round(grid.width), tableW: Math.round(table.width),
                      innerW: d.documentElement.clientWidth,
                      overflow: d.documentElement.scrollWidth > d.documentElement.clientWidth + 1};
            }""")
            check("当前 sheet 从左上角开始排（left=0），不是居中的窄卡片",
                  book2["left"] == 0, book2)
            check("网格铺满面板宽度（不是 332px 的窄条飘在宽屏中间）",
                  abs(book2["gridW"] - book2["innerW"]) <= 2, book2)
            check("表格和网格一样宽（列被拉开填满）",
                  book2["tableW"] == book2["gridW"], book2)
            check("铺满之后没有横向溢出（Excel 是让网格内部滚动，不是把窗口撑宽）",
                  book2["overflow"] is False, book2)
            # ⚠️ Compared against the sheet that IS on screen, not against a literal.
            # This used to assert `gridW == 702`, which is the width the document had
            # while the toolbar was eating 306px of it as a column — so the check was
            # pinning the width the bug produced, and would have failed the moment the
            # toolbar was given back its row. "Same rule as the sheet you can see" is
            # the claim; the number is whatever the window makes it.
            check("另一张 sheet 也在同一套排版规则下（left=0，铺满）",
                  page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
                    const b = Array.from(d.querySelectorAll('.pv-sheet-block'))[1];
                    const g = b.querySelector('.pv-grid').getBoundingClientRect();
                    return {left: Math.round(g.left), gridW: Math.round(g.width),
                            innerW: d.documentElement.clientWidth}; }""")
                  == {"left": 0, "gridW": book2["gridW"], "innerW": book2["innerW"]},
                  page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
                    const b = Array.from(d.querySelectorAll('.pv-sheet-block'))[1];
                    const g = b.querySelector('.pv-grid').getBoundingClientRect();
                    return {left: Math.round(g.left), gridW: Math.round(g.width),
                            innerW: d.documentElement.clientWidth}; }"""))
            check("工作表标签还是两张（表名一个不少）",
                  book["tabs"] == ["Price bands", "Notes"], book["tabs"])

            # ── but PRINT still gets every sheet ──────────────────────────────
            # ⚠️ Print media, because a rule that only applies when printing cannot
            # be checked by a test that never prints. One sheet on screen is right;
            # one sheet in the PDF would mean a reader silently loses two thirds of
            # the workbook the moment they print it.
            page.emulate_media(media="print")
            page.wait_for_timeout(300)
            printed = page.evaluate("""() => { const d = document.getElementById('rpt-frame').contentDocument;
              return Array.from(d.querySelectorAll('.pv-sheet-block'))
                .filter(b => { const r = b.getBoundingClientRect();
                               return r.width > 0 && r.height > 0; }).length; }""")
            check("打印时所有 sheet 都在（屏幕上只显示一张，不等于打印也只剩一张）",
                  printed == 2, printed)
            page.emulate_media(media="screen")
            page.wait_for_timeout(200)

            # ── a workbook's OUTLINE, through the real two-level path ──────────
            # ⚠️ Opened with `open_doc`, not by re-pointing the frame. The frame
            # above was aimed straight at the preview, and `editFrameInner()`
            # returns null for a document with no `iframe.dp-frame` inside it — so
            # edit mode was never ON for that layout work, and an outline is only
            # built when it is. Pointing the frame by hand and then asserting on
            # the outline would have measured the .docx's leftover rows.
            book_doc = doc("book_price_bands.xlsx", doc_type="xlsx", slug="book-price-bands")
            open_doc(page, [doc("q0_pricing_memo.docx"), book_doc],
                     slug="book-price-bands")
            rows = page.evaluate("""() => Array.from(
                document.querySelectorAll('#rpt-outline-list .rpt-ol'))
                .map(r => r.querySelector('.rpt-ol-t').textContent.trim())""")
            # ⚠️ The sheet NAMES, not the first cell. The tab strip at the bottom
            # of the document already says "Price bands" and "Notes"; a panel beside
            # it that said the first cell of each sheet gave the reader the same
            # three things under two different labels with nothing joining them up.
            check("工作簿的目录行用的是工作表名（和底部标签说的是同一批东西）",
                  rows == ["Price bands", "Notes"], rows)

            # ⚠️ A row pointing at a sheet that is NOT on screen has to bring it
            # on screen. One sheet is displayed at a time, and `scrollIntoView` on
            # a `display: none` block is a no-op — so the click did nothing at all
            # and the panel read as three dead rows. Clicked, then measured: which
            # sheet is actually on screen afterwards.
            page.evaluate("""() => document.querySelectorAll(
                '#rpt-outline-list .rpt-ol')[1].click()""")
            page.wait_for_timeout(500)
            on_screen = in_frame(page, "Array.from(doc.querySelectorAll('.pv-sheet-block'))"
                                       ".filter(b => b.getBoundingClientRect().height > 0)"
                                       ".map(b => b.querySelector('.pv-caption').textContent)")
            check("点目录第二行会把那张 sheet 切到屏上（不是滚到一个看不见的地方）",
                  on_screen == ["Notes"], on_screen)
            check("切过去之后底部标签的高亮也跟着走",
                  in_frame(page, "Array.from(doc.querySelectorAll('.pv-tab'))"
                                 ".filter(t => t.classList.contains('is-on'))"
                                 ".map(t => t.textContent)") == ["Notes"],
                  in_frame(page, "Array.from(doc.querySelectorAll('.pv-tab'))"
                                 ".map(t => [t.textContent, t.classList.contains('is-on')])"))

            check("整页没有 JS 报错", not errors, errors)
            browser.close()
    finally:
        httpd.shutdown()

    print()
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), "；".join(FAILURES)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
