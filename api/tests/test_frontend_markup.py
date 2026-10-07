"""The SPA's static markup must be BALANCED — and one missing `</div>` is not cosmetic.

`frontend/out/index.html` is a 15,000-line single file with no build step, so a
structural mistake is a hand-editing mistake, and the browser does not report it. It
repairs the tree instead: an unclosed element swallows every sibling that follows it,
the page still parses, every `<script>` still runs, and the only symptom is that
something is *in the wrong place*.

⚠️ That is exactly what was live in this file. `#page-system-settings` was never
closed, so `.shell` stayed open to the end of `<body>` and re-parented the sign-in gate
inside itself. `app.css` has `body.auth-gate-open > .shell { display: none !important }`
— so opening the gate hid the gate, and **nobody could sign in through a browser at
all**. Nothing in the JavaScript failed, no request 500'd, and every API-level test
passed, because the API never looks at the DOM.

So this file asserts the two things that are cheap to check and expensive to discover:

1. the markup is balanced — every non-void element is closed by its own end tag, and
   nothing is left open at `</body>`;
2. the elements that MUST be reachable are reachable — the sign-in gate, the shared
   dialog, the overlay host and the whole left rail are direct children of `<body>`, not
   trapped inside `.shell`. A `position: fixed` element inside a hidden ancestor does not
   render, and `getComputedStyle` on it still answers `display: flex`, so a DOM probe that
   only asks "what display does it have" reports the element as fine.

No browser, no server: `html.parser` handles `<script>`/`<style>` as CDATA, which is
what makes a text-level tag count useless here — this file builds hundreds of HTML
strings inside its own JavaScript, and every `<div` in those strings is not a tag.
"""
import os
import re
import unittest
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX = os.path.join(REPO, "frontend", "out", "index.html")

#: Void elements, and the ones whose CONTENT is not markup.
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "path", "circle", "rect",
        "line", "use", "col", "area", "base", "embed", "track", "wbr"}
OPAQUE = {"script", "style", "noscript", "template"}

#: Must be a direct child of `<body>`, with the reason. Each of these is either
#: `position: fixed` or hidden together with `.shell`, so nesting it one level too deep
#: makes it unrenderable while every style probe still says it is fine.
BODY_LEVEL = {
    "auth-gate": "the sign-in gate; app.css hides `.shell` when the gate is open, so "
                 "nesting it inside the shell hides the gate itself",
    "kld-dlg": "the shared confirm/prompt dialog; it is `position: fixed` and its "
               "z-index only competes with modals if it is not clipped by an ancestor",
    "kld-toasts": "notifications; same reason as the dialog",
    "shell-wrap": "the application shell itself",
    # ── the left rail, and the trap that hid it ──
    # ⚠️ These four sat inside `#shell-wrap`, which is precisely what
    # `switchToDataCenter()` hides — so the Data Center page had no sidebar at all, no
    # hover hot zone and no drop target. Measured there:
    # `getComputedStyle(rail-hotzone).display` still answered `block`, because a hidden
    # ANCESTOR does not change an element's own computed display. Every style probe said
    # the rail was healthy while nothing rendered, which is why the browser regression
    # (`verify_nav_clickable_ui.py`) hit-tests it instead.
    "rail-hotzone": "the rail's hover trigger; it must survive the Data Center, which "
                    "hides the shell",
    "rail": "the rail panel itself; `position: fixed`, and a child of a hidden shell "
            "renders nothing on the Data Center page",
    "rail-ctx": "the rail context menu; it is `position: fixed` and placed from client "
                "coordinates, so it needs no shell ancestor",
    "rail-picker": "the rail's folder dialog, a `.rpt-modal`; the rail is the only thing "
                   "that opens it, so it disappears with the rail on the Data Center page",
}


class _Tree(HTMLParser):
    """The parsed tree, as a parent→child map plus a list of structural problems."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parent_of = {}
        self._parents = {}
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID or tag in OPAQUE:
            return
        attrs = dict(attrs)
        node_id = attrs.get("id") or ""
        self.parent_of[node_id] = self.stack[-1][1] if self.stack else ""
        self._parents[node_id] = self.stack[-1][0] if self.stack else None
        self.stack.append((tag, node_id, self.getpos()[0]))

    def parent_tag_of(self, element_id):
        """The TAG NAME of an element's parent, or None if the id was never parsed.

        ⚠️ Stored rather than recomputed from `parent_of`, because a parent with no id
        and a grandparent with no id are indistinguishable by id alone — and "the sign-in
        gate's parent has no id" is exactly the wrong way to conclude that the gate is a
        child of `<body>`.
        """
        return self._parents.get(element_id)

    def handle_endtag(self, tag):
        if tag in VOID or tag in OPAQUE:
            return
        if not self.stack:
            self.problems.append(
                f"line {self.getpos()[0]}: </{tag}> arrives with nothing open")
            return
        if self.stack[-1][0] != tag:
            # A `<div>` end tag is NOT optional in HTML5, so this is a hard error rather
            # than something a browser would forgive. Report which element is stranded so
            # the message names the place to look, not just the symptom.
            self.problems.append(
                f"line {self.stack[-1][2]}: <{self.stack[-1][0]}"
                f"{' id=' + repr(self.stack[-1][1]) if self.stack[-1][1] else ''}> is "
                f"never closed — </{tag}> arrives instead at line {self.getpos()[0]}. "
                f"The browser will swallow every following sibling into it.")
            self.stack.pop()
            return
        self.stack.pop()

    @property
    def unclosed(self):
        return [(tag, node_id, line) for tag, node_id, line in self.stack]


def _parse():
    with open(INDEX, encoding="utf-8") as handle:
        source = handle.read()
    tree = _Tree()
    tree.feed(source)
    return tree, source


class MarkupBalanceTests(unittest.TestCase):
    def test_every_element_is_closed(self):
        tree, _source = _parse()
        self.assertEqual(tree.problems, [],
                         "⚠️ the static markup is not balanced. The browser does not "
                         "report this — it repairs the tree, so scripts keep running and "
                         "only the LAYOUT is wrong.")
        self.assertEqual(tree.unclosed, [],
                         "⚠️ these elements are still open at the end of the document, so "
                         "everything after them is nested inside them.")

    def test_the_global_overlays_are_not_inside_the_shell(self):
        """The assertion that names the actual consequence.

        ⚠️ Not "is the element hidden" — a `position: fixed` element inside a
        `display: none` ancestor reports its own `display: flex` perfectly happily, and
        `offsetParent` is null for a *fixed* ancestor too, so both cheap probes say the
        element is fine. The parentage is the only thing that tells the truth.

        ⚠️ The set is not "things that used to be broken once". Each entry is a global
        overlay that has to exist on EVERY surface, and `#shell-wrap` is a surface that
        gets hidden — the rail was in there, and the Data Center page silently had no
        sidebar while every style probe reported it as healthy.
        """
        tree, _source = _parse()
        for element_id, why in BODY_LEVEL.items():
            with self.subTest(element=element_id):
                self.assertIn(element_id, tree.parent_of,
                              f"#{element_id} is missing from the document")
                parent = tree.parent_tag_of(element_id)
                self.assertEqual(parent, "body",
                                 f"#{element_id} is a child of <{parent}> — {why}")

    def test_the_parser_is_not_vacuous(self):
        """A balance test that parses a truncated or wrong file passes forever.

        ⚠️ So this asserts the parse actually SAW the document: a few landmarks by id
        from three different regions of the file, including one that only exists after
        the settings page the balance check is really about.
        """
        tree, source = _parse()
        self.assertGreater(len(source), 1_000_000,
                           "the SPA shrank — is this the right file?")
        for element_id in ("shell-wrap", "page-system-settings", "ss-panel-admin",
                           "auth-gate", "kld-dlg", "datacenter-page", "page-inbox"):
            self.assertIn(element_id, tree.parent_of,
                          f"#{element_id} was not parsed — the balance check is looking "
                          f"at a document it is not actually reading")


class DeadAdminSurfaceTests(unittest.TestCase):
    """The cards that moved to the admin console must be GONE from this file.

    ⚠️ Asserted on the source rather than on the running page, because the failure mode
    is silence: a card left behind after moving its subject elsewhere is a *second*
    control for the same thing, and two of those is how one of them ends up wrong. The
    absence of an id is checkable without a browser; the presence of a working one is
    not, and that is what `verify_account_ui.py` and `shot_settings_admin_ui.py` are for.
    """

    def setUp(self):
        with open(INDEX, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_moved_cards_are_not_in_the_markup(self):
        for element_id, moved_to in (
            ("adm-bin-body", "the admin console's recycle bin"),
            ("adm-mail-host", "the admin console's system mailbox"),
        ):
            with self.subTest(element=element_id):
                self.assertNotIn(f'id="{element_id}"', self.source,
                                 f"#{element_id} is still on the Settings page; it "
                                 f"belongs in {moved_to}")

    def test_the_new_cards_are_in_the_markup(self):
        for element_id in ("adm-card-members", "adm-card-org", "adm-card-approvals"):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', self.source,
                              f"#{element_id} is missing from the Settings page")

    def test_the_module_matrix_card_is_gone(self):
        """The module decision moved onto the member row, so the card went with it.

        ⚠️ Asserted as an ABSENCE. A card left behind would still render (empty, or
        worse: still full of a matrix nobody edits any more), and a Settings page with
        two places to set the same thing is how one of them becomes wrong without
        anybody noticing which.
        """
        for gone in ('id="adm-card-modules"', 'id="adm-modules-body"'):
            with self.subTest(markup=gone):
                self.assertNotIn(gone, self.source,
                                 f"{gone} is still in the markup; the module decision is "
                                 f"made per member from the roster row now")


class IconSourceTests(unittest.TestCase):
    """Every `ri-*` class the document uses must be a glyph THE FILE defines.

    ⚠️ The Remix Icon set is not loaded from a stylesheet: it is inlined into
    `index.html` as a `<style id="rc-font">` block, because a `<link>` to
    `remixicon.css` would not survive a proxy that blocks font requests. So the
    inline block is the ONLY source of these glyphs, and a class that is not in it
    is not a wrong colour or a wrong size — it is nothing at all.

    That is not a theoretical gap. The editor toolbar's B / I / U buttons were
    written as `ri-bold-line` / `ri-italic-line` / `ri-underline-line`; this block
    defines the un-suffixed `ri-bold` / `ri-italic` / `ri-underline` and nothing
    else under those names, so the three buttons rendered as 30x28 boxes with
    `background: none` and no glyph in them, right beside two buttons that worked.
    Every other check passed: the buttons existed, they had area, the toolbar had
    five controls with the right `data-cmd`s, and the only evidence was a
    screenshot — `getComputedStyle(i, ':before').content` answered `none` and the
    `<i>` had a 0x0 box, and nothing in CI was asking.

    `verify_edit_mode_ui.py` measures the rendered glyph in the browser; this is
    the cheap half that runs in `ci_check.py` with no server, so the next one fails
    at the gate instead of in a screenshot.
    """

    @classmethod
    def setUpClass(cls):
        with open(INDEX, encoding="utf-8") as handle:
            source = handle.read()
        # ⚠️ HTML comments are stripped FIRST, and not as tidiness: this file
        # documents the wrong spellings on purpose, and a comment that names a
        # class is not a use of it. The same reason `verify_user_menu_ui.py`
        # strips them — without it, every explanation here is a false failure.
        cls.markup = re.sub(r"<!--.*?-->", "", source, flags=re.S)
        cls.defined = set(re.findall(r"\.(ri-[a-z0-9-]+):before", source))
        # ⚠️ A regex over `class="…"` rather than the parsed tree above, because
        # half of this SPA's markup is built inside JavaScript strings — and one
        # of the four broken icons was exactly that: a status tick in a query
        # result, invisible in the Data Center, and no `<link>` anywhere to have
        # caught it.
        cls.used = set()
        for value in re.findall(r"""class\s*=\s*["']([^"']*)["']""", cls.markup):
            cls.used.update(tok for tok in value.split() if tok.startswith("ri-"))

    def test_every_icon_used_is_defined_by_the_inline_block(self):
        missing = sorted(self.used - self.defined)
        self.assertEqual(
            missing, [],
            "⚠️ these classes are used but the inline icon block defines no glyph for "
            "them, so they render as nothing at all (the buttons keep their box, "
            "which is why a screenshot is the only place this shows up): %s" % missing)

    def test_the_scan_is_not_vacuous(self):
        """A guard whose regex stopped matching passes forever.

        ⚠️ So this asserts the scan actually SAW both sides: the block is still
        there (a floor with plenty of headroom, not a count to match), the markup
        really uses dozens of icons, and the four glyphs the toolbar depends on
        are among the ones in use. All floors, deliberately — a subset gets pruned
        over time and a guard that fails on the number is a guard that gets
        deleted.
        """
        self.assertGreater(len(self.defined), 500,
                           "the inline icon block shrank to nothing — is this the "
                           "right file?")
        self.assertGreaterEqual(len(self.used), 50,
                                "only %d `ri-*` classes were found in the markup; the "
                                "scan is not reading the document" % len(self.used))
        for name in ("ri-bold", "ri-italic", "ri-underline", "ri-arrow-go-back-line"):
            with self.subTest(icon=name):
                # ⚠️ `assertIn`, not `assertTrue(x in y)`'s opposite: asserting
                # membership of a 90-element set dumps the whole set into the log
                # when it fails, and a guard whose failure output is 90 lines is a
                # guard nobody reads.
                self.assertTrue(name in self.used,
                                "%s is no longer used in the markup, so this file is "
                                "reading a different document" % name)
                self.assertTrue(name in self.defined,
                                "%s is no longer defined by the inline block" % name)


if __name__ == "__main__":
    unittest.main()
