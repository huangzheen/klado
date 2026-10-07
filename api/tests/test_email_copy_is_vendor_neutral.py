"""
Email copy must name the recipient's **role**, not a vendor.

Why this file exists: the agent-code mail was written around one assistant
("paste this into Doubao Work / 豆包工作"), and the headline carried the token's
internal label — so the live signup mail read "an access code was issued for
signup", which explains nothing to the person reading it. Both are wrong in the
same way: they tell the recipient something about *our* setup that they cannot
act on, and both become false the moment anyone uses a different assistant.

The guard therefore has two halves, and the second is the one that matters:

* a **denial** — no product name may appear in any recipient-visible string;
* a **positive control** — the same query must still *find* the strings it is
  scanning. A "no forbidden word" assertion on its own passes just as happily
  when the query matches nothing at all (renamed module, moved function,
  template no longer built), which is the failure mode that turns a guard into
  a permanent green light. So we assert the scan actually reaches real copy
  before we trust its verdict.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from klado_shared import mail_templates as mt  # noqa: E402


# Vendors an assistant product could be named by. Matched case-insensitively and
# on a word boundary so an unrelated word that merely contains one of them (a
# domain, an id) cannot trip the guard.
FORBIDDEN = re.compile(
    r"\b(?:doubao|feishu|lark|coze|cursor|copilot|chatgpt|openai|gemini|claude|"
    r"qwen|wenxin|deepseek|kimi|dingtalk|slack|notion)\b|豆包|飞书|扣子|钉钉|通义|文心",
    re.IGNORECASE,
)

# The strings a real recipient sees, and the label values that reach them.
SAMPLE_CODE = "klado_agent_SAMPLE0NOT0AREAL0CODE000000"
LABELS = ("signup", "laptop", "")


def _rendered_copy() -> dict[str, str]:
    """Every recipient-visible string: HTML and plain text of all three mails."""
    out: dict[str, str] = {}
    for label in LABELS:
        for name, template in (
            ("agent_code",
             mt.agent_code_email(SAMPLE_CODE, label, "someone@example.com",
                                 "https://klado.team/agent.zip")),
            ("invite",
             mt.invite_email("831742", 7, "someone@example.com",
                             "https://klado.team/", invited_by="A colleague")),
            ("verification",
             mt.verification_code_email("314159", 10, "someone@example.com",
                                        app_url="https://klado.team/")),
        ):
            subject, html, text = template
            out[f"{name}[{label or 'no-label'}] subject"] = subject
            out[f"{name}[{label or 'no-label'}] html"] = html
            out[f"{name}[{label or 'no-label'}] text"] = text
    return out


class EmailCopyNamesNoVendor(unittest.TestCase):
    """No recipient-visible string may name a specific assistant product."""

    def test_no_vendor_name_in_any_rendered_copy(self):
        copy = _rendered_copy()
        # Positive control. Measured, not assumed: I deleted the three separate
        # checks that used to live here (count > 9, "Klado" in the sample, html
        # longer than 500 chars) one at a time and the suite stayed green every
        # time — the other test, `test_scan_set_covers_every_template_and_label`,
        # was already catching the vacuous case. So this one assertion is here
        # because it is the thing that makes THIS test's verdict trustworthy on
        # its own, not because three similar lines looked thorough.
        self.assertGreater(len(copy), 9, f"expected the real templates, got {list(copy)}")
        empty = [k for k, v in copy.items() if not v.strip()]
        self.assertEqual(empty, [], f"rendered empty — the scan is a no-op: {empty}")
        # One real template, read back: if the copy is a stub, the whole test is.
        sample = copy["agent_code[signup] html"]
        self.assertIn("agent access code", sample.lower(), "scan target is not the real template")
        self.assertIn("Klado", sample, "scan target is not the real template")

        bad = {k: sorted(set(FORBIDDEN.findall(v))) for k, v in copy.items()
               if FORBIDDEN.search(v)}
        self.assertEqual(bad, {}, f"vendor name in recipient-visible copy: {bad}")

    def test_scan_set_covers_every_template_and_label(self):
        """
        The denial above is only as broad as `_rendered_copy`. A template added
        without a matching entry here would simply never be scanned — so assert
        the inventory, not just the verdict.
        """
        keys = set(_rendered_copy())
        for name in ("agent_code", "invite", "verification"):
            for label in ("signup", "laptop", "no-label"):
                for part in ("subject", "html", "text"):
                    self.assertIn(f"{name}[{label}] {part}", keys,
                                  f"{name}/{label}/{part} is not being scanned")

    def test_agent_code_says_assistant_not_a_specific_one(self):
        """The instruction must still be actionable: paste it into *your* assistant."""
        _, html, text = mt.agent_code_email(SAMPLE_CODE, "signup", "someone@example.com",
                                            "https://klado.team/agent.zip")
        for name, body in (("html", html), ("text", text)):
            self.assertIn("AI assistant", body,
                          f"{name} no longer tells the recipient where to paste this")

    def test_internal_label_is_not_the_headline(self):
        """
        The regression that prompted the guard: `label` is our bookkeeping name
        for the token, so printing it as the headline produced "an access code
        was issued for signup". It may still appear in the small print, where it
        can only help the recipient find the row in the app.
        """
        _, html, text = mt.agent_code_email(SAMPLE_CODE, "signup", "someone@example.com")
        for name, body in (("html", html), ("text", text)):
            self.assertNotIn("issued for <b>signup</b>", body,
                             f"{name}: the internal label is back in the headline")
            self.assertNotIn("issued for signup", body,
                             f"{name}: the internal label is back in the headline")
        # …and it is still findable where it helps.
        self.assertIn("signup", text, "the label should still be in the small print")

    def test_label_absent_still_renders(self):
        """
        A blank label is the default (`send_agent_code` is called with `label=""`
        when a token has none), and the copy must not leave a dangling clause —
        `&ldquo;<b></b>&rdquo;` or "as ''" is the shape such a bug takes.

        Note the revoke instructions differ by variant on purpose: the HTML names
        the menu ("listed in the app under Agent access") so the reader knows
        *where* to click, the plain text cannot point at a menu and only says the
        code can be revoked. Assert each against its own wording, not one string
        for both — that mistake is what this assertion made first.
        """
        _, html, text = mt.agent_code_email(SAMPLE_CODE, "", "someone@example.com")
        for name, body in (("html", html), ("text", text)):
            self.assertNotIn("&ldquo;<b></b>&rdquo;", body, f"{name}: empty label rendered")
            self.assertNotIn("as ''", body, f"{name}: empty label rendered")
            self.assertNotIn("is listed in the app as", body,
                             f"{name}: empty label still claims a name in the app")
            self.assertIn("revoke", body, f"{name}: the revoke instructions vanished")
            self.assertIn("AI assistant", body, f"{name} lost its instruction")
        self.assertIn("Agent access", html,
                      "the HTML should still name the menu the code is listed under")

    def test_test_preview_does_not_show_a_vendor_as_the_label(self):
        """
        The admin's preview mail is the sample people copy from, so a vendor name
        in its label would reintroduce exactly what the guard above forbids.

        Read as text rather than by importing `mailer`: the module pulls in the
        app's settings and third-party clients, and a guard that cannot run
        outside the container is a guard that quietly stops running at all.
        """
        source = (_ROOT / "klado_shared" / "mailer.py").read_text(encoding="utf-8")
        self.assertIn("agent_code_email(", source, "preview no longer builds that template")
        match = FORBIDDEN.search(source)
        self.assertIsNone(
            match,
            f"mailer.py names a vendor ({match.group(0) if match else ''!r}); "
            "the preview is what admins copy from",
        )


class GuardItselfIsLoadBearing(unittest.TestCase):
    """
    A guard that cannot fail is worse than no guard, because it reads as
    coverage. These two assert the *scanner* is real, not the copy.
    """

    def test_forbidden_pattern_matches_a_planted_vendor(self):
        for word in ("Doubao Work", "豆包工作", "paste into Feishu", "飞书机器人"):
            self.assertTrue(FORBIDDEN.search(word),
                            f"the pattern does not match {word!r} — it would never catch it")

    def test_forbidden_pattern_ignores_unrelated_text(self):
        for word in ("Klado agent access code", "your AI assistant",
                     "signin@klado.team", "report card"):
            self.assertIsNone(FORBIDDEN.search(word),
                              f"false positive on {word!r} — the guard would cry wolf")


if __name__ == "__main__":
    unittest.main()
