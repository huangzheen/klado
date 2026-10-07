#!/usr/bin/env python3
"""Guard: app.css must not gain conflicting duplicate declarations.

Why
---
`app.css` is one ~7k-line file, loaded as-is, with no build step. The cascade
resolves equal specificity by source order, so when one selector is declared in
two places **the later one silently wins**. An agent that edits the copy near
the front of the file and re-measures gets a rule that never applies — the
failure AGENTS.md records as "you wrote a new flex rule and not one line of it
passed", after three wasted rounds of tuning flex properties against a rule
nobody could see.

So the invariant is not "this file is tidy". It is the one AGENTS.md already
names as the second of its two checks:

    grep -n '^\\s*\\.dc-dataset-footer' app.css   # more than one hit?

…expressed so it cannot be forgotten. This is a `test_*.py` on purpose: it needs
no server and no database, so the pipeline's `unittest discover` already runs it.

Baseline
--------
The file legitimately contains a handful of conflicts that predate this test
(they are listed in `KNOWN_CONFLICTS` with the reason each is tolerated). The
test therefore asserts **two** things, and the second is the one that matters:

  1. every known conflict is still exactly where the baseline says it is;
  2. **no new conflict appears anywhere else** — new selector OR new property on
     an already-known selector.

A test that only froze the current count would pass while someone adds a 20th
one. A test that demanded zero would demand a 19-file CSS refactor as the price
of admission. This one lets the debt stand and blocks new debt.

What is deliberately NOT a conflict (see css_stale_rules.scan for the parser)
--------------------------------------------------------------------------
  * rules in different at-rule contexts — `@media` overriding a desktop rule is
    how responsive CSS is supposed to work; app.css's mobile block is entirely
    `!important` by design;
  * `@keyframes` step selectors — `to { transform: … }` is not an element;
  * one selector repeated inside a single rule's comma list
    (`.sku-modal, /* modal */ .sku-modal { … }`) — redundant, not conflicting;
    reported as a LINT by the probe.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from css_stale_rules import scan  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CSS = os.path.join(REPO, "frontend", "out", "app.css")

# selector -> {property: (line_hint_a, line_hint_b)} for conflicts that are allowed
# to exist today. Adding a key here is a deliberate decision; a reviewer should
# be able to ask "why is this tolerated?" and get an answer from the comment.
#
# ⚠️ The line numbers are a *hint for where to look*, not part of the contract.
# The test asserts only that each pair still conflicts; it does not compare line
# numbers, because every edit above them shifts them and that would turn the
# guard into noise.
KNOWN_CONFLICTS = {
    # Resolved 2026-10-05: `.dc-dataset-card-top` used to declare `flex` and
    # `min-width` twice (the flex-row layout and a stray refinement further down).
    # The dataset list became a card grid and the two rules were folded into one,
    # so the entry that used to be here is gone — a baseline that still credited
    # itself for a fixed conflict is the second failure direction this test covers.

    # Tolerated: `.rpt-field textarea` sets a field-wide font at 4196 and a
    # monospace override for the body area at 4209. Different intent, same
    # selector — the fix is a more specific selector, tracked as tech debt.
    ".rpt-field textarea": {"font-size": (4196, 4209), "font-family": (4196, 4209)},

    # Tolerated: `.cal-fld textarea` — base field at 5237/5248, then a taller
    # textarea variant at 5302. Same shape as above.
    ".cal-fld textarea": {
        "font-size": (5237, 5302),
        "resize": (5248, 5302),
        "min-height": (5248, 5302),
    },

    # ⚠️ LATENT, not currently a live bug. `.adm-input, .adm-select` sizes selects
    # at 30px (6640). The rule at 6873 re-declares the BARE `.adm-select` at 26px
    # with the comment "a <select> in the Actions cell, sized to the roster
    # rather than to the column".
    #
    # Verified before writing this: the app contains exactly ONE `.adm-select`
    # (index.html, `_orgRow`'s role dropdown) and it does sit in the Actions cell,
    # so the rendering is correct **by accident** — the scoping happens to match
    # the intent because there is only one instance. The moment a second select
    # borrows the class, every one of them silently drops to 26px, and the rule
    # that does it is 230 lines below the one the author would have edited.
    #
    # That is the exact failure mode this whole test exists for, so it is left in
    # the baseline with the reason written down rather than silently tolerated.
    # Fixing it means scoping 6873 to the actions cell (e.g. `.adm-actions
    # .adm-select`); that is a visual-affecting change and out of scope here.
    ".adm-select": {
        "height": (6640, 6873),
        "padding": (6640, 6873),
        "border": (6640, 6873),
        "border-radius": (6640, 6873),
        "background": (6640, 6873),
        "color": (6640, 6873),
        "font-family": (6640, 6873),
        "font-size": (6640, 6873),
    },

    # Tolerated: verified dead copy/paste residue — two byte-identical rule
    # pairs (`.sku-modal-x:hover` and `#sku-modal-bg .sku-modal`) repeated at
    # 2777/2781 and 2779/2783. Same values, so the second copy changes nothing;
    # deleting either is behaviour-neutral.
    ".sku-modal-x:hover": {"background": (2777, 2781), "color": (2777, 2781)},
    "#sku-modal-bg .sku-modal": {"width": (2779, 2783)},

    # Tolerated: `.rail-picker-new` sets its own colour at 508 and is then
    # recoloured by an `:hover` sibling rule at 528.
    ".rail-picker-new": {"color": (508, 528)},
}


class StaleCssRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(CSS):
            raise unittest.SkipTest(f"app.css not found at {CSS}")
        with open(CSS, encoding="utf-8") as fh:
            cls.css = fh.read()
        cls.conflicts, cls.lints = scan(cls.css)

    def _found(self):
        """{(selector, property): sorted(lines)} across every at-rule context."""
        found = {}
        for (ctx, sel, prop, _imp), hits in self.conflicts.items():
            found.setdefault((sel, prop), set()).update(h[1] for h in hits)
        return {k: sorted(v) for k, v in found.items()}

    def test_no_new_conflicting_declarations(self):
        """The load-bearing assertion: nothing outside the baseline."""
        found = self._found()
        allowed = {(sel, prop) for sel, props in KNOWN_CONFLICTS.items() for prop in props}
        new = sorted(k for k in found if k not in allowed)
        self.assertEqual(
            [], new,
            "app.css gained a conflicting duplicate declaration.\n"
            "Two rules now declare the same property for the same selector at the same\n"
            "importance, so the LATER one silently wins. Either fold them into one rule,\n"
            "or make the loser more specific so the intent survives the cascade.\n"
            "If this one is deliberate, add it to KNOWN_CONFLICTS in this file with a\n"
            "comment saying why it is tolerated.\n\n"
            f"New conflict(s): {new}",
        )

    def test_known_conflicts_are_still_where_the_baseline_says(self):
        """A baseline that silently drifts stops describing reality.

        This catches the sneaky direction too: someone *fixes* a conflict without
        removing it from KNOWN_CONFLICTS, and the baseline starts crediting
        itself for a problem that no longer exists.

        ⚠️ Asserted on **existence**, never on line numbers. An earlier version
        compared the recorded line numbers and mutation testing showed exactly
        why that is wrong: deleting one duplicate shifted every later rule up by
        four lines, so the test failed with "moved: baseline says [4196, 4209],
        found [4192, 4205]" — reporting a line shift when the real event was the
        conflict being resolved. Anything that edits app.css adds or removes
        lines, so a line-pinned baseline would fail on every unrelated comment
        above it, and the guard would be switched off inside a week. The line
        numbers stay in KNOWN_CONFLICTS purely as a *where to look* hint.
        """
        found = self._found()
        problems = []
        for sel, props in KNOWN_CONFLICTS.items():
            for prop in props:
                if (sel, prop) not in found:
                    problems.append(
                        f"{sel} {{ {prop} }} is listed in KNOWN_CONFLICTS but no longer "
                        f"conflicts — it was fixed. Delete the baseline entry.\n"
                        f"  (baseline pointed at lines {sorted(props[prop])}, "
                        f"which are now only a hint.)"
                    )
        self.assertEqual([], problems, "\n".join(problems))

    def test_no_selector_is_listed_twice_in_one_rule(self):
        """`.sku-modal, .sku-modal { … }` is dead weight in a comma list.

        Harmless, but it makes the conflict report double-count and it reads as
        a merge artefact. Two exist today and are recorded here; the guard is
        that the number does not grow.
        """
        dup_rules = {rid: sels for rid, sels in self.lints.items()}
        self.assertLessEqual(
            len(dup_rules), 2,
            "app.css gained a selector listed twice in one comma list: "
            f"{sorted(dup_rules)}\n"
            "Delete the duplicate entry; the declarations after it are unaffected.",
        )

    def test_probe_detects_a_planted_conflict(self):
        """The parser itself is the thing that can silently break.

        Every filter above (at-rule context, @keyframes, importance, comma-list)
        is a decision that could start mis-classifying, and a broken probe that
        reports zero findings is indistinguishable from a clean file. So plant a
        known conflict in a minimal string and require it to be found.
        """
        planted = (
            ".a { color: red; }\n"
            ".a { color: blue; }\n"
        )
        conflicts, _ = scan(planted)
        self.assertIn(
            ((), ".a", "color", False), conflicts,
            "probe failed to detect a planted same-selector same-property conflict",
        )

    def test_probe_ignores_responsive_and_animation_contexts(self):
        """The three exclusions, pinned.

        Without this, a future refactor of the parser that drops the at-rule
        stack would start flagging every responsive override in the file — 47
        findings instead of 19 — and the guard would be switched off within a
        week.
        """
        cases = {
            "media override": "@media (max-width: 768px) { .a { color: red; } }",
            "keyframes step": (
                "@keyframes slide { from { transform: none; } to { transform: none; } }"
            ),
            "important override": ".a { color: red; } .a { color: blue !important; }",
        }
        for name, css in cases.items():
            with self.subTest(case=name):
                conflicts, _ = scan(css)
                self.assertEqual(
                    0, len(conflicts),
                    f"{name} must not be reported as a conflict; app.css relies on this. "
                    f"Got: {conflicts}",
                )


if __name__ == "__main__":
    unittest.main()
