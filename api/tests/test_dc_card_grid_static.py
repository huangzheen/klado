"""Static guard for the Data Center dataset WALL (a grid of folders you open).

⚠️ This file has been rewritten twice, and both rewrites were forced by the guard
being right about the code and the code being wrong about the design:

1. It first guarded a grid of dataset cards. `d3dc547` gave the container card
   `grid-column: 1 / -1`, which put one card on every line, and the guard went on
   passing — because its browser half's fixtures contained no container at all.
2. It then guarded that card grid, and the reader asked for something else again
   (2026-10-05): a dataset is a FOLDER you open, like the Workspace projects, and
   the list lives inside. Restoring a card grid was answering a question that had
   been superseded.

So the invariants below are deliberately about the THING, not about a class name:
what has to be true for the wall to be a wall of openable folders. A guard pinned
to today's markup is a guard that reports the next legitimate change as a failure
and gets deleted instead of updated.

And the same reasoning says why this file is STATIC when
`shot_datacenter_list_ui.py` measures the same wall in a real browser: that one
needs a live server and a real database, so it is not in `scripts/ci_check.py` and
CI never runs it. Its red light is only seen by whoever remembers to look. These
checks need nothing and run every time.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "frontend" / "out" / "index.html"
CSS = ROOT / "frontend" / "out" / "app.css"

FOLDER_CARD = "dc-proj"          # bare class name: markup has no dot, CSS does
NEW_TILE = "dc-proj-new"
GRID = "dc-datasets-grid"
WORKSPACE_TILE = "wr-proj"        # the shape the reader asked this to look like
LOOSE_FOLDER = "dc-proj-loose"


def strip_comments(css: str) -> str:
    """Remove `/* … */` before any rule matching.

    ⚠️ Not optional. A character class like `[^{}]` MATCHES NEWLINES, so without
    this a multi-line comment sitting above a rule is read as part of that rule's
    selector list — the selector then does not contain the class being looked for
    and a rule that is present reports as deleted. That failure is silent and
    points the reader at the wrong file.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def css_rule(css: str, selector: str) -> str:
    """Declarations of the first top-level rule whose selector LIST contains
    `selector`.

    Comma groups are the norm in this stylesheet — the folder card is written
    `.kb-proj, .wr-proj, .dc-proj` precisely so the three walls share one rule —
    so a matcher that looks for the selector standing alone at the start of a line
    finds NOTHING for the shared rules and reports them as deleted.

    Returns '' when the selector appears in no rule at all, which every caller
    treats as a failure rather than as an empty declaration list.
    """
    for m in re.finditer(r"(?m)^([^{}\n][^{}]*?)\{([^}]*)\}", strip_comments(css)):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            return m.group(2)
    return ""


def strip_js_comments(src: str) -> str:
    """Remove `// …` line comments from an extracted JS body.

    ⚠️ Without this, every code assertion in this file also matches the SAME TEXT
    inside a comment — and a comment is exactly where the reasoning about a fix
    lives, so the match is guaranteed. Found by mutation: deleting the statement
    `_dsOpen = '';` from `confirmGroupCreate()` left the test green, because the
    comment directly above it still spelled the assignment out and `assertRegex`
    was reading that. A guard that cannot fail is worse than no guard, because the
    next reader concludes the thing is protected.

    Block comments and string literals are left alone deliberately: neither can be
    confused with a statement here, and mangling them would only risk false
    failures."""
    return re.sub(r"//[^\n]*", "", src)


def fn_body(src: str, name: str) -> str:
    """The body of `function <name>(…)`, with comments stripped.

    Comment-free on purpose — see `strip_js_comments`. Callers that want to assert
    on a comment (none today) can re-read the raw file."""
    m = re.search(rf"function {re.escape(name)}\(.*?\n  \}}", src, re.S)
    return strip_js_comments(m.group(0)) if m else ""


class CssSanityTests(unittest.TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")

    def test_css_braces_balance(self):
        """A missing `}` swallows every rule after it, so a guard that PARSES the
        file would never see them and would report nothing at all. Without this,
        every other check in this file can pass on a stylesheet half of which does
        not exist."""
        self.assertEqual(self.css.count("{"), self.css.count("}"),
                         "app.css braces are unbalanced — later rules may be swallowed")


class WallCssTests(unittest.TestCase):
    """The wall must look like the Workspace folder wall and stay a grid."""

    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")

    def test_folder_card_shares_the_workspace_rule(self):
        """The card is not a second copy of `.wr-proj`, it is the same rule. A
        separate declaration block would be a second thing to drift, and the drift
        is invisible until someone notices the two walls stopped matching."""
        self.assertRegex(
            self.css, r"(?m)^\.kb-proj,\s*\.wr-proj,\s*\.dc-proj\s*\{",
            ".dc-proj is not merged into the shared `.kb-proj, .wr-proj` rule")

    def test_new_tile_shares_the_workspace_rule(self):
        self.assertRegex(
            self.css, r"(?m)^\.kb-proj-new,\s*\.wr-proj-new,\s*\.dc-proj-new\s*\{",
            ".dc-proj-new is not merged into the shared `.kb-proj-new, .wr-proj-new` rule")

    def test_no_card_is_forced_to_span_the_row(self):
        """⚠️ THE load-bearing one. `grid-column: 1 / -1` on a wall card puts one
        card on every line while THIS container still reports its full track
        count, so a read of the grid rule says "grid" and `getBoundingClientRect()`
        says "a list". It is checked on BOTH card classes because either one
        carrying it re-breaks the wall."""
        for cls in (FOLDER_CARD, NEW_TILE):
            body = css_rule(self.css, "." + cls)
            self.assertTrue(body, f".{cls} has no rule in app.css — renamed?")
            span = re.search(r"grid-column\s*:\s*([^;]+);", body)
            self.assertIsNone(
                span, f".{cls} forces grid-column: {span.group(1) if span else ''} — "
                      "one card per line is a list, not a wall")

    def test_wall_track_matches_the_workspace_wall(self):
        """Same folder size is the whole of 'looks like the Workspace folders'."""
        wall = css_rule(self.css, ".wr-wall-grid")
        grid = css_rule(self.css, "." + GRID)
        self.assertTrue(wall, ".wr-wall-grid has no rule in app.css")
        self.assertTrue(grid, f".{GRID} has no rule in app.css")
        w = re.search(r"minmax\(([^,]+),", wall)
        g = re.search(r"minmax\(([^,]+),", grid)
        self.assertTrue(w and g, "one of the two walls declares no minmax() track")
        self.assertEqual(g.group(1).strip(), w.group(1).strip(),
                         f".{GRID} track {g.group(1)} != .wr-wall-grid track "
                         f"{w.group(1)} — the dataset wall stops matching the "
                         "Workspace folder wall")

    def test_wall_can_resolve_more_than_one_track(self):
        grid = css_rule(self.css, "." + GRID)
        self.assertRegex(grid, r"repeat\(auto-(fill|fit)",
                         f".{GRID} does not auto-fill, so it cannot be multi-column")

    def test_the_list_inside_a_folder_spans_the_wall(self):
        """The list lives in the SAME grid container. Without `1 / -1` here it
        inherits the 225px folder track and renders one table per line."""
        for sel in (".dc-ds-list", ".dc-ds-list-head", ".dc-ds-list-acts"):
            body = css_rule(self.css, sel)
            self.assertTrue(body, f"{sel} has no rule in app.css")
            self.assertRegex(body, r"grid-column\s*:\s*1\s*/\s*-1",
                             f"{sel} does not span the wall — it would inherit a "
                             "single folder track")


class WallMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = HTML.read_text(encoding="utf-8")

    def test_wall_renders_a_card_per_dataset(self):
        """One folder card per dataset. The shape this replaces put every loose
        table on the wall as its own card, which is the shape that made the wall
        too wide to be a wall."""
        body = fn_body(self.html, "dsFolderCard")
        self.assertTrue(body, "dsFolderCard() not found in index.html")
        self.assertIn(FOLDER_CARD, body, f"dsFolderCard() does not emit a .{FOLDER_CARD}")
        self.assertIn("dc.openDsFolder(", body,
                      "the folder card is not clickable — a card that does not open "
                      "is a label, and the list is then unreachable")

    def test_wall_renders_loose_tables_as_their_own_folder(self):
        """Tables in no dataset stay reachable — as a folder, not as a pile of
        cards, and never dropped."""
        body = fn_body(self.html, "dsLooseFolderCard")
        self.assertTrue(body, "dsLooseFolderCard() not found in index.html")
        self.assertIn(LOOSE_FOLDER, body, f"the loose folder is not marked .{LOOSE_FOLDER}")
        self.assertIn("dc.openLooseFolder()", body, "the loose folder cannot be opened")

    def test_new_dataset_tile_is_always_rendered(self):
        """⚠️ It has to be rendered even when the wall is otherwise empty: it is the
        only way to create a first dataset, so omitting it in the empty state
        strands a new account. The tile is therefore concatenated OUTSIDE the
        `rows.length || containers.length` branch — asserted by position."""
        body = fn_body(self.html, "renderDatasets")
        self.assertTrue(body, "renderDatasets() not found in index.html")
        tile = body.find(NEW_TILE)
        self.assertNotEqual(tile, -1, f"renderDatasets() never emits a .{NEW_TILE}")
        empty_branch = body.find("!rows.length && !containers.length")
        self.assertNotEqual(empty_branch, -1, "the empty-state branch is gone")
        self.assertLess(
            tile, empty_branch,
            f"the .{NEW_TILE} tile is rendered inside the empty-state branch — a "
            "first-run account would have no way to create its first dataset")

    def test_there_is_a_way_out_of_a_folder(self):
        """The wall is only navigable if the folder can be closed. The button lives
        in the SECTION header, not in the grid: the grid's content is replaced when
        a folder opens, so a control inside it is destroyed the moment it is used."""
        self.assertIn('id="dc-ds-back"', self.html, "there is no back button")
        self.assertIn("dc.closeDsFolder()", self.html,
                      "the back button does not call closeDsFolder()")
        m = re.search(r'(?s)<section id="dc-section-datasets".*?</section>', self.html)
        self.assertTrue(m, "#dc-section-datasets not found")
        self.assertIn('id="dc-ds-back"', m.group(0),
                      "the back button is not in #dc-section-datasets")

    def test_deleting_the_open_dataset_falls_back_to_the_wall(self):
        """A dataset deleted from inside its own folder leaves a slug that no
        longer resolves. Staying open on it is a header for something that is
        gone, so the render has to notice and go back."""
        body = fn_body(self.html, "renderDatasets")
        self.assertRegex(
            body, r"!\s*containers\.some\(",
            "renderDatasets() does not check whether the open dataset still exists")
        self.assertIn("closeDsFolder()", fn_body(self.html, "renderDsList"),
                      "renderDsList() does not fall back to the wall for a missing slug")

    def test_open_folder_is_exported_under_a_free_name(self):
        """⚠️ `openFolder` is ALREADY exported by this module — the files view uses
        it. Two exports with one name means the later one wins and the other
        feature silently calls the wrong function. Assert the name is free."""
        # Anchored on a member only THIS module exports, because a bare
        # `^  return {` matches the first module in the file rather than the one
        # under test — which is a guard that reads somebody else's export list.
        m = re.search(r"(?s)^  return \{(?=[^}]*loadDatasets)(.*?)\n  \};",
                      self.html, re.M)
        self.assertTrue(m, "the dc module's return object not found")
        exported = m.group(1)
        self.assertIn("openDsFolder", exported, "openDsFolder is not exported")
        self.assertIn("closeDsFolder", exported, "closeDsFolder is not exported")
        # ⚠️ `openFolder: dcOpenFolder` IS in this list and is CORRECT — that is the
        # files view's folder picker, a different function that genuinely owns the
        # name. What must not exist is a SECOND binding of the bare name, which would
        # overwrite it and leave the files view calling the dataset opener.
        bare = re.findall(r"(?m)^\s*openFolder\s*[:,}]", exported)
        self.assertLessEqual(
            len(bare), 1,
            f"the bare name `openFolder` is bound {len(bare)} times in the dc "
            "module's exports — the later one wins and the other feature silently "
            "calls the wrong function")


    def test_the_loose_folder_is_hooked_up_through_an_exported_function(self):
        """⚠️ The card used to say `dc.openDsFolder(DS_LOOSE_KEY)`. An inline
        `onclick` runs in GLOBAL scope and `DS_LOOSE_KEY` is a `const` inside the dc
        IIFE, so the click threw `ReferenceError` and the card did nothing — with no
        sign of it anywhere on screen. The key therefore crosses from module scope to
        markup through a NO-ARGUMENT exported function and nowhere else, and writing
        the literal `'@loose'` into the markup is refused for the same reason a second
        copy of any value is.

        The class-level version of this check is in the browser test ("no card throws
        when clicked"); a static scan for bare identifiers in inline handlers is NOT
        usable here — the handlers are built by string concatenation, so every local
        variable in the surrounding function matches too (109 false positives when
        tried)."""
        body = fn_body(self.html, "dsLooseFolderCard")
        self.assertIn("dc.openLooseFolder()", body,
                      "the loose folder no longer goes through openLooseFolder()")
        m = re.search(r"(?s)^  return \{(?=[^}]*loadDatasets)(.*?)\n  \};",
                      self.html, re.M)
        self.assertTrue(m, "the dc module's return object not found")
        self.assertIn("openLooseFolder", m.group(1),
                      "openLooseFolder is not exported, so the card's handler is dead")

    def test_creating_a_dataset_returns_to_the_wall(self):
        """The reported bug: creating a dataset from inside a folder left the reader
        looking at that folder's list, where the new dataset is not — the dialog
        closes, the screen does not change, and it reads as a save that did nothing.
        The wall is the only view that can show what was just created."""
        body = fn_body(self.html, "confirmGroupCreate")
        self.assertTrue(body, "confirmGroupCreate() not found in index.html")
        self.assertRegex(body, r"_dsOpen\s*=\s*''",
                         "creating a dataset does not clear _dsOpen — the reader stays "
                         "inside a folder and never sees the new one")
        # …and it must be cleared BEFORE the re-render, or the re-render paints the
        # old view and the reset is overwritten by the next filterDatasets.
        self.assertLess(body.find("_dsOpen = ''"), body.find("loadDatasets()"),
                        "_dsOpen is cleared after loadDatasets() — the wall is "
                        "re-rendered with the folder still open")


class CountPairVocabularyTests(unittest.TestCase):
    """⚠️ `kladoI18n.t()` only splits `中文 / English` in that direction. A pair
    whose left side is also English comes back WHOLE, and the card prints both
    languages at once. It looks like a rendering bug and it is a vocabulary bug, it
    is invisible in a screenshot nobody reads carefully, and it shipped once already
    (`wrCount`'s own vocabulary had English on both sides, so every Workspace
    project card read "report: 2 / reports: 2"). Measured in the browser, 2026-10-05.

    These are the count vocabularies: a known, small set, so they can be pinned
    rather than pattern-matched across the whole file."""

    # (constant, what it counts)
    COUNTS = (("DS_TABLES", "dataset card"), ("DS_ROWS", "dataset card"),
              ("WR_REPORTS", "workspace card"), ("WR_FOLDERS", "workspace card"))

    def setUp(self):
        self.html = HTML.read_text(encoding="utf-8")

    def _halves(self, const):
        """The vocabulary as a flat list of its string halves, in source order.

        The shape is `[[zh_sg, en_sg], [zh_pl, en_pl]]`, so the halves alternate
        Chinese / English. Position parity IS the language marker — which is why
        this returns a list and not a dict keyed by language: there is no other
        place in these four constants that records which half is which.
        """
        m = re.search(rf"const {const} = (\[.*?\]);", self.html, re.S)
        self.assertTrue(m, f"{const} is not declared — renamed or removed?")
        return re.findall(r"'([^']*)'", m.group(1))

    def test_left_half_of_each_pair_is_chinese(self):
        for const, where in self.COUNTS:
            halves = self._halves(const)
            self.assertTrue(halves, f"{const} has no strings in it")
            self.assertEqual(len(halves) % 2, 0,
                             f"{const} ({where}) has {len(halves)} halves — a pair "
                             "is missing one side")
            for i, half in enumerate(halves):
                if i % 2 == 0:
                    self.assertRegex(
                        half, r"[一-鿿]",
                        f"{const} ({where}): {half!r} is the LEFT half of a pair and "
                        "has no Chinese — t() will not split it, so the card prints "
                        "both languages at once")
                else:
                    self.assertNotRegex(
                        half, r"[一-鿿]",
                        f"{const} ({where}): {half!r} is the RIGHT half of a pair and "
                        "carries Chinese, so English mode falls back to Chinese")

    def test_each_half_is_a_single_language(self):
        """A half must not itself be a pair. The old `wrCount` vocabulary had exactly
        that on its singular half (`'{n} 份报告', '{n} 份报告'`), which printed Chinese
        in an English UI — and the comment above it claimed the opposite."""
        for const, where in self.COUNTS:
            for half in self._halves(const):
                self.assertNotIn(" / ", half,
                                 f"{const} ({where}): {half!r} is a whole pair where a "
                                 "single-language half belongs — the two nesting "
                                 "levels have been swapped")

    def test_a_count_placeholder_is_present(self):
        """Every half carries `{n}`; the count is what the line is for, and a half
        without it is a static word where a number belongs."""
        for const, where in self.COUNTS:
            for half in self._halves(const):
                self.assertIn("{n}", half,
                              f"{const} ({where}): {half!r} has no {{n}} placeholder")


class ExternalImportStaysTests(unittest.TestCase):
    """The wall work must not cost the external-database import. These are the two
    facts that make it a feature rather than a button: the entry point renders, and
    the routes exist."""

    def setUp(self):
        self.html = HTML.read_text(encoding="utf-8")

    def test_import_entry_point_is_rendered(self):
        self.assertIn("openImportExternal", self.html,
                      "no 'Import from database' entry point in index.html")
        self.assertIn("peek-external", self.html,
                      "the dialog no longer peeks the external database")
        self.assertIn("import-external", self.html,
                      "the dialog no longer posts the external import")

    def test_import_is_reachable_from_inside_a_dataset(self):
        """The import was a button on the container card. The card is now a label
        you click through, so the action has to live on the LIST the click lands on
        — otherwise the only way in is a button that no longer renders."""
        body = fn_body(self.html, "renderDsList")
        self.assertTrue(body, "renderDsList() not found in index.html")
        self.assertIn("dc.openImportExternal(", body,
                      "the list inside a dataset offers no way to import from a "
                      "database — the card it replaced had that button")

    def test_routes_are_declared(self):
        router = (ROOT / "api" / "routers" / "data_center.py").read_text(encoding="utf-8")
        for route in ('"/dataset-groups/peek-external"',
                      '"/dataset-groups/{slug}/import-external"'):
            self.assertIn(route, router, f"the {route} route is gone")


if __name__ == "__main__":
    unittest.main()
