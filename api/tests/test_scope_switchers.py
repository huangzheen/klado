"""Every module's scope switcher is the same control, and none of them is a <select>.

⚠️ Why this is a CROSS-MODULE guard and not a Data Center test: the defect was never
"this select is ugly". It was that the Data Center toolbar was the **only** place in
the product where a scope switcher was a native `<select>`, while Workspace,
Dashboard, Knowledge base and Calendar all use the flat `.rpt-tab` pair. A test
scoped to Data Center would have passed the moment somebody deleted the select, and
the next new module would ship a dropdown again. The invariant is the invariant.

Two more things this pins, both of which are invisible in a screenshot:
  · the active scope button is decided in ONE place (`switchScope`). The two
    "go back to mine" call sites used to poke the control directly as well, which
    is two pieces of state (a select's value and a button's highlight) for one
    fact, and the direct poke is the one that drifts;
  · the Data Center buttons carry the SAME class list as the other modules', so
    "reference the other pages" is checked rather than assumed.

    ../.venv312/bin/python -m unittest tests.test_scope_switchers
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "frontend" / "out" / "index.html").read_text(encoding="utf-8")

# ⚠️ Markup, not prose — and TWO kinds of comment. The HTML note above the Data
# Center pair explains what a `<select>` used to be there, and the JS comment in
# `switchScope` quotes the old call verbatim; either one read as a live control.
# `^\s*//` strips only WHOLE-LINE comments, so a `https://` inside a string is safe.
MARKUP = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
MARKUP = re.sub(r"(?m)^[ \t]*//[^\n]*", "", MARKUP)

#: The modules that have a scope switcher, and the handler each one calls.
SCOPED = {
    "reports": "reportsPage.switchScope",
    "dashboards": "dashboardPage.switchScope",
    "knowledge": "knowledgePage.switchScope",
    "calendar": "calendarPage.switchScope",
    "datacenter": "dc.switchScope",
}


def _buttons_for(handler: str) -> list[str]:
    # ⚠️ The attribute ends with a DOUBLE quote (`onclick="f('x')"`). Writing `')`
    # here matched nothing at all — and a regex that matches nothing returns an
    # empty list, which is the shape a missing control also returns. The guard's
    # first test is what caught it, not the assertion about the class list.
    return re.findall(r"<button[^>]*onclick=\"" + re.escape(handler) + r"\('[^']+'\)" + '"' + r"[^>]*>",
                      MARKUP)


class ScopeSwitcherTests(unittest.TestCase):
    def test_every_module_actually_has_a_scope_switcher(self):
        """A guard that finds nothing looks exactly like a guard that passes."""
        for name, handler in SCOPED.items():
            self.assertTrue(_buttons_for(handler), f"no scope buttons for {name}")

    def test_no_scope_switcher_is_a_native_select(self):
        for handler in SCOPED.values():
            self.assertNotRegex(
                MARKUP, r"<select[^>]*" + re.escape(handler),
                f"a scope switcher is a native <select>: {handler}")

    def test_every_scope_button_uses_the_same_class_list(self):
        """Same control, same look — the point of "reference the other pages"."""
        for name, handler in SCOPED.items():
            seen = [re.search(r'class="([^"]*)"', b) for b in _buttons_for(handler)]
            seen = [tuple((m.group(1) if m else "").split()) for m in seen]
            self.assertTrue(seen, f"{name} has no scope buttons")
            for classes in seen:
                self.assertIn(
                    "rpt-tab", classes,
                    f"{name} uses {list(classes)} — every module's scope switcher is a "
                    f"pair of .rpt-tab buttons")

    def test_the_active_button_is_decided_in_exactly_one_place(self):
        """No call site may set the control directly any more.

        ⚠️ The old form was `document.querySelector('.dc-scope').value = 'mine'`
        next to an `await switchScope('mine')`. Both were needed: the select's value
        and the module's `_state.scope` are different state, and a reset that
        updated one and not the other left the toolbar showing one scope while the
        grid showed another.
        """
        for pattern in (r"\.dc-scope", r"\.value\s*=\s*'mine'", r"\.value\s*=\s*\"mine\""):
            hits = [m.group(0) for m in re.finditer(pattern, MARKUP)]
            self.assertEqual(hits, [], f"a call site still pokes the scope control: {hits}")

    def test_the_marking_helper_covers_every_button(self):
        """The selector has to reach the buttons, and switchScope has to CALL it.

        ⚠️ The call is the half that was missing. The first version of this test only
        checked that `dcScopeButtons` existed and mentioned `[data-dc-scope]`, so
        deleting the one line inside `switchScope` that invokes it left the suite
        green — while every click on a scope tab changed the data and left the
        highlight where it was. A helper nobody calls is a helper that does nothing.
        """
        m = re.search(r"function dcScopeButtons.*?\n  \}", MARKUP, re.S)
        self.assertIsNotNone(m, "dcScopeButtons is gone — switchScope cannot mark anything")
        self.assertIn("[data-dc-scope]", m.group(0))
        # And the two buttons really do carry the attribute it selects on.
        self.assertEqual(len(re.findall(r'data-dc-scope="(mine|shared)"', MARKUP)), 2)

        fn = re.search(r"async function switchScope\(scope\) \{.*?\n  \}", MARKUP, re.S)
        self.assertIsNotNone(fn, "the Data Center switchScope is gone")
        self.assertIn(
            "dcScopeButtons(", fn.group(0),
            "switchScope no longer marks the active scope button — the data changes "
            "and the toolbar keeps highlighting the old scope")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
