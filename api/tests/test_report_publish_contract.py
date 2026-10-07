"""
The published-report contract: which format a document is, and what the slug looks like.

Two rules from 2026-09-27 live here, both decided by pure functions so they can be tested
without a database:

* every report is expected to be a 16:9 paged deck, and the publish response has to say so
  (`format` + `format_warning` on a long-form document);
* a slug-less publication always carries `-YYYYMMDD-HHMM-xxxxxx` (timestamp + 6 random
  characters), so two publications can never share an address.

The trap worth pinning down: a **bilingual long-form** document matches the deck-runtime
inlining regex (`data-lang=`), so "does the runtime get inlined" and "is this a deck" are
different questions and must be answered by different tests.
"""
import re
import unittest

from routers import reports


DECK = ('<html><body><div class="deck">'
        '<section class="slide" data-lang="en">Page 1</section>'
        '</div></body></html>')
LONG_BILINGUAL = ('<html><body><div class="wrap">'
                  '<p data-lang="en">one scrolling page</p><p data-lang="zh">一页长文</p>'
                  '</div></body></html>')
LONG_PLAIN = "<html><body><div class=\"wrap\"><h1>plain</h1></div></body></html>"


class DetectFormatTests(unittest.TestCase):
    def test_deck_is_detected(self):
        self.assertEqual(reports._detect_format(DECK), "deck")

    def test_plain_long_form_is_detected(self):
        self.assertEqual(reports._detect_format(LONG_PLAIN), "long-form")

    def test_bilingual_long_form_is_not_a_deck(self):
        # It DOES get the deck runtime inlined (for the language layer) — the format answer
        # must not be derived from that regex.
        self.assertTrue(reports._DECK_HINT_RE.search(LONG_BILINGUAL))
        self.assertEqual(reports._detect_format(LONG_BILINGUAL), "long-form")

    def test_empty_document_is_long_form(self):
        self.assertEqual(reports._detect_format(""), "long-form")

    def test_deck_and_pptx_gate_agree(self):
        # `format == "deck"` must imply "exportable to editable PPTX", which the export
        # endpoint decides with the same `.slide` test.
        self.assertFalse(reports._detect_format(DECK) == "deck"
                         and reports.BeautifulSoup(DECK, "html.parser").select_one(".slide") is None)

    def test_republished_deck_keeps_model_but_replaces_old_runtime(self):
        old = DECK.replace('</body>',
            '<script type="application/json" data-report-deck-model>{"version":1}</script>'
            '<style data-report-deck>old style</style>'
            '<script data-report-deck>old runtime</script></body>')
        rendered = reports._inline_deck_runtime(old)
        self.assertNotIn('old style', rendered)
        self.assertNotIn('old runtime', rendered)
        tags = reports.BeautifulSoup(rendered, "html.parser")
        self.assertEqual(len(tags.select('script[data-report-deck-model]')), 1)
        self.assertEqual(len(tags.select('script[data-report-deck]')), 1)


class AnnotateFormatTests(unittest.TestCase):
    def test_deck_gets_no_warning(self):
        meta = reports._annotate_format({}, DECK)
        self.assertEqual(meta["format"], "deck")
        self.assertNotIn("format_warning", meta)

    def test_long_form_gets_a_warning_that_names_the_rule(self):
        meta = reports._annotate_format({}, LONG_PLAIN)
        self.assertEqual(meta["format"], "long-form")
        self.assertIn("16:9", meta["format_warning"])
        self.assertIn("<section", meta["format_warning"])

    def test_existing_meta_fields_are_kept(self):
        meta = reports._annotate_format({"slug": "x"}, DECK)
        self.assertEqual(meta["slug"], "x")


class SlugRuleTests(unittest.TestCase):
    def test_collision_suffix_shape(self):
        suffix = reports._collision_suffix()
        self.assertRegex(suffix, r"^-\d{8}-\d{4}-[a-z0-9]{6}$")

    def test_two_suffixes_in_the_same_minute_still_differ(self):
        self.assertNotEqual(reports._collision_suffix(), reports._collision_suffix())

    def test_slugify_is_url_safe_and_bounded(self):
        slug = reports._slugify("Q3 Channel Deep-Dive — Brand A vs Brand B / 2026")
        self.assertRegex(slug, r"^[a-z0-9][a-z0-9-]*$")
        self.assertLessEqual(len(slug), 80)

    def test_all_cjk_title_falls_back_to_a_hash(self):
        # A constant "report" would make every Chinese-titled publication collide.
        self.assertTrue(reports._slugify("渠道深度复盘").startswith("report-"))
        self.assertNotEqual(reports._slugify("渠道深度复盘"), reports._slugify("价格段矩阵"))

    def test_mostly_cjk_title_keeps_only_its_ascii_residue(self):
        # Recorded, not endorsed: the hash fallback above only fires when NOTHING ascii
        # survives, so a mostly-Chinese title with a stray number keeps just that number
        # ("渠道深度复盘 2026" → "2026"). Uniqueness no longer depends on it — a slug-less
        # publish always carries the timestamp+random suffix — so the cost is a less
        # readable address, and a one-line fix is available if that ever matters.
        self.assertEqual(reports._slugify("渠道深度复盘 2026"), "2026")

    def test_suffixed_slug_is_still_a_valid_slug(self):
        slug = reports._slugify("Market Overview")[:60] + reports._collision_suffix()
        self.assertTrue(reports.SLUG_RE.match(slug), slug)
        self.assertRegex(slug, r"-\d{8}-\d{4}-[a-z0-9]{6}$")


class BilingualSummaryTests(unittest.TestCase):
    """A bilingual report must carry a summary in both of its languages.

    The card veil shows the two lines, so a report that offers EN and ZH but only one
    summary hands one of its two readers a sentence in the wrong language — and nothing
    looks broken. `summary` alone (the pre-existing field) is therefore refused on a
    bilingual document, loudly, instead of being quietly reused.
    """

    BILINGUAL = ('<html><body><div class="deck">'
                 '<section class="slide" data-lang="en">Page 1</section>'
                 '<section class="slide" data-lang="zh">第 1 页</section>'
                 '</div></body></html>')

    def test_is_bilingual_needs_both_languages(self):
        self.assertTrue(reports.is_bilingual(["en", "zh"]))
        self.assertTrue(reports.is_bilingual(["zh", "en"]))
        self.assertTrue(reports.is_bilingual(["zh-CN", "en-US"]))
        self.assertFalse(reports.is_bilingual(["en"]))
        self.assertFalse(reports.is_bilingual(["zh"]))
        self.assertFalse(reports.is_bilingual([]))
        self.assertFalse(reports.is_bilingual(None))

    def test_the_language_list_comes_from_the_document_itself(self):
        self.assertTrue(reports.is_bilingual(reports.detect_langs(self.BILINGUAL)))
        self.assertFalse(reports.is_bilingual(reports.detect_langs(LONG_PLAIN)))

    def test_single_language_report_still_uses_the_plain_summary(self):
        self.assertEqual(reports.bilingual_summaries("One line", "", "", ["en"]),
                         ("One line", "", ""))
        self.assertEqual(reports.bilingual_summaries("一句话", "", "", ["zh"]),
                         ("一句话", "", ""))

    def test_bilingual_report_with_only_the_plain_summary_is_refused(self):
        with self.assertRaises(reports.HTTPException) as ctx:
            reports.bilingual_summaries("One line", "", "", ["en", "zh"])
        self.assertEqual(ctx.exception.status_code, 400)
        # The message has to name the fields, or an agent cannot fix it without guessing.
        self.assertIn("summary_en", str(ctx.exception.detail))
        self.assertIn("summary_zh", str(ctx.exception.detail))

    def test_half_a_pair_is_refused_and_names_the_missing_half(self):
        with self.assertRaises(reports.HTTPException) as ctx:
            reports.bilingual_summaries("", "One line", "", ["en", "zh"])
        self.assertIn("summary_zh", str(ctx.exception.detail))
        self.assertNotIn("Missing: summary_en", str(ctx.exception.detail))
        with self.assertRaises(reports.HTTPException) as ctx2:
            reports.bilingual_summaries("", "", "一句话", ["en", "zh"])
        self.assertIn("summary_en", str(ctx2.exception.detail))

    def test_a_complete_pair_passes_and_fills_the_plain_field(self):
        # `summary` is what every existing caller reads; leaving it empty on a bilingual
        # report would put a hole in the card for anything that predates the pair.
        self.assertEqual(reports.bilingual_summaries("", "One line", "一句话", ["en", "zh"]),
                         ("One line", "One line", "一句话"))

    def test_an_explicit_plain_summary_still_wins(self):
        self.assertEqual(reports.bilingual_summaries("Card line", "One line", "一句话", ["en", "zh"]),
                         ("Card line", "One line", "一句话"))

    def test_whitespace_is_not_a_summary(self):
        with self.assertRaises(reports.HTTPException):
            reports.bilingual_summaries("  ", "   ", " ", ["en", "zh"])


if __name__ == "__main__":
    unittest.main()
