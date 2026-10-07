"""The left rail — the rules that decide what a folder may hold and who may touch it.

A folder is a **virtual tag**, so the interesting failures are not layout ones:

* **an owner filter that is forgotten somewhere.** `folder_id` and `item_id` are
  sequential integers, so an unfiltered `DELETE … WHERE id = 7` is a way to delete a
  stranger's folder, and the rail would render it as the user's own row vanishing.
* **a 403 where a 404 belongs.** The status code must not confirm that a folder
  exists to an account that cannot see it.
* **an unbounded tree.** A cycle terminates; a depth past the ceiling is refused.
* **a slug that no longer exists.** The item row stays (the user may want to see that
  it is gone) but is reported `missing` rather than rendered as a link to nothing.

⚠️ **The fake here is a real in-memory database, not a scripted cursor.** An earlier
version answered each query from a lookup table keyed by a substring of the SQL, and
three tests passed for the wrong reason: the fake's `rowcount` default was 1, so
"the DELETE matched nothing" was indistinguishable from "it matched something", and
`assertNotIn("UPDATE", …)` was really asserting that the fake had not been asked.
That is the failure mode AGENTS.md warns about — a fake that cannot reach the branch
under test reports it as covered. This one keeps rows in dicts and applies the
statement's own WHERE, so "somebody else's folder" is genuinely absent from the
result and `rowcount` is genuinely 0.
"""
import unittest
from unittest.mock import patch

from services import rail_store as store

WHO = "me@example.com"
STRANGER = "them@example.com"


class _Cursor:
    """Rows come from the fake database, and `rowcount` reflects what matched."""

    def __init__(self, db):
        self.db = db
        self.queries = []
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.queries.append((flat, params))
        self._rows, self.rowcount = self.db.run(flat, params)
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, db):
        self.db = db
        self.autocommit = False

    def cursor(self, *a, **k):
        return _Cursor(self.db)

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


class _Db:
    """Enough SQL to run this module: a WHERE of `col = %s` and a couple of aggregates,
    applied to in-memory tables. Anything it cannot parse raises, so a new query in
    the store fails the suite instead of silently matching nothing."""

    def __init__(self):
        self.folders = []       # {id, owner_email, parent_id, name, position}
        self.items = []         # {id, owner_email, folder_id, item_type, item_slug, position}
        self.labels = []        # {owner_email, module_key, label_zh, label_en}
        self.titles = {}        # (table, slug) -> title; table is one of the three
        self._next_folder = 1
        self._next_item = 1

    # ── helpers the tests use ──
    def add_folder(self, owner, name, parent_id=None, position=0):
        row = {"id": self._next_folder, "owner_email": owner, "parent_id": parent_id,
               "name": name, "position": position}
        self._next_folder += 1
        self.folders.append(row)
        return row["id"]

    def add_item(self, owner, folder_id, item_type, item_slug, position=0):
        row = {"id": self._next_item, "owner_email": owner, "folder_id": folder_id,
               "item_type": item_type, "item_slug": item_slug, "position": position}
        self._next_item += 1
        self.items.append(row)
        return row["id"]

    def add_title(self, table, owner, slug, title):
        self.titles[(table, owner, slug)] = title

    # ── the tiny SQL engine ──
    def _rows_for(self, table):
        return {"klado_folders": self.folders,
                "klado_folder_items": self.items,
                "klado_module_labels": self.labels}[table]

    def run(self, sql, params):
        params = list(params or [])
        if "SELECT COUNT(*)" in sql:
            table = self._table_of(sql)
            rows = [r for r in self._rows_for(table) if self._match(r, sql, params, start=0)]
            return [{"n": len(rows)}], 1
        if sql.startswith("SELECT"):
            table = self._table_of(sql)
            if table in ("ai_reports", "ai_dashboards", "ai_knowledge_items", "_file_library"):
                # Live records live in `titles`; a slug with no entry does not exist,
                # which is exactly what "the document was deleted" looks like. Note
                # this branch comes BEFORE `_rows_for`, which only knows the three
                # rail tables — asking it for `ai_reports` is a KeyError.
                owner, wanted = params[0], params[1]
                out = []
                for slug in wanted:
                    if (table, owner, slug) in self.titles:
                        title = self.titles[(table, owner, slug)]
                        out.append({"slug": slug, "title": title, "filename": title,
                                    "object_name": slug})
                return out, len(out)
            rows = [r for r in self._rows_for(table) if self._match(r, sql, params)]
            return rows, len(rows)
        if sql.startswith("INSERT"):
            return self._insert(sql, params)
        if sql.startswith("UPDATE"):
            return self._update(sql, params)
        if sql.startswith("DELETE"):
            return self._delete(sql, params)
        raise AssertionError("the fake does not implement: " + sql[:70])

    def _table_of(self, sql):
        # ⚠️ Longest name first: `klado_folder_items` contains neither of the others,
        # but `ai_knowledge_items` and `ai_knowledge_documents` do overlap, and picking
        # the wrong one makes a query read the wrong pool and match nothing.
        for name in ("klado_folder_items", "klado_module_labels", "ai_knowledge_documents",
                     "ai_knowledge_items", "ai_dashboards", "ai_reports", "klado_folders",
                     "_file_library"):
            if name in sql:
                return name
        raise AssertionError("no table in: " + sql[:70])

    def _match(self, row, sql, params, start=0):
        """Apply the statement's own `col = %s` pairs, in order, to the row.

        ⚠️ Two things this parser gets wrong if it is written casually, and both fail
        SILENTLY by matching every row:
        * split on `AND`, not on commas — the WHERE clauses here are AND-joined;
        * the column is on the LEFT of `=`, the placeholder on the RIGHT, so the test
          is "does the right side end with `%s`", not "does the fragment".
        A predicate that vanishes turns every ownership test into a test that nothing
        is filtered — i.e. every one of them passes for the wrong reason.
        """
        tail = sql.split("WHERE", 1)[-1]
        # ORDER BY is not a predicate. Leaving it in makes the last fragment
        # `owner_email = %s ORDER BY position, id`, whose right side is not `%s`, so
        # that clause is dropped — and the row filter quietly stops filtering.
        for keyword in ("ORDER BY", "GROUP BY", "LIMIT"):
            tail = tail.split(keyword, 1)[0]
        clauses = []
        for fragment in tail.split(" AND "):
            sides = fragment.split("=")
            if len(sides) != 2:
                continue
            column, value = sides[0].strip(), sides[1].strip()
            if value == "%s":
                clauses.append(column.split(".")[-1])
        if "WHERE" in sql and not clauses:
            raise AssertionError(
                "the fake read no predicate out of: " + tail.strip()[:70])
        for index, column in enumerate(clauses):
            if row.get(column) != params[start + index]:
                return False
        return True

    def _insert(self, sql, params):
        if "klado_folders" in sql:
            row = {"id": self._next_folder, "owner_email": params[0],
                   "parent_id": params[1], "name": params[2], "position": params[3]}
            self._next_folder += 1
            self.folders.append(row)
            return [dict(row)], 1
        if "klado_folder_items" in sql:
            row = {"id": self._next_item, "owner_email": params[0], "folder_id": params[1],
                   "item_type": params[2], "item_slug": params[3], "position": params[4]}
            self._next_item += 1
            # The upsert conflicts on (owner, type, slug, folder) — all four.
            self.items = [r for r in self.items
                          if not (self._same_item(r, row) and r["folder_id"] == row["folder_id"])]
            self.items.append(row)
            return [dict(row)], 1
        if "klado_module_labels" in sql:
            self.labels = [r for r in self.labels
                           if not (r["owner_email"] == params[0] and r["module_key"] == params[1])]
            self.labels.append({"owner_email": params[0], "module_key": params[1],
                                "label_zh": params[2], "label_en": params[3]})
            return [dict(self.labels[-1])], 1
        raise AssertionError("unexpected INSERT: " + sql[:70])

    def _same_item(self, a, b):
        """「同一个对象」 — NOT 「同一行」.

        ⚠️ `folder_id` must NOT be part of this. It is what the retire-the-duplicate
        DELETE is trying to *change*, so including it makes the predicate ask "is this
        already in the target folder?" of every row including the one that is not,
        which is never true and the fake then deletes nothing. The store's unique key
        is (owner, type, slug, folder) — the object identity is (owner, type, slug).
        """
        return (a["owner_email"] == b["owner_email"] and a["item_type"] == b["item_type"]
                and a["item_slug"] == b["item_slug"])

    def _update(self, sql, params):
        """`SET <col> = %s WHERE id = %s AND owner_email = %s` — the SET value comes
        FIRST in the parameter list, before the two WHERE values. Guessing that order
        makes the fake move the wrong column on the wrong row, which reads as a store
        bug rather than a fake bug, and costs an hour."""
        table = self._table_of(sql)
        new_value, target_id, owner = params[0], params[1], params[2]
        column = sql.split("SET", 1)[1].split("=", 1)[0].strip().split(".")[-1]
        touched = []
        for row in self._rows_for(table):
            if row["id"] == target_id and row["owner_email"] == owner:
                row[column] = new_value
                touched.append(dict(row))
        return touched, len(touched)

    def _delete(self, sql, params):
        """Four distinct DELETEs live in this store, and they are told apart by their
        SHAPE rather than by which column they mention.

        ⚠️ Substring tests on `id = %s` do not work: `move_item`'s retire-the-duplicate
        statement contains `WHERE id = %s` twice, inside subqueries, so it matches the
        "delete one item by id" branch and deletes the wrong row. The fake then reports
        a duplicate the real database would not have — a store bug that does not exist.
        """
        if "klado_module_labels" in sql:
            # Clearing a renamed module back to the registry's own label.
            owner, key = params[0], params[1]
            before = len(self.labels)
            self.labels = [r for r in self.labels
                           if not (r["owner_email"] == owner and r["module_key"] == key)]
            return [], before - len(self.labels)
        if "(SELECT item_type" in sql:
            # move_item: retire the row already sitting in the target folder.
            owner, item_id, target = params[0], params[1], params[3]
            source = next((r for r in self.items
                           if r["id"] == item_id and r["owner_email"] == owner), None)
            if not source:
                return [], 0
            before = len(self.items)
            self.items = [r for r in self.items
                          if not (self._same_item(r, source) and r["folder_id"] == target)]
            return [], before - len(self.items)
        table = self._table_of(sql)
        if table == "klado_folders":
            # delete_folder by id, owner-scoped.
            target, owner = params[0], params[1]
            before = len(self.folders)
            self.folders = [r for r in self.folders
                            if not (r["id"] == target and r["owner_email"] == owner)]
            return [], before - len(self.folders)
        if "folder_id = %s" in sql or " id = %s" in sql:
            # remove_item by id, owner-scoped. Its WHERE names only `id`, so the test
            # is on the leading space — without it this string also matches a
            # `…_id = %s` column and steals delete_folder's branch.
            target, owner = params[0], params[1]
            before = len(self.items)
            self.items = [r for r in self.items
                          if not (r["id"] == target and r["owner_email"] == owner)]
            return [], before - len(self.items)
        raise AssertionError("the fake does not implement this DELETE: " + sql[:80])


def _patched(db):
    return patch.object(store, "_conn", lambda: _Conn(db))


# ── names ────────────────────────────────────────────────────────────────────

class NameTests(unittest.TestCase):
    def test_a_name_is_collapsed_and_trimmed(self):
        self.assertEqual(store._clean_name("  Q3   Channel  Review "), "Q3 Channel Review")

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(store.FolderError) as ctx:
            store._clean_name("   ")
        self.assertIn("不能为空", str(ctx.exception))

    def test_an_overlong_name_is_refused(self):
        with self.assertRaises(store.FolderError):
            store._clean_name("x" * (store.MAX_NAME + 1))

    def test_a_name_at_the_limit_is_accepted(self):
        self.assertEqual(len(store._clean_name("x" * store.MAX_NAME)), store.MAX_NAME)


class TypeTests(unittest.TestCase):
    def test_the_three_filable_types_are_accepted(self):
        for kind in store.ITEM_TYPES:
            self.assertEqual(store._clean_type(kind), kind)
            self.assertEqual(store._clean_type(kind.upper()), kind)

    def test_an_unknown_type_is_refused_with_the_allowed_set_named(self):
        # The error has to say what IS allowed, or the client cannot fix the call.
        with self.assertRaises(store.FolderError) as ctx:
            store._clean_type("spreadsheet")
        message = str(ctx.exception)
        for kind in store.ITEM_TYPES:
            self.assertIn(kind, message)


# ── ownership ────────────────────────────────────────────────────────────────

class OwnershipTests(unittest.TestCase):
    """Every statement filters on `owner_email`. A folder id is a sequential integer,
    so an unfiltered WHERE is a way to touch somebody else's row."""

    def setUp(self):
        self.db = _Db()
        self.db.add_folder(STRANGER, "Theirs")
        self.db.add_item(STRANGER, self.db.folders[0]["id"], "document", "secret.pdf")

    def test_creating_a_folder_normalises_the_address(self):
        # "Me@Example.com" and "me@example.com" are one person; without this a folder
        # is unreachable from the other spelling.
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.create_folder("Me@Example.com", "Q3")
        self.assertEqual(self.db.folders[-1]["owner_email"], "me@example.com")

    def test_listing_shows_only_this_accounts_folders(self):
        self.db.add_folder(WHO, "Mine")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            rows = store.list_folders(WHO)
        self.assertEqual([r["name"] for r in rows], ["Mine"])

    def test_renaming_somebody_elses_folder_is_a_404_and_changes_nothing(self):
        theirs = self.db.folders[0]["id"]
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.rename_folder(WHO, theirs, "stolen")
        self.assertEqual(self.db.folders[0]["name"], "Theirs")

    def test_deleting_somebody_elses_folder_is_a_404_and_the_row_survives(self):
        theirs = self.db.folders[0]["id"]
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.delete_folder(WHO, theirs)
        self.assertTrue(any(f["id"] == theirs for f in self.db.folders))
        self.assertTrue(self.db.items, "their filed items must survive too")

    def test_unfiling_somebody_elses_item_is_a_404(self):
        theirs = self.db.items[0]["id"]
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.remove_item(WHO, theirs)
        self.assertTrue(self.db.items)

    def test_moving_an_item_into_a_strangers_folder_is_a_404(self):
        mine = self.db.add_folder(WHO, "Mine")
        item = self.db.add_item(WHO, mine, "document", "a.pdf")
        theirs = self.db.folders[0]["id"]
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.move_item(WHO, item, theirs)
        self.assertEqual(self.db.items[-1]["folder_id"], mine)

    def test_an_anonymous_caller_cannot_use_the_rail_at_all(self):
        # `_owner` raises before any SQL runs: an empty address would otherwise be a
        # real, shared bucket that every logged-out request writes into.
        for fn, args in ((store.list_folders, (None,)),
                         (store.list_items, ("",)),
                         (store.create_folder, (None, "x"))):
            with self.assertRaises(store.FolderError):
                fn(*args)
        self.assertEqual(len(self.db.folders), 1, "nothing was written")


# ── tree shape ───────────────────────────────────────────────────────────────

class TreeTests(unittest.TestCase):
    def setUp(self):
        self.db = _Db()

    def test_depth_is_measured_by_walking_up(self):
        # grandparent → parent → child: two hops, so depth 2.
        top = self.db.add_folder(WHO, "A")
        mid = self.db.add_folder(WHO, "B", parent_id=top)
        leaf = self.db.add_folder(WHO, "C", parent_id=mid)
        with _patched(self.db), patch.object(store, "ensure_schema"):
            cursor = _Cursor(self.db)
            depth = store._depth_of(cursor, leaf, WHO)
        self.assertEqual(depth, 2)

    def test_a_top_level_folder_has_depth_zero(self):
        top = self.db.add_folder(WHO, "A")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            self.assertEqual(store._depth_of(_Cursor(self.db), top, WHO), 0)

    def test_a_cycle_terminates(self):
        # A rename cannot make one, but `parent_id` is writable and a future move
        # could; an unbounded walk would hang the request rather than refuse it.
        a = self.db.add_folder(WHO, "A")
        b = self.db.add_folder(WHO, "B", parent_id=a)
        self.db.folders[0]["parent_id"] = b            # a → b → a
        with _patched(self.db), patch.object(store, "ensure_schema"):
            depth = store._depth_of(_Cursor(self.db), a, WHO)
        self.assertLessEqual(depth, store.MAX_DEPTH + 2)

    def test_a_parent_past_the_ceiling_is_refused(self):
        deep = None
        for level in range(store.MAX_DEPTH + 1):
            deep = self.db.add_folder(WHO, f"L{level}", parent_id=deep)
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderError) as ctx:
                store.create_folder(WHO, "too deep", parent_id=deep)
        self.assertIn(str(store.MAX_DEPTH), str(ctx.exception))

    def test_a_parent_that_does_not_exist_is_a_404(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.create_folder(WHO, "orphan", parent_id=999)

    def test_a_top_level_folder_needs_no_parent_check(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.create_folder(WHO, "Q3")
        self.assertIsNone(self.db.folders[-1]["parent_id"])


# ── items ────────────────────────────────────────────────────────────────────

class ItemTests(unittest.TestCase):
    def setUp(self):
        self.db = _Db()
        self.folder = self.db.add_folder(WHO, "Q3")

    def test_filing_records_the_slug_and_the_folder(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            row = store.add_item(WHO, self.folder, "dashboard", "overview-board")
        self.assertEqual(row["item_slug"], "overview-board")
        self.assertEqual(row["folder_id"], self.folder)

    def test_refiling_into_the_same_folder_does_not_duplicate(self):
        # A drag that ends where it started must not look like a failure, so the
        # second drop updates the row rather than colliding with the unique key.
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.add_item(WHO, self.folder, "document", "a.pdf")
            store.add_item(WHO, self.folder, "document", "a.pdf")
        filed = [r for r in self.db.items if r["item_slug"] == "a.pdf"]
        self.assertEqual(len(filed), 1)

    def test_filing_something_with_no_slug_is_refused(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderError):
                store.add_item(WHO, self.folder, "document", "   ")

    def test_filing_into_a_strangers_folder_is_a_404(self):
        theirs = self.db.add_folder(STRANGER, "Theirs")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.add_item(WHO, theirs, "document", "a.pdf")
        self.assertEqual(self.db.items, [])

    def test_a_folder_past_the_item_ceiling_is_refused(self):
        for i in range(store.MAX_ITEMS_PER_FOLDER):
            self.db.add_item(WHO, self.folder, "document", f"f{i}.pdf")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderError) as ctx:
                store.add_item(WHO, self.folder, "document", "one-too-many.pdf")
        self.assertIn(str(store.MAX_ITEMS_PER_FOLDER), str(ctx.exception))

    def test_moving_retires_the_duplicate_in_the_target(self):
        # The unique key is (owner, type, slug, folder): moving INTO a folder that
        # already holds the object would otherwise raise a bare UniqueViolation,
        # which the router maps to a 500.
        other = self.db.add_folder(WHO, "Archive")
        one = self.db.add_item(WHO, self.folder, "document", "a.pdf")
        self.db.add_item(WHO, other, "document", "a.pdf")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.move_item(WHO, one, other)
        filed = [r for r in self.db.items if r["item_slug"] == "a.pdf"]
        self.assertEqual(len(filed), 1)
        self.assertEqual(filed[0]["folder_id"], other)

    def test_moving_leaves_the_source_without_a_duplicate(self):
        other = self.db.add_folder(WHO, "Archive")
        one = self.db.add_item(WHO, self.folder, "document", "a.pdf")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.move_item(WHO, one, other)
        self.assertEqual([r["folder_id"] for r in self.db.items if r["item_slug"] == "a.pdf"],
                         [other])

    def test_moving_an_item_that_is_not_filed_here_is_a_404(self):
        other = self.db.add_folder(WHO, "Archive")
        theirs = self.db.add_item(STRANGER, other, "document", "secret.pdf")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderNotFound):
                store.move_item(WHO, theirs, other)
        self.assertEqual(self.db.items[0]["folder_id"], other)

    def test_listing_marks_a_row_whose_target_is_gone(self):
        # A filed slug whose document was deleted must be reported, not rendered as a
        # link to nothing — and must not be silently dropped either: the user filed it
        # on purpose and deserves to see that it is gone.
        gone = self.db.add_item(WHO, self.folder, "document", "gone.pdf")
        alive = self.db.add_item(WHO, self.folder, "dashboard", "overview-board")
        self.db.add_title("ai_dashboards", WHO, "overview-board", "Overview Board")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            rows = {r["id"]: r for r in store.list_items(WHO)}
        self.assertTrue(rows[gone]["missing"])
        self.assertEqual(rows[gone]["title"], "")
        self.assertFalse(rows[alive]["missing"])
        self.assertEqual(rows[alive]["title"], "Overview Board")

    def test_listing_resolves_all_three_types(self):
        report = self.db.add_item(WHO, self.folder, "document", "a.pdf")
        dash = self.db.add_item(WHO, self.folder, "dashboard", "board")
        page = self.db.add_item(WHO, self.folder, "knowledge", "k1")
        self.db.add_title("ai_reports", WHO, "a.pdf", "A Report")
        self.db.add_title("ai_dashboards", WHO, "board", "A Board")
        self.db.add_title("ai_knowledge_items", WHO, "k1", "A Page")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            rows = {r["id"]: r for r in store.list_items(WHO)}
        self.assertEqual(rows[report]["title"], "A Report")
        self.assertEqual(rows[dash]["title"], "A Board")
        self.assertEqual(rows[page]["title"], "A Page")
        self.assertFalse(any(r["missing"] for r in rows.values()))

    def test_listing_only_returns_this_accounts_items(self):
        self.db.add_item(WHO, self.folder, "document", "mine.pdf")
        self.db.add_item(STRANGER, self.folder, "document", "theirs.pdf")
        with _patched(self.db), patch.object(store, "ensure_schema"):
            rows = store.list_items(WHO)
        self.assertEqual([r["item_slug"] for r in rows], ["mine.pdf"])


# ── module labels ────────────────────────────────────────────────────────────

class LabelTests(unittest.TestCase):
    def setUp(self):
        self.db = _Db()

    def test_a_label_for_a_module_that_does_not_exist_is_refused(self):
        # Otherwise the row is invisible in the rail and unremovable from it: a typo
        # becomes permanent state.
        with _patched(self.db), patch.object(store, "ensure_schema"):
            with self.assertRaises(store.FolderError) as ctx:
                store.set_module_labels(WHO, {"dashbord": {"label_zh": "看板"}})
        self.assertIn("dashbord", str(ctx.exception))
        self.assertEqual(self.db.labels, [], "nothing was written")

    def test_a_real_module_label_is_written_and_replaces_the_old_one(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "看板", "label_en": "Board"}})
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "面板", "label_en": ""}})
        self.assertEqual(len(self.db.labels), 1)
        self.assertEqual(self.db.labels[0]["label_zh"], "面板")

    def test_both_blank_labels_write_no_row(self):
        # "Renamed to nothing" is not a state the rail can show; absence means
        # "never renamed" and falls back to the registry label.
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "  ", "label_en": ""}})
        self.assertEqual(self.db.labels, [])

    def test_clearing_a_label_deletes_the_row_not_just_the_new_write(self):
        # ⚠️ This is the difference between "no row" and "a row with two blanks". A
        # caller who clears a name has to be able to REMOVE it; a write that skips
        # blank rows leaves the old name in place forever, with no UI anywhere that
        # can take it back off.
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "看板", "label_en": "Board"}})
            self.assertEqual(len(self.db.labels), 1)
            out = store.set_module_labels(WHO, {"dashboard": {"label_zh": "", "label_en": ""}})
        self.assertEqual(self.db.labels, [], "the row must be gone, not blank")
        self.assertEqual(out["dashboard"], {"label_zh": "", "label_en": ""})

    def test_clearing_one_side_keeps_the_other(self):
        # A Chinese user who only wants the Chinese short name must not lose it when
        # they later clear the English one, and vice versa.
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "看板", "label_en": "Board"}})
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "看板", "label_en": ""}})
        self.assertEqual(self.db.labels[0]["label_zh"], "看板")
        self.assertEqual(self.db.labels[0]["label_en"], "")

    def test_labels_are_per_account(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            store.set_module_labels(WHO, {"dashboard": {"label_zh": "看板"}})
            store.set_module_labels(STRANGER, {"dashboard": {"label_zh": "Their board"}})
            self.assertEqual(store.list_module_labels(WHO)["dashboard"]["label_zh"], "看板")
            self.assertEqual(store.list_module_labels(STRANGER)["dashboard"]["label_zh"],
                             "Their board")

    def test_a_module_with_no_row_is_absent_rather_than_blank(self):
        with _patched(self.db), patch.object(store, "ensure_schema"):
            self.assertEqual(store.list_module_labels(WHO), {})


if __name__ == "__main__":
    unittest.main()
