"""The agent skill package must not drift from the documents it points at.

Two drift modes, both observed on 2026-09-30 (this test exists because of them):

1. **The version table goes stale.** `SKILL.md` records the version of every document an
   agent must re-fetch, and that table is the ONLY thing that tells an agent "your copy is
   old, fetch the live one". It said `format-calendar-event 1.7` while the document was
   already 1.8 — so an agent that trusted the package would have written the OLD layout
   (filled cards, description filling half the column) onto every new event page.
2. **The zip is not rebuilt.** `agent.zip` is what users download; `agent_skill/` is the
   source. Editing the source and forgetting `scripts/build_agent_skill.sh` ships the old
   text under a new commit — and the package's own `SKILL_PACKAGE_VERSION` stamp is the
   thing an operator uses to confirm a refresh actually landed.
"""
import re
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "api" / "knowledge_docs"
SKILL = ROOT / "agent_skill" / "klado" / "SKILL.md"
ZIP = ROOT / "frontend" / "out" / "agent.zip"

# `| `klado-v2:format-calendar-event` | **1.9** | … |`
# ⚠️ The `(?::zh)?` is not decoration. Every document id in the manual is Chinese-only
# since 2026-10-03, so every id in this table now ends in `:zh` — and a pattern that
# accepts one colon silently matches **nothing**. `assertTrue(recorded, "no version rows
# parsed")` would have been the only thing standing between that and a suite that checks
# a table it never read.
ROW = re.compile(r"^\s*\|\s*`([a-z0-9-]+:[a-z0-9-]+(?::zh)?)`\s*\|\s*\*\*([0-9][^*]*)\*\*\s*\|", re.M)
FRONT = re.compile(r"^version:\s*(\S+)\s*$", re.M)
FRONT_ID = re.compile(r"^document_id:\s*(\S+)\s*$", re.M)


def committed_versions():
    out = {}
    for path in sorted(DOCS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue                      # `_README.md` and friends are not documents
        head, _, _ = text[3:].partition("\n---")
        found_id = FRONT_ID.search(head)
        found_version = FRONT.search(head)
        if found_id and found_version:
            out[found_id.group(1)] = found_version.group(1)
    return out


class _SourceTreeTest(unittest.TestCase):
    """These assertions are about the REPOSITORY, not about the running app.

    ⚠️ The paths below — `agent_skill/` and `frontend/out/agent.zip` — are repository
    paths. A packaged build that ships only `api/` would not have them, so fail loudly
    with a reason instead of an ImportError-looking path error: the check belongs to the
    source tree.
    """

    @classmethod
    def setUpClass(cls):
        missing = [str(p) for p in (SKILL, ZIP, DOCS_DIR) if not p.exists()]
        if missing:
            raise unittest.SkipTest(
                "source-tree test: not found in this layout — %s" % ", ".join(missing))


class SkillVersionTableTests(_SourceTreeTest):
    def test_the_skill_is_where_this_test_thinks_it_is(self):
        # A guard against the test silently passing because the path moved.
        self.assertTrue(SKILL.is_file(), SKILL)
        self.assertTrue(ZIP.is_file(), ZIP)

    def test_every_recorded_version_matches_the_committed_document(self):
        recorded = dict(ROW.findall(SKILL.read_text(encoding="utf-8")))
        self.assertTrue(recorded, "no version rows parsed — did the table format change?")
        live = committed_versions()
        stale = {doc_id: (version, live.get(doc_id))
                 for doc_id, version in recorded.items()
                 if live.get(doc_id) != version}
        self.assertEqual(stale, {}, "SKILL.md records a version the document no longer has "
                                    "(bump the table AND the SKILL_PACKAGE_VERSION stamp)")

    def test_a_recorded_document_actually_exists(self):
        # A typo in the doc id would make an agent re-fetch nothing and see no error.
        recorded = dict(ROW.findall(SKILL.read_text(encoding="utf-8")))
        missing = sorted(set(recorded) - set(committed_versions()))
        self.assertEqual(missing, [], "SKILL.md points at documents that are not committed")


class PackagedZipTests(_SourceTreeTest):
    def test_all_files_and_licence_match_sources_without_stale_entries(self):
        source = ROOT / 'agent_skill'
        expected = {p.relative_to(source).as_posix(): p.read_bytes()
                    for p in (source / 'klado').rglob('*') if p.is_file()
                    and not any(part.startswith('.') or part == '__pycache__' for part in p.parts)}
        expected['klado/LICENSE'] = (ROOT / 'LICENSE').read_bytes()
        self.assertIn('klado/NOTICE', expected)
        with zipfile.ZipFile(ZIP) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), set(expected))
            self.assertEqual(len(archive.namelist()), len(expected))
            for name, data in expected.items():
                self.assertEqual(archive.read(name), data, name)

    def _packaged(self, name="klado/SKILL.md"):
        with zipfile.ZipFile(ZIP) as archive:
            return archive.read(name).decode("utf-8")

    def test_the_zip_contains_the_current_skill_source(self):
        # Byte-for-byte: `agent_skill/klado/SKILL.md` is what the build script zips, so
        # any difference means the package was built from an older revision.
        self.assertEqual(self._packaged(), SKILL.read_text(encoding="utf-8"),
                         "agent.zip is stale — run `bash scripts/build_agent_skill.sh`")

    def test_the_package_stamp_is_present_and_readable(self):
        packaged = self._packaged()
        stamp = re.search(r"^SKILL_PACKAGE_VERSION:\s*(\S+)\s*$", packaged, re.M)
        self.assertIsNotNone(stamp, "the package carries no SKILL_PACKAGE_VERSION stamp, so "
                                    "an operator cannot tell which generation they downloaded")

    def test_the_zip_carries_the_same_version_table(self):
        # Belt and braces for the case where SKILL.md is edited but nothing else changes.
        recorded = dict(ROW.findall(self._packaged()))
        self.assertEqual(recorded, dict(ROW.findall(SKILL.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main()
