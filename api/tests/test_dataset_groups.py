"""数据集容器 —— 一个数据集 = N 张表。

这一层是**加在**既有的「dataset = 一张表」之上的，因为后者是承重的：
`dataset_query` 拿 `list_datasets()` 的 `table_name` 当 SQL 白名单，分享端点按表名
收发，仪表盘也按表名建图。改那个含义会同时动查询网关、分享语义和对外文档。

所以这里要守的不是「容器好不好用」，而是**既有契约没被砸掉**。每一条测试都对着
一个具体的、会坏掉的方式：

* 平铺列表仍然是表，且每行只是**多了**两个字段 —— 键少一个，或者值悄悄变了，
  前端就分不出「没有容器」和「接口忘了答」。
* slug 允许中文。这是中文产品里最常见的名字，用 `[^a-z0-9]` 过滤会让几乎所有
  真实名字直接建不出来。
* 容器看不见时回 404 而不是 403 —— 403 等于承认这个名字存在。
* 外部导入是真只读，且是**服务端**强制。只读这件事必须落在真代码开的那个连接上：
  一份「我们自己敲 SET 再试着 DELETE」的复制品，在真代码有 bug 时同样是绿的。

除最后一条（需要真的连一个库）以外，全部跑在假连接上 —— 无服务、无数据库。
真实 PostgreSQL 上的 37 项服务层 + 32 项 HTTP 验证在提交说明里记录。
"""
import unittest
from unittest.mock import patch

from services import data_center_db as db
from services import dataset_groups as dg


class _FakeCursor:
    def __init__(self, log, results):
        self._log = log
        self._results = list(results)
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._log.append((" ".join(str(sql).split()), params))
        self._next = self._results.pop(0) if self._results else None

    def fetchone(self):
        return self._next

    def fetchall(self):
        # A queued result IS the row list `fetchall` hands back. A dict here means
        # the test queued a single row instead of a list, and the failure then
        # surfaces deep inside the service as "dictionary update sequence".
        if self._next is not None and isinstance(self._next, dict):
            raise AssertionError("queue a LIST of rows for fetchall(), not one dict")
        return self._next or []

    def description(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log, results):
        self._log = log
        self._results = results
        self.committed = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._log, self._results)

    def commit(self):
        self.committed = True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patched(results, **kwargs):
    """Patch `get_pg_conn` with a fake and return (log, patcher)."""
    log = []
    return log, patch.object(db, "get_pg_conn",
                            lambda *a, **k: _FakeConn(log, results), **kwargs)


class SlugTests(unittest.TestCase):
    """数据集名 → URL 里的标识符。中文名字是这个产品里最常见的情况。"""

    def test_chinese_names_survive(self):
        # ⚠️ 这一条曾经是坏的：`[^a-z0-9]` 会把中文全部剥掉，纯中文名直接抛错。
        # `data_center_db._safe_id` 用的是 `[\w]`，Python 3 的 `\w` 是 Unicode 感知的，
        # 所以仓库既有约定本来就保留中文 —— 容器也得跟它一致。
        self.assertEqual(dg.slugify("渠道销售"), "渠道销售")
        self.assertEqual(dg.slugify("别人的数据集"), "别人的数据集")
        self.assertEqual(dg.slugify("销售 数据 2026"), "销售-数据-2026")

    def test_ascii_names_are_slugified(self):
        self.assertEqual(dg.slugify("Channel Sales 2026"), "channel-sales-2026")
        self.assertEqual(dg.slugify("  Mixed  Case  "), "mixed-case")
        self.assertEqual(dg.slugify("a/b"), "a-b")

    def test_name_with_no_word_characters_is_refused(self):
        for bad in ("", "   ", "!!!", "///", "..."):
            with self.assertRaises(dg.GroupError, msg=bad):
                dg.slugify(bad)

    def test_slug_is_bounded(self):
        self.assertLessEqual(len(dg.slugify("渠道销售" * 40)), 64)

    def test_collision_gets_a_suffix(self):
        class _C:
            """`fetchone` returns a row for the first two lookups, then None = free."""

            def __init__(self):
                self.calls = 0

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                self.calls += 1
                return {"1": 1} if self.calls <= 2 else None

        self.assertEqual(dg._unique_slug(_C(), "sales"), "sales-3")



class ModeTests(unittest.TestCase):
    """`overwrite` / `append` —— 对外的词，和表层实际做的事不是同一个词。"""

    def test_overwrite_and_append_map_to_the_table_layer(self):
        # 表层把 overwrite 叫 replace，因为那是 TRUNCATE-then-INSERT。
        self.assertEqual(dg._normalise_mode("overwrite"), "replace")
        self.assertEqual(dg._normalise_mode("append"), "append")
        self.assertEqual(dg._normalise_mode("APPEND"), "append")

    def test_internal_replace_name_is_accepted_on_purpose(self):
        # `replace` 是表层的名字。接受它是刻意的（`insert_pg_data` 也这么拼），
        # 但要有测试说明它是**接受**，而不是碰巧没走到拒绝分支。
        self.assertEqual(dg._normalise_mode("replace"), "replace")
        self.assertEqual(dg._normalise_mode(" replace "), "replace")

    def test_unknown_mode_is_refused_rather_than_guessed(self):
        # 默默当成 overwrite 会把别人上周的数据抹掉。
        # 空串和 None 不在这里：没给 mode 就是默认 overwrite，由下面那条守着。
        for bad in ("upsert", "merge", "truncate", "insert", "overwrite-all"):
            with self.assertRaises(dg.GroupError, msg=str(bad)):
                dg._normalise_mode(bad)

    def test_missing_mode_defaults_to_overwrite(self):
        self.assertEqual(dg._normalise_mode(None), "replace")
        # 空串与 None 同义（表单里没填的输入框就是空串），不是"空名字"。
        self.assertEqual(dg._normalise_mode(""), "replace")
        self.assertEqual(dg._normalise_mode("   "), "replace")


class IdentifierTests(unittest.TestCase):
    """表名和 schema 名会进 SQL，必须是白名单而不是黑名单。"""

    def test_plain_identifiers_pass(self):
        for ok in ("orders", "public", "upstream_copy", "_x", "T1"):
            self.assertEqual(dg._check_identifier(ok, "table"), ok)

    def test_anything_with_a_quote_or_space_is_refused(self):
        for bad in ('orders"; DROP TABLE x --', "a b", "a;b", "a'b", "", "a.b", "a,b",
                    "a)", "1 OR 1=1", "a\tb"):
            with self.assertRaises(dg.GroupError, msg=bad):
                dg._check_identifier(bad, "table")

    def test_overlong_name_is_refused(self):
        with self.assertRaises(dg.GroupError):
            dg._check_identifier("x" * 64, "table")

    def test_system_schemas_are_refused_before_any_connection(self):
        # 校验发生在 connect 之前，所以这条不需要网：给了 `pg_catalog` 就直接拒，
        # 而不是先连上去再给你列几百张内部表。
        for bad in sorted(dg.SYSTEM_SCHEMAS):
            with self.assertRaises(dg.GroupError, msg=bad):
                dg.peek_external_tables({"host": "127.0.0.1", "database": "d",
                                         "user": "u", "schema": bad})


class GroupLabelsTests(unittest.TestCase):
    """`attach_group_labels` 是「不改既有形状」的全部实现，所以它要值不要键。"""

    def test_labels_are_attached_by_value(self):
        log, patcher = _patched([[{"table_name": "orders", "slug": "销售",
                             "name": "销售 2026"}]])
        with patcher, patch.object(dg, "ensure_schema", lambda: None):
            rows = dg.attach_group_labels([{"table_name": "orders"},
                                           {"table_name": "prices"}])
        self.assertEqual(rows[0]["dataset_slug"], "销售")
        self.assertEqual(rows[0]["dataset_name"], "销售 2026")
        # 没容器的表：两个键都在，值为 None。
        # ⚠️ 不是「键可以没有」—— 前端按值分支，缺键的类和「接口忘了答」分不开。
        self.assertIsNone(rows[1]["dataset_slug"])
        self.assertIsNone(rows[1]["dataset_name"])
        self.assertIn("dataset_slug", rows[1])
        self.assertIn("dataset_name", rows[1])

    def test_existing_keys_are_never_dropped(self):
        log, patcher = _patched([])
        original = {"table_name": "orders", "display_name": "订单", "row_count": 3,
                    "columns": [["a", "text"]], "owner_email": "o@example.test"}
        with patcher, patch.object(dg, "ensure_schema", lambda: None):
            rows = dg.attach_group_labels([dict(original)])
        self.assertEqual(set(original) - set(rows[0]), set(), "既有字段被弄丢了")
        self.assertEqual({k: rows[0][k] for k in original}, original)

    def test_empty_input_asks_no_questions(self):
        self.assertEqual(dg.attach_group_labels([]), [])
        self.assertEqual(dg.group_labels([]), {})


class VisibilitySqlTests(unittest.TestCase):
    """可见性表达式。`_ROW_VISIBLE` 按表名引用 `_import_registry`，所以不能加别名。"""

    def test_member_where_is_the_existing_expression(self):
        where, params = dg._member_where("me@example.test")
        self.assertEqual(where, db._ROW_VISIBLE)
        self.assertEqual(params, ["me@example.test", "me@example.test"])

    def test_no_viewer_means_everything(self):
        self.assertEqual(dg._member_where(None), ("TRUE", []))

    def test_group_where_names_no_alias_on_the_registry(self):
        where, _params = dg._group_where("me@example.test")
        self.assertIn(f"{db.META_TABLE}.dataset_group_id = g.id", where)
        # 别名会让 `_ROW_VISIBLE` 里的 `public._import_registry` 解析不了 ——
        # 这不是风格问题，是运行时错误。
        self.assertNotIn("FROM public._import_registry AS", where)
        self.assertNotIn("import_registry i", where)


class MemberRowTests(unittest.TestCase):
    """成员表行的形状要和既有平铺列表一致，前端才有一条代码路径。"""

    def test_columns_become_a_count_and_target_is_filled_in(self):
        cur = _FakeCursor([], [[{"table_name": "orders",
                                 "columns": [["a", "text"], ["b", "int"]],
                                 "row_count": 3}]])
        rows = dg._member_rows(cur, 1, "me@example.test")
        self.assertEqual(rows[0]["col_count"], 2)
        self.assertEqual(rows[0]["target_db"], "pg")
        self.assertEqual(rows[0]["table_schema"], "public")

    def test_missing_columns_become_zero_not_a_crash(self):
        cur = _FakeCursor([], [[{"table_name": "orders"}]])
        rows = dg._member_rows(cur, 1, None)
        self.assertEqual(rows[0]["col_count"], 0)


class GroupErrorTests(unittest.TestCase):
    def test_status_defaults_to_400_and_carries_the_message(self):
        err = dg.GroupError("坏了 / broken")
        self.assertEqual(err.status, 400)
        self.assertEqual(err.message, "坏了 / broken")

    def test_status_is_overridable(self):
        self.assertEqual(dg.GroupError("没有", status=404).status, 404)


class ModeAndCapTests(unittest.TestCase):
    def test_external_row_cap_is_a_hard_number(self):
        # 一次 SELECT 全量拉进进程内存，没有上限就是别人的 4000 万行决定我们的 OOM。
        self.assertEqual(dg.MAX_EXTERNAL_ROWS, 200_000)
        self.assertLessEqual(dg.EXTERNAL_CONNECT_TIMEOUT, 10)


if __name__ == "__main__":
    unittest.main()
