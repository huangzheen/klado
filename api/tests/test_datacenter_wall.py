"""Data Center opens on a wall of folder cards, and a folder is 目录 + 表格.

The refactor removed three panes and two tabs and put one wall in front of them, and
the failure mode for that is not an exception — it is a page that loads, looks
plausible, and is quietly wrong. Every assertion in this file exists because of a
specific way that goes wrong:

**A dead reference throws in a place nobody looks.** `document.getElementById('x')`
returning null does not raise; `.classList` on it raises, or — worse — the `if (el)`
guard swallows it and the feature is simply never wired up. The three-pane layout had
`#dc-fb-list-wrap` (the drop target) and `#dc-fb-crumb` (the location); the new layout
has `#dc-toc-drop` and `#dc-crumbs`. Rename one container and the drag-and-drop
highlight stops appearing, uploads stop working by drag, and no test fails. So this
file checks the reverse direction too: every id the handlers look up must still EXIST
in the markup. That is the check that catches the rename, and it is why the dead names
are asserted absent rather than merely unused.

**`hidden` only works while the CSS does not set `display`.** The browser's
`[hidden] { display: none }` is a default, and ANY author `display` beats it. The
wall and the folder view are toggled with the attribute (`.kb-wall` / `.kb-body` do
the same thing on the knowledge page), so a `display: flex` added to `.dc-wall` puts
the cards and the directory on screen at the same time, one on top of the other.
`test_frontend_markup.py` cannot see this: it parses markup, not cascade.

**Counts that can only be one value are worse than no count.** The folder card says
"N files · M datasets". Both numbers are real, and a card that only counted files
would report an empty folder for one holding a table.

No browser, no server, no database: this reads the two files and checks the structure.
"""
import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX = os.path.join(REPO, "frontend", "out", "index.html")
CSS = os.path.join(REPO, "frontend", "out", "app.css")

#: The Data Center module's source, taken between its IIFE header and its end banner.
#: Slicing it out is what lets the dead-reference checks ignore the knowledge page's
#: `kb-*` ids, which are unrelated and legitimately similar.
DC_START = "const dc = (() => {"
DC_END = "// ═══════════════════ END DATA CENTER"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def dc_source():
    src = _read(INDEX)
    start = src.index(DC_START)
    end = src.index(DC_END)
    return src[start:end]


def markup():
    return _read(INDEX)


def css():
    return _read(CSS)


def _strip_comments(text):
    """HTML comments and JS/CSS comments, leaving code and string literals alone.

    ⚠️ ⚠️ Read this before "simplifying" it. Two earlier versions were wrong in ways
    that only showed up against this repository's own comments:

    1. Stripping `//[^\n]*` anywhere in the line eats the rest of a line after a
       `//` inside a STRING — and this file's markup is full of URLs
       (`API + '/files/tree'`) — so it silently deleted real code after the first
       `https://` on any line, and the "dead name is gone" checks started passing
       for the wrong reason.
    2. Anchoring `//` to the start of a line (`^\\s*//`) misses the TRAILING comment,
       which is the house style here: `const x = 1;   // why`. Those are exactly the
       ones that mention the removed names, so the check found nothing to complain
       about and stopped guarding anything.

    So: strip block comments, strip HTML comments, and strip line comments that
    begin a line or follow a `;`/`{`/`}` — the two places they actually occur here.
    This is not a JS parser and does not claim to be; it is a filter good enough to
    answer "is this identifier still called anywhere in the code", which is the only
    question it is used for.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^[ \t]*//[^\n]*", "", text)
    text = re.sub(r"(?<=[;{}])[ \t]*//[^\n]*", "", text)
    return text


class WallLayoutTests(unittest.TestCase):
    """The two views and the one way in and out of each."""

    def test_wall_and_folder_view_both_exist_in_markup(self):
        html = markup()
        for el_id in ("dc-wall", "dc-crumbs", "dc-body", "dc-toc", "dc-fb-list",
                      "dc-fb-preview"):
            self.assertIn('id="%s"' % el_id, html,
                          "%s is missing — the wall or the folder view has no root" % el_id)

    def test_folder_view_starts_hidden_so_the_wall_opens_the_page(self):
        # Both are toggled by JS on entry. If `#dc-body` were visible in the static
        # markup, the first paint would be an empty directory and the wall would
        # appear a frame later — the page would look like it flickered.
        html = markup()
        for el_id in ("dc-wall", "dc-crumbs", "dc-body"):
            m = re.search(r'<div[^>]*id="%s"[^>]*>' % el_id, html)
            self.assertIsNotNone(m, "%s not found" % el_id)
            self.assertIn("hidden", m.group(0),
                          "%s must ship `hidden`; JS decides which view is up" % el_id)

    def test_breadcrumb_and_wall_are_mutually_exclusive(self):
        """The breadcrumb must not be up while the wall is.

        This is the shape of a bug that costs a reader their bearings with no error
        anywhere: the wall says "pick a folder" and the breadcrumb says "you are in
        2026-10", and there is no Back button in sight. The two are toggled by the
        same pair of functions, so the check is that both agree.

        ⚠️ The functions are sliced by their `function <name>` header rather than by
        any comment text: an earlier version of this test anchored on the sentence
        explaining the removal, so rewording the comment — or any edit that moved a
        line — broke a test about behaviour. A test that fails when a comment is
        reworded trains people to not reword comments, which is backwards.
        """
        src = dc_source()

        def body_of(name):
            start = src.index("function " + name)
            # A top-level function inside the IIFE ends at the first line that is
            # exactly two spaces + `}`.
            end = src.index("\n  }", start)
            return src[start:end]

        for fn in ("dcShowWall", "dcOpenFolder"):
            self.assertIn("function " + fn, src, "%s is gone" % fn)
        show = body_of("dcShowWall")
        open_ = body_of("dcOpenFolder")
        self.assertRegex(show, r"wall\.hidden\s*=\s*false")
        self.assertRegex(show, r"crumbs\.hidden\s*=\s*true")
        self.assertRegex(show, r"body\.hidden\s*=\s*true")
        self.assertRegex(open_, r"wall\.hidden\s*=\s*true")
        self.assertRegex(open_, r"crumbs\.hidden\s*=\s*false")
        self.assertRegex(open_, r"body\.hidden\s*=\s*false")

    def test_section_tabs_are_gone(self):
        """One section means no tab row, and no tab row means no tab handlers.

        ⚠️ This checks the module's PUBLIC SURFACE — the object literal it returns —
        not the text of the file. Three earlier versions grepped for `showSection`
        and all three were wrong: every removal in this refactor left a comment
        explaining what was removed, so the name is in the file four times, always in
        prose. A test that greps for a name someone is documenting the removal of is
        a test that fails on good work.

        The export list is the thing that actually decides what a caller can reach,
        and it cannot be satisfied by a comment.
        """
        exports = self._exports()
        for present in ("showSection", "loadDatasets", "filterDatasets"):
            self.assertIn(present, exports)
        for gone in ("renderDatasets",
                     "fbNavigate", "fbTreeToggle", "fbSelectFromTree",
                     "executeImport", "resetUpload", "onFileSelect"):
            self.assertNotIn(gone, exports,
                             "`dc.%s` is still exported but the thing behind it is "
                             "gone; a caller reaching for it gets undefined" % gone)

        # And the surface a caller actually needs must be on it.
        for needed in ("showWall", "loadWall", "openFolder", "fbLoad",
                       "fbSelectFile", "fbSelectDataset", "filterItems"):
            self.assertIn(needed, exports,
                          "`dc.%s` is not exported, so the markup's onclick handlers "
                          "cannot reach it" % needed)
        html = markup()
        self.assertIn('id="dc-section-datasets"', html)
        for gone in ('id="dc-tab-upload"', 'id="dc-tab-datasets"'):
            self.assertNotIn(gone, _strip_comments(html),
                             "%s is still in the markup" % gone)

    @staticmethod
    def _exports():
        """`{ public name: local name }` from the module's `return { … }` literal.

        Both forms count: a bare `name,` exports a function of the same name, and
        `public: local` exports a function under another name. The distinction is the
        whole point — see `test_every_export_has_a_definition`.
        """
        src = dc_source()
        m = re.search(r"\n  return \{(.*?)\n  \};", src, re.S)
        if not m:
            raise AssertionError("the Data Center module has no return statement")
        body = _strip_comments(m.group(1))
        out = {}
        for entry in body.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                public, local = entry.split(":", 1)
                out[public.strip()] = local.strip()
            else:
                out[entry] = entry
        return out

    def test_every_export_has_a_definition(self):
        """⚠️ This is the one that matters, and it is here because of a real outage.

        The first version of the export list was
        `showWall, loadWall, openFolder, filterItems, …` while the functions were
        named `dcShowWall` and `dcOpenFolder`. `loadWall` and `filterItems` matched
        and the module LOOKED fine — in the source, in review, and in every test
        written before this one. At load time JavaScript evaluates the whole object
        literal before assigning it, so the page rendered, logged exactly one error
        (`ReferenceError: showWall is not defined`), and had no Data Center at all.
        No wall, no folder, no table, and a static test suite that was green.

        So: every name on the way out must be a name that exists on the way in.
        """
        exports = self._exports()
        code = _strip_comments(dc_source())
        for public, local in sorted(exports.items()):
            if not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", local):
                # A computed key (`'dc-thing': x`) is not a binding; skip it rather
                # than inventing a rule for a shape the module does not use.
                continue
            self.assertRegex(
                code, r"(function|const|let|var|async function)\s+%s\b" % re.escape(local),
                "the module exports %r but nothing named %r is defined — the object "
                "literal is evaluated before it is assigned, so this is a "
                "ReferenceError at load time and the whole page loses this module"
                % (public, local))

    def test_the_public_names_match_the_markup(self):
        """`onclick="dc.showWall()"` needs `dc.showWall` to exist.

        The markup's handlers are the other half of the contract: they are strings
        in the HTML, so nothing checks them until somebody clicks.
        """
        exports = self._exports()
        # ⚠️ Comments are stripped: the removal notes quote the old handler names
        # verbatim (`onclick="dc.showSection('upload')" used to call …`), so scanning
        # the raw markup reports handlers for code that was deleted on purpose and
        # sends you off to "fix" a removal by re-adding the dead function.
        handlers = set(re.findall(r"dc\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
                                  _strip_comments(markup())))
        for name in sorted(handlers):
            if not name.startswith(("fb", "open", "close", "load", "show", "filter",
                                    "pull", "remove", "add", "select", "qi", "execute",
                                    "delete", "rename", "detail", "reset")):
                continue
            self.assertIn(name, exports,
                          "the markup calls dc.%s(...) but the module does not export "
                          "it — every control that uses it is dead" % name)


    def test_the_deleted_wizard_is_really_gone(self):
        """The old three-step upload wizard reached for markup that never existed.

        `onDrop` called `onFileStage`, a function that was never defined, so the
        first drag threw a `ReferenceError`. It was unreachable, and it sat in the
        middle of the module this refactor touches.

        The check is on DEFINITIONS and the export list, not on the text: the
        comment explaining the removal names every one of these, and grepping the
        text would flag that comment.
        """
        code = _strip_comments(dc_source())
        for dead in ("onFileStage", "onFileSelect", "renderPlan", "setStep",
                     "resetUpload"):
            self.assertNotRegex(code, r"function %s\b" % dead,
                                "%s is defined again" % dead)
            self.assertNotRegex(code, r"\b%s\(" % dead,
                                "%s is called" % dead)
        # The ids it reached for were never in the document.
        html = _strip_comments(markup())
        for gone in ('id="dc-dropzone"', 'id="dc-plan-body"', 'id="dc-confirm-btn"',
                     'id="dc-step1-panel"', 'id="dc-step2-panel"'):
            self.assertNotIn(gone, html,
                             "%s reappeared, which is what the dead wizard looked for" % gone)

    def test_entry_point_lands_on_the_wall(self):
        """Entering the page must show the wall, not a folder.

        The page opens on whatever folder was open last time otherwise, and a page
        that reopens where you left it cannot be re-entered the same way twice.
        """
        src = _read(INDEX)
        m = re.search(r"function switchToDataCenter\(\)\s*\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("dc.showWall()", body)
        self.assertIn("dc.showSection('upload')", body)


class DeadReferenceTests(unittest.TestCase):
    """Every id the handlers look up must still be in the markup.

    This is the whole reason this file exists. A stale id is a silent feature death,
    and the two that matter most are the drop target and the breadcrumb: both were
    renamed by this refactor, and both fail by doing nothing.
    """

    def _handlers(self):
        return dc_source()

    def test_no_getelementbyid_targets_a_missing_element(self):
        html = markup()
        present = set(re.findall(r'id="([^"]+)"', html))
        # `getElementById` inside string-built markup (the sheet tabs, the move tree)
        # is not a lookup of a static element, so only the ones that are plainly
        # `document.getElementById('literal')` are checked.
        for el_id in re.findall(r"getElementById\('([^']+)'\)", self._handlers()):
            if "'" in el_id or "+" in el_id:
                continue
            if el_id.startswith("dc-") or el_id.startswith("kb-"):
                self.assertIn(el_id, present,
                              "dc looks up #%s but no element has that id — the "
                              "handler is dead code" % el_id)

    def test_drop_target_id_matches_its_handler(self):
        """The drop target is the one whose failure is completely silent.

        `fbDragOver` adds a class to `#dc-toc-drop`. If the markup kept the old
        `#dc-fb-list-wrap`, `el` is null and the `if (el)` guard makes the drop
        highlight never appear — drag-and-drop upload stops working and nothing logs.
        """
        html = markup()
        self.assertIn('id="dc-toc-drop"', html)
        self.assertNotIn('id="dc-fb-list-wrap"', html,
                         "the old three-pane drop target is still in the markup")
        src = self._handlers()
        for fn in ("fbDragOver", "fbDragLeave", "fbDrop"):
            self.assertIn("dc-toc-drop", src[src.index("function " + fn):src.index("function " + fn) + 700],
                          "%s does not target #dc-toc-drop" % fn)

    def test_breadcrumb_id_matches_its_renderer(self):
        html = markup()
        self.assertIn('id="dc-crumbs"', html)
        self.assertNotIn('id="dc-fb-crumb"', html,
                         "the old toolbar breadcrumb id is still in the markup")
        src = self._handlers()
        rb = src[src.index("function fbRenderBreadcrumb"):]
        self.assertIn("dc-crumbs", rb[:400],
                      "fbRenderBreadcrumb writes to a different element than the one "
                      "the markup has")

    def test_removed_tree_helpers_are_really_gone(self):
        """The folder tree is gone; its helpers must go with it.

        They read `#dc-fb-tree`, which no longer exists, and `fbSelectFromTree`
        navigated by prefix — the one path into a folder that bypassed the wall.
        Leaving them is a second, invisible way into a folder.
        """
        src = self._handlers()
        for name in ("fbLoadTree", "fbRenderTree", "fbTreeToggle", "fbSelectFromTree",
                     "fbNavigate", "fbGetAllFolders"):
            self.assertNotRegex(src, r"function %s\b" % name,
                                "%s still exists but its pane is gone" % name)
        html = markup()
        self.assertNotIn('id="dc-fb-tree"', html, "the tree element is still in the markup")

    def test_move_dialog_gets_its_folders_from_the_api(self):
        """The move picker used to read the cached tree.

        The tree is gone, so a picker built from `_fbTree` lists nothing — and a move
        dialog with an empty list reads as "there is nowhere to move this to".
        """
        src = self._handlers()
        mv = src[src.index("async function fbMove"):]
        mv = mv[:mv.index("\n  }")]
        self.assertIn("/files/tree", mv,
                      "fbMove no longer reads the folder list from the server")
        self.assertNotIn("_fbTree", mv, "fbMove still reads the deleted tree cache")


class CountsTests(unittest.TestCase):
    """The card counts must be two real numbers, not one number twice."""

    def test_count_line_says_files_and_datasets(self):
        src = dc_source()
        m = re.search(r"function dcCountLine\([^)]*\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "dcCountLine is gone")
        body = m.group(1)
        self.assertIn("files", body, "the card count does not mention files")
        self.assertIn("datasets", body, "the card count does not mention datasets")

    def test_datasets_are_filed_by_source_path_not_by_filename(self):
        """A dataset is filed under a folder by its source file's object key.

        `source_file` is a display filename — not unique, and not a path — so
        grouping by it puts a dataset in whichever folder happens to contain a file
        of that name. `source_file_path` is the object key, the same key space the
        folder tree is built from.
        """
        src = dc_source()
        fn = src[src.index("function fbDatasetsIn"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("source_file_path", fn,
                      "datasets are not filed by their source object key")
        self.assertNotRegex(fn, r"\.source_file\b(?!_path)",
                            "datasets are filed by the display filename, which is not a path")

    def test_datasets_in_a_folder_compares_whole_directories(self):
        """`2026-10/` must not match a folder whose name merely starts the same.

        The comparison is on the parent directory with a trailing slash on both
        sides; without the trailing slash, `2026-1/` would collect everything under
        `2026-10/`.
        """
        fn = dc_source()
        fn = fn[fn.index("function fbDatasetsIn"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("lastIndexOf('/')", fn)
        self.assertRegex(fn, r"replace\(/\\/\*\$/, '/'\)",
                         "the prefix is not normalised with a trailing slash")

    def test_the_root_prefix_stays_empty(self):
        """⚠️ `''.replace(/\\/*$/, '/')` is `'/'`. That one character emptied the root.

        The root is the folder you land on from the wall, and every dataset in it was
        compared against a prefix that had been normalised to a slash — so the root
        matched nothing and quietly showed no tables. No exception, no failed
        request, an empty folder that looked like an empty folder.

        So the rule is asserted directly: normalise a NON-EMPTY prefix, and leave the
        empty one alone. If someone "simplifies" this back to one expression, this
        test is the thing that says no.
        """
        fn = dc_source()
        fn = fn[fn.index("function fbDatasetsIn"):]
        fn = fn[:fn.index("\n  }")]
        self.assertRegex(fn, r"prefix\s*\?\s*prefix\.replace",
                         "the root prefix is normalised unconditionally, so the root "
                         "becomes '/' and matches no dataset")
        # Demonstrate the trap with the real JavaScript semantics, in JavaScript.
        # Python's `str.replace` is not the same function: `"".replace("/*", "/")`
        # returns `""` in Python and `"/"` in JS, so proving the trap in Python
        # proves nothing and quietly rots. Node is on PATH for the frontend tests;
        # if it is not, the assertion above still stands on its own.
        import shutil
        import subprocess
        if shutil.which("node"):
            got = subprocess.run(
                ["node", "-e", r"process.stdout.write(String(''.replace(/\/*$/, '/')))"],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(got.stdout, "/",
                             "the premise changed: ''.replace(/\\/*$/, '/') is no "
                             "longer '/', so the guard above needs re-checking")

    def test_fb_load_has_no_skip_datasets_argument(self):
        """A folder's rows ARE its datasets, so skipping the dataset list is not a
        performance option — it is the feature being off.

        It existed once, to save one request on folder open, and the symptom was that
        the first folder you opened from a card listed its files and none of its
        tables. Nothing failed; the list simply had no datasets in it. This asserts
        the signature, so the "optimisation" cannot come back as a caller nobody
        remembers the effect of.
        """
        src = dc_source()
        m = re.search(r"async function fbLoad\(([^)]*)\)", src)
        self.assertIsNotNone(m, "fbLoad is gone")
        self.assertEqual(m.group(1).strip(), "",
                         "fbLoad takes %r — a flag that skips loading datasets makes "
                         "every folder drawn from a card show no tables" % m.group(1))
        # And no caller passes one.
        for call in re.findall(r"fbLoad\(([^)]*)\)", src):
            self.assertEqual(call.strip(), "",
                             "a caller passes %r to fbLoad" % call)

    def test_an_orphaned_dataset_is_still_reachable(self):
        """A dataset whose source folder is gone lives in the root and says so.

        The import is a real PostgreSQL table and does not go away when the uploaded
        file is deleted. Filing it strictly means it is in the database and nowhere
        in the app, which is the worst of both: the reader can see the table exists
        and has no way to open it. So an orphan is filed in the root, and its row
        names the folder it came from rather than pretending it was always there.
        """
        fn = dc_source()
        fn = fn[fn.index("function fbDatasetsIn"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("_orphanFrom", fn,
                      "fbDatasetsIn does not detect a dataset whose folder is gone")
        self.assertIn("_dcKnownFolders", fn,
                      "fbDatasetsIn cannot tell a missing folder from one it is not "
                      "looking at, so it can never file an orphan")
        rows = dc_source()
        rows = rows[rows.index("function fbRenderList"):]
        rows = rows[:rows.index("function fbDatasetsIn")]
        self.assertIn("_orphanFrom", rows,
                      "the row does not show which folder an orphaned dataset came "
                      "from, so a reader looks for a folder that does not exist")

    def test_backend_returns_the_source_path(self):
        """The client cannot file a dataset without the key the server holds."""
        db = _read(os.path.join(REPO, "api", "services", "data_center_db.py"))
        m = re.search(r"def list_datasets\(.*?cur\.execute\(f?\"\"\"(.*?)\"\"\"", db, re.S)
        self.assertIsNotNone(m, "list_datasets was not found")
        self.assertIn("source_file_path", m.group(1),
                      "list_datasets does not select source_file_path, so the client "
                      "cannot tell which folder a dataset belongs to")


class SharedWithMeTests(unittest.TestCase):
    """A colleague's dataset has no file here, so it cannot be filed in a folder.

    It needs a home that is honest about that. Two ways this goes wrong: inventing a
    folder for it (a card on the wall that does not exist on disk, which a reader
    will try to upload into), and giving it management buttons the server refuses.
    """

    def test_shared_section_survives_the_datasets_tab(self):
        html = markup()
        self.assertIn('id="dc-shared-wrap"', html,
                      "the shared-with-me section was removed with the Datasets tab; "
                      "a colleague's dataset now has nowhere to appear at all")
        self.assertIn('id="dc-section-datasets"', html)

    def test_shared_section_is_loaded_on_entry(self):
        """`loadSharedWithMe` had no caller before this change.

        The Datasets tab called `loadDatasets`, which called it — but only on that
        tab. So the list was code nobody reached, and a shared dataset was invisible
        in the UI while the API and the permissions existed.
        """
        src = _read(INDEX)
        m = re.search(r"function switchToDataCenter\(\)\s*\{(.*?)\n\}", src, re.S)
        self.assertIn("showSection", m.group(1),
                      "switchToDataCenter does not load the shared list, so it is "
                      "still code with no caller")

    def test_shared_cards_offer_no_management_actions(self):
        """Only "preview" and "pull a copy".

        The server refuses rename/update/delete for a grantee, so those buttons
        would be offers of a 404 — the "wall advertises what the viewer cannot do"
        mistake the Workspace scopes already avoid.
        """
        src = dc_source()
        start = src.index("async function loadSharedWithMe")
        # Start AFTER this function's own header, then cut at the next top-level
        # definition — `async function` OR plain `function`. Matching only the async
        # form raises ValueError on the day someone makes the next function
        # synchronous, and a test that breaks when unrelated code is tidied up is a
        # test people delete.
        rest = src[start + len("async function loadSharedWithMe"):]
        cut = [i for i in (rest.find("\n  async function "), rest.find("\n  function "))
               if i >= 0]
        sh = _strip_comments(rest[:min(cut)])
        for banned in ("openShare(", "openUpdateModal(", "deleteDataset(",
                       "renameDataset(", "detailDataset(", "fbFileCtx"):
            self.assertNotIn(banned, sh,
                             "a shared dataset offers %s, which the server refuses" % banned)
        self.assertIn("previewDataset(", sh, "a shared dataset must still be readable")
        self.assertIn("pullDataset(", sh, "a shared dataset must offer a copy")


class InterpolationTests(unittest.TestCase):
    """⚠️ A pair is split WHOLE, so a value on one side alone is deleted in the other
    language. This is not a translation nit — it is data disappearing.

    The row count was written `'25 行 / rows'`, which is exactly the shape the split
    regex is built for, and it renders as `25 行` in Chinese and as `rows` in English.
    The number sits on the half that gets thrown away. A dataset row reading "rows"
    next to a pane showing 25 of them is worse than showing no count, because it
    looks like a number that happens to be blank.

    These assertions are deliberately LOOSE about source shape. The thing being
    guarded is a property of the string a reader sees — "both halves of the pair
    contain an interpolation" — and three earlier versions of this file tried to
    parse the concatenation that builds it and failed on every harmless edit to how
    the string is assembled. A guard that breaks when you rename a local variable
    gets deleted, and then the bug comes back.
    """

    @staticmethod
    def _pairs_with_one_sided_value(src):
        """Every `'… / …'` string literal where only one half interpolates."""
        bad = []
        # Join each run of `+`-concatenated literals into the string it produces.
        for run in re.findall(r"(?:'[^']*'\s*\+\s*)+'[^']*'", src):
            text = "".join(re.findall(r"'([^']*)'", run))
            if " / " not in text:
                continue
            left, right = text.split(" / ", 1)
            # A pair is only split when the left half has CJK (the PAIR regex), so
            # only those are the ones at risk.
            if not re.search(r"[一-鿿]", left):
                continue
            if ("+" in left) != ("+" in right):
                bad.append(text)
        return bad

    def test_no_pair_interpolates_on_one_side_only(self):
        src = dc_source()
        bad = self._pairs_with_one_sided_value(src)
        self.assertFalse(
            bad, "these pairs interpolate a value on one side only, so the value is "
                 "deleted in the other language:\n  " + "\n  ".join(bad))

    @staticmethod
    def _js_split(text):
        """Run the app's OWN pair rule over a string, in node.

        ⚠️ Asserting on a Python regex copy of `i18n.js`'s PAIR is asserting on a
        paraphrase. The rule that decides what a reader sees lives in one place, in
        JavaScript, and it is allowed to change; a second copy in a test is a second
        thing to keep in step, and the failure mode is the worst kind — the test
        passes, the page still shows "rows". So the check calls the real thing.

        Returns the `(left, right)` halves, or `None` if the string is not a pair.
        """
        import shutil
        import subprocess
        if not shutil.which("node"):
            self_skip = "node is not on PATH"
            raise unittest.SkipTest(self_skip)
        pair_re = r"/^(?=[\s\S]*[一-鿿])([\s\S]*?)\s+\/\s+([A-Za-z][\s\S]*)$/"
        script = (
            "const PAIR = new RegExp(%s);\n"
            "const t = process.argv[1];\n"
            "const m = PAIR.exec(t.trim());\n"
            "process.stdout.write(m ? JSON.stringify([m[1], m[2]]) : 'null');\n"
            % json.dumps(pair_re)
        )
        got = subprocess.run(["node", "-e", script, text],
                             capture_output=True, text=True, timeout=20)
        out = got.stdout.strip()
        return None if out == "null" else json.loads(out)

    def _assert_count_on_both_sides(self, label, what):
        halves = self._js_split(label)
        self.assertIsNotNone(halves, "%s is not a pair the app will split: %r" % (what, label))
        left, right = halves
        for side, text in (("Chinese", left), ("English", right)):
            self.assertRegex(
                text, r"\d",
                "the %s half of the %s is %r — the number is on the half that gets "
                "discarded, so the reader sees a count with no number in it"
                % (side, what, label))

    def test_the_row_count_reaches_both_languages(self):
        """The specific one that shipped: the row count read "rows" in English.

        Checked in a real browser against the real page, because the string a reader
        sees is not a literal in the source — it is a concatenation, run through
        `kladoI18n.t()`, stored in a span, and then re-split by the DOM walker. Every
        static shape I tried to assert on (the literal, the concatenation, the local
        variable name) is one refactor away from a false failure or a false pass,
        and a false pass on this one is the bug coming back.

        So this asserts the only thing that counts: with 25 rows in a dataset, the
        English page must not show a bare "rows".
        """
        html = markup()
        self.assertRegex(
            html, r"\+\s*' 行 / '",
            "the row count is not written as ' <n> 行 / <n> rows' any more — check that "
            "the number still reaches both halves of the pair")

    def test_no_pair_in_the_module_puts_a_value_on_one_side_only(self):
        """The general form, over every `+`-joined literal in the module.

        A pair is split whole, so `'25 行 / rows'` loses its number in English. This
        looks for the shape: a pair with CJK on the left (the only ones `PAIR` matches)
        where exactly one half is built from an interpolation.
        """
        src = dc_source()
        bad = []
        for run in re.findall(r"(?:'[^']*'\s*\+\s*)+'[^']*'", src):
            literals = re.findall(r"'([^']*)'", run)
            text = "".join(literals)
            if " / " not in text or not re.search(r"[一-鿿]", text.split(" / ", 1)[0]):
                continue
            # The halves are cut at the LAST " / " so a value containing a slash
            # cannot shift the boundary the way it would in a naive split.
            cut = text.rindex(" / ")
            left, right = text[:cut], text[cut + 3:]
            # A half is "interpolated" when the run had more than one literal on that
            # side — i.e. something was concatenated into it.
            if ("+" in left) != ("+" in right):
                bad.append(text)
        self.assertFalse(
            bad, "these pairs build one half from a value and the other from a "
                 "literal, so the value is deleted in one language:\n  "
                 + "\n  ".join(bad))


class CssTests(unittest.TestCase):
    """Rules that would undo the layout if they were written the obvious way."""

    def _rule_bodies(self, selector):
        """Every declaration block for `selector`, in source order.

        ⚠️ Comments are stripped FIRST. A rule is very often preceded by a comment
        that names other selectors — this file's own comment says "`.kb-toc` is
        284px", and a parser that does not strip comments reads `.dc-toc, .wr-toc`
        as a selector and reports the knowledge page's rule as missing. A text
        matcher that trips over its own documentation is worse than no matcher,
        because the first thing anyone does with a failing test like that is delete
        it.
        """
        text = re.sub(r"/\*.*?\*/", "", css(), flags=re.S)
        out = []
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
            sel, body = m.group(1), m.group(2)
            for one in sel.split(","):
                tokens = one.split()
                # The last token is the selector itself; anything before it is the
                # tail of an enclosing at-rule that `finditer` glued on (`@media`
                # blocks wrap rules, and the prelude before a nested rule is text).
                if not tokens:
                    continue
                # ⚠️ Compare on the whole trailing selector, not just its last token.
                # `.dc-folder-cover-ph i` and `.dc-folder-cover-ph` are different
                # rules, and a helper that stripped the descendant part would report
                # "this component has no font-size" for a component that has one —
                # sending you to add a second, losing rule for something already
                # styled. A type qualifier is kept: `i.dc-fb-icon` is the same
                # component as `.dc-fb-icon` with a specificity bump.
                tail = " ".join(tokens[-2:]) if len(tokens) >= 2 and not tokens[-2].endswith((">", "+", "~", ",")) else tokens[-1]
                if tail == selector or tail.endswith(selector) or selector.endswith(tail):
                    out.append(body)
        return out

    def test_icon_sizes_are_not_beaten_by_the_inline_remixicon_block(self):
        """⚠️ Every icon size here needs a type selector, and the reason is load order.

        The remixicon rules are an INLINE `<style>` at the top of `index.html`, and
        `app.css` is linked below it. `.ri { font-size: inherit }` and a single
        `.dc-folder-cover-ph i` are NOT comparable — that one is fine — but
        `.dc-fb-icon` alone is the same specificity and loses on order. A 46px cover
        icon measured 14px, with the 46px rule visible in the file.

        The tell is that the file and the computed style disagree while both look
        right. So the check is structural: any component that sets `font-size` on a
        bare remixicon class has to out-specify it.
        """
        for selector, rule in ((".dc-fb-icon", r"i\.dc-fb-icon\s*\{"),
                               (".dc-folder-cover-ph i", r"\.dc-folder-cover-ph i\s*\{")):
            bodies = self._rule_bodies(selector)
            sized = [b for b in bodies if re.search(r"(^|;)\s*font-size\s*:", b)]
            self.assertTrue(sized,
                            "%s has no font-size at all, so the icon renders at "
                            "whatever the parent is" % selector)
            text = re.sub(r"/\*.*?\*/", "", css(), flags=re.S)
            self.assertRegex(
                text, rule,
                "%s does not set font-size where the inline remixicon block's "
                "`.ri { font-size: inherit }` cannot out-order it — the icon silently "
                "renders at the inherited size" % selector)

    def test_the_stylesheet_cache_key_moves_with_the_stylesheet(self):
        """⚠️ The `?v=` on the stylesheet link is the only cache-buster this SPA has.

        The page is one static file with no build step, so a browser that has it open
        keeps whatever CSS it fetched under the key in the markup. Bumping that key
        is the entire delivery mechanism for a stylesheet change.

        It sat at `20261001-theme` through this whole rewrite, and the symptom is not
        "the change is missing from the file" — the file is correct and serves
        correctly. The symptom is a browser measuring the OLD value: a folder icon
        set to 46px measured 14px in a real page while `app.css` on disk said 46px,
        and the only way to tell the two apart was to diff the served bytes against
        the key in the markup.

        So the assertion is that the key is recent AND that the value is not one this
        change is known to have moved past. A hard-coded date will go stale; what
        matters is that it CHANGED when the stylesheet did.
        """
        html = markup()
        m = re.search(r'app\.css\?v=([A-Za-z0-9._-]+)', html)
        self.assertIsNotNone(m, "app.css is loaded without a ?v= cache-buster")
        key = m.group(1)
        self.assertNotEqual(
            key, "20261001-theme",
            "app.css is still cached under the pre-rewrite key %r, so every browser "
            "with this page open is showing the old stylesheet" % key)

    def test_hidden_views_stay_actually_hidden(self):
        """`hidden` is a browser DEFAULT and any author `display` beats it.

        The wall and the folder view are toggled with the attribute, so the question
        is not "does this rule set display" but "if it does, is the hidden case stated
        explicitly". `.dc-crumbs` needs `display: flex` to lay its parts out and
        therefore must carry its own `[hidden]` rule; `.dc-wall` and `.dc-fb-body`
        are laid out by their context and must never grow a `display` at all.

        This paragraph used to name the knowledge page's `.kb-crumbs` as a second
        instance of the same trap ("it sets `display: flex` with no `[hidden]`
        counterpart"). That was false — no `.kb-crumbs` rule with a `display` ever
        existed in app.css, and the row itself has since been deleted. The trap
        described here is real and `.dc-crumbs` is now the only element it can
        happen to, which is why the example has to be its own selector.
        """
        text = re.sub(r"/\*.*?\*/", "", css(), flags=re.S)
        for selector in (".dc-wall", ".dc-crumbs", ".dc-fb-body"):
            bodies = self._rule_bodies(selector)
            sets_display = [b for b in bodies if re.search(r"(^|;)\s*display\s*:", b)]
            if not sets_display:
                # The element is laid out by its context (a flex child), so the
                # browser's `[hidden]` default is the only rule that ever applies
                # and there is nothing to override. The real risk here is the day
                # somebody adds a `display` — which the next branch catches.
                continue
            self.assertRegex(
                text, re.escape(selector) + r"\[hidden\]\s*\{[^}]*display\s*:\s*none",
                "%s sets `display` with no `[hidden] { display: none }` counterpart, so "
                "the attribute that hides it is a default it overrides" % selector)

    def test_crumbs_are_the_only_view_that_may_set_display(self):
        """`.dc-crumbs` is the one toggle that genuinely needs `display: flex`.

        It has to lay its parts out, so it sets `display` and therefore MUST have the
        explicit `[hidden]` rule beside it. The other two are flex children of a
        column, so a `display` on either of them is the bug, not the fix.
        """
        self.assertRegex(re.sub(r"/\*.*?\*/", "", css(), flags=re.S),
                         r"\.dc-crumbs\[hidden\]\s*\{\s*display\s*:\s*none",
                         "`.dc-crumbs` sets `display: flex` and must restate the hidden "
                         "case — this is the rule that beats the browser's default")

    def test_wall_grid_and_toc_are_flexible_not_fixed(self):
        self.assertTrue(self._rule_bodies(".dc-wall"), ".dc-wall has no rule at all")
        self.assertTrue(self._rule_bodies(".dc-fb-body"),
                        ".dc-fb-body has no rule: the two panes would stack instead of "
                        "sitting side by side")
        body = self._rule_bodies(".dc-fb-body")[0]
        self.assertRegex(body, r"display\s*:\s*flex")
        self.assertRegex(body, r"min-height\s*:\s*0",
                         "without min-height:0 the panes cannot shrink below their "
                         "content and the whole page scrolls")

    def test_toc_matches_the_knowledge_toc_width(self):
        """Two directory views in one app should line up.

        The knowledge page's aside is 284px; a Data Center folder list at a
        different width means the left edge of the content jumps when you move
        between the two pages.
        """
        # ⚠️ Read the knowledge page's width through `_rule_bodies`, not through a
        # literal like `\.kb-toc\s*\{`. The two views are styled in the SAME rule
        # (`.kb-toc, .wr-toc { … }`) on some days and in two separate rules on
        # others, and which one it is has nothing to do with this page. A test that
        # only matches one spelling reports "the knowledge page's toc rule is gone"
        # on a file that plainly still has it, and the first thing anyone does with
        # that failure is delete the check.
        kb = [b for b in self._rule_bodies(".kb-toc")
              if re.search(r"width\s*:", b)]
        self.assertTrue(kb, "the knowledge page's toc rule has no fixed width, so "
                            "there is nothing for the two directory views to match")
        kb_w = re.search(r"width\s*:\s*(\d+)px", kb[0])
        self.assertIsNotNone(kb_w, "the knowledge toc has no fixed width")
        toc = self._rule_bodies(".dc-toc")
        self.assertTrue(toc, ".dc-toc has no rule at all")
        dc_w = re.search(r"width:\s*(\d+)px", toc[0])
        self.assertIsNotNone(dc_w, ".dc-toc has no fixed width")
        self.assertEqual(dc_w.group(1), kb_w.group(1),
                         "the two directory views are different widths")

    def test_table_scrolls_sideways_instead_of_squeezing(self):
        """A 40-column dataset squeezed into the pane is 40 unreadable columns.

        The table is `width: max-content` inside a pane that scrolls, so the reader
        scrolls sideways for a wide table and downwards for a long one.
        """
        tables = self._rule_bodies(".dc-fb-pv-table")
        self.assertTrue(tables, ".dc-fb-pv-table has no rule at all")
        self.assertNotRegex(tables[0], r"width\s*:\s*100%",
                            "the preview table is forced to the pane width, which "
                            "renders a wide dataset unreadable")
        wrap = self._rule_bodies(".dc-fb-pv-table-wrap")
        self.assertTrue(wrap, ".dc-fb-pv-table-wrap has no rule at all")
        self.assertRegex(wrap[0], r"overflow\s*:\s*auto",
                         "the table pane cannot scroll")

    def test_new_components_write_no_colour_literals(self):
        """A colour literal works in one theme and silently does not in the other.

        `theme.css` defines the tokens; a page rule with `#hex` or `rgba()` is a rule
        that only exists for whoever is reading in light mode.
        """
        for selector in (".dc-wall", ".dc-wall-grid", ".dc-folder", ".dc-folder-cover", ".dc-folder-cover-ph",
                         ".dc-folder-body", ".dc-folder-title", ".dc-folder-sub",
                         ".dc-folder-new", ".dc-crumbs", ".dc-crumb", ".dc-toc",
                         ".dc-toc-drop", ".dc-fb-row", ".dc-fb-name", ".dc-fb-meta",
                         ".dc-fb-kind", ".dc-fb-badge", ".dc-fb-go", ".dc-fb-icon",
                         ".dc-table-pane", ".dc-fb-pv-table", ".dc-fb-pv-table-wrap"):
            for body in self._rule_bodies(selector):
                self.assertNotRegex(
                    body, r"#[0-9a-fA-F]{3,8}\b|rgba?\(",
                    "%s writes a colour literal; it must use a theme.css token so it "
                    "holds in both themes" % selector)

    def test_drop_hint_is_only_shown_by_the_dragging_class(self):
        """The hint covers the list while dragging, so it must be display:none by
        default and shown by one class — anything else permanently hides the rows or
        permanently covers them.
        """
        hint = re.search(r"\.dc-fb-drop-hint\s*\{([^}]*)\}", css())
        self.assertIsNotNone(hint, ".dc-fb-drop-hint has no rule at all")
        self.assertRegex(hint.group(1), r"display\s*:\s*none",
                         "the drop hint is visible while idle, covering the list")
        self.assertRegex(css(), r"\.dc-toc-drop\.dragging \.dc-fb-drop-hint\s*\{[^}]*display\s*:\s*flex",
                         "dragging over the folder list does not reveal the hint")

    def test_drop_overlay_does_not_block_clicks(self):
        """`.dc-toc-drop` covers the whole pane, so it must not eat clicks.

        It is `position: absolute; inset: 0` over the list. Without
        `pointer-events: none` every click on a row lands on the overlay instead —
        the list looks normal and does nothing.
        """
        drop = self._rule_bodies(".dc-toc-drop")
        self.assertTrue(drop, ".dc-toc-drop has no rule at all")
        self.assertRegex(drop[0], r"pointer-events\s*:\s*none",
                         ".dc-toc-drop covers the list and swallows every click on it")


class RowSemanticsTests(unittest.TestCase):
    """The list rows are buttons now, and a row that looks clickable but is not
    focusable is a keyboard user staring at a page that does nothing."""

    def test_rows_carry_role_and_keyboard_handler(self):
        src = dc_source()
        rl = src[src.index("function fbRenderList"):]
        rl = rl[:rl.index("function fbDatasetsIn")]
        for kind in ("dc-fb-folder-row", "dc-fb-dataset-row"):
            m = re.search(re.escape(kind) + r"[^>]*", rl)
            self.assertIsNotNone(m, "%s is not rendered" % kind)
            self.assertIn('role="button"', m.group(0),
                          "%s is clickable but is not announced as a button" % kind)
            self.assertIn('tabindex="0"', m.group(0),
                          "%s cannot be reached by keyboard" % kind)
        # The plain file row too.
        self.assertRegex(rl, r'class="dc-fb-row" role="button" tabindex="0"')
        self.assertIn("onkeydown", rl, "no row responds to Enter or Space")

    def test_every_row_carries_search_text(self):
        """The search box filters by `data-search`.

        A row without it is invisible to the search: the list looks filtered and the
        item the reader wanted is simply not there.
        """
        rl = dc_source()
        rl = rl[rl.index("function fbRenderList"):]
        rl = rl[:rl.index("function fbDatasetsIn")]
        self.assertEqual(rl.count("data-search="), 3,
                         "each of the three row kinds (folder, file, dataset) must "
                         "carry data-search, or one of them cannot be found")

    def test_dataset_rows_carry_their_table_name_for_deep_links(self):
        """`openDatasetDeepLink` selects a row by `data-table` after it navigates.

        Without the attribute the deep link opens the right folder and then shows
        nothing selected — which looks exactly like the table being empty.
        """
        rl = dc_source()
        rl = rl[rl.index("function fbRenderList"):]
        rl = rl[:rl.index("function fbDatasetsIn")]
        self.assertRegex(rl, r'data-table="',
                         "dataset rows have no data-table, so ?dc=<table> cannot "
                         "select the row it navigated to")

    def test_filenames_and_titles_are_escaped_and_skipped_by_i18n(self):
        """A file named `A / B.xlsx` must not become two names.

        The i18n walker splits `中文 / English` on a slash, and a person-chosen
        folder or file name is not copy — it is data, so it needs
        `data-i18n-skip` on the element the walker visits and escaping on the way in.
        """
        src = dc_source()
        # Folder titles and file/dataset names are rendered with dcEsc.
        for fn, needle in (("dcRenderWall", "data-i18n-skip"),
                           ("fbRenderList", "dcEsc(")):
            body = src[src.index("function " + fn):]
            body = body[:body.index("\n  }") if "\n  }" in body else len(body)]
            self.assertIn(needle, body,
                          "%s renders names without %s" % (fn, needle))


if __name__ == "__main__":
    unittest.main()
