"""Excel readers must ignore the sheets a person cannot see.

⚠️ The reported file was an SAP Analysis Office export. Those carry a hidden
technical sheet whose `sheetId` is literally `com.sap.ip.bi.xls.hiddensheet`, and
it is usually the FIRST sheet. Both readers used to start at the first sheet, so
they landed on it and raised `Sheet 'Sheet1' 没有可导入的数据` while the real data
sat in the very next sheet.

It had TWO faces and only one was visible:
  · the preview pane showed the error;
  · `parse_excel` raised as well, and `/files/stage` swallows parser errors
    (`except Exception: pass`) — so the upload still "succeeded", with EMPTY
    `sheets_meta`, and the file then appeared in every "pick a source file"
    dropdown with nothing selectable.

So the fixture is built to look like the real export rather than to look like a
test: a hidden empty first sheet, a visible one after it with real data, and a
workbook whose only sheet is empty so the "no data at all" message has something
honest to say. Asserting `sheet_state` is READ somewhere would pass on a function
that reads it and ignores it; every assertion here is about what comes OUT.

Every expectation in this file was measured against the implementation before it
was written down (see the probe that produced the `chartsheets` note below — that
scenario turned out NOT to be constructible, so it is asserted with a stub
instead of a workbook).

    ../.venv312/bin/python -m unittest tests.test_excel_hidden_sheets
"""
import io
import re
import unittest
import zipfile
from pathlib import Path

import openpyxl

from processors.excel import _ordered_sheet_titles, parse_excel, preview_excel

# ⚠️ CI runs `unittest discover` with cwd=api, so this cannot be a relative
# `open("frontend/out/index.html")` — that path only resolves from the repo root,
# and the file would look missing rather than the assertion looking wrong.
INDEX_HTML = Path(__file__).resolve().parents[2] / "frontend" / "out" / "index.html"


def workbook(sheets):
    """`sheets` is [(title, state, rows|None)]; `rows=None` means a truly empty sheet."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, state, rows in sheets:
        ws = wb.create_sheet(title)
        if state != "visible":
            ws.sheet_state = state
        for row in (rows or []):
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


DATA = [["项目", "负责人", "状态"], ["A 项目", "张三", "进行中"], ["B 项目", "李四", "完成"]]

# ⚠️ Hidden and VISIBLE-EMPTY both have to be survived, and they fail differently:
# the hidden one is never something a reader asked for, the empty visible one
# might be a "说明" cover sheet sitting in front of the data.
SAP_LIKE = workbook([
    ("Sheet1", "hidden", None),
    ("审核排期表", "visible", DATA),
])
COVER_FIRST = workbook([
    ("说明", "visible", None),
    ("数据", "visible", DATA),
])
TWO_DATA_SHEETS = workbook([
    ("甲", "visible", DATA),
    ("乙", "visible", DATA),
])
# ⚠️ A hidden sheet that HAS data is the only fixture that separates "hidden sheets
# are skipped" from "empty sheets raise and are caught". Against a hidden EMPTY
# sheet both explanations produce the same result, so a guard built on one of them
# passes when the other is deleted — measured, not assumed: with the skip removed,
# `parse_excel` on `SAP_LIKE` still returned exactly `[('审核排期表', 2)]`.
HIDDEN_WITH_DATA = workbook([
    ("缓存", "hidden", DATA),
    ("审核排期表", "visible", DATA),
])
ALL_HIDDEN = None   # set below, once `all_hidden` is defined — see its docstring
NO_DATA = workbook([("Sheet1", "visible", None)])


def all_hidden():
    """A workbook with NO visible sheet, which `workbook()` above cannot produce:
    openpyxl refuses to save one (`IndexError: At least one sheet must be visible`),
    and Excel's UI will not let you hide the last sheet either.

    ⚠️ So it is built by patching `xl/workbook.xml` inside the saved zip —
    `state="visible"` becomes `state="hidden"`. Written as a plain `workbook()`
    call this scenario silently degrades into a broken fixture, which is how the
    first version of this file failed to even import.
    """
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title in ("Sheet1", "Sheet2"):
        wb.create_sheet(title)
    buf = io.BytesIO()
    wb.save(buf)

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zin, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/workbook.xml":
                patched = data.decode("utf-8").replace('state="visible"', 'state="hidden"')
                hidden = patched.count('state="hidden"')
                assert hidden == 2, f"expected 2 hidden sheets in the patch, got {hidden}"
                data = patched.encode("utf-8")
            zout.writestr(item, data)
    return out.getvalue()


ALL_HIDDEN = all_hidden()


class _Sheet:
    def __init__(self, title, state="visible"):
        self.title = title
        self.sheet_state = state


class _Wb:
    def __init__(self, worksheets, sheetnames):
        self.worksheets = worksheets
        self.sheetnames = sheetnames


class TestSheetOrder(unittest.TestCase):
    def test_visible_sheets_come_first(self):
        wb = openpyxl.load_workbook(io.BytesIO(SAP_LIKE))
        self.assertEqual(_ordered_sheet_titles(wb), ["审核排期表"])

    def test_a_workbook_with_nothing_visible_is_still_usable(self):
        wb = openpyxl.load_workbook(io.BytesIO(ALL_HIDDEN))
        self.assertEqual([w.sheet_state for w in wb.worksheets], ["hidden", "hidden"],
                         "the fixture stopped being all-hidden — it would assert nothing")
        order = _ordered_sheet_titles(wb)
        self.assertEqual(sorted(order), ["Sheet1", "Sheet2"],
                         "dropping them would make the file unopenable with no message")

    def test_order_follows_the_workbook_not_the_list(self):
        """Order is what makes the preview pick the FIRST readable sheet, so a
        shuffled candidate list must not be sorted back into a different one."""
        wb = openpyxl.load_workbook(io.BytesIO(TWO_DATA_SHEETS))
        self.assertEqual(_ordered_sheet_titles(wb), ["甲", "乙"])

    def test_chartsheets_are_not_offered_as_data(self):
        """A chartsheet has no cells, so reading one yields nothing and reports a
        broken file. `sheetnames` contains chartsheets; `worksheets` does not.

        ⚠️ This is asserted with a STUB, not a real workbook, because the scenario
        is not constructible: openpyxl 3.1.5 cannot load a workbook containing a
        chartsheet it just wrote — `read_chartsheet` raises `AttributeError:
        'list' object has no attribute 'find'` from its own reader. A fixture here
        would be a vacuous assertion dressed as a real one.
        """
        wb = _Wb(worksheets=[_Sheet("数据")], sheetnames=["chart1", "数据"])
        self.assertEqual(_ordered_sheet_titles(wb), ["数据"])

    def test_a_sheet_without_a_state_attribute_still_counts_as_visible(self):
        """`sheet_state` is not guaranteed on every reader object. Defaulting to
        'visible' keeps such a sheet importable instead of silently skipping it."""
        wb = _Wb(worksheets=[type("S", (), {"title": "旧文件"})()], sheetnames=["旧文件"])
        self.assertEqual(_ordered_sheet_titles(wb), ["旧文件"])


class TestPreview(unittest.TestCase):
    def test_previews_the_real_sheet_not_the_hidden_one(self):
        r = preview_excel(SAP_LIKE, "audit.xlsx")
        self.assertEqual(r["sheet_name"], "审核排期表")
        self.assertEqual(r["columns"], ["项目", "负责人", "状态"])
        self.assertEqual(r["total_rows"], 2)

    def test_skips_an_empty_visible_cover_sheet(self):
        r = preview_excel(COVER_FIRST, "audit.xlsx")
        self.assertEqual(r["sheet_name"], "数据")

    def test_never_previews_a_hidden_sheet_that_has_data(self):
        """A readable hidden sheet is the one case where "skip hidden" and "skip
        empty" disagree. `缓存` is perfectly readable — it must still not be the
        thing the reader is shown."""
        r = preview_excel(HIDDEN_WITH_DATA, "audit.xlsx")
        self.assertEqual(r["sheet_name"], "审核排期表")

    def test_an_explicitly_asked_for_sheet_is_still_honoured(self):
        """The picker passes a sheet name. Skipping must not override a choice —
        here `乙` is readable too, so returning it proves priority rather than luck."""
        r = preview_excel(TWO_DATA_SHEETS, "audit.xlsx", sheet="乙")
        self.assertEqual(r["sheet_name"], "乙")

    def test_an_explicitly_asked_for_hidden_sheet_reports_the_file(self):
        """Naming the hidden technical sheet is a mistake, but the answer must
        still be about the FILE: 'Sheet 'Sheet1' 没有可导入的数据' sent the reader
        looking inside a tab that is not even visible."""
        with self.assertRaises(ValueError) as ctx:
            preview_excel(SAP_LIKE, "audit.xlsx", sheet="Sheet1")
        msg = str(ctx.exception)
        self.assertIn("没有可导入的数据", msg)
        self.assertNotIn("Sheet 'Sheet1'", msg)
        self.assertIn("审核排期表", msg, "it should list what it actually looked at")

    def test_no_data_anywhere_says_so_about_the_file(self):
        with self.assertRaises(ValueError) as ctx:
            preview_excel(NO_DATA, "audit.xlsx")
        msg = str(ctx.exception)
        self.assertIn("没有可导入的数据", msg)
        # ⚠️ Naming a sheet sends the reader looking inside the wrong tab, and a
        # hidden SAP sheet is not even a tab they can see.
        self.assertNotIn("Sheet1", msg.split("没有可导入的数据")[0])
        self.assertIn("Sheet1", msg, "the message should still name what it looked at")
        self.assertIn("CSV", msg, "a person needs to be told what to do next")

    def test_an_all_hidden_workbook_is_reported_about_rather_than_crashed_on(self):
        """The fallback in `_ordered_sheet_titles` is what keeps this from becoming
        an unopenable file with no explanation."""
        with self.assertRaises(ValueError) as ctx:
            preview_excel(ALL_HIDDEN, "audit.xlsx")
        msg = str(ctx.exception)
        self.assertIn("Sheet1", msg)
        self.assertIn("Sheet2", msg)
        self.assertIn("CSV", msg)

    def test_the_message_is_a_chinese_english_pair(self):
        """`kladoI18n` splits ONLY when the left side is Chinese, so a message
        without the pair shows English to a Chinese reader and vice versa."""
        with self.assertRaises(ValueError) as ctx:
            preview_excel(NO_DATA, "audit.xlsx")
        zh, sep, en = str(ctx.exception).partition(" / ")
        self.assertTrue(sep, "no ' / ' pair in the message")
        self.assertTrue(any("一" <= c <= "鿿" for c in zh), f"left side is not Chinese: {zh!r}")
        self.assertTrue(en[:1].isascii() and en[:1].isalpha(), f"right side: {en[:40]!r}")


class TestParse(unittest.TestCase):
    def test_registered_sheets_exclude_the_hidden_one(self):
        """This is the one that matters: an empty `sheets_meta` is what makes an
        uploaded file un-importable while still looking uploaded."""
        rs = parse_excel(SAP_LIKE, "audit.xlsx")
        self.assertEqual([r["sheet_name"] for r in rs], ["审核排期表"])
        self.assertEqual(rs[0]["row_count"], 2)

    def test_a_readable_hidden_sheet_is_never_registered_as_a_dataset(self):
        """Same reason as the preview case, and the one that actually separates the
        two explanations — see `HIDDEN_WITH_DATA`."""
        rs = parse_excel(HIDDEN_WITH_DATA, "audit.xlsx")
        self.assertEqual([r["sheet_name"] for r in rs], ["审核排期表"])

    def test_one_bad_sheet_does_not_cost_the_others(self):
        rs = parse_excel(COVER_FIRST, "audit.xlsx")
        self.assertEqual([r["sheet_name"] for r in rs], ["数据"])

    def test_nothing_importable_raises_rather_than_returning_nothing(self):
        """An empty return is the worst outcome: `/files/stage` stores it and the
        file appears in the dropdown with nothing to choose."""
        with self.assertRaises(ValueError) as ctx:
            parse_excel(NO_DATA, "audit.xlsx")
        self.assertIn("CSV", str(ctx.exception))

    def test_an_all_hidden_workbook_raises_instead_of_registering_nothing(self):
        with self.assertRaises(ValueError) as ctx:
            parse_excel(ALL_HIDDEN, "audit.xlsx")
        self.assertIn("CSV", str(ctx.exception))

    def test_an_explicitly_selected_visible_sheet_is_not_filtered_out(self):
        """A Smart Import job is mapped to explicit sheets. The hidden-sheet skip
        must not remove a name the caller asked for."""
        rs = parse_excel(SAP_LIKE, "audit.xlsx", sheet_names=["审核排期表"])
        self.assertEqual([r["sheet_name"] for r in rs], ["审核排期表"])

    def test_naming_a_hidden_sheet_does_not_import_it(self):
        """Deliberately the opposite of a claim worth pinning: asking for `Sheet1`
        by name still refuses. A hidden sheet is not a dataset, and silently
        returning one empty row set would look like a successful import."""
        with self.assertRaises(ValueError) as ctx:
            parse_excel(SAP_LIKE, "audit.xlsx", sheet_names=["Sheet1"])
        self.assertIn("CSV", str(ctx.exception))


class TestErrorMessagesReachTheReaderAsSentences(unittest.TestCase):
    """`throw new Error(await r.text())` puts `{"detail": "..."}` on screen. It was
    reported exactly that way — a raw JSON body sitting in the file preview."""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_no_call_site_uses_the_raw_body_as_the_message(self):
        self.assertNotIn("new Error(await r.text())", self.src,
                         "a failed call still puts the JSON envelope on screen")
        self.assertGreaterEqual(self.src.count("await apiErr(r)"), 20,
                                "the helper was added but the call sites were not converted")

    def test_the_helper_unwraps_detail_and_never_returns_empty(self):
        import re
        m = re.search(r"async function apiErr\(r\) \{.*?\n\}", self.src, re.S)
        self.assertIsNotNone(m, "apiErr is gone")
        body = m.group(0)
        self.assertIn(".detail", body, "the JSON envelope is not unwrapped")
        self.assertIn("r.text()", body, "a proxy answering plain text must still work")
        self.assertIn("r.status", body, "an empty body must not yield an empty message")
        # ⚠️ Substring presence is not behaviour. An early `return body;` keeps all
        # three substrings in place while making every one of them dead code, and
        # the suite stayed green through that mutation. Order is what carries the
        # unwrap, so order is what gets asserted.
        #
        # ⚠️ And `r.status` CANNOT be required to precede the `return`: it lives
        # inside the return expression, so its index is always after it. Asserting
        # that ordering fails against correct code — which is a guard nobody reads.
        first_return = body.index("return")
        self.assertLess(body.index(".detail"), first_return,
                        "a `return` before the unwrap leaves `.detail` as dead code — "
                        "the reader gets the raw JSON envelope again")
        self.assertIn("r.status", body[first_return:],
                      "the fallback for an empty body must live in the return expression")

    def test_apiErr_is_reachable_from_every_module(self):
        """It is a top-level function, so it must not live inside an IIFE — the
        inline `onclick` handlers that would use it run in global scope, which
        cannot see a module IIFE's `const`/`let`.

        ⚠️ Two traps in this assertion, both of which make it pass for the wrong
        reason if written naively:
          · the module marker is `const X = (() => {`, NOT `(() => {` — that
            substring also matches `.then(() => {` and `setInterval(() => {`;
          · the search must be bounded to the text BEFORE apiErr. Unbounded, the
            last IIFE in a 2MB file sits long after it and the assertion fails
            against correct code — or, with the comparison flipped, would pass
            against an apiErr that really is trapped.
        """
        import re
        self.assertNotIn("const apiErr =", self.src)
        self.assertIsNone(re.search(r"(?:const|let|var)\s+apiErr\s*=", self.src),
                          "apiErr must be a function declaration, not a binding")
        i = self.src.index("async function apiErr(")
        before = max((m.start() for m in
                      re.finditer(r"(?m)^\s*(?:const|let|var)\s+\w+\s*=\s*\(\(\)\s*=>\s*\{",
                                  self.src[:i])), default=-1)
        self.assertLess(before, i, "apiErr appears to be trapped inside a module IIFE")


if __name__ == "__main__":
    unittest.main()