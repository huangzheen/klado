"""删一个数据集：全有或全无，而且「到底会删掉什么」由服务端说了算。

这一轮修的是三个真实的坏：

1. **确认框里的表数来自客户端。** 旧前端从 `_state.groups` 数，那是页面上次渲染时
   的快照 —— 别人刚往容器里加了一张表，框里仍写旧数字，用户点 OK 才发现少看到一张。
   ⇒ `delete_preview` 让表名、行数、有没有定时任务关联都出自服务端**这一次**的查询。

2. **一张表删不掉，整单照删。** 旧实现逐表独立连接，`except` 只记一行 warning 就
   继续，最后照样删容器 ⇒ 留下一张**没有注册行、没有卡片、谁都找不到**的孤儿物理表，
   数据还在盘上而系统里已经「删干净」了。PostgreSQL 的 DDL 本来就是事务化的，所以
   现在所有 DROP、注册行、授权行、容器行走同一个连接一次提交。

3. **定时任务会活过它的容器。** `external_db_sync_jobs.slug` 是 TEXT 且**没有外键**，
   删掉容器后它继续每个周期去拉一个不存在的表、继续失败，而界面上再也看不到它。
   ⇒ 删除本身**不碰**任务（那是另一个决定），只把它们回传给前端去问。

⚠️ 这些测试刻意**不**伪造事务语义：`get_pg_conn` 是真的 `_PooledConn`，只把
`_ensure_pool` 换掉，所以「异常时回滚而不是提交」这条断言验的是生产代码里真正
写下的那几行，而不是测试自己抄的一份复制品 —— 复制品在生产代码改坏时照样是绿的。

跑在假连接上：无服务、无数据库。
"""
import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from services import data_center_db as db
from services import dataset_groups as dg
from services import dataset_shares
from services import external_sync


def _sql_text(sql) -> str:
    """Whatever `execute` was handed, as a string a substring test can read.

    ⚠️ `DROP TABLE` goes through `psycopg2.sql.Composed`, and `str()` on one of those
    is its **repr** — `Composed([SQL('DROP TABLE IF EXISTS '), Identifier('public', 'x')])`.
    Not the SQL, but it does carry the table name, which is what the assertions below
    are about. Chasing the exact text would mean handing it a live connection.
    """
    return " ".join(str(sql).split())


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        text = _sql_text(sql)
        self._conn.log.append((text, params))
        if self._conn.fail_on and self._conn.fail_on in text:
            raise RuntimeError("boom: " + self._conn.fail_on)
        self.rowcount = self._conn.rowcount

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    """Just enough psycopg2 for `delete_group`, and honest about what it recorded."""

    def __init__(self, fail_on=None, rowcount=0):
        self.log = []
        self.fail_on = fail_on
        self.rowcount = rowcount
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def sql(self, *fragments):
        """Every statement as one string, for substring assertions."""
        return "\n".join(s for s, _ in self.log)


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.gets = 0
        self.puts = 0

    def getconn(self):
        self.gets += 1
        return self._conn

    def putconn(self, _conn):
        self.puts += 1


def _group(*tables, owner="owner@x"):
    return {"id": 7, "slug": "sales", "name": "Sales", "description": "",
            "owner_email": owner,
            "tables": [{"table_name": t, "row_count": 3} for t in tables]}


def _no_jobs():
    """`delete_preview` asks the scheduler which jobs are on this slug.

    \u26a0\ufe0f Every preview test has to answer that. CI runs `scripts/ci_check.py` with the
    database pointed at loopback port 1 \u2014 there is nothing to reach \u2014 so patching only
    `_require_group` and letting `_jobs_for_slug` fall through to a real connection
    gives two green tests on a laptop and two errors in CI. Removing this from a test
    turns it red again, which is how it earned its place.
    """
    return patch.object(external_sync, "list_jobs", lambda owner: [])


class _Base(unittest.TestCase):
    """One pooled connection, tracked. `self.conn` is what the service wrote on."""

    fail_on = None
    rowcount = 0

    def setUp(self):
        self.conn = _FakeConn(fail_on=self.fail_on, rowcount=self.rowcount)
        self.pool = _FakePool(self.conn)
        p_pool = patch.object(db, "_ensure_pool", lambda: self.pool)
        p_group = patch.object(dg, "_require_group",
                               lambda slug, v, a: _group(*self.tables))
        p_share = patch.object(dataset_shares, "revoke_all_for",
                               side_effect=AssertionError(
                                   "a second connection commits on its own — "
                                   "revoke_all_for_in_tx is the one that belongs here"))
        # `delete_group` ends by asking which schedules were on the slug. That is a real
        # query against another table, so it gets its own patch rather than an accident
        # of the fake connection; who it is asked about is `ScheduleLookupTests`.
        p_jobs = patch.object(external_sync, "list_jobs", lambda owner: [])
        self._patchers = (p_pool, p_group, p_share, p_jobs)
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

    tables = ("sales_2026", "sales_2026_q1")


class DeleteIsAllOrNothingTests(_Base):
    """One failing `DROP` must not leave the dataset half-gone."""

    tables = ("sales_2026", "sales_2026_q1")

    def test_a_failing_drop_raises_instead_of_being_logged_and_skipped(self):
        # ⚠️ The old code caught this and carried on, so the failure was invisible to
        # everyone: one log line, and a container deleted without its tables.
        fail = _FakeConn(fail_on="sales_2026_q1")
        pool = _FakePool(fail)
        with patch.object(db, "_ensure_pool", lambda: pool), \
             patch.object(dg, "_require_group", lambda s, v, a: _group(*self.tables)), \
             self.assertRaises(dg.GroupError) as caught:
            dg.delete_group("sales")
        self.assertEqual(caught.exception.status, 500)

    def test_the_container_row_is_never_deleted_when_a_drop_fails(self):
        # The container row is the LAST statement, so raising before it is what keeps
        # the card on screen. Asserted against the statement list, not the return value.
        fail = _FakeConn(fail_on="sales_2026_q1")
        with patch.object(db, "_ensure_pool", lambda: _FakePool(fail)), \
             patch.object(dg, "_require_group", lambda s, v, a: _group(*self.tables)), \
             self.assertRaises(dg.GroupError):
            dg.delete_group("sales")
        self.assertNotIn("dataset_groups", fail.sql())

    def test_the_whole_transaction_is_rolled_back_and_never_committed(self):
        # ⚠️ This is the assertion that makes "nothing was deleted" true rather than
        # merely likely. It runs the REAL `_PooledConn.__exit__` — only the pool is
        # faked — so a change from `rollback()` to `commit()` on the error path turns
        # it red instead of quietly shipping a half-deleted dataset.
        fail = _FakeConn(fail_on="sales_2026_q1")
        with patch.object(db, "_ensure_pool", lambda: _FakePool(fail)), \
             patch.object(dg, "_require_group", lambda s, v, a: _group(*self.tables)), \
             self.assertRaises(dg.GroupError):
            dg.delete_group("sales")
        self.assertTrue(fail.rolled_back, "an exception must roll the delete back")
        self.assertFalse(fail.committed, "a failed delete must not commit anything")

    def test_the_failure_names_the_table_and_says_nothing_went(self):
        # The user has to be able to tell "it refused" from "it half-did it". Both
        # languages, because that is what `pick()` splits on.
        fail = _FakeConn(fail_on="sales_2026_q1")
        with patch.object(db, "_ensure_pool", lambda: _FakePool(fail)), \
             patch.object(dg, "_require_group", lambda s, v, a: _group(*self.tables)), \
             self.assertRaises(dg.GroupError) as caught:
            dg.delete_group("sales")
        msg = caught.exception.message
        self.assertIn("sales_2026_q1", msg)
        self.assertIn("一个都没删", msg)
        self.assertIn("nothing was deleted at all", msg)


class DeleteHappyPathTests(_Base):
    """The positive control for every "nothing happened" assertion above.

    ⚠️ Without these, `test_the_container_row_is_never_deleted_when_a_drop_fails` would
    also pass against a `delete_group` that deletes nothing at all — which is exactly
    the failure mode a "0 of these statements ran" assertion has.
    """

    tables = ("sales_2026", "sales_2026_q1")

    def test_every_table_is_dropped_and_unregistered(self):
        res = dg.delete_group("sales")
        self.assertEqual(res["dropped_tables"], ["sales_2026", "sales_2026_q1"])
        text = self.conn.sql()
        for name in self.tables:
            self.assertIn(name, text, name)
        self.assertEqual(text.count("DELETE FROM public._import_registry"), 2)

    def test_the_container_row_is_deleted_and_the_transaction_commits(self):
        dg.delete_group("sales")
        self.assertIn("DELETE FROM public.dataset_groups", self.conn.sql())
        self.assertTrue(self.conn.committed)
        self.assertFalse(self.conn.rolled_back)

    def test_shares_are_revoked_on_the_same_connection(self):
        # One borrow, one commit. A second connection would commit on its own and leave
        # grants behind on tables that no longer exist — the hazard `dataset_shares`'
        # own docstring is about.
        dg.delete_group("sales")
        self.assertEqual(self.pool.gets, 1, "the delete must not open a second connection")
        self.assertIn("DELETE FROM public.dataset_shares", self.conn.sql())

    def test_it_does_not_delete_the_schedules(self):
        # Deleting the data and stopping the automation are two decisions. The service
        # returns the schedules; the frontend asks.
        res = dg.delete_group("sales")
        self.assertNotIn("external_db_sync_jobs", self.conn.sql())
        self.assertIn("schedules", res)


class PreviewTests(unittest.TestCase):
    """The confirm box has to be able to say what goes."""

    def test_it_reports_the_name_the_tables_and_their_rows(self):
        with patch.object(dg, "_require_group", lambda s, v, a: _group("a", "b")), \
             _no_jobs():
            pv = dg.delete_preview("sales", "owner@x")
        self.assertEqual(pv["slug"], "sales")
        self.assertEqual(pv["name"], "Sales")
        self.assertEqual([t["table_name"] for t in pv["tables"]], ["a", "b"])
        self.assertEqual([t["row_count"] for t in pv["tables"]], [3, 3])

    def test_a_missing_row_count_becomes_zero_not_a_none(self):
        # The dialog prints it. `None` there would render "None rows", and the guard is
        # that every value in that markup is printed, not that it is well-formed.
        g = _group("a")
        g["tables"][0]["row_count"] = None
        with patch.object(dg, "_require_group", lambda s, v, a: g), _no_jobs():
            pv = dg.delete_preview("sales", "owner@x")
        self.assertEqual(pv["tables"][0]["row_count"], 0)

    def test_it_refuses_what_the_caller_may_not_see(self):
        # Delegating to `_require_group` is the whole permission story (404 for a
        # stranger, 403 for a non-owner); this pins that the preview does not open a
        # side door by reading the registry itself.
        boom = dg.GroupError("nope / nope", status=403)
        with patch.object(dg, "_require_group", side_effect=boom):
            with self.assertRaises(dg.GroupError) as caught:
                dg.delete_preview("sales", "someone@x")
        self.assertEqual(caught.exception.status, 403)


class ScheduleLookupTests(unittest.TestCase):
    """Whose schedules these are — the container's owner's, not the caller's."""

    def test_it_reads_the_owners_jobs_not_the_callers(self):
        from services import external_sync
        seen = []

        def _list_jobs(owner):
            seen.append(owner)
            return [{"id": 1, "slug": "sales", "target_table": "a"},
                    {"id": 2, "slug": "other", "target_table": "b"}]

        g = _group("a", owner="owner@x")
        with patch.object(dg, "_require_group", lambda s, v, a: g), \
             patch.object(external_sync, "list_jobs", _list_jobs):
            pv = dg.delete_preview("sales", "admin@x")
        self.assertEqual(seen, ["owner@x"])
        self.assertEqual([j["id"] for j in pv["schedules"]], [1])

    def test_a_container_with_no_owner_asks_nobody(self):
        g = _group("a", owner="")
        with patch.object(dg, "_require_group", lambda s, v, a: g), _no_jobs():
            self.assertEqual(dg.delete_preview("sales", "owner@x")["schedules"], [])


class ScheduleCleanupTests(unittest.TestCase):
    """`DELETE /dataset-groups/{slug}/schedules` — the second half of a delete."""

    def test_it_refuses_while_the_dataset_is_still_there(self):
        # Otherwise it becomes "silently stop this live dataset's automation", which
        # looks like it worked and leaves the data going stale with nobody told.
        with patch.object(dg, "get_group", lambda s, v, *a, **k: _group("a")):
            with self.assertRaises(dg.GroupError) as caught:
                dg.delete_schedules_for_slug("sales", "owner@x")
        self.assertEqual(caught.exception.status, 409)

    def test_it_needs_a_signed_in_caller(self):
        with self.assertRaises(dg.GroupError) as caught:
            dg.delete_schedules_for_slug("sales", "")
        self.assertEqual(caught.exception.status, 401)

    def test_it_deletes_by_slug_and_owner_together(self):
        conn = _FakeConn(rowcount=2)
        with patch.object(db, "_ensure_pool", lambda: _FakePool(conn)), \
             patch.object(dg, "get_group", lambda s, v, *a, **k: None):
            res = dg.delete_schedules_for_slug("sales", "owner@x")
        text, params = conn.log[-1]
        self.assertIn("DELETE FROM public.external_db_sync_jobs", text)
        # ⚠️ Both keys, or a slug that was reused could take the new owner's jobs with it.
        self.assertEqual(params, ("sales", "owner@x"))
        self.assertEqual(res, {"slug": "sales", "deleted_schedules": 2})
        self.assertTrue(conn.committed)

    def test_an_admin_cleans_up_the_owners_schedules_not_their_own(self):
        # ⚠️ The confirm box lists the container OWNER's schedules (`_jobs_for_slug`),
        # so an admin deleting somebody else's dataset is shown those and then has
        # "delete them too" remove zero of them — a 200 with `deleted_schedules: 0`,
        # which is a green no-op and exactly the shape of bug this round removes.
        conn = _FakeConn(rowcount=1)
        with patch.object(db, "_ensure_pool", lambda: _FakePool(conn)), \
             patch.object(dg, "get_group", lambda s, v, *a, **k: None):
            res = dg.delete_schedules_for_slug("sales", "admin@x", admin=True)
        text, params = conn.log[-1]
        self.assertIn("DELETE FROM public.external_db_sync_jobs", text)
        self.assertNotIn("owner_email", text)
        # Bounded by the slug — only jobs on the container that was just deleted.
        self.assertEqual(params, ("sales",))
        self.assertEqual(res["deleted_schedules"], 1)


class RevokeInTxTests(unittest.TestCase):
    """`dataset_shares.revoke_all_for_in_tx` — the caller's cursor, no commit."""

    def test_it_reports_how_many_grants_went_and_uses_one_statement(self):
        cur = _FakeCursor(_FakeConn(rowcount=3))
        self.assertEqual(dataset_shares.revoke_all_for_in_tx(cur, ["a", "b"]), 3)
        text, params = cur._conn.log[0]
        self.assertIn("DELETE FROM public.dataset_shares", text)
        self.assertEqual(params, (["a", "b"],))

    def test_an_empty_list_touches_nothing(self):
        # `= ANY('{}')` is valid SQL that matches nothing, so this is about not sending
        # a pointless statement — and about the return value staying an int.
        for empty in ([], None, (), ["", "  "]):
            cur = _FakeCursor(_FakeConn())
            self.assertEqual(dataset_shares.revoke_all_for_in_tx(cur, empty), 0)
            self.assertEqual(cur._conn.log, [], repr(empty))


class RouterWiringTests(unittest.TestCase):
    """The endpoints the confirm box talks to have to actually be routed.

    ⚠️ Parsed with `ast`, not by grepping the source. A regex over a file this size
    matches inside comments and inside the docstring that talks about the very route
    being looked for, and then reports "the endpoint exists" for a line of prose.
    """
    SRC = Path(__file__).resolve().parents[1] / "routers" / "data_center.py"

    def _routes(self):
        tree = ast.parse(self.SRC.read_text(encoding="utf-8"))
        out = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                if call is None or not call.args:
                    continue
                func = call.func
                name = getattr(func, "attr", None)
                if not name or not name.startswith(("get", "post", "put", "delete")):
                    continue
                path = getattr(call.args[0], "value", None)
                if isinstance(path, str):
                    out[(name.upper(), path)] = node
        return out

    def test_the_preview_endpoint_exists_and_calls_the_preview(self):
        node = self._routes().get(("GET", "/dataset-groups/{slug}/delete-preview"))
        self.assertIsNotNone(node, "the confirm box fetches this before anything is deleted")
        called = {getattr(n.func, "attr", "") for n in ast.walk(node)
                  if isinstance(n, ast.Call)}
        self.assertIn("delete_preview", called)

    def test_the_schedule_cleanup_endpoint_exists_and_is_owner_scoped(self):
        node = self._routes().get(("DELETE", "/dataset-groups/{slug}/schedules"))
        self.assertIsNotNone(node, "the second dialog asks, then deletes through this")
        called = {getattr(n.func, "attr", "") for n in ast.walk(node)
                  if isinstance(n, ast.Call)}
        self.assertIn("delete_schedules_for_slug", called)

    def test_the_cleanup_endpoint_forwards_admin(self):
        # Without this the endpoint above is owner-filtered forever and the admin path
        # is a green no-op. `admin` has to arrive from `_viewer`, not be defaulted.
        node = self._routes().get(("DELETE", "/dataset-groups/{slug}/schedules"))
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "delete_schedules_for_slug"]
        self.assertEqual(len(calls), 1)
        self.assertIn("admin", [k.arg for k in calls[0].keywords])

    def test_the_delete_endpoint_still_routes_to_delete_group(self):
        # ⚠️ The positive control for the two above: if this one also went missing the
        # file would simply have no dataset-group routes and all three would "pass" on
        # the strength of a lookup returning something for a different key.
        node = self._routes().get(("DELETE", "/dataset-groups/{slug}"))
        self.assertIsNotNone(node)
        called = {getattr(n.func, "attr", "") for n in ast.walk(node)
                  if isinstance(n, ast.Call)}
        self.assertIn("delete_group", called)


if __name__ == "__main__":
    unittest.main()