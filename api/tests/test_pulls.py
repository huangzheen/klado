"""Pulling: making a copy that outlives the person who shared it.

Sharing hands over access — one grant row, dead the moment the owner closes their
account. Pulling hands over the data: a new table, a new object, a new page, owned by
the puller. These tests are about the ways that promise breaks quietly:

* a copy that reuses the source's name is not a copy. `CREATE TABLE new AS SELECT * FROM
  old` under the old name is the same table, and dropping the owner takes the copy with
  it. `_free_name` must prove a name is unused *before* creating it.
* the "is this name free?" check must look at the DATABASE, not the registry. A table
  dropped outside `delete_pg_table`, or an import that crashed after CREATE and before
  registering, leaves a name the registry calls free.
* `_object_exists` must return what storage said. Dropping the return value makes every
  key read as taken, so no pull can ever succeed — from a helper that reads like a
  three-line guard.
* a pulled dashboard must point at ITS OWN datasets. Copying the row while leaving the
  names alone yields a page that looks private, is not, and dies with the original.
* a dataset a puller may not touch must not be silently pulled.

Fake connections throughout — no server, no database.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import pulls


# ── fake connections ──────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, results):
        self._results = results
        self.rowcount = 0
        self._next = None

    def execute(self, sql, params=None):
        # `sql` arrives as a psycopg2 Composed object for identifier-safe DDL and as
        # a plain string for everything else; `as_string` needs no live connection for
        # Composed, so normalise for matching.
        try:
            self._next = self._results.pop(0) if self._results else None
        except TypeError:                       # a Composed object is not a container
            self._next = self._results.pop(0) if self._results else None
        self.rowcount = 1

    def fetchone(self):
        if isinstance(self._next, list):
            return self._next[0] if self._next else None
        return self._next

    def fetchall(self):
        if isinstance(self._next, list):
            return self._next
        return [] if self._next is None else [self._next]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, results):
        self._results = list(results)
        self._cursor = None

    def cursor(self, cursor_factory=None):
        if self._cursor is None:
            self._cursor = _FakeCursor(self._results)
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


DATASET = {
    "table_name": "sales_q3", "display_name": "Q3 sales", "fingerprint": "fp1",
    "columns": [["region", "text"], ["amount", "numeric"]],
    "modules": ["core"], "sheet_name": "Sheet1", "row_count": 2,
}


# ── the name a copy gets ──────────────────────────────────────────────────────

class FreeNameTests(unittest.TestCase):
    def test_a_free_name_is_used_as_is(self):
        with patch.object(pulls, "_table_exists", return_value=False):
            self.assertEqual(pulls._free_name("sales_q3"), "sales_q3_copy")

    def test_a_taken_name_gets_the_next_number(self):
        with patch.object(pulls, "_table_exists", side_effect=lambda n: n != "sales_q3_copy2"):
            self.assertEqual(pulls._free_name("sales_q3"), "sales_q3_copy2")

    def test_it_checks_the_database_not_the_registry(self):
        # A table created outside the registry (crashed import, manual DROP bypass)
        # occupies its name even though nothing in `_import_registry` mentions it.
        seen = []

        def fake_exists(name):
            seen.append(name)
            return name == "ghost_table_copy"

        with patch.object(pulls, "_table_exists", side_effect=fake_exists):
            name = pulls._free_name("ghost_table")
        self.assertEqual(name, "ghost_table_copy2")
        self.assertIn("ghost_table_copy", seen)

    def test_a_long_name_is_trimmed_to_leave_room(self):
        with patch.object(pulls, "_table_exists", return_value=False):
            name = pulls._free_name("x" * 120)
        self.assertLessEqual(len(name), 63, "PostgreSQL truncates identifiers at 63 bytes")

    def test_a_name_space_that_is_exhausted_raises_rather_than_looping(self):
        with patch.object(pulls, "_table_exists", return_value=True):
            with self.assertRaises(pulls.PullError):
                pulls._free_name("sales_q3")

    def test_a_name_a_database_would_reject_is_refused_before_anything_is_created(self):
        # A name starting with a digit cannot be a bare identifier; `_safe_name`
        # lowercases and replaces punctuation, and what comes out must still be legal.
        self.assertIsNone(pulls._safe_name("9lives"))


# ── may this caller pull it ───────────────────────────────────────────────────

class PermissionTests(unittest.TestCase):
    def _row(self, **over):
        return dict(DATASET, **over)

    def test_the_owner_may_pull_their_own_dataset(self):
        with patch.object(pulls.db, "get_dataset", return_value=self._row()):
            self.assertEqual(pulls._assert_can_pull("sales_q3", "me@x.com")["table_name"], "sales_q3")

    def test_a_grantee_may_pull_it(self):
        with patch.object(pulls.db, "get_dataset", side_effect=[None, self._row()]), \
             patch.object(pulls.shares, "shared_with_me", return_value=["sales_q3"]):
            row = pulls._assert_can_pull("sales_q3", "them@x.com")
        self.assertEqual(row["table_name"], "sales_q3")

    def test_a_stranger_is_refused(self):
        with patch.object(pulls.db, "get_dataset", return_value=None), \
             patch.object(pulls.shares, "shared_with_me", return_value=[]):
            with self.assertRaises(pulls.PullError) as ctx:
                pulls._assert_can_pull("sales_q3", "stranger@x.com")
        self.assertIn("/", str(ctx.exception), "the message must stay bilingual")

    def test_an_illegal_name_is_refused_before_any_database_work(self):
        with self.assertRaises(pulls.PullError):
            pulls._assert_can_pull("9lives", "me@x.com")


# ── the copy itself ───────────────────────────────────────────────────────────

class PullDatasetTests(unittest.TestCase):
    def _run(self, results, email="me@x.com", name="sales_q3", dataset=None):
        """`pull_dataset` issues: CREATE TABLE…AS SELECT, then SELECT count(*), then
        an UPDATE of the registry row count — three statements on one connection."""
        conn = _FakeConn(results)
        source = dataset or DATASET
        made: dict[str, dict] = {}

        def get_dataset(table_name, viewer_email=None, admin=False):
            """The source row is readable; the copy only exists after it is created.
            A stub that ignores its argument would hand the source row back for the
            copy too, and the "the copy has its own name" assertion would pass on the
            very row it is meant to distinguish from."""
            return made.get(table_name, source if table_name == source["table_name"] else None)

        def create_pg_table(table_name, *a, **kw):
            made[table_name] = dict(source, table_name=table_name,
                                    owner_email=kw.get("owner_email", ""))

        with patch.object(pulls.db, "get_pg_conn", return_value=conn), \
             patch.object(pulls.db, "get_dataset", side_effect=get_dataset), \
             patch.object(pulls, "_table_exists", return_value=False), \
             patch.object(pulls.db, "create_pg_table", side_effect=create_pg_table) as create:
            out = pulls.pull_dataset(name, email)
        return out, create

    def test_the_copy_never_reuses_the_source_name(self):
        # The single most important property here: a copy under the original's name is
        # the original, and dies with it.
        out, _create = self._run([None, (2,), None])
        self.assertNotEqual(out["table_name"], DATASET["table_name"])
        self.assertEqual(out["pulled_from"], DATASET["table_name"])

    def test_the_copy_is_stamped_with_the_puller_as_owner(self):
        _out, create = self._run([None, (2,), None], email="taker@x.com")
        _args, kwargs = create.call_args
        self.assertEqual(kwargs["owner_email"], "taker@x.com")

    def test_the_provenance_is_honest(self):
        # The copy came from another account's dataset, never from an upload of theirs.
        _out, create = self._run([None, (2,), None])
        _args, kwargs = create.call_args
        self.assertTrue(kwargs["source_file"].startswith("pulled:"))

    def test_columns_that_arrived_decoded_are_not_parsed_twice(self):
        # `columns` is JSONB, so psycopg2 hands back a list. A second `json.loads`
        # raises TypeError and the pull fails for a dataset that is perfectly fine.
        # `create_pg_table(name, columns, fingerprint, display, …)` — the first four
        # are positional.
        _out, create = self._run([None, (2,), None])
        self.assertEqual(create.call_args[0][1], DATASET["columns"])

    def test_columns_that_arrived_as_text_are_parsed(self):
        row = dict(DATASET, columns=json.dumps(DATASET["columns"]))
        _out, create = self._run([None, (2,), None], dataset=row)
        self.assertEqual(create.call_args[0][1], DATASET["columns"])

    def test_modules_that_arrived_as_text_become_a_list(self):
        # `modules` is a real Postgres ARRAY. Normalising it as JSON writes the string
        # "['core']" into an array column, and the registry then names a module that
        # does not exist.
        row = dict(DATASET, modules="['core']")
        _out, create = self._run([None, (2,), None], dataset=row)
        _args, kwargs = create.call_args
        self.assertEqual(kwargs["modules"], ["core"])

    def test_the_row_count_is_recorded(self):
        # Two connections in `pull_dataset`: one for CREATE + count, one for the
        # registry UPDATE. The count is what the card will show, so it has to be the
        # number of rows the COPY holds, not the source's.
        conn_a = _FakeConn([None, (2,), None])
        conn_b = _FakeConn([])
        conns = [conn_a, conn_b]
        made: dict[str, dict] = {}

        def get_dataset(table_name, viewer_email=None, admin=False):
            return made.get(table_name, DATASET if table_name == DATASET["table_name"] else None)

        def create_pg_table(table_name, *a, **kw):
            made[table_name] = dict(DATASET, table_name=table_name)

        with patch.object(pulls.db, "get_pg_conn", side_effect=lambda: conns.pop(0)), \
             patch.object(pulls.db, "get_dataset", side_effect=get_dataset), \
             patch.object(pulls, "_table_exists", return_value=False), \
             patch.object(pulls.db, "create_pg_table", side_effect=create_pg_table):
            out = pulls.pull_dataset("sales_q3", "me@x.com")
        self.assertEqual(out["rows"], 2)


# ── objects ───────────────────────────────────────────────────────────────────

class ObjectKeyTests(unittest.TestCase):
    def test_a_copy_is_never_stored_under_its_own_source_key(self):
        # Owner-snapshot case: `{owner}/{filename}` IS the source key, so it always
        # reads as taken and the search runs out before it finds anything.
        with patch.object(pulls, "_object_exists", return_value=False):
            key = pulls._free_object_key("me@x.com", "me@x.com/report.csv", "report.csv")
        self.assertNotEqual(key, "me@x.com/report.csv")
        self.assertEqual(key, "me@x.com/report_copy.csv")

    def test_the_extension_is_kept_outside_the_suffix(self):
        with patch.object(pulls, "_object_exists", side_effect=lambda k: k != "me@x.com/report_copy2.csv"):
            key = pulls._free_object_key("me@x.com", "me@x.com/report.csv", "report.csv")
        self.assertEqual(key, "me@x.com/report_copy2.csv")

    def test_a_nested_source_filename_does_not_produce_a_nested_copy_key(self):
        with patch.object(pulls, "_object_exists", return_value=False):
            key = pulls._free_object_key("me@x.com", "a/b/c/report.csv", "a/b/c/report.csv")
        self.assertEqual(key, "me@x.com/report_copy.csv")

    def test_a_file_without_an_extension_still_gets_one(self):
        with patch.object(pulls, "_object_exists", return_value=False):
            key = pulls._free_object_key("me@x.com", "me@x.com/README", "README")
        self.assertEqual(key, "me@x.com/README_copy")

    def test_the_existence_check_passes_storage_s_answer_through(self):
        # Dropping the return value makes every key read as taken, so no file pull can
        # ever succeed — and the helper still looks like a correct guard.
        from services import oss_storage
        with patch.object(oss_storage, "object_exists", return_value=False):
            self.assertFalse(pulls._object_exists("me@x.com/x.csv"))
        with patch.object(oss_storage, "object_exists", return_value=True):
            self.assertTrue(pulls._object_exists("me@x.com/x.csv"))

    def test_a_storage_error_is_not_read_as_a_free_name(self):
        # Treating an unreachable bucket as "the name is free" would let a copy
        # overwrite somebody's object.
        from services import oss_storage
        with patch.object(oss_storage, "object_exists", side_effect=OSError("bucket down")):
            with self.assertRaises(pulls.PullError):
                pulls._object_exists("me@x.com/x.csv")


# ── a page, and the data it reads ─────────────────────────────────────────────

class PullDashboardTests(unittest.TestCase):
    def _store(self, *, datasets='["sales_q3"]', slug="their-page"):
        row = {"slug": slug, "title": "Sales", "html": "<html></html>",
               "summary": "", "datasets": datasets, "visibility": "public",
               "owner_email": "them@x.com"}
        return SimpleNamespace(
            get_dashboard=lambda s, email="", include_html=False: (
                row if s == slug else None),
            may_read=lambda r, e: True,
            datasets_of=lambda r: json.loads(r.get("datasets") or "[]"),
            normalise_slug=lambda raw, fallback_title="": str(raw).strip().lower(),
            save_dashboard=lambda fields, email, s: fields,
            DashboardError=RuntimeError,
        )

    def test_a_dashboard_the_caller_cannot_read_is_refused(self):
        store = self._store()
        store.may_read = lambda r, e: False
        with patch.object(pulls, "store", store):
            with self.assertRaises(pulls.PullError):
                pulls.pull_dashboard("their-page", "stranger@x.com")

    def test_each_dataset_is_pulled_and_the_page_is_rewritten(self):
        store = self._store(datasets='["sales_q3","other"]')
        store.datasets_of = lambda r: ["sales_q3", "other"]
        pulled = []

        def fake_pull(name, email):
            pulled.append(name)
            return {"table_name": f"{name}_copy"}

        saved = {}
        store.save_dashboard = lambda fields, email, s: saved.update(fields) or fields
        with patch.object(pulls, "store", store), \
             patch.object(pulls, "pull_dataset", side_effect=fake_pull):
            out = pulls.pull_dashboard("their-page", "me@x.com")
        self.assertEqual(pulled, ["sales_q3", "other"])
        self.assertEqual(saved["datasets"], ["sales_q3_copy", "other_copy"],
                         "the copy must point at ITS OWN datasets")
        self.assertEqual(out["missing_datasets"], [])

    def test_the_copy_is_private(self):
        # A pulled page that kept `public` would be a second copy of somebody's
        # public page on an account they cannot remove it from.
        store = self._store()
        saved = {}
        store.save_dashboard = lambda fields, email, s: saved.update(fields) or fields
        with patch.object(pulls, "store", store), \
             patch.object(pulls, "pull_dataset", side_effect=lambda n, e: {"table_name": n + "_copy"}):
            pulls.pull_dashboard("their-page", "me@x.com")
        self.assertEqual(saved["visibility"], "private")

    def test_a_dataset_that_cannot_be_pulled_is_reported_not_hidden(self):
        # Leaving the name pointing at the original keeps the page working today, but
        # it must be visible in the response — otherwise the reader is told a copy is
        # self-contained when it is not.
        store = self._store()
        store.datasets_of = lambda r: ["sales_q3"]
        saved = {}
        store.save_dashboard = lambda fields, email, s: saved.update(fields) or fields
        with patch.object(pulls, "store", store), \
             patch.object(pulls, "pull_dataset", side_effect=pulls.PullError("revoked")):
            out = pulls.pull_dashboard("their-page", "me@x.com")
        self.assertEqual(out["missing_datasets"], ["sales_q3"])
        self.assertEqual(saved["datasets"], ["sales_q3"])

    def test_one_failing_dataset_does_not_lose_the_others(self):
        store = self._store()
        store.datasets_of = lambda r: ["ok_one", "bad", "ok_two"]

        def fake_pull(name, email):
            if name == "bad":
                raise pulls.PullError("nope")
            return {"table_name": name + "_copy"}

        saved = {}
        store.save_dashboard = lambda fields, email, s: saved.update(fields) or fields
        with patch.object(pulls, "store", store), \
             patch.object(pulls, "pull_dataset", side_effect=fake_pull):
            out = pulls.pull_dashboard("their-page", "me@x.com")
        self.assertEqual(saved["datasets"], ["ok_one_copy", "bad", "ok_two_copy"])
        self.assertEqual(out["missing_datasets"], ["bad"])


if __name__ == "__main__":
    unittest.main()
