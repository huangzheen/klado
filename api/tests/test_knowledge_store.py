import unittest

from services.ai.knowledge_store import KnowledgeDocument, _score, _search_tokens, validate_document


class KnowledgeStoreSearchTests(unittest.TestCase):
    def test_scores_exact_title_matches_above_body_matches(self):
        query = "performance analysis"
        self.assertGreater(
            _score(query, "Performance analysis rule", "miscellaneous"),
            _score(query, "miscellaneous", "performance analysis"),
        )

    def test_keeps_chinese_query_as_a_search_token(self):
        self.assertIn("线上销售", _search_tokens("线上销售"))

    def test_builds_cjk_ngrams_for_unsegmented_requests(self):
        tokens = _search_tokens("线上今年销售情况")
        self.assertIn("线上", tokens)
        self.assertIn("销售", tokens)

    def test_document_value_is_database_neutral(self):
        document = KnowledgeDocument("rule", "Rule", "Content", "rule", "managed")
        self.assertEqual(document.document_id, "rule")

    def test_tags_are_a_first_class_retrieval_signal(self):
        query = "渠道诊断"
        tagged = _score(query, "通用规则", "业务说明", metadata={"tags": ["渠道诊断"]})
        body_only = _score(query, "通用规则", "业务说明 渠道")
        self.assertGreater(tagged, body_only)

    def test_document_contract_rejects_unknown_types_and_bad_versions(self):
        document = KnowledgeDocument("bad", "Bad", "Content", "unknown", "managed", version="latest")
        errors = validate_document(document)
        self.assertTrue(any("document_type" in error for error in errors))
        self.assertTrue(any("version" in error for error in errors))

    def test_document_contract_accepts_versioned_playbook(self):
        document = KnowledgeDocument(
            "channel-playbook", "Channel Playbook", "Content", "playbook", "managed",
            version="1.2.0", metadata={"tags": ["channel", "diagnosis"]},
        )
        self.assertEqual(validate_document(document), [])

    # ── retrieval scoring (2026-09-26) ──────────────────────────────────────
    # Latin tokens used to be counted as SUBSTRINGS, so short common words hit
    # unrelated documents ("not" matched "nothing", "and" matched "standard").
    def test_latin_tokens_match_whole_words_only(self):
        # stopwords are gone entirely (stronger than before), and a NON-stopword
        # short token still must not match inside a longer word
        self.assertEqual(_score("not", "misc", "nothing cannot annotation"), 0)
        self.assertEqual(_score("and", "misc", "standard brand expand"), 0)
        self.assertEqual(_score("ann", "misc", "annotation"), 0)
        self.assertGreater(_score("ann", "misc", "the ann report"), 0)

    def test_english_stopwords_are_dropped_but_phrases_survive(self):
        self.assertNotIn("the", _search_tokens("the"))
        self.assertNotIn("not", _search_tokens("not"))
        # A multi-word token is meaningful even when it contains a stopword.
        self.assertIn("the report", _search_tokens("the report"))
        # CJK is never touched by the stopword list.
        self.assertIn("线上销售", _search_tokens("线上销售"))

    def test_phrase_token_keeps_its_boost(self):
        exact = _score("quarterly report", "misc", "the quarterly report is attached")
        loose = _score("quarterly report", "misc", "quarterly and report separately")
        self.assertGreater(exact, loose)

    def test_idf_downweights_tokens_that_are_in_every_document(self):
        content = "common rare"
        no_idf = _score("common rare", "misc", content)
        weighted = _score("common rare", "misc", content, idf={"common": 0.7, "rare": 2.8})
        self.assertGreater(weighted, no_idf)          # rare term now pulls its weight
        # A rare token alone must beat a token present in every document.
        self.assertGreater(
            _score("rare", "misc", content, idf={"rare": 2.8}),
            _score("common", "misc", content, idf={"common": 0.7}),
        )

    def test_idf_absent_keeps_title_above_body(self):
        self.assertGreater(
            _score("channel", "Channel review", "misc"),
            _score("channel", "misc", "channel review"),
        )

    # ── term-frequency saturation (2026-09-26) ─────────────────────────────
    # Raw tf let a long document win by merely repeating a word. Saturation keeps
    # a single hit at full weight (sat(1) == 1.0) but stops the growth.
    def test_single_body_hit_keeps_its_documented_weight(self):
        self.assertEqual(_score("ann", "misc", "ann"), 8)          # 1 × 8

    def test_repeated_terms_are_saturated(self):
        once = _score("ann", "misc", "ann")
        ten = _score("ann", "misc", "ann " * 10)
        self.assertGreater(ten, once)          # still more, but far from 10×
        self.assertLess(ten, once * 3)

    def test_saturation_does_not_disturb_title_over_body(self):
        self.assertGreater(
            _score("ann", "ann report", "misc"),
            _score("ann", "misc", "ann " * 10),
        )
