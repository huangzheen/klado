"""Static + unit half of "upload a whole local folder" and "the dataset card has a menu".

`verify_dc_folder_import_ui.py` is the half that runs a real browser against a real
server; it is not in `scripts/ci_check.py` and therefore, on its own, a guard that
only exists when somebody remembers to run it. This is the half CI runs, and it
holds the three things that are cheap to check and expensive to discover late:

  · the folder picker really carries `webkitdirectory` — without it the input is
    a plain file picker, "upload a folder" quietly uploads ONE file from it, and
    nothing errors anywhere;
  · the dataset card's menu exists, names the card, and every item has a handler
    that is actually exported (an item wired to a name the IIFE does not export is
    a visible button that throws);
  · the i18n labels are `中文 / English` PAIRS, with Chinese on the left — the
    walker only splits a pair whose left side is Chinese, and a menu that shows
    both languages at once reads as a translation bug;
  · `_safe_folder` refuses traversal. This one matters most: the `folder` field
    used to come only from the page's current directory, so it never needed
    checking. A folder upload feeds it the browser's `webkitRelativePath`, and
    that is the first time a `..` could actually arrive.

    ../.venv312/bin/python -m unittest discover -s tests -p "test_*.py"
"""
import re
import unittest
from pathlib import Path

from fastapi import HTTPException

from routers.data_center import _safe_folder

INDEX = (Path(__file__).resolve().parents[2] / "frontend" / "out" / "index.html")


def _markup() -> str:
    """The document with HTML comments blanked (newlines kept).

    ⚠️ Safe over the WHOLE file, because `<!-- -->` never appears inside a JS
    regex literal. The JS block-comment form does — see `_js`.
    """
    def blank(m):
        return " " + "\n" * m.group(0).count("\n")
    return re.sub(r"<!--.*?-->", blank, _index(), flags=re.S)


def _js() -> str:
    """The `dc` IIFE, comments blanked, newlines kept.

    ⚠️ Do NOT strip JS comments from this whole file with a block-comment regex.
    I did, and it ate ~200,000 characters: a `/…/` REGEX LITERAL containing `/*`
    opens a "comment" that swallows everything up to some later `*/`. Nothing
    errors — the assertions just report "not found", which reads like a missing
    feature instead of a broken reader. Same failure as a guard that has quietly
    stopped looking at the code.

    So it is scoped to the hand-written IIFE, and only strips comments that START
    A LINE. Both rules match what a hand-written comment actually looks like, and
    neither can match `.split('/')` or `https://`.
    """
    src = _index()
    start = src.index("const dc = (() => {")
    end = src.index("\n})();", start)
    body = src[start:end]
    def blank(m):
        return " " + "\n" * m.group(0).count("\n")
    body = re.sub(r"(?ms)^[ \t]*/\*.*?^[ \t]*\*/", blank, body)
    body = re.sub(r"(?m)^[ \t]*//[^\n]*$", " ", body)
    body = re.sub(r"(?<=\s)//[^\n]*$", "", body, flags=re.M)
    return body


def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


class TestFolderPicker(unittest.TestCase):
    def test_picker_input_is_a_directory_picker(self):
        body = _markup()
        m = re.search(r"<input[^>]*id=\"dc-fb-folder-input\"[^>]*>", body)
        self.assertIsNotNone(m, "no #dc-fb-folder-input: the folder picker is gone")
        tag = m.group(0)
        self.assertIn("webkitdirectory", tag,
                      "the input is not a directory picker — picking a folder would "
                      "upload ONE file from it, silently")
        self.assertIn("directory", tag, "standards-track spelling missing")
        self.assertIn("multiple", tag, "a folder has many files")

    def test_toolbar_button_opens_that_input(self):
        # The button is markup; the function that clicks the input is JS.
        self.assertIn('onclick="dc.fbUploadFolderClick()"', _markup())
        body = _js()
        self.assertIn("function fbUploadFolderClick()", body)
        self.assertIn("getElementById('dc-fb-folder-input').click()", body)

    def test_folder_upload_keeps_every_segment_but_the_filename(self):
        """The picked folder's OWN NAME must survive.

        ⚠️ `webkitRelativePath.split('/').slice(1, -1)` drops the first segment,
        which reads like "that is the folder they already chose, so it is
        redundant" — and it scatters the contents into wherever the reader was
        standing while losing the folder's name. A browser test caught it: the
        wall came back showing the subfolder and not the folder.
        """
        body = _js()
        self.assertIn("webkitRelativePath", body)
        # ⚠️ Capture the WHOLE slice call. An earlier version of this line matched
        # only up to `slice(` — the arguments were outside the match, so
        # `assertNotIn("slice(1, -1)")` was a statement about a string that could
        # not contain it. It passed under the exact bug it was written to catch,
        # and the mutation test is what said so.
        m = re.search(r"\.split\('/'\)\.(slice\([^)]*\))", body)
        self.assertIsNotNone(m, "cannot find the relative-path split in fbUploadFolder")
        self.assertEqual("slice(0, -1)", m.group(1),
                         "the picked folder's own name is dropped: the reader picks "
                         "Sales Data/ and gets its contents scattered instead")

    def test_upload_refresh_uses_the_visible_surface_helper(self):
        """Uploading from the wall must repaint the WALL.

        `fbLoad()` fills the folder list, which is hidden while the wall is up —
        the same trap the mkdir handler had, and the same silent no-op.
        """
        body = _js()
        start = body.index("async function fbUploadFolder(")
        end = body.index("async function fbUploadOne(", start)
        self.assertIn("fbRefreshVisible()", body[start:end],
                      "fbUploadFolder finishes on fbLoad() — nothing repaints on the wall")


class TestDatasetCardMenu(unittest.TestCase):
    def test_menu_exists_with_all_four_handlers(self):
        body = _markup()
        m = re.search(r'<div id="dc-ds-ctx".*?</div>\s*\n\s*</div>', body, re.S)
        self.assertIsNotNone(m, "no #dc-ds-ctx menu in the markup")
        block = m.group(0)
        for handler in ("dc.dsCtxOpen()", "dc.dsCtxAddTable()",
                        "dc.dsCtxImportExternal()", "dc.dsCtxDelete()"):
            self.assertIn(handler, block, f"{handler} is not on the menu")

    def test_every_menu_item_has_a_handler(self):
        body = _markup()
        m = re.search(r'<div id="dc-ds-ctx".*?</div>\s*\n\s*</div>', body, re.S)
        items = re.findall(r'<div class="dc-fb-ctx-item[^"]*"([^>]*)>', m.group(0))
        self.assertTrue(items, "the menu has no items")
        for attrs in items:
            self.assertIn("onclick=", attrs, f"a menu item with no handler: {attrs}")

    def test_handlers_are_defined_and_exported(self):
        """An item wired to a name the IIFE does not export is a button that throws."""
        body = _js()
        for name in ("dsShowCtx", "dsHideCtx", "dsCtxOpen", "dsCtxAddTable",
                     "dsCtxImportExternal", "dsCtxDelete"):
            self.assertIn(f"function {name}(", body, f"{name} is not defined")
            self.assertRegex(body, rf"\b{name},",
                             f"{name} is not in the export table, so `dc.{name}` is undefined")

    def test_card_carries_the_context_menu_hook(self):
        body = _markup()
        card = re.search(r"class=\"dc-proj\" data-slug=.*?title=\"", body, re.S)
        self.assertIsNotNone(card, "the dataset card markup moved")
        hook = body[card.start():card.start() + 700]
        self.assertIn("oncontextmenu=", hook, "a dataset card cannot be right-clicked")
        # Without these two the browser's own menu covers ours, or the click lands
        # on the card behind and navigates as the menu opens.
        self.assertIn("event.preventDefault()", hook)
        self.assertIn("event.stopPropagation()", hook)

    def test_menu_names_the_card_it_acts_on(self):
        """A menu on a card called '华东 2026' that does not say so makes the reader
        hunt the wall to find out which card is armed."""
        # The empty title row is markup; filling it in is JS.
        self.assertRegex(_markup(), r'<div class="dc-ds-ctx-title"[^>]*></div>',
                         "no title row on the dataset menu")
        m = re.search(r"function dsShowCtx\([^)]*\)\s*\{(.*?)\n  \}", _js(), re.S)
        self.assertIsNotNone(m)
        self.assertIn("dc-ds-ctx-title", m.group(1),
                      "the menu never fills in the card's name")

    def test_outside_click_closes_this_menu_too(self):
        body = _js()
        # ⚠️ Match to the terminator, not to end-of-line. This handler used to be a
        # single line and the assertion read `[^;]*`; when it was re-wrapped across
        # lines the match stopped at the first newline and started reporting the
        # menu as never closing. A guard's READER is part of the guard, and editing
        # the code it reads can break it with no red anywhere else.
        handler = re.search(r"document\.addEventListener\('click'.*?\}, true\);", body, re.S)
        self.assertIsNotNone(handler)
        self.assertIn("dsHideCtx()", handler.group(0),
                      "the dataset menu never closes on an outside click")

    def test_labels_are_bilingual_pairs_with_chinese_on_the_left(self):
        """`kladoI18n.t()` only splits `中文 / English`; a reversed or half pair is
        returned whole, so the menu shows BOTH languages at once."""
        body = _markup()
        m = re.search(r'<div id="dc-ds-ctx".*?</div>\s*\n\s*</div>', body, re.S)
        labels = re.findall(r'<i class="[^"]*"></i>\s*([^<]+)</div>', m.group(0))
        self.assertTrue(labels, "no labels found in the dataset menu")
        for label in labels:
            label = label.strip()
            self.assertIn(" / ", label, f"not a bilingual pair: {label!r}")
            left = label.split(" / ")[0]
            self.assertTrue(re.search(r"[\u4e00-\u9fff]", left),
                            f"the left side is not Chinese, so the walker will not "
                            f"split it and both languages show: {label!r}")



class TestSafeFolder(unittest.TestCase):
    """`folder` used to come only from `_fbPrefix` and never needed validating."""

    def test_plain_paths_pass_through(self):
        self.assertEqual(_safe_folder(None, ""), "")
        self.assertEqual(_safe_folder(None, None), "")
        self.assertEqual(_safe_folder(None, "2026Q3"), "2026Q3")
        self.assertEqual(_safe_folder(None, "2026Q3/华东/销售"), "2026Q3/华东/销售")

    def test_surrounding_and_duplicate_slashes_are_normalised(self):
        self.assertEqual(_safe_folder(None, "/2026Q3/"), "2026Q3")
        self.assertEqual(_safe_folder(None, "//2026Q3//华东//"), "2026Q3/华东")

    def test_windows_separators_are_accepted_not_kept(self):
        # A `\` inside an object key is legal but it is not a directory separator in
        # MinIO, so a Windows-shaped path would become one object with a backslash
        # in its name rather than a folder.
        self.assertEqual(_safe_folder(None, "2026Q3\\华东"), "2026Q3/华东")

    def test_dot_segments_are_dropped_not_followed(self):
        self.assertEqual(_safe_folder(None, "a/./b"), "a/b")

    def test_traversal_is_refused(self):
        for bad in ("..", "../secret", "a/../../secret", "a/..", "/../etc"):
            with self.assertRaises(HTTPException, msg=f"accepted {bad!r}") as ctx:
                _safe_folder(None, bad)
            self.assertEqual(ctx.exception.status_code, 400, f"{bad!r} was not a 400")

    def test_backslash_traversal_is_refused_too(self):
        with self.assertRaises(HTTPException):
            _safe_folder(None, "a\\..\\..\\secret")


if __name__ == "__main__":
    unittest.main()
