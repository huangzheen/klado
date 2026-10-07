"""Editing a document and saving it: does the change land in the FILE.

    .venv312/bin/python api/tests/test_doc_edit.py   (or via scripts/ci_check.py)

The renderer mints a `data-oid` per editable unit and `apply_edits` writes text
back by that oid. The two are separate walks over the same file, so the failure
mode is a silent one: if they number differently, the preview still looks perfect,
every assertion about markup still passes, and the reader's edit lands in the
wrong paragraph. **That is why these tests round-trip through the real file
instead of asserting on the HTML** — the only question that matters is whether
re-opening the saved bytes shows the text the reader typed, in the place they
typed it.

`RoundTripMixin` builds a document, renders it, edits by the oids the render
produced, saves, re-renders, and checks the result. Each format adds the
assertions that are specific to it.
"""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import doc_preview as dp  # noqa: E402


def _docx_bytes():
    """A document with the two things a naive writer gets wrong.

    * an EMPTY cell, so a writer that skips cells it cannot see shifts every oid
      after it by one and the test can notice;
    * a BOLD run, so a writer that does `paragraph.text = …` (which rebuilds the
      run and loses run-level formatting) is caught. The paragraph STYLE survives
      either way, so a style-only assertion passes both and measures nothing.
    """
    import docx

    d = docx.Document()
    d.add_heading("结算条款速查表", level=1)
    d.add_paragraph("本页列出主要结算条款。")
    t = d.add_table(rows=2, cols=3)
    for i, row in enumerate(t.rows):
        for j, cell in enumerate(row.cells):
            cell.text = f"条款{i}-{j}"
    t.rows[0].cells[2].text = ""          # the empty cell that must still be numbered
    bolded = d.add_paragraph()
    bolded.add_run("加粗的部分").bold = True
    bolded.add_run("普通部分")
    d.add_paragraph("结尾一段。")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def _pptx_bytes():
    from pptx import Presentation

    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[5])
    slide.shapes.title.text = "第一页 季度定价概览"
    box = slide.shapes.add_textbox(914400, 1828800, 4000000, 800000)
    box.text_frame.text = "正文内容第 1 页"
    second = p.slides.add_slide(p.slide_layouts[5])
    second.shapes.title.text = "第二页 客户分层"
    b = io.BytesIO()
    p.save(b)
    return b.getvalue()


def _xlsx_bytes():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "定价"
    ws["A1"] = "条款"
    ws["B1"] = "金额"
    ws["A2"] = "账期"
    ws["B2"] = "30 天"
    ws["A3"] = "逾期"
    ws["B3"] = "60 天"
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _oids(html, prefix):
    import re

    return re.findall(r'data-oid="(%s[^"]*)"' % prefix, html)


def _oid_by_text(html, needle):
    """The oid of the box whose text is `needle`.

    Picking a target by its POSITION in the oid list is how these tests first
    went wrong: adding a slide to the fixture moved the index, and the test then
    asserted against a different shape without any test going red. Addressing the
    box the way a reader does — by what is written in it — is both stable and the
    thing the assertion is actually about.
    """
    import re

    for match in re.finditer(
            r'<[^>]*data-oid="([^"]+)"[^>]*>(.*?)</(?:div|td|th|p|h\d|li)>', html, re.S):
        if needle in re.sub(r"<[^>]+>", "", match.group(2)):
            return match.group(1)
    raise AssertionError("no editable box contains %r" % needle)


class DocxRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.orig = _docx_bytes()
        self.html = dp.preview_html(self.orig, "docx", "结算条款速查表")
        self.oids = _oids(self.html, "b")

    def test_every_paragraph_and_cell_gets_an_oid(self):
        # 1 heading + 1 paragraph + 6 table cells (one of them EMPTY) + 1 bold
        # paragraph + 1 closing paragraph. The empty cell is in the count on
        # purpose: see `_docx_bytes`.
        self.assertEqual(self.oids, ["b%d" % i for i in range(10)])

    def test_the_empty_cell_does_not_shift_the_oids_after_it(self):
        # b2..b7 are the six cells, b2 being the empty one. If the renderer or the
        # writer skipped it, everything after it would be off by one and an edit
        # addressed to the last cell would land in the wrong one.
        for oid in self.oids:
            _new, applied, dropped = dp.apply_edits(
                self.orig, "docx", {oid: "标记%s" % oid})
            self.assertEqual(applied, [oid], "%s 没有被识别" % oid)
            self.assertEqual(dropped, [], "%s 被丢掉了" % oid)

    def test_edit_lands_where_it_was_typed(self):
        # b3 is the cell holding 条款0-1; b4 is the EMPTY one and stays untouched
        # here so the "the old text is gone" assertion below has something to say.
        new, applied, dropped = dp.apply_edits(
            self.orig, "docx", {"b0": "改过的标题", "b3": "改过的单元格"})
        self.assertEqual(sorted(applied), ["b0", "b3"])
        self.assertEqual(dropped, [])
        after = dp.preview_html(new, "docx", "x")
        self.assertIn("改过的标题", after)
        self.assertIn("改过的单元格", after)
        # ⚠️ And the text it REPLACED must be gone. An edit that appends instead of
        # replacing is invisible in the "is my text there" assertion above.
        self.assertNotIn("结算条款速查表", after)
        self.assertNotIn("条款0-1", after)

    def test_editing_one_cell_does_not_shift_the_others(self):
        # ⚠️ The drift test. If `apply_edits` numbered cells its own way — say by
        # walking `row.cells`, which includes the continuation of a merged cell —
        # the oids would line up here by luck and the edits would cross over in a
        # file that has a merge. Assert the neighbours are untouched explicitly,
        # so a numbering change has to break THIS test, not only a merged-cell one.
        new, _, _ = dp.apply_edits(self.orig, "docx", {"b3": "只改这一个"})
        after = dp.preview_html(new, "docx", "x")
        self.assertNotIn("条款0-1", after)
        # Every other cell, in both rows, and the paragraphs around the table.
        for kept in ("条款0-0", "条款1-0", "条款1-1", "条款1-2",
                     "结算条款速查表", "本页列出主要结算条款。", "结尾一段。"):
            self.assertIn(kept, after)

    def test_unknown_oid_is_dropped_not_guessed(self):
        new, applied, dropped = dp.apply_edits(self.orig, "docx", {"b999": "x"})
        self.assertEqual(applied, [])
        self.assertEqual(dropped, ["b999"])
        # Nothing was written, so the file is byte-identical in meaning.
        self.assertEqual(new, self.orig)

    def test_editing_preserves_the_parts_that_were_not_edited(self):
        new, _, _ = dp.apply_edits(self.orig, "docx", {"b0": "只改标题"})
        after = dp.preview_html(new, "docx", "x")
        for kept in ("本页列出主要结算条款。", "条款0-0", "条款1-1", "结尾一段。"):
            self.assertIn(kept, after)

    def test_editing_keeps_the_first_runs_formatting(self):
        # ⚠️ Asserted on the RUN, not on the paragraph style. python-docx's
        # `paragraph.text = …` rebuilds the run from the paragraph default, so the
        # bold is gone — but the paragraph's STYLE is untouched either way, which
        # is why a style-only assertion here passes both implementations and
        # measures nothing at all.
        import docx

        bold_oid = _oid_by_text(self.html, "加粗的部分")
        new, applied, _dropped = dp.apply_edits(
            self.orig, "docx", {bold_oid: "改过的加粗文字"})
        self.assertEqual(applied, [bold_oid])
        reopened = docx.Document(io.BytesIO(new))
        target = [p for p in reopened.paragraphs if "改过的加粗文字" in p.text]
        self.assertEqual(len(target), 1, "改动后的文字没有落在原来的段落里")
        self.assertTrue(target[0].runs[0].bold, "加粗在保存后丢了")

    def test_editing_keeps_the_paragraph_style(self):
        import docx

        new, _, _ = dp.apply_edits(self.orig, "docx", {"b0": "还是标题"})
        reopened = docx.Document(io.BytesIO(new))
        self.assertEqual(reopened.paragraphs[0].style.name,
                         docx.Document(io.BytesIO(self.orig)).paragraphs[0].style.name)
        self.assertEqual(reopened.paragraphs[0].text, "还是标题")

    def test_the_file_is_still_a_valid_docx(self):
        import docx

        new, _, _ = dp.apply_edits(self.orig, "docx", {"b0": "x"})
        reopened = docx.Document(io.BytesIO(new))
        self.assertEqual(len(reopened.tables), 1)
        self.assertEqual(len(reopened.tables[0].rows), 2)
        self.assertEqual(len(reopened.tables[0].columns), 3)


class PptxRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.orig = _pptx_bytes()
        self.html = dp.preview_html(self.orig, "pptx", "deck")
        self.oids = _oids(self.html, "s")

    def test_oid_names_slide_and_shape_not_position(self):
        # `s<slide>r<shape id>` — the shape's OWN id. A render index would make
        # re-ordering the deck redirect a saved edit into a different text box.
        self.assertEqual(self.oids, ["s0r2", "s0r3", "s1r2"])
        # The body box on slide 1 is shape 3; if this were a positional index it
        # would be 1, and the same edit would follow the box when shapes move.
        self.assertIn("s0r3", self.oids)

    def test_edit_lands_in_the_right_shape(self):
        target = _oid_by_text(self.html, "正文内容第 1 页")
        new, applied, dropped = dp.apply_edits(self.orig, "pptx", {target: "改过的正文"})
        self.assertEqual(applied, [target])
        self.assertEqual(dropped, [])
        after = dp.preview_html(new, "pptx", "x")
        self.assertIn("改过的正文", after)
        self.assertNotIn("正文内容第 1 页", after)
        # The other shape on the same slide is untouched...
        self.assertIn("第一页 季度定价概览", after)
        # ...and so is the other slide. An edit that only "worked" on the box it
        # names while also eating its neighbours would pass the two lines above.
        self.assertIn("第二页 客户分层", after)

    def test_unknown_shape_is_dropped(self):
        _new, applied, dropped = dp.apply_edits(self.orig, "pptx", {"s9r9": "x"})
        self.assertEqual(applied, [])
        self.assertEqual(dropped, ["s9r9"])

    def test_oid_of_the_wrong_shape_is_rejected(self):
        # A real slide index, a shape id that is not on it.
        _new, applied, dropped = dp.apply_edits(self.orig, "pptx", {"s0r999": "x"})
        self.assertEqual(applied, [])
        self.assertEqual(dropped, ["s0r999"])


class XlsxRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.orig = _xlsx_bytes()
        self.html = dp.preview_html(self.orig, "xlsx", "book")
        self.oids = _oids(self.html, "x")

    def test_oid_is_the_real_excel_address(self):
        # ⚠️ Not a render index. The preview truncates rows, so an index would put
        # an edit in a cell that merely LOOKS like the right one.
        self.assertEqual(self.oids, ["x定价!R1C1", "x定价!R1C2",
                                     "x定价!R2C1", "x定价!R2C2",
                                     "x定价!R3C1", "x定价!R3C2"])

    def test_edit_lands_in_the_addressed_cell(self):
        import openpyxl

        new, applied, dropped = dp.apply_edits(
            self.orig, "xlsx", {"x定价!R2C2": "45 天"})
        self.assertEqual(applied, ["x定价!R2C2"])
        self.assertEqual(dropped, [])
        book = openpyxl.load_workbook(io.BytesIO(new))
        self.assertEqual(book["定价"]["B2"].value, "45 天")
        # The row below is a different cell, and a numbering slip would hit it.
        self.assertEqual(book["定价"]["B3"].value, "60 天")
        self.assertEqual(book["定价"]["A2"].value, "账期")

    def test_clearing_a_cell_empties_it(self):
        import openpyxl

        new, _, _ = dp.apply_edits(self.orig, "xlsx", {"x定价!R2C2": ""})
        self.assertIsNone(openpyxl.load_workbook(io.BytesIO(new))["定价"]["B2"].value)

    def test_unknown_sheet_is_dropped(self):
        _new, applied, dropped = dp.apply_edits(self.orig, "xlsx", {"x没有这张表!R1C1": "x"})
        self.assertEqual(applied, [])
        self.assertEqual(dropped, ["x没有这张表!R1C1"])


class ApplyEditsContractTests(unittest.TestCase):
    def test_pdf_is_refused(self):
        # ⚠️ A PDF's text is glyphs at coordinates. Refusing is the honest answer;
        # "supporting" it by redrawing the page produces a different document.
        with self.assertRaises(dp.EditError):
            dp.apply_edits(b"%PDF-1.4 fake", "pdf", {"x": "y"})

    def test_editable_types_exclude_pdf(self):
        self.assertNotIn("pdf", dp.EDITABLE_DOC_TYPES)
        self.assertEqual(set(dp.EDITABLE_DOC_TYPES), {"docx", "pptx", "xlsx"})

    def test_empty_edits_are_refused(self):
        with self.assertRaises(dp.EditError):
            dp.apply_edits(_docx_bytes(), "docx", {})

    def test_unsupported_type_is_refused(self):
        with self.assertRaises(dp.EditError):
            dp.apply_edits(b"x", "html", {"a": "b"})

    def test_a_corrupt_file_reports_rather_than_raises_a_500(self):
        with self.assertRaises(dp.EditError):
            dp.apply_edits(b"not a docx at all", "docx", {"b0": "x"})

    def test_source_bytes_are_never_mutated(self):
        # The caller holds the bytes it read; if `apply_edits` edited them in place
        # the caller would find its copy already changed, and a failed save would
        # still have destroyed the original.
        orig = _docx_bytes()
        snapshot = bytes(orig)
        dp.apply_edits(orig, "docx", {"b0": "改了"})
        self.assertEqual(orig, snapshot)


class RichFormatRoundTripTests(unittest.TestCase):
    """Strike / superscript / size / colour / alignment must survive the file.

    ⚠️ Same rule as `RunFormatRoundTripTests`: every case RE-OPENS the bytes
    `apply_edits` returned and measures them. Checking the returned `applied` list
    would pass for a writer that formatted nothing.

    ⚠️ And the second half of this file is the half that is easy to get wrong in
    the OTHER direction. The four new attributes are OPTIONAL in the payload,
    because a size or colour a paragraph inherits from its style was never in the
    file: sending it always would rewrite the whole document's inherited
    formatting into hardcoded values on every keystroke. So "absent" must mean
    "the file said nothing, leave it alone" — and `test_absent_*_leaves_the_file_
    exactly_as_it_was` is the guard for that, because a writer that treats absent
    as "off" produces a file that looks right until someone opens it in Word.
    """

    @staticmethod
    def _docx():
        import docx
        d = docx.Document()
        p = d.add_paragraph()
        run = p.add_run("plain text here")
        run.font.size = docx.shared.Pt(18)
        run.font.color.rgb = docx.shared.RGBColor(0x0B, 0x5C, 0xAD)
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue(), "b0"

    @staticmethod
    def _struck_docx():
        """A paragraph the reader wants to UN-strike.

        ⚠️ Sharpened after a mutation proved the first version of
        `test_docx_turning_strike_off_…` was a FAKE GREEN. Its fixture was a
        plain 18pt run, so the plain-text path — which leaves the run exactly as
        it found it — produced the same observable file as the runs path: 1 run,
        18pt, no strike. Every assertion passed in both worlds, and putting the
        old "no b/i/u at all" comparison back left the test green.

        The only thing that separates "wrote strike=off" from "never ran the runs
        path" is a run that WAS struck."""
        import docx
        d = docx.Document()
        p = d.add_paragraph()
        run = p.add_run("plain text here")
        run.font.size = docx.shared.Pt(18)
        run.font.strike = True
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue(), "b0"

    @staticmethod
    def _pptx():
        from pptx import Presentation
        from pptx.util import Inches, Pt
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.6), Inches(1), Inches(8), Inches(1))
        box.text_frame.text = "plain text here"
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue(), "s0r%d" % box.shape_id

    @staticmethod
    def _xlsx():
        import openpyxl
        from openpyxl.styles import Font
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "S"
        sheet["B2"] = "plain text here"
        sheet["B2"].font = Font(name="Calibri", size=14)
        buf = io.BytesIO()
        book.save(buf)
        return buf.getvalue(), "xS!R2C2"

    RICH = [{"text": "plain ", "b": False, "i": False, "u": False},
            {"text": "TEXT", "b": False, "i": False, "u": False,
             "s": True, "va": "sup", "sz": 24, "color": "C0392B"},
            {"text": " here", "b": False, "i": False, "u": False}]

    #: ⚠️ ONE run, for the workbook cases only. A cell holds a single font, so a
    #: payload that disagrees with itself is refused on purpose (see
    #: `test_xlsx_refuses_to_guess_when_the_cell_disagrees`) — and the client
    #: never sends one, because it widens a cell selection to the whole cell
    #: first, exactly as Excel does.
    CELL = [{"text": "plain text here", "b": True, "i": False, "u": False,
             "s": True, "va": "sup", "sz": 24, "color": "C0392B"}]

    # ── docx ────────────────────────────────────────────────────────────────
    def test_docx_writes_strike_vertalign_size_and_colour(self):
        from services import doc_preview as dp
        data, oid = self._docx()
        out, applied, dropped = dp.apply_edits(data, "docx", {
            oid: {"text": "plain TEXT here", "runs": self.RICH}})
        self.assertEqual((applied, dropped), ([oid], []))
        import docx
        runs = docx.Document(io.BytesIO(out)).paragraphs[0].runs
        self.assertEqual([r.text for r in runs], ["plain ", "TEXT", " here"])
        self.assertTrue(bool(runs[1].font.strike), runs[1].text)
        self.assertTrue(bool(runs[1].font.superscript), runs[1].text)
        self.assertFalse(bool(runs[1].font.subscript),
                         "one vertAlign element, two values — setting both leaves a "
                         "run that claims to be raised AND lowered")
        self.assertEqual(runs[1].font.size, docx.shared.Pt(24), runs[1].text)
        self.assertEqual(str(runs[1].font.color.rgb), "C0392B", runs[1].text)

    def test_docx_absent_optional_keys_leave_the_file_exactly_as_it_was(self):
        """The whole point of them being optional.

        ⚠️ A b/i/u-only edit on a 18pt blue run must leave BOTH at 18pt blue. A
        writer that resolved "no size sent" to "no size" would strip the size off
        every paragraph a reader ever fixed a typo in."""
        from services import doc_preview as dp
        data, oid = self._docx()
        out, _a, _d = dp.apply_edits(data, "docx", {oid: {
            "text": "PLAIN text here",
            "runs": [{"text": "PLAIN", "b": True, "i": False, "u": False},
                     {"text": " text here", "b": False, "i": False, "u": False}]}})
        import docx
        for run in docx.Document(io.BytesIO(out)).paragraphs[0].runs:
            self.assertEqual(run.font.size, docx.shared.Pt(18), run.text)
            self.assertEqual(str(run.font.color.rgb), "0B5CAD", run.text)
            self.assertFalse(bool(run.font.strike), run.text)

    def test_docx_turning_strike_off_is_not_mistaken_for_plain_text(self):
        """`{"s": false}` on everything is a real change, not "no formatting".

        ⚠️ This is the case a "no flags at all" test throws away — and it is the
        reader un-striking their own text, which is the single most common thing
        anybody does with a strike-through button.

        ⚠️ The fixture arrives STRUCK and must arrive unstruck. Measured, not
        assumed: with a plain fixture this test also passed with the old
        comparison put back, because the plain-text path preserves the original
        run and the two paths then produced the same bytes."""
        from services import doc_preview as dp
        data, oid = self._struck_docx()
        out, _a, _d = dp.apply_edits(data, "docx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "plain text here", "b": False, "i": False, "u": False,
                      "s": False}]}})
        import docx
        runs = docx.Document(io.BytesIO(out)).paragraphs[0].runs
        self.assertEqual(len(runs), 1, runs)
        # ⚠️ THE assertion. The file came in struck; the plain-text path leaves
        # the run exactly as it found it, so "still struck" is the signature of a
        # payload that was thrown away before it reached the writer.
        self.assertFalse(bool(runs[0].font.strike),
                         "strike was never turned off — the runs path did not run")
        # And the size survives, which is what proves the runs path rewrote the
        # run rather than replacing it with a bare one.
        self.assertEqual(runs[0].font.size, docx.shared.Pt(18))

    # ── pptx ────────────────────────────────────────────────────────────────
    def test_pptx_writes_strike_and_baseline_as_drawingml_attributes(self):
        from services import doc_preview as dp
        data, oid = self._pptx()
        out, applied, dropped = dp.apply_edits(data, "pptx", {
            oid: {"text": "plain TEXT here", "runs": self.RICH}})
        self.assertEqual((applied, dropped), ([oid], []))
        from pptx import Presentation
        from pptx.oxml.ns import qn
        shape = [sh for sh in Presentation(io.BytesIO(out)).slides[0].shapes
                 if sh.has_text_frame][0]
        runs = shape.text_frame.paragraphs[0].runs
        self.assertEqual([r.text for r in runs], ["plain ", "TEXT", " here"])
        rpr = runs[1]._r.find(qn("a:rPr"))
        self.assertEqual(rpr.get("strike"), "sngStrike", "DrawingML attribute")
        self.assertEqual(rpr.get("baseline"), "30000", "per-mille baseline")
        self.assertEqual(runs[1].font.size.pt, 24.0, runs[1].text)
        self.assertEqual(str(runs[1].font.color.rgb), "C0392B", runs[1].text)
        # ⚠️ The failure this case exists for, twice over: a `w:` element in a
        # slide opens, renders nothing, and raises nothing.
        self.assertNotIn(b"w:strike", out)
        self.assertNotIn(b"w:vertAlign", out)

    def test_pptx_absent_optional_keys_leave_the_template_alone(self):
        from services import doc_preview as dp
        data, oid = self._pptx()
        out, _a, _d = dp.apply_edits(data, "pptx", {oid: {
            "text": "PLAIN text here",
            "runs": [{"text": "PLAIN", "b": True, "i": False, "u": False},
                     {"text": " text here", "b": False, "i": False, "u": False}]}})
        from pptx import Presentation
        from pptx.oxml.ns import qn
        shape = [sh for sh in Presentation(io.BytesIO(out)).slides[0].shapes
                 if sh.has_text_frame][0]
        for run in shape.text_frame.paragraphs[0].runs:
            rpr = run._r.find(qn("a:rPr"))
            self.assertIsNone(rpr.get("strike"), run.text)
            self.assertIsNone(rpr.get("baseline"), run.text)
            self.assertEqual(run.font.size.pt, 28.0, run.text)

    # ── xlsx ────────────────────────────────────────────────────────────────
    def test_xlsx_cell_takes_strike_size_colour_and_vertalign(self):
        from services import doc_preview as dp
        data, oid = self._xlsx()
        out, applied, dropped = dp.apply_edits(data, "xlsx", {
            oid: {"text": "plain text here", "runs": self.CELL}})
        self.assertEqual((applied, dropped), ([oid], []))
        import openpyxl
        cell = openpyxl.load_workbook(io.BytesIO(out))["S"]["B2"]
        self.assertEqual(cell.value, "plain text here")
        self.assertTrue(bool(cell.font.bold))
        self.assertTrue(bool(cell.font.strike))
        self.assertEqual(float(cell.font.size), 24.0)
        self.assertEqual(str(cell.font.color.rgb)[-6:], "C0392B")
        self.assertEqual(cell.font.vertAlign, "superscript")
        self.assertEqual(cell.font.name, "Calibri", "the font it already had is kept")

    def test_xlsx_disagreement_about_size_is_refused_too(self):
        """The guard compares the whole state, not the three old flags.

        ⚠️ A cell whose words differ only in SIZE is exactly as unwritable as one
        that differs in bold, and a guard that looked at three of the seven
        attributes would half-apply it: the text is written, one size sticks, and
        nothing anywhere reports an error."""
        from services import doc_preview as dp
        data, oid = self._xlsx()
        out, _a, _d = dp.apply_edits(data, "xlsx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "plain ", "b": False, "i": False, "u": False, "sz": 20},
                     {"text": "text here", "b": False, "i": False, "u": False,
                      "sz": 30}]}})
        import openpyxl
        cell = openpyxl.load_workbook(io.BytesIO(out))["S"]["B2"]
        self.assertEqual(cell.value, "plain text here", "the words are still saved")
        self.assertEqual(float(cell.font.size), 14.0, "the size was left alone")

    def test_xlsx_absent_optional_keys_keep_the_existing_font(self):
        from services import doc_preview as dp
        data, oid = self._xlsx()
        out, _a, _d = dp.apply_edits(data, "xlsx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "plain text here", "b": True, "i": False, "u": False}]}})
        import openpyxl
        cell = openpyxl.load_workbook(io.BytesIO(out))["S"]["B2"]
        self.assertTrue(bool(cell.font.bold))
        self.assertEqual(float(cell.font.size), 14.0)
        self.assertEqual(cell.font.name, "Calibri")

    # ── alignment, all three ────────────────────────────────────────────────
    def test_alignment_lands_on_the_paragraph_in_all_three_formats(self):
        from services import doc_preview as dp
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        cases = {
            "docx": (self._docx, lambda out: docx.Document(io.BytesIO(out))
                     .paragraphs[0].alignment,
                     {"left": WD_ALIGN_PARAGRAPH.LEFT,
                      "center": WD_ALIGN_PARAGRAPH.CENTER,
                      "right": WD_ALIGN_PARAGRAPH.RIGHT}),
        }
        for kind, (make, read, expected) in cases.items():
            for value in ("left", "center", "right"):
                with self.subTest(kind=kind, align=value):
                    data, oid = make()
                    out, applied, dropped = dp.apply_edits(
                        data, kind, {oid: {"text": "plain text here", "align": value}})
                    self.assertEqual((applied, dropped), ([oid], []))
                    self.assertEqual(read(out), expected[value])

    def test_pptx_alignment_lands_on_the_first_paragraph_only(self):
        from services import doc_preview as dp
        from pptx import Presentation
        from pptx.enum.text import PP_ALIGN
        data, oid = self._pptx()
        out, _a, _d = dp.apply_edits(data, "pptx", {
            oid: {"text": "plain text here", "align": "center"}})
        shape = [sh for sh in Presentation(io.BytesIO(out)).slides[0].shapes
                 if sh.has_text_frame][0]
        self.assertEqual(shape.text_frame.paragraphs[0].alignment, PP_ALIGN.CENTER)

    def test_xlsx_alignment_is_the_cells_horizontal_alignment(self):
        from services import doc_preview as dp
        import openpyxl
        data, oid = self._xlsx()
        out, _a, _d = dp.apply_edits(data, "xlsx", {
            oid: {"text": "plain text here", "align": "right"}})
        cell = openpyxl.load_workbook(io.BytesIO(out))["S"]["B2"]
        self.assertEqual(cell.alignment.horizontal, "right")
        self.assertEqual(cell.value, "plain text here")

    def test_an_alignment_that_was_not_sent_is_not_written(self):
        """Optional for the same reason the run attributes are.

        ⚠️ A paragraph that inherits its alignment from its style must keep doing
        so. Writing the inherited value in would replace "follows the style" with
        "matched it today", and the next edit to a sibling paragraph would then
        disagree with the rest of the document."""
        from services import doc_preview as dp
        import docx
        data, oid = self._docx()
        out, _a, _d = dp.apply_edits(data, "docx", {oid: {"text": "plain text here"}})
        self.assertIsNone(docx.Document(io.BytesIO(out)).paragraphs[0].alignment)

    def test_justify_is_refused_rather_than_accepted_and_dropped(self):
        """It is a real Word feature and an impossible spreadsheet one.

        ⚠️ Accepting it and ignoring it would be worse than refusing: the reader
        presses 两端对齐, the button lights, the file is unchanged, and there is no
        way to tell that from "saved"."""
        from services import doc_preview as dp
        import docx
        data, oid = self._docx()
        out, applied, _d = dp.apply_edits(data, "docx", {
            oid: {"text": "plain text here", "align": "justify"}})
        self.assertEqual(applied, [oid], "the TEXT is still saved")
        self.assertIsNone(docx.Document(io.BytesIO(out)).paragraphs[0].alignment)

    # ── the payload is not trusted ──────────────────────────────────────────
    def test_a_malformed_optional_value_is_dropped_not_refused(self):
        """One bad field must not cost the reader their words.

        ⚠️ Refusing the whole edit would make a typo in a colour string lose a
        paragraph of text. The value is dropped; the run and the text stand."""
        from services import doc_preview as dp
        import docx
        data, oid = self._docx()
        out, applied, dropped = dp.apply_edits(data, "docx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "plain text here", "b": False, "i": False, "u": False,
                      "color": "not-a-colour", "sz": 99999, "va": "sideways"}]}})
        self.assertEqual((applied, dropped), ([oid], []))
        run = docx.Document(io.BytesIO(out)).paragraphs[0].runs[0]
        self.assertEqual(run.text, "plain text here")
        # ⚠️ Dropped, not applied and not blanked: the run keeps the colour the FILE
        # had, because a value nobody sent must not become "no colour".
        self.assertEqual(str(run.font.color.rgb), "0B5CAD")
        self.assertEqual(run.font.size, docx.shared.Pt(18), "the size it already had")
        self.assertFalse(bool(run.font.superscript))

    def test_the_preview_draws_what_the_file_says(self):
        """The read side of the same loop.

        ⚠️ The payload is built by asking the browser what it is DRAWING, so an
        attribute the preview does not draw is an attribute the save cannot send:
        the button lights, the file changes, and the next load shows the old text.
        This asserts the markup carries the inline style, for all three formats."""
        from services import doc_preview as dp

        data, _oid = self._docx()
        html = dp.preview_html(data, "docx", "x")
        self.assertIn("color:#0b5cad", html.lower(), "colour is drawn")

        struck = data
        import docx
        document = docx.Document(io.BytesIO(struck))
        run = document.paragraphs[0].runs[0]
        run.font.strike = True
        run.font.superscript = True
        buf = io.BytesIO()
        document.save(buf)
        html = dp.preview_html(buf.getvalue(), "docx", "x")
        self.assertIn("line-through", html, "strike-through is drawn")
        self.assertIn("vertical-align:super", html, "superscript is drawn")
        self.assertIn("text-align:center", dp.preview_html(
            _centred_docx(), "docx", "x"), "alignment is drawn")

        data, _oid = self._pptx()
        html = dp.preview_html(data, "pptx", "x")
        self.assertIn("color:", html.lower(), "the deck draws its run colours")

        data, _oid = self._xlsx()
        html = dp.preview_html(data, "xlsx", "x")
        self.assertIn("font-size:14pt", html, "the grid draws the cell's own size")

    def test_the_grid_draws_a_bold_cell_as_bold(self):
        """⚠️ The bug this whole class would have inherited.

        The grid drew NO cell font at all, so a bold cell read back as plain and a
        reader who fixed one character inside it saved it back unbolded — no
        error anywhere, because the file said bold and the PREVIEW said plain, and
        the preview is what got written."""
        from services import doc_preview as dp
        import openpyxl
        from openpyxl.styles import Font
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "S"
        sheet["A1"] = "Head"
        sheet["A1"].font = Font(bold=True, color="FF1122FF")
        sheet["B2"] = "value"
        sheet["B2"].font = Font(bold=True, strike=True)
        buf = io.BytesIO()
        book.save(buf)
        html = dp.preview_html(buf.getvalue(), "xlsx", "x")
        self.assertIn("font-weight:700", html)
        self.assertIn("#1122ff", html.lower())
        self.assertIn("line-through", html)


def _centred_docx() -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("centred")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()


class RunFormatRoundTripTests(unittest.TestCase):
    """B / I / U must come BACK OUT of the saved file, not just look right on screen.

    ⚠️ Every one of these re-opens the bytes `apply_edits` returned. A test that
    only checked the returned `applied` list would pass for a writer that formatted
    nothing, and the reader would find out by opening the file.

    The three formats spell emphasis differently and that is the point of the
    separate cases:

    * WordprocessingML puts it in CHILD ELEMENTS (`<w:b/>`);
    * DrawingML puts it in an ATTRIBUTES on the run properties (`b="1"`);
    * a spreadsheet has ONE font per cell and cannot hold two at all.

    A single implementation written for the first one produces a slide and a
    workbook that open cleanly, show nothing, and raise nothing. That is exactly
    what the first version of this did.
    """

    @staticmethod
    def _docx():
        import docx
        d = docx.Document()
        p = d.add_paragraph()
        run = p.add_run("plain text here")
        run.font.size = docx.shared.Pt(18)
        run.font.color.rgb = docx.shared.RGBColor(0x0B, 0x5C, 0xAD)
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue(), "b0"

    @staticmethod
    def _pptx():
        from pptx import Presentation
        from pptx.util import Inches, Pt
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.6), Inches(1), Inches(8), Inches(1))
        box.text_frame.text = "plain text here"
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue(), "s0r%d" % box.shape_id

    @staticmethod
    def _xlsx():
        import openpyxl
        from openpyxl.styles import Font
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "S"
        sheet["B2"] = "plain text here"
        sheet["B2"].font = Font(name="Calibri", size=14)
        buf = io.BytesIO()
        book.save(buf)
        return buf.getvalue(), "xS!R2C2"

    def test_docx_bold_italic_land_on_the_right_runs(self):
        from services import doc_preview as dp
        data, oid = self._docx()
        out, applied, dropped = dp.apply_edits(data, "docx", {oid: {
            "text": "plain TEXT here",
            "runs": [{"text": "plain ", "b": False, "i": False, "u": False},
                     {"text": "TEXT", "b": True, "i": True, "u": False},
                     {"text": " here", "b": False, "i": False, "u": True}]}})
        self.assertEqual((applied, dropped), ([oid], []))
        import docx
        runs = docx.Document(io.BytesIO(out)).paragraphs[0].runs
        self.assertEqual([r.text for r in runs], ["plain ", "TEXT", " here"])
        self.assertEqual([bool(r.bold) for r in runs], [False, True, False])
        self.assertEqual([bool(r.italic) for r in runs], [False, True, False])
        self.assertEqual([bool(r.underline) for r in runs], [False, False, True])

    def test_docx_keeps_the_first_runs_own_formatting(self):
        """Cloning run 0 is the whole reason mixed formatting is possible at all."""
        from services import doc_preview as dp
        data, oid = self._docx()
        out, _a, _d = dp.apply_edits(data, "docx", {oid: {
            "text": "PLAIN text here",
            "runs": [{"text": "PLAIN", "b": True, "i": False, "u": False},
                     {"text": " text here", "b": False, "i": False, "u": False}]}})
        import docx
        runs = docx.Document(io.BytesIO(out)).paragraphs[0].runs
        self.assertEqual(len(runs), 2, runs)
        for run in runs:
            self.assertEqual(run.font.size, docx.shared.Pt(18), run.text)
            self.assertEqual(str(run.font.color.rgb), "0B5CAD", run.text)

    def test_pptx_writes_drawingml_attributes_not_wordprocessingml(self):
        from services import doc_preview as dp
        data, oid = self._pptx()
        out, applied, dropped = dp.apply_edits(data, "pptx", {oid: {
            "text": "plain TEXT here",
            "runs": [{"text": "plain ", "b": False, "i": False, "u": False},
                     {"text": "TEXT", "b": True, "i": False, "u": True},
                     {"text": " here", "b": False, "i": False, "u": False}]}})
        self.assertEqual((applied, dropped), ([oid], []))
        from pptx import Presentation
        prs = Presentation(io.BytesIO(out))
        shape = [sh for sh in prs.slides[0].shapes if sh.has_text_frame][0]
        runs = shape.text_frame.paragraphs[0].runs
        self.assertEqual([r.text for r in runs], ["plain ", "TEXT", " here"])
        self.assertEqual([bool(r.font.bold) for r in runs], [False, True, False])
        self.assertEqual([bool(r.font.underline) for r in runs], [False, True, False])
        # ⚠️ The whole failure this case exists for: a `w:b` element in a slide
        # opens, renders unformatted, and raises nothing.
        self.assertNotIn(b"w:b", out)

    def test_pptx_runs_are_validated_against_the_first_line_only(self):
        """A text box is several paragraphs; the toolbar edits the line in the cursor."""
        from services import doc_preview as dp
        data, oid = self._pptx()
        out, applied, _d = dp.apply_edits(data, "pptx", {oid: {
            # ⚠️ The runs cover the WHOLE first line. A payload whose runs stop
            # short of the line is refused by `normalise_runs` — correctly, since
            # applying it would leave the paragraph's own text unaccounted for —
            # and the refusal shows up as this edit silently having no bold in it.
            "text": "plain TEXT here\nsecond line",
            "runs": [{"text": "plain ", "b": False, "i": False, "u": False},
                     {"text": "TEXT", "b": True, "i": False, "u": False},
                     {"text": " here", "b": False, "i": False, "u": False}]}})
        self.assertEqual(applied, [oid])
        from pptx import Presentation
        prs = Presentation(io.BytesIO(out))
        shape = [sh for sh in prs.slides[0].shapes if sh.has_text_frame][0]
        paras = shape.text_frame.paragraphs
        self.assertEqual([p.text for p in paras], ["plain TEXT here", "second line"])
        self.assertTrue(paras[0].runs[1].font.bold)

    def test_xlsx_bolds_the_whole_cell(self):
        from services import doc_preview as dp
        data, oid = self._xlsx()
        out, applied, dropped = dp.apply_edits(data, "xlsx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "plain text ", "b": True, "i": False, "u": False},
                     {"text": "here", "b": True, "i": False, "u": False}]}})
        self.assertEqual((applied, dropped), ([oid], []))
        import openpyxl
        cell = openpyxl.load_workbook(io.BytesIO(out))["S"]["B2"]
        self.assertEqual(cell.value, "plain text here")
        self.assertTrue(cell.font.bold, "a cell's whole content was bolded")
        # ⚠️ The cell's own font must survive: a fresh `Font()` resets size and name,
        # which is the same class of bug as `paragraph.text = x` in the .docx path.
        self.assertEqual(cell.font.size, 14.0)
        self.assertEqual(cell.font.name, "Calibri")

    def test_xlsx_refuses_to_guess_when_the_cell_disagrees(self):
        """A cell cannot hold two formats, so a mixed payload must not pick one.

        ⚠️ The first version took the leading run's flags, which is wrong in exactly
        the case it was written for: the reader bolds the SECOND word, the leading
        run is the plain first one, and the cell comes back not bold — the button
        silently did nothing. The text is still written; only the formatting is
        left alone, and the CLIENT is expected to widen a cell selection first."""
        from services import doc_preview as dp
        data, oid = self._xlsx()
        # ⚠️ ⚠️ The runs are ordered BOLD FIRST on purpose. With a plain first run
        # the two implementations are indistinguishable — "guess the leading run"
        # and "do not guess" both leave the cell plain — so the test passed against
        # the guesser too. An assertion that cannot tell the two behaviours apart
        # is not an assertion, it is a coincidence. Ordered this way, guessing
        # would bold the cell and not-guessing must not.
        out, applied, dropped = dp.apply_edits(data, "xlsx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "plain ", "b": True, "i": False, "u": False},
                     {"text": "text here", "b": False, "i": False, "u": False}]}})
        self.assertEqual((applied, dropped), ([oid], []))
        import openpyxl
        cell = openpyxl.load_workbook(io.BytesIO(out))["S"]["B2"]
        self.assertEqual(cell.value, "plain text here", "the words are still saved")
        self.assertFalse(bool(cell.font.bold),
                         "a mixed cell is not guessed at — taking the leading run "
                         "would have bolded the whole cell")

    def test_runs_that_disagree_with_the_text_are_refused(self):
        """A payload whose runs do not add up to the text is not written as runs.

        ⚠️ Writing it anyway would put words in the file in an order the reader
        never typed — and it would be written SILENTLY, because the `text` half of
        the edit is perfectly valid and would be applied first."""
        from services import doc_preview as dp
        data, oid = self._docx()
        out, applied, dropped = dp.apply_edits(data, "docx", {oid: {
            "text": "plain text here",
            "runs": [{"text": "PLAIN ", "b": True, "i": False, "u": False},
                     {"text": "text", "b": False, "i": False, "u": False}]}})
        self.assertEqual((applied, dropped), ([oid], []))
        import docx
        runs = docx.Document(io.BytesIO(out)).paragraphs[0].runs
        self.assertEqual(runs[0].text, "plain text here", "the TEXT was written")
        self.assertFalse(bool(runs[0].bold), "but the disagreeing runs were not")

    def test_an_edit_with_no_runs_still_works(self):
        """An older client sends `{"oid": ..., "text": ...}` and must not be broken."""
        from services import doc_preview as dp
        data, oid = self._docx()
        out, applied, dropped = dp.apply_edits(data, "docx", {oid: {"text": "typed text"}})
        self.assertEqual((applied, dropped), ([oid], []))
        import docx
        self.assertEqual(docx.Document(io.BytesIO(out)).paragraphs[0].text, "typed text")
        # …and the bare string form too, which is what a hand-written curl sends.
        out2, applied2, _ = dp.apply_edits(data, "docx", {oid: "bare string"})
        self.assertEqual(applied2, [oid])
        self.assertEqual(docx.Document(io.BytesIO(out2)).paragraphs[0].text, "bare string")
