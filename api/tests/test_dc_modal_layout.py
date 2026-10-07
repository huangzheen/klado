"""A modal whose body cannot scroll is a dialog whose buttons you cannot reach.

⚠️ This is a defect that has now been in five dialogs at once, and it is invisible to
every assertion in the repo: the markup is well-formed, the CSS parses, the handler
is wired, and the buttons are in the DOM. What fails is that `.dc-modal` is
`max-height: 82vh` with `overflow: hidden`, so a body that is neither `flex: 1` nor
scrollable is **clipped** — and the footer goes with it. "Import from a database" was
unfinishable: the mode radios sat at the cut line and Cancel/Import were never on
screen, at any window size.

Two halves, because neither is enough:
  · **static** — every `.dc-modal` has a body that is `.dc-modal-pad` or
    `.dc-modal-body`, and those classes really are the scroll container. This catches
    the next dialog somebody writes.
  · **runtime** — `verify_dc_modal_layout_ui.py` measures the Import button's
    rectangle against the viewport. The static half cannot tell whether a body
    actually scrolls; only geometry can.

    ../.venv312/bin/python -m unittest tests.test_dc_modal_layout
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "frontend" / "out" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "out" / "app.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """One CSS rule's declarations, whitespace-tolerant.

    `verify_to_static_ui.py` had to learn this the hard way: `padding: 5px 9px` and
    `padding:5px 9px` are the same rule, and a test that pins one spelling fails on
    a reformat.
    """
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for m in re.finditer(r"(?:^|[},])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", body):
        return m.group(1)
    return ""


class EveryModalScrollsTests(unittest.TestCase):
    def _modal_bodies(self):
        """`(title, body_class)` for every `.dc-modal` in the page.

        ⚠️ Scans by DIV DEPTH, not by regex. The first version used a non-greedy
        `.*?` up to the first `</div>`, which is the end of `.dc-modal-head` — so
        every block came back as ~200 characters of header with no body in it, and
        the guard reported "no modal has a scrolling body" for all seven. A reader
        that finds the right N but the wrong span is the worst kind: it fails
        loudly on correct code, which is indistinguishable from a real regression.

        ⚠️ `dc-modal` is a PREFIX here, not the whole class attribute. The import
        dialog is `class="dc-modal dc-modal-wide"`, and the first version of this
        pattern (`class="dc-modal"`) stopped matching it the day that second class
        was added — the guard carried on reporting success over the other six while
        no longer looking at the one dialog this file exists for. It is the
        "finds nothing, looks like it passes" failure, reached from the other end.
        """
        out = []
        for m in re.finditer(r'class="dc-modal(?:\s[^"]*)?"[^>]*>', HTML):
            # ⚠️ depth starts at 1, not 0: the modal's own opening tag was just
            # consumed by the match, so it is already one level open. Starting at 0
            # stops the scan at the END OF THE HEADER (200-odd characters, no body)
            # — which is what the second version of this reader did.
            depth, i = 1, m.end() - 1
            while i < len(HTML):
                nxt_open = HTML.find("<div", i)
                nxt_close = HTML.find("</div>", i)
                if nxt_close == -1:
                    break
                if nxt_open != -1 and nxt_open < nxt_close:
                    depth += 1
                    i = nxt_open + 4
                    continue
                depth -= 1
                i = nxt_close + 6
                if depth == 0:
                    break
            block = HTML[m.end():i]
            head = re.search(r'class="dc-modal-h?e?a?d?e?r?"[^>]*>\s*<h3>(.*?)</h3>',
                             block, re.S)
            title = re.sub(r"<[^>]+>", " ", head.group(1)).strip()[:44] if head else "?"
            # ⚠️ The class attribute is a LIST, not a name. The import dialog's body is
            # `class="dc-modal-pad dc-ie-grid"`, and matching the whole string made
            # this guard report that very dialog as having no scrolling body.
            body = re.search(r'class="[^"]*\b(dc-modal-pad|dc-modal-body)\b[^"]*"', block)
            out.append((title, body.group(1) if body else None, block))
        return out

    def test_the_page_really_has_modals_to_check(self):
        # ⚠️ A guard that finds nothing looks exactly like a guard that passes.
        self.assertGreaterEqual(len(self._modal_bodies()), 5,
                                "the modal pattern changed and this guard stopped looking")

    def test_the_import_dialog_is_one_of_them(self):
        """POSITIVE CONTROL for the reader above.

        The count check says "at least five modals", which stays true while one of
        them quietly stops being found. This names the dialog this feature is about,
        so a class-name change that hides it fails instead of shrinking the sample.
        """
        titles = [t for t, _, _ in self._modal_bodies()]
        self.assertTrue(any("从数据库导入" in t for t in titles),
                        f"the import dialog is not in the scanned set: {titles}")

    def test_every_modal_has_a_scrolling_body(self):
        for title, body_class, block in self._modal_bodies():
            self.assertIn(
                body_class, ("dc-modal-pad", "dc-modal-body"),
                f"the modal 「{title}」 has a body that is neither .dc-modal-pad nor "
                f".dc-modal-body, so it cannot scroll and its footer is unreachable")
            self.assertNotIn('style="padding:20px"', block,
                             f"the modal 「{title}」 still uses a plain padded div")

    def test_the_scroll_container_really_scrolls(self):
        """`min-height: 0` is the declaration that is easy to leave out.

        ⚠️ Stated accurately, because the first version of this comment claimed the
        rule "never scrolls" without it, and that is not true: a flex item's
        automatic minimum size is 0 while its `overflow` is not `visible`, so
        `.dc-modal-body` was working by that accident. What the assertion buys is
        that the contract is written down — the protection disappears silently the
        moment somebody changes `overflow` or copies the declaration somewhere new.
        """
        for selector in (".dc-modal-pad", ".dc-modal-body"):
            decls = re.sub(r"\s+", " ", _rule(selector))
            self.assertTrue(decls, f"{selector} is not in app.css at all")
            for prop, value in (("flex", "1"), ("overflow", "auto")):
                self.assertRegex(decls, rf"{prop}\s*:\s*[^;]*{value}",
                                 f"{selector} has no working {prop}: {value}")
            self.assertRegex(decls, r"min-height\s*:\s*0",
                             f"{selector} relies on the automatic minimum size "
                             f"instead of saying it")

    def test_the_modal_box_is_the_thing_that_clips(self):
        """This is the constraint the body has to answer. If `.dc-modal` stops being
        height-capped, the guard above is protecting against nothing."""
        decls = re.sub(r"\s+", " ", _rule(".dc-modal"))
        self.assertRegex(decls, r"max-height\s*:", f".dc-modal is no longer capped: {decls}")

    def test_the_footer_cannot_be_what_scrolls_away(self):
        decls = re.sub(r"\s+", " ", _rule(".dc-modal-foot"))
        self.assertRegex(decls, r"flex-shrink\s*:\s*0",
                         "the footer is allowed to be squeezed out of a short modal")

    def test_the_stylesheet_parses(self):
        """⚠️ Braces balanced, because a rule can be split without any error.

        A text edit anchored on a PREFIX of a rule (`.dc-at-modes label { … flex: 1;`)
        inserted the next rule inside it. The browser then read
        `.dc-ie-modes label { … }` as a *declaration* of the rule it was sitting in, so
        the CSSOM never had it, and the rest of that rule's own declarations were left
        stranded after it. Nothing threw, the page rendered, and the effect was "my
        override does nothing" — which reads as a specificity problem and is not one.

        `test_stale_css_rules.py` cannot catch it: it looks for one selector declared
        twice, and this was two selectors fighting over one property.
        """
        body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        self.assertEqual(body.count("{"), body.count("}"),
                         "app.css has unbalanced braces — a rule was probably split by "
                         "an edit anchored on part of it")


class OneScreenImportDialogTests(unittest.TestCase):
    """2026-10-06 用户原话：「所有的信息在一屏里面显示，不希望滑动。信息尽量紧凑点。」

    Geometry is `verify_dc_modal_layout_ui.py`'s job — it can measure, this cannot.
    What lives here is the part of "one screen" that is a DECISION rather than a
    measurement, because each of these is a way the dialog silently stops being one:
    a list that grows without a cap, a native control that re-inflates a row, a pair
    broken by a tag, a handler an inline `onclick` cannot see.
    """

    def setUp(self):
        self.parent = EveryModalScrollsTests()
        blocks = [b for t, _, b in self.parent._modal_bodies() if "从数据库导入" in t]
        # ⚠️ An empty block rather than `blocks[0]`. When the reader stops matching
        # the import dialog, `next(...)` raised StopIteration inside setUp and every
        # test in this class came back as `ERROR: … StopIteration` — a stack trace
        # that reads like a bug in the guard rather than the thing it is reporting.
        # Every test below opens with `_need()`, which names the real problem.
        self.block = blocks[0] if blocks else ""
        # ⚠️ Comments are stripped before any "is this control still here" check.
        # The markup carries long notes ABOUT the redesign, and one of them says
        # `Pills, not a <select>` — the assertion below then found the word in the
        # prose and reported a native control that had been gone for an hour. An
        # assertion about markup must not be able to read a comment.
        self.markup = re.sub(r"<!--.*?-->", "", self.block, flags=re.S)

    def _need(self):
        self.assertNotEqual(self.block, "",
                            "the import dialog is not in the scanned set — the modal "
                            "reader stopped matching it, and these checks are about "
                            "that one dialog")

    def test_it_is_two_columns(self):
        self._need()
        self.assertIn('class="dc-modal-pad dc-ie-grid"', self.markup,
                      "the import dialog is not the two-column grid any more")
        self.assertEqual(self.markup.count('class="dc-ie-col"'), 2,
                         "the redesign is two columns: connection on the left, "
                         "source→local→schedule on the right")

    def test_every_list_caps_its_own_height(self):
        self._need()
        """A list that grows pushes the footer off, which is the bug being replaced.

        `.dc-ie-list` is the cap for all three (connections, source tables, schedules);
        the guard is on the RULE, not on each usage, because a fourth list would
        otherwise inherit the cap by accident rather than by decision.
        """
        decls = re.sub(r"\s+", " ", _rule(".dc-ie-list"))
        self.assertRegex(decls, r"max-height\s*:", f".dc-ie-list has no cap: {decls}")
        self.assertRegex(decls, r"overflow\s*:\s*auto", f".dc-ie-list does not scroll: {decls}")

    def test_there_is_no_native_select(self):
        self._need()
        """The user's complaint was a screenshot OF a native select and checkbox.

        A `<select>` in this dialog cannot be styled into the rest of the form, and
        it is the one control whose height the browser decides, which is exactly what
        "one screen, compact" cannot afford. The interval is a radio group and the
        target is a text input plus chips.
        """
        self.assertNotIn("<select", self.markup,
                         "a native <select> is back in the import dialog")

    def test_the_interval_is_a_radio_group_with_five_faces(self):
        self._need()
        radios = re.findall(r'name="dc-ie-interval" value="(\d+)"', self.markup)
        self.assertEqual(sorted(radios), ["10080", "1440", "15", "360", "60"],
                         f"the interval radio group changed: {radios}")

    def test_the_switch_is_a_real_checkbox(self):
        self._need()
        """Drawn, but still a control: keyboard, focus and `.checked` all come free.

        A switch faked with a `<div>` and a JS variable is three places to keep in
        step (markup, state, ARIA) and none of them checked by a browser.
        """
        self.assertRegex(self.markup, r'<input type="checkbox" id="dc-ie-sched"',
                         "the schedule switch is not a real checkbox")

    def test_no_tag_inside_a_translated_pair(self):
        self._need()
        """⚠️ The bug this round actually shipped and then fixed.

        The promise line carried `<b>连接信息永远不会随分享出去</b>` in the middle of
        a `中文 / English` pair. `t()` can only split a pair that sits in ONE text
        node, so the tag cut the sentence in half: the English half replaced the
        second half of the Chinese one and the two ran together with no space —
        `口令只用这一次…连接信息永远不会随分享出去The password is for this one…`.
        The rule is the general one, so it is asserted over the whole dialog: no
        element tags inside a `.dc-at-hint` / `.dc-ie-hint` paragraph.
        """
        for m in re.finditer(r'<p class="dc-at-hint[^"]*">(.*?)</p>', self.markup, re.S):
            self.assertNotRegex(m.group(1), r"<[a-zA-Z]",
                                f"a tag inside a translated pair: {m.group(1)[:70]}")

    def test_the_new_handlers_are_reachable_from_inline_onclick(self):
        self._need()
        """`onclick` runs in global scope and cannot see a module IIFE's `const`.

        An unexported handler is a `ReferenceError` whose only symptom is a button
        that does nothing — no console noise in some paths, no layout clue at all.
        ⚠️ Read the IIFE's RETURN OBJECT, not `window.dc = …`: the module is
        `const dc = (() => { … return {…}; })();`, so there is no assignment of that
        shape to find, and an `HTML.index("window.dc = {")` raises ValueError
        instead of checking anything.
        """
        start = HTML.index("const dc = (() => {")
        end = HTML.index("})();", start)
        ret = HTML.rindex("return {", start, end)
        exported = HTML[ret:end]
        for fn in ("dcSchedNew", "dcSchedTarget", "dcSchedCreate"):
            self.assertRegex(exported, rf"\b{fn}\b",
                             f"{fn} is used by an inline onclick but not returned by the dc IIFE")

    def test_the_dialog_is_wide_enough_for_two_columns(self):
        self._need()
        decls = re.sub(r"\s+", " ", _rule(".dc-modal-wide"))
        self.assertRegex(decls, r"width\s*:", f".dc-modal-wide has no width: {decls}")
        px = re.search(r"(\d{3,4})px", decls)
        self.assertTrue(px and int(px.group(1)) >= 900,
                        f"two columns inside {decls} leaves each one too narrow")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
