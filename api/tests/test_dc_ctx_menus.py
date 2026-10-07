"""CI half of: the wall folder card has a menu, and "Add table" reads a local file.

`verify_dc_ctx_menus_ui.py` is the half that clicks in a real browser; it is not in
`scripts/ci_check.py`, so on its own this is a guard that only exists when somebody
remembers to run it.

⚠️ The two source readers are IMPORTED from `test_dc_folder_upload` rather than
copied. They are the exact code that already failed once: a block-comment regex
run over the whole 2.1MB `index.html` matches a `/*` inside a JS REGEX LITERAL and
swallows ~200k characters, after which every assertion reports "not found" — which
reads like a missing feature rather than a broken reader. One copy, already
debugged.

What this file holds, each item being something that fails silently:

  · every item on the wall card's menu has a handler, and every handler is in the
    IIFE's export table (a name that is not exported is a button that throws);
  · the four actions the backend CANNOT do to a folder are ABSENT — asserted by
    their absence, so nobody "completes" the menu with an item that returns 200
    and moves nothing;
  · the Root card cannot offer to delete the whole of someone's Files;
  · a card that can be right-clicked says so in the markup, with both
    `preventDefault` and `stopPropagation`;
  · "upload files here" / "new subfolder here" carry the chosen FOLDER, and the
    destination is consumed and cleared on use — from the wall `_fbPrefix` is '',
    so an implementation that ignored it would upload to the root and succeed;
  · the new-folder dialog has ONE opener with three callers, not three copies;
  · "Add table" has a real file input, and the picked file is selected BY ID
    (two files can share a name; `object_name` carries a timestamp and a uuid);
  · `list_files` skips the `.keep` folder marker.

    ../.venv312/bin/python -m unittest discover -s tests -p "test_*.py"
"""
import re
import unittest

from tests.test_dc_folder_upload import _js, _markup   # the debugged readers

from pathlib import Path

# ⚠️ Absolute: `ci_check.py` runs unittest with cwd=api, so a relative
# "api/services/..." does not resolve there. Anchored on __file__ instead.
DB = Path(__file__).resolve().parents[2] / "api" / "services" / "data_center_db.py"

WALL_MENU = r'<div id="dc-wall-ctx".*?</div>\s*\n\s*</div>'


class TestWallCardMenu(unittest.TestCase):
    def setUp(self):
        self.js = _js()
        self.markup = _markup()

    def _menu(self):
        m = re.search(WALL_MENU, self.markup, re.S)
        self.assertIsNotNone(m, "no #dc-wall-ctx menu in the markup")
        return m.group(0)

    def test_menu_offers_exactly_the_actions_that_work(self):
        block = self._menu()
        handlers = re.findall(r'onclick="(dc\.\w+\(\))"', block)
        self.assertEqual(sorted(handlers), sorted([
            "dc.wallCtxOpen()", "dc.wallCtxNewDir()", "dc.wallCtxUpload()",
            "dc.wallCtxUploadFolder()", "dc.wallCtxDelete()",
        ]), f"handlers={handlers}")

    def test_no_item_is_wired_to_a_dead_action(self):
        """The backend cannot rename, move or publish a FOLDER.

        `rename_file` / `move_file` copy one MinIO object key, and a folder is only
        a shared key PREFIX with no object of its own — so calling them returns 200
        and moves nothing. A visible item that does nothing is indistinguishable
        from a broken button, so they are asserted ABSENT.
        """
        block = self._menu()
        for dead in ("fbFileCtxRename", "fbFileCtxMove", "fbFileCtxPublish",
                     "fbFileCtxDownload", "fbFileCtxShare"):
            self.assertNotIn(dead, block, f"{dead} cannot work on a folder")

    def test_root_card_cannot_offer_delete(self):
        block = self._menu()
        self.assertRegex(block, r'id="dc-wall-ctx-del"[^>]*onclick="dc\.wallCtxDelete\(\)"',
                         "the delete item lost its id, so it cannot be hidden for Root")
        self.assertRegex(block, r'class="dc-fb-ctx-sep dc-wall-ctx-danger-sep"',
                         "the separator above Delete must be hideable with it")

    def test_delete_is_hidden_for_root_at_runtime(self):
        m = re.search(r"function wallShowCtx\([^)]*\)\s*\{(.*?)\n  \}", self.js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("dc-wall-ctx-del", body, "Root would be offered Delete")
        # ⚠️ Anchored on `del`, not on the bare ternary. The separator line right
        # below it is character-for-character the same, so a looser pattern is
        # satisfied by the WRONG line: mutating the delete item to always show left
        # this assertion green, because the separator's copy still matched.
        self.assertRegex(body, r"if \(del\) del\.style\.display = _wallCtxPath \? '' : 'none';",
                         "the delete item is not conditional on the folder")
        self.assertRegex(body, r"if \(sep\) sep\.style\.display = _wallCtxPath \? '' : 'none';",
                         "the separator is not hidden with it, so Root grows an "
                         "orphan rule above nothing")

    def test_the_cards_can_be_right_clicked(self):
        cards = re.findall(r'class="dc-folder"[^>]*?oncontextmenu=', self.markup, re.S)
        self.assertGreaterEqual(len(cards), 2,
                                "both the Root card and a folder card need a menu")
        for hook in re.findall(r'oncontextmenu="([^"]+)"', self.markup):
            if "wallShowCtx" in hook:
                self.assertIn("event.preventDefault()", hook)
                self.assertIn("event.stopPropagation()", hook)

    def test_every_handler_is_exported(self):
        for name in ("wallShowCtx", "wallHideCtx", "wallCtxOpen", "wallCtxNewDir",
                     "wallCtxUpload", "wallCtxUploadFolder", "wallCtxDelete",
                     "fbOpenMkdir", "fbUploadTo", "fbUploadFolderTo",
                     "dcAtPickLocal", "dcAtUploadLocal"):
            self.assertIn(f"function {name}(", self.js, f"{name} is not defined")
            self.assertRegex(self.js, rf"\b{name},",
                             f"{name} is not exported, so `dc.{name}` is undefined")

    def test_an_outside_click_closes_it(self):
        # ⚠️ To the terminator, not to end-of-line: this handler is written across
        # several lines, and a `[^\n]*` match stops before the line that closes it.
        handler = re.search(r"document\.addEventListener\('click'.*?\}, true\);", self.js, re.S)
        self.assertIsNotNone(handler)
        self.assertIn("wallHideCtx()", handler.group(0),
                      "the menu would stay open over everything after one right-click")
        for other in ("fbHideCtx()", "fbHideFileCtx()", "dsHideCtx()"):
            self.assertIn(other, handler.group(0), f"{other} lost its outside-click close")


class TestMenuActionsTargetTheChosenFolder(unittest.TestCase):
    def setUp(self):
        self.js = _js()

    def test_upload_destination_is_overridable(self):
        self.assertIn("let _fbUploadDest = ''", self.js)
        for name in ("function fbUploadTo(", "function fbUploadFolderTo("):
            m = re.search(re.escape(name) + r".*?\}", self.js, re.S)
            self.assertIsNotNone(m, f"{name} is missing")
            self.assertIn("_fbUploadDest = path", m.group(0),
                          f"{name} does not carry the chosen folder")

    def test_the_destination_is_consumed_and_cleared(self):
        """A cancelled picker is the most likely way to reach the early return, and
        a destination left behind would silently redirect the NEXT upload."""
        for fn in ("async function fbUploadFiles(", "async function fbUploadFolder("):
            m = re.search(re.escape(fn) + r".*?\n  \}", self.js, re.S)
            self.assertIsNotNone(m, f"{fn} is missing")
            body = m.group(0)
            self.assertIn("_fbUploadDest = ''", body,
                          f"{fn} never clears the override")
            # The clear must come BEFORE the empty-FileList return: a cancelled
            # picker is the most likely way to reach it.
            clear_at = body.index("_fbUploadDest = ''")
            guard = body.find("if (!files.length) return")
            self.assertLess(clear_at, guard,
                           f"{fn} returns before clearing the override, so a "
                           f"cancelled picker redirects the NEXT upload")
            self.assertIn("_fbUploadDest || _fbPrefix", body,
                          f"{fn} does not fall back to the folder being viewed")

    def test_the_new_folder_dialog_has_one_opener(self):
        """Three callers used to each carry their own copy of the state they had to
        set first, and two copies of one variable drift."""
        openers = re.findall(r"function fbOpenMkdir\(", self.js)
        self.assertEqual(len(openers), 1)
        self.assertEqual(self.js.count("function fbOpenMkdir("), 1)
        # And the old hand-rolled copies are gone: only fbOpenMkdir may set the
        # parent AND open the dialog.
        for fn in ("async function fbMkdirCurrent(", "function fbCtxNewDir(",
                   "function wallCtxNewDir("):
            m = re.search(re.escape(fn) + r".*?\n  \}", self.js, re.S)
            self.assertIsNotNone(m, f"{fn} is missing")
            body = m.group(0)
            if "dc-fb-mkdir-modal" in body:
                self.assertIn("fbOpenMkdir(", body,
                              f"{fn} opens the dialog itself instead of delegating")


class TestAddTableReadsALocalFile(unittest.TestCase):
    def setUp(self):
        self.js = _js()
        self.markup = _markup()

    def test_the_dialog_has_a_file_picker(self):
        m = re.search(r'<input[^>]*id="dc-at-local-input"[^>]*>', self.markup)
        self.assertIsNotNone(m, "no #dc-at-local-input: the dialog can only list what "
                                 "is already in the library, so the FIRST file a "
                                 "person imports cannot be chosen here")
        tag = m.group(0)
        self.assertIn('type="file"', tag)
        self.assertIn("dc.dcAtUploadLocal(this.files)", tag)
        for ext in (".xlsx", ".csv"):
            self.assertIn(ext, tag, f"{ext} is not accepted")

    def test_a_button_opens_it(self):
        self.assertRegex(self.markup, r'onclick="dc\.dcAtPickLocal\(\)"',
                         "nothing opens the picker")
        m = re.search(r"function dcAtPickLocal\(\).*?\}", self.js, re.S)
        self.assertIsNotNone(m)
        self.assertIn("getElementById('dc-at-local-input').click()", m.group(0))

    def test_the_picked_file_is_staged_and_selected_by_id(self):
        m = re.search(r"async function dcAtUploadLocal\(.*?\n  \}", self.js, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("/files/stage", body, "the file is never entered into the library")
        # ⚠️ By id, not by filename: `stage_file` prefixes object_name with a
        # timestamp and a uuid, and two uploads can share a filename.
        self.assertIn("sel.value = String(staged.id)", body,
                      "the picked file is not selected in the dropdown")
        self.assertIn("dcAtSheets()", body, "its sheets are never loaded")
        self.assertIn("dcAtLoadFiles()", body, "the dropdown is never re-read")
        self.assertIn("dc-at-local-hint", body,
                      "the reader is never told whether it worked")

    def test_the_picker_resets_so_the_same_file_can_be_picked_twice(self):
        m = re.search(r"async function dcAtUploadLocal\(.*?\n  \}", self.js, re.S)
        self.assertRegex(m.group(0), r"input\.value = ''")


class TestListFilesHidesFolderMarkers(unittest.TestCase):
    """The `.keep` row is a folder MARKER, and it is load-bearing.

    `browse_files` drops any folder with no visible object under it, and an empty
    folder's only object is its `.keep` — so deleting the row would make every
    freshly created empty folder vanish from the wall. It has to stay. What must
    not happen is a person being offered it as a source file, which is what made
    "Add table" look like a broken dialog.
    """

    def setUp(self):
        self.src = DB.read_text(encoding="utf-8")

    def _list_files(self):
        m = re.search(r"def list_files\(.*?\n(?=def )", self.src, re.S)
        self.assertIsNotNone(m)
        return m.group(0)

    def test_markers_are_filtered(self):
        body = self._list_files()
        self.assertIn(".keep", body, "list_files still offers folder markers as files")
        self.assertRegex(body, r"rsplit\(\"/\", 1\)\[-1\]\.startswith\(\"\.keep\"\)",
                         "the filter must test the LAST SEGMENT, so a folder merely "
                         "named 'keep' or containing '.keep' in its path is not hidden")

    def test_the_other_readers_agree(self):
        """Four places already skip `.keep`. They are the reason the fifth was a
        bug rather than a design choice, so the set is asserted, not just the one."""
        for fn in ("def browse_files(", "def build_tree("):
            m = re.search(re.escape(fn) + r".*?\n(?=def )", self.src, re.S)
            if m:
                self.assertIn(".keep", m.group(0), f"{fn} stopped skipping markers")

    def test_folder_creation_still_registers_the_marker(self):
        """The other half of the contract: if someone ever 'cleans up' the marker
        row, empty folders disappear from the wall."""
        m = re.search(r"def create_folder\(.*?\n(?=def )", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("_insert_library_row", m.group(0),
                      "create_folder no longer registers the marker, so an empty "
                      "folder will not appear on the wall")


if __name__ == "__main__":
    unittest.main()
