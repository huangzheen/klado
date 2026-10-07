"""End-to-end retrieval expectations for the deployed knowledge base.

Why this exists
---------------
`services/ai/knowledge_store.py` documents that its term-frequency constant
(`_TF_K1 = 0.8`) was *measured* against a "12-query expectation set" — but that
set was never checked in, so from 2026-09-26 to 2026-09-29 every change to the
weights, the tokeniser or a document's tags was unfalsifiable. This module is
that set, in the repository, running against the real corpus.

It runs the real ranking over the version-controlled files
(``api/knowledge_docs/*.md``) — no database, no network — via ``rank()``, which
was split out of ``search()`` for exactly this reason.

What it caught on the day it was written (2026-09-29)
----------------------------------------------------
* ``report-html`` and ``meeting-one-pager`` scored **0** against the whole
  corpus: ``_WORD_RE`` counts ``-`` as a word character, so ``format-report-html``
  is a single word and the prefix-stripped query was an exact-word miss.
* ``SPTO是什么意思`` never produced the token ``spto`` at all — the splitter
  cannot separate Latin from CJK because both are ``\\w``.
* A deliverable query ("做一份 16:9 deck") returned ``task-routing-playbook``
  first for 10 of 14 Chinese phrasings, because that document indexes every topic.
* ``format-html-slides`` — a retired spec whose markup the runtime no longer
  renders — was the #1 hit for its own name.

If an assertion here fails, decide whether the new ranking is **better** before
changing the expectation: the failure is the signal, not the noise.
"""
from __future__ import annotations

import pathlib
import unittest

from services.ai.knowledge_docs import load_documents, retired_document_ids
from services.ai.knowledge_store import _normalise_metadata, _search_tokens, rank

DOCS_DIR = pathlib.Path(__file__).resolve().parents[1] / "knowledge_docs"

# (query, expected top-1) pairs. Chinese phrasings are deliberately the ones a
# real user/agent types — including unsegmented ones, which is the hard case.
EXPECTATIONS: list[tuple[str, str]] = [
    # ── the report / deck path ────────────────────────────────────────────
    ("帮我做一份16:9分页deck报告", "klado-v2:format-report-html"),
    ("把这个分析发到 workspace", "klado-v2:format-report-html"),
    ("把这复盘发布到工作区", "klado-v2:format-report-html"),
    ("format-report-html", "klado-v2:format-report-html"),
    ("report-html", "klado-v2:format-report-html"),
    # ── interaction / export ──────────────────────────────────────────────
    ("要能右键标注、能改字的互动页", "klado-v2:format-interactive-report"),
    ("互动的报告状态存在哪", "klado-v2:format-interactive-report"),
    ("导出的ppt要能编辑", "klado-v2:format-pptx-export"),
    ("可编辑pptx 导出意图", "klado-v2:format-pptx-export"),
    # ── other deliverable shapes ──────────────────────────────────────────
    ("发给总经理的一页纸", "klado-v2:format-meeting-one-pager"),
    ("会议汇报一页纸的框架", "klado-v2:format-meeting-one-pager"),
    ("one-pager", "klado-v2:format-meeting-one-pager"),
    ("产品线排布图怎么做", "klado-v2:format-product-line-layout"),
    ("产品线 宽度段 价格段 生命周期", "klado-v2:format-product-line-layout"),
    # ── presentation-layer specs ──────────────────────────────────────────
    ("图表用什么颜色和字号", "klado-v2:format-chart-style"),
    ("这张数据该用什么图型", "klado-v2:format-chart-style"),
    ("图表规范", "klado-v2:format-chart-style"),
    ("销售数字用什么单位", "klado-v2:format-number-readability"),
    ("金额和百分比怎么进位", "klado-v2:format-number-readability"),
    ("回答模式有哪几种", "klado-v2:format-output-standards"),
    ("管理层deck的幻灯片架构", "klado-v2:format-output-standards"),
    ("给报告配一张封面图", "klado-v2:media-image-prompt"),
    ("报告封面配色怎么定", "klado-v2:media-image-prompt"),
    # The business-document rows (sales/channel/competitor dictionaries, the business-case
    # and performance playbooks, the poster spec) were removed with those documents: this
    # corpus is now the retained workspace/report/calendar specs only.
]

# The router is allowed to be *retrieved*; it must not be the answer.
ROUTER = "klado-v2:task-routing-playbook:zh"

# Every document a deliverable query may legitimately land on. Guards the data
# side of the fix: the intent bonus only fires for `document_type: format`.
DELIVERABLE_OWNERS = {
    "klado-v2:format-report-html:zh",
    "klado-v2:format-interactive-report:zh",
    "klado-v2:format-pptx-export:zh",
    "klado-v2:format-meeting-one-pager:zh",
    "klado-v2:format-product-line-layout:zh",
    "klado-v2:format-chart-style:zh",
    "klado-v2:format-number-readability:zh",
    "klado-v2:format-output-standards:zh",
    "klado-v2:media-image-prompt:zh",
}


class KnowledgeRetrievalTests(unittest.TestCase):
    # ── corpus guards ───────────────────────────────────────────────────────

    @classmethod
    def setUpClass(cls):
        cls.documents = load_documents(DOCS_DIR)
        cls.by_id = {document.document_id: document for document in cls.documents}
        # Every id in the corpus is `:zh` (the manual is Chinese-only since
        # 2026-10-03), while EXPECTATIONS is written in canonical form because that is
        # how a question is asked about a document. The collapse below is the same one
        # `_document_key` applies to a search result, so both sides of the comparison
        # speak the same language — before this, the existence check compared canonical
        # expectations against `:zh` ids and reported all 9 as missing.
        cls.canonical_ids = {cls._document_key(d) for d in cls.by_id}

    def test_expectations_reference_documents_that_exist(self):
        """A renamed or retired owner must fail here, not silently rank wrong."""
        missing = sorted({want for _, want in EXPECTATIONS} - self.canonical_ids)
        self.assertEqual(missing, [], f"expectation targets not in the corpus: {missing}")

    def test_retired_documents_are_gone_and_listed(self):
        retired = retired_document_ids(DOCS_DIR)
        self.assertIn("klado-v2:format-html-slides", retired)
        self.assertEqual(sorted(set(retired) & set(self.by_id)), [])

    def test_no_declared_tag_list_is_silently_truncated(self):
        """`_normalise_metadata` keeps only the first 20 tags.

        The 21st is dropped WITHOUT a word, so a file can read as if it carries a
        tag that the retriever never sees (format-report-html declared 23 before
        2026-09-29). Fail on the file, not on the runtime.
        """
        for document in self.documents:
            with self.subTest(document_id=document.document_id):
                declared = (document.metadata or {}).get("tags") or []
                self.assertLessEqual(len(declared), 20, f"{document.document_id} declares {len(declared)} tags")

    def test_deliverable_owners_are_declared_as_format(self):
        for document_id in sorted(DELIVERABLE_OWNERS):
            with self.subTest(document_id=document_id):
                self.assertEqual(self.by_id[document_id].document_type, "format")

    def test_router_is_marked_as_a_router(self):
        metadata = _normalise_metadata(self.by_id[ROUTER].metadata)
        self.assertEqual(str(metadata.get("retrieval_role", "")).lower(), "router")

    # ── ranking ─────────────────────────────────────────────────────────────

    @staticmethod
    def _document_key(document_id: str | None) -> str | None:
        """Collapse a `:zh` variant onto its canonical document.

        The `*.zh.md` variants live in the same corpus by design (Chinese
        questions may land on the Chinese variant), so "the right document"
        means the canonical id OR its `:zh` variant. Language detection in the
        scorer is deliberately out of scope.
        """
        if document_id and document_id.endswith(":zh"):
            return document_id[: -len(":zh")]
        return document_id

    def test_top_1_matches_the_expectation_set(self):
        failures = []
        for query, expected in EXPECTATIONS:
            results = rank(query, self.documents, top_k=3)
            actual = self._document_key(results[0].document_id if results else None)
            if actual != expected:
                failures.append(f"{query!r}\n    want {expected}\n    got  {[d.document_id for d in results]}")
        self.assertEqual(failures, [], "retrieval regressions:\n  " + "\n  ".join(failures))

    def test_router_is_never_the_top_answer(self):
        """It may appear in a result list; it must not be the answer people get."""
        for query, _ in EXPECTATIONS:
            results = rank(query, self.documents, top_k=3)
            with self.subTest(query=query):
                if results:
                    self.assertNotEqual(self._document_key(results[0].document_id), ROUTER)

    # ── the specific defects this work fixed ────────────────────────────────

    def test_hyphenated_document_ids_are_findable_by_their_tail(self):
        """`format-report-html` is one word, so `report-html` used to score 0."""
        for query in ("report-html", "meeting-one-pager", "interactive-report"):
            with self.subTest(query=query):
                self.assertTrue(rank(query, self.documents, top_k=1), f"{query!r} found nothing")

    def test_mixed_script_queries_keep_their_latin_term(self):
        """'SPTO是什么意思' must yield `spto`, not only CJK n-grams."""
        self.assertIn("spto", _search_tokens("SPTO是什么意思"))
        self.assertIn("ytd", _search_tokens("线上2026年YTD销量是多少"))
        self.assertIn("cd85", _search_tokens("帮我做CD85的Business Case"))
        # A one-character Latin run is noise (it substring-matches every digit),
        # so it must not become a token.
        self.assertNotIn("9", _search_tokens("16:9"))

    def test_empty_query_still_raises(self):
        with self.assertRaises(Exception):
            rank("   ", self.documents)


if __name__ == "__main__":
    unittest.main()