"""
`POST /api/data-center/query` — the statement-shape guard.

The guard's job is a readable error, not safety: safety is `SET TRANSACTION READ ONLY`
inside `services.data_center_db.query_pg()`. Keeping the two roles separate is what allows
`WITH … SELECT` (2026-09-27) without opening a write path — this test pins the text half
(the first keyword, skipping comments), and `verify_sql_endpoint`-style checks pin the
transaction half against a real database.

Why comments matter: `-- 口径说明\nSELECT …` is valid SQL that a `startswith("SELECT")`
test rejects without ever reading the statement, which is exactly what agents hit.
"""
import unittest

from routers import data_center


class LeadingKeywordTests(unittest.TestCase):
    def test_plain_statements(self):
        self.assertEqual(data_center._leading_keyword("SELECT 1"), "SELECT")
        self.assertEqual(data_center._leading_keyword("  select 1"), "SELECT")
        self.assertEqual(data_center._leading_keyword("WITH a AS (SELECT 1) SELECT * FROM a"), "WITH")
        self.assertEqual(data_center._leading_keyword("insert into t values (1)"), "INSERT")

    def test_leading_line_comment_is_skipped(self):
        self.assertEqual(data_center._leading_keyword("-- 本月口径\nSELECT 1"), "SELECT")

    def test_leading_block_comment_is_skipped(self):
        self.assertEqual(data_center._leading_keyword("/* note */\n  SELECT 1"), "SELECT")

    def test_several_comments_in_a_row_are_skipped(self):
        self.assertEqual(
            data_center._leading_keyword("-- one\n/* two */\n-- three\nWITH x AS (SELECT 1) SELECT 1"),
            "WITH")

    def test_empty_and_comment_only_input(self):
        self.assertEqual(data_center._leading_keyword(""), "")
        self.assertEqual(data_center._leading_keyword("   \n"), "")
        self.assertEqual(data_center._leading_keyword("-- only a comment"), "")

    def test_with_prefix_cannot_smuggle_a_write_as_the_first_word(self):
        # `WITH x AS (DELETE …)` is allowed by this text guard on purpose — the READ ONLY
        # transaction refuses it. What must never pass is a *write* keyword in first place.
        self.assertEqual(data_center._leading_keyword("DELETE FROM t"), "DELETE")
        self.assertEqual(data_center._leading_keyword("DROP TABLE t"), "DROP")


if __name__ == "__main__":
    unittest.main()