"""File-sourced knowledge base tests.

The documents under ``api/knowledge_docs/`` are the deployed source of truth, so
these tests guard the properties the deployment relies on: every committed file
must be loadable, reconciliation must be idempotent, and nothing may be deleted
implicitly.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.ai import knowledge_docs
from services.ai.knowledge_store import KnowledgeDocument, KnowledgeStoreError

VALID = """---
document_id: klado-v2:demo
title: demo
document_type: rule
source: test
version: 1.2
tags: [Alpha,  beta , alpha]
extra_key: kept
---

Body line one.
Body line two.
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


class ParseTests(unittest.TestCase):
    def test_parses_identity_tags_and_extra_metadata(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        self.assertEqual(document.document_id, "klado-v2:demo")
        self.assertEqual(document.document_type, "rule")
        self.assertEqual(document.version, "1.2")
        self.assertEqual(document.metadata["tags"], ["Alpha", "beta", "alpha"])
        self.assertEqual(document.metadata["extra_key"], "kept")
        self.assertEqual(document.content, "Body line one.\nBody line two.")

    def test_body_keeps_internal_blank_lines(self):
        text = VALID.replace("Body line one.\nBody line two.", "A\n\nB")
        self.assertEqual(knowledge_docs.parse_document(text, source_name="demo.md").content, "A\n\nB")

    def test_missing_front_matter_is_named(self):
        with self.assertRaises(KnowledgeStoreError) as ctx:
            knowledge_docs.parse_document("# just a heading\n", source_name="broken.md")
        self.assertIn("broken.md", str(ctx.exception))
        self.assertIn("front matter", str(ctx.exception))

    def test_missing_required_key_is_reported(self):
        text = VALID.replace("version: 1.2\n", "")
        with self.assertRaises(KnowledgeStoreError) as ctx:
            knowledge_docs.parse_document(text, source_name="demo.md")
        self.assertIn("version", str(ctx.exception))

    def test_bad_document_type_is_rejected_by_the_store_contract(self):
        text = VALID.replace("document_type: rule", "document_type: nonsense")
        with self.assertRaises(KnowledgeStoreError) as ctx:
            knowledge_docs.parse_document(text, source_name="demo.md")
        self.assertIn("document_type", str(ctx.exception))

    def test_bad_version_is_rejected(self):
        text = VALID.replace("version: 1.2", "version: latest")
        with self.assertRaises(KnowledgeStoreError):
            knowledge_docs.parse_document(text, source_name="demo.md")

    def test_malformed_line_is_located(self):
        text = VALID.replace("source: test", "source test")
        with self.assertRaises(KnowledgeStoreError) as ctx:
            knowledge_docs.parse_document(text, source_name="demo.md")
        self.assertIn("demo.md:5", str(ctx.exception))


class LoadTests(unittest.TestCase):
    def test_committed_documents_all_load(self):
        documents = knowledge_docs.load_documents()
        # The corpus is now the retained set: the business documents (sales/channel/competitor
        # dictionaries) were removed with the modules that used them.
        self.assertGreaterEqual(len(documents), 15)
        ids = [document.document_id for document in documents]
        self.assertEqual(len(ids), len(set(ids)))
        # ⚠️ Every id carries `:zh`: the manual is Chinese-only since 2026-10-03 (the
        # canonical English-side files were deleted and retired). Asserting the bare id
        # here would be asserting a document that no longer exists.
        self.assertIn("klado-v2:format-report-html:zh", ids)
        self.assertFalse([i for i in ids if not i.endswith(":zh")],
                         "a canonical (English-side) document came back")
        for document in documents:
            self.assertTrue(document.content.strip(), document.document_id)
            self.assertTrue(document.metadata.get("tags"), document.document_id)

    def test_underscore_files_are_metadata_not_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.md", VALID)
            _write(root, "_README.md", "# not a document\n")
            _write(root, "_retired.txt", "old:doc\n")
            documents = knowledge_docs.load_documents(root)
            self.assertEqual([d.document_id for d in documents], ["klado-v2:demo"])

    def test_duplicate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.md", VALID)
            _write(root, "b.md", VALID.replace("title: demo", "title: demo-two"))
            with self.assertRaises(KnowledgeStoreError) as ctx:
                knowledge_docs.load_documents(root)
            self.assertIn("duplicate document_id", str(ctx.exception))

    def test_one_broken_file_fails_the_whole_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.md", VALID)
            _write(root, "b.md", "no front matter\n")
            with self.assertRaises(KnowledgeStoreError) as ctx:
                knowledge_docs.load_documents(root)
            self.assertIn("b.md", str(ctx.exception))

    def test_empty_directory_is_an_error_not_an_empty_knowledge_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KnowledgeStoreError):
                knowledge_docs.load_documents(Path(tmp))

    def test_missing_directory_is_an_error(self):
        with self.assertRaises(KnowledgeStoreError):
            knowledge_docs.load_documents(Path("/nonexistent/knowledge_docs"))


class RetiredListTests(unittest.TestCase):
    def test_comments_blanks_and_duplicates_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "_retired.txt", "# a comment\n\ngone:one\ngone:one   # twice\n  gone:two  \n")
            self.assertEqual(knowledge_docs.retired_document_ids(root), ["gone:one", "gone:two"])

    def test_absent_file_means_delete_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(knowledge_docs.retired_document_ids(Path(tmp)), [])


class ApplyTests(unittest.TestCase):
    def _apply(self, documents, stored, retired=()):
        upserted: list[KnowledgeDocument] = []
        deleted: list[str] = []
        summaries: list[dict] = []
        with (
            patch.object(knowledge_docs, "_ensure_schema"),
            patch.object(knowledge_docs, "_stored_hashes", return_value=stored),
            patch.object(knowledge_docs, "_upsert_document", side_effect=lambda d: upserted.append(d)),
            patch.object(knowledge_docs, "_delete_document", side_effect=lambda i: deleted.append(i) or True),
            patch.object(knowledge_docs, "retired_document_ids", return_value=list(retired)),
            patch.object(knowledge_docs, "default_docs_dir", return_value=Path(".")),
            patch.object(knowledge_docs, "save_apply_summary", side_effect=lambda s: summaries.append(s)),
        ):
            result = knowledge_docs.apply_knowledge_docs(documents)
        return result, upserted, deleted, summaries

    def test_identical_content_writes_nothing(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        stored = {document.document_id: knowledge_docs.document_hash(document)}
        result, upserted, _, _ = self._apply([document], stored)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(upserted, [])

    def test_changed_content_is_applied_with_a_stored_hash(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        result, upserted, _, _ = self._apply([document], {})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(len(upserted), 1)
        self.assertEqual(
            upserted[0].metadata["content_sha256"], knowledge_docs.document_hash(document)
        )
        self.assertEqual(upserted[0].document_id, "klado-v2:demo")

    def test_trailing_whitespace_does_not_count_as_a_change(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        padded = knowledge_docs.parse_document(
            VALID.replace("Body line two.\n", "Body line two.\n\n  \n"), source_name="demo.md"
        )
        stored = {document.document_id: knowledge_docs.document_hash(padded)}
        result, upserted, _, _ = self._apply([document], stored)
        self.assertEqual((result["applied"], result["skipped"]), (0, 1))
        self.assertEqual(upserted, [])

    # ── front-matter edits must deploy (2026-09-29) ─────────────────────────
    # The digest used to cover `content` only, so a tags/version-only edit
    # compared equal to what was stored and was SKIPPED: the database silently
    # kept the previous revision. Measured on the real deploy — 15 of 19 edited
    # documents never shipped (`applied: 4, skipped: 28`) while everything looked
    # green. These three tests are the guard.
    def test_a_tag_only_change_is_applied(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        retagged = knowledge_docs.parse_document(
            VALID.replace("tags: [Alpha,  beta , alpha]", "tags: [Alpha, beta, 图表]"),
            source_name="demo.md",
        )
        self.assertEqual(retagged.content, document.content)          # body identical
        result, upserted, _, _ = self._apply([retagged], {document.document_id: knowledge_docs.document_hash(document)})
        self.assertEqual((result["applied"], result["skipped"]), (1, 0))
        self.assertEqual(len(upserted), 1)

    def test_a_version_only_change_is_applied(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        bumped = knowledge_docs.parse_document(VALID.replace("version: 1.2", "version: 1.3"), source_name="demo.md")
        self.assertEqual(bumped.content, document.content)
        result, _, _, _ = self._apply([bumped], {document.document_id: knowledge_docs.document_hash(document)})
        self.assertEqual((result["applied"], result["skipped"]), (1, 0))

    def test_a_document_type_only_change_is_applied(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        retyped = knowledge_docs.parse_document(VALID.replace("document_type: rule", "document_type: format"), source_name="demo.md")
        result, _, _, _ = self._apply([retyped], {document.document_id: knowledge_docs.document_hash(document)})
        self.assertEqual((result["applied"], result["skipped"]), (1, 0))

    def test_stored_row_hashes_back_to_the_same_digest(self):
        """The invariant that keeps reconciliation idempotent.

        `document_hash` must agree with itself across the store round trip
        (normalised metadata, stripped values, an injected `content_sha256`).
        If it does not, every startup rewrites the whole knowledge base forever.
        """
        from services.ai.knowledge_store import _normalise_metadata

        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        digest = knowledge_docs.document_hash(document)
        as_stored = KnowledgeDocument(
            document_id=document.document_id,
            title=document.title.strip(),
            content=document.content.strip(),
            document_type=document.document_type.strip().lower(),
            source=document.source.strip(),
            version=document.version.strip(),
            metadata=_normalise_metadata({**document.metadata, "content_sha256": digest}),
        )
        self.assertEqual(knowledge_docs.document_hash(as_stored), digest)

    def test_retired_ids_are_deleted_explicitly(self):
        result, _, deleted, _ = self._apply([], {}, retired=["gone:one"])
        self.assertEqual(deleted, ["gone:one"])
        self.assertEqual(result["deleted"], ["gone:one"])

    def test_removing_a_file_does_not_delete_anything(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        stored = {document.document_id: "stale", "not-in-any-file:doc": "whatever"}
        result, _, deleted, _ = self._apply([document], stored)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(deleted, [])

    def test_pass_records_an_ok_summary(self):
        document = knowledge_docs.parse_document(VALID, source_name="demo.md")
        _, _, _, summaries = self._apply([document], {})
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "ok")
        self.assertEqual(summaries[0]["total"], 1)
        self.assertEqual(summaries[0]["applied"], 1)
        self.assertIn("at", summaries[0])


class StartupSummaryTests(unittest.TestCase):
    """A pass that never ran and a pass that failed must not look the same."""

    def test_disabled_is_recorded_not_silent(self):
        summaries: list[dict] = []
        with (
            patch.dict("os.environ", {"KNOWLEDGE_DOCS_AUTOAPPLY": "0"}),
            patch.object(knowledge_docs, "save_apply_summary", side_effect=lambda s: summaries.append(s)),
        ):
            self.assertIsNone(knowledge_docs.startup_apply())
        self.assertEqual(summaries[0]["status"], "disabled")

    def test_failure_is_recorded_and_reraised(self):
        summaries: list[dict] = []
        with (
            patch.object(knowledge_docs, "apply_knowledge_docs",
                         side_effect=__import__("services.ai.knowledge_store", fromlist=["x"]).KnowledgeStoreError("bad file")),
            patch.object(knowledge_docs, "save_apply_summary", side_effect=lambda s: summaries.append(s)),
        ):
            with self.assertRaises(KnowledgeStoreError):
                knowledge_docs.startup_apply()
        self.assertEqual(summaries[0]["status"], "failed")
        self.assertIn("bad file", summaries[0]["error"])


class DirectoryTests(unittest.TestCase):
    def test_default_directory_is_the_committed_one(self):
        resolved = knowledge_docs.default_docs_dir()
        self.assertTrue(resolved.is_dir(), resolved)
        self.assertEqual(resolved.name, "knowledge_docs")
        self.assertTrue((resolved / "_README.md").is_file())

    def test_autoapply_can_be_disabled(self):
        with patch.dict("os.environ", {"KNOWLEDGE_DOCS_AUTOAPPLY": "0"}):
            self.assertFalse(knowledge_docs.autoapply_enabled())
        with patch.dict("os.environ", {}, clear=False):
            self.assertTrue(knowledge_docs.autoapply_enabled())


if __name__ == "__main__":
    unittest.main()