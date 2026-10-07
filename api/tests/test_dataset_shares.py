"""Dataset sharing — the grant table and the two things that can leak through it.

Sharing hands a colleague read access to rows, not to the file you uploaded. Two
failure modes are worth a test each:

- a grant that outlives its dataset. If a table is dropped and later an upload
  reuses the name, leftover rows would silently hand the old grantees access to
  somebody else's data. `delete_pg_table` therefore revokes; this pins that.
- a grant the owner never made. `share()` checks the caller owns the dataset
  before writing, and the router narrows that to 404 for non-owners so a
  stranger cannot probe which dataset names exist.

Everything here runs against a fake connection — no server, no database.
"""
import unittest
from unittest.mock import patch

from services import data_center_db as db
from services import dataset_shares as shares


class _FakeCursor:
    """Records SQL and replays queued results, in the order queries arrive."""

    def __init__(self, log, results, factory=None):
        self._log = log
        self._results = list(results)
        self._factory = factory
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._log.append((sql, params))
        if self._results:
            self._next = self._results.pop(0)
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def fetchall(self):
        return self._next or []

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


class ValidationTests(unittest.TestCase):
    """Names and addresses are validated before anything reaches SQL."""

    def test_dataset_name_must_be_an_identifier(self):
        for bad in ('a; DROP TABLE x', 'has space', '1leading_digit', '', 'x' * 64, 'drop--'):
            with self.assertRaises(shares.ShareError, msg=bad):
                shares.share(bad, "b@example.com", "a@example.com")

    def test_valid_identifier_is_accepted_by_the_name_check(self):
        self.assertEqual(shares._check_name(" sales_2026_q1 "), "sales_2026_q1")

    def test_email_shape_is_enforced(self):
        for bad in ("not-an-email", "a@b", "a b@example.com", "@example.com", "a@", ""):
            with self.assertRaises(shares.ShareError, msg=bad):
                shares.share("t", bad, "owner@example.com")

    def test_email_is_normalised_to_lowercase(self):
        # Two spellings of the same address must not become two grants: the
        # UNIQUE(table_name, grantee_email) constraint would treat them as
        # distinct rows and the second share would be invisible to revoke.
        self.assertEqual(shares._check_email("  Bob@Example.COM "), "bob@example.com")

    def test_error_messages_are_bilingual_pairs(self):
        # The service layer raises before any request object exists, so the pair
        # is the whole contract — `pick()` on the router side splits it later.
        with self.assertRaises(shares.ShareError) as caught:
            shares.share("t", "nope", "owner@example.com")
        self.assertIn(" / ", str(caught.exception))


class GrantTests(unittest.TestCase):
    def test_self_share_is_refused(self):
        # It would grant nothing and then sit in the sharing UI looking like a
        # mistake the owner has to delete.
        with self.assertRaises(shares.ShareError) as caught:
            shares.share("t", "Owner@Example.com", "owner@example.com")
        self.assertIn("/", str(caught.exception))

    def test_non_owner_cannot_create_a_grant(self):
        log = []
        conn = _FakeConn(log, [{"owner_email": "real@example.com"}])
        with patch.object(shares, "connect_main", return_value=conn), \
             patch.object(shares, "_ensure_schema"):
            with self.assertRaises(shares.ShareError) as caught:
                shares.share("t", "colleague@example.com", "intruder@example.com")
        self.assertIn("/", str(caught.exception))
        # No INSERT may have been attempted.
        self.assertFalse([q for q in log if "INSERT" in str(q[0]).upper()])

    def test_missing_dataset_is_not_created_into_a_grant(self):
        log = []
        conn = _FakeConn(log, [None])
        with patch.object(shares, "connect_main", return_value=conn), \
             patch.object(shares, "_ensure_schema"):
            with self.assertRaises(shares.ShareError):
                shares.share("ghost", "colleague@example.com", "owner@example.com")
        self.assertFalse([q for q in log if "INSERT" in str(q[0]).upper()])

    def test_ownerless_dataset_cannot_be_shared_by_an_arbitrary_user(self):
        log = []
        conn = _FakeConn(log, [{"owner_email": ""}])
        with patch.object(shares, "connect_main", return_value=conn), patch.object(shares, "_ensure_schema"):
            with self.assertRaises(shares.ShareError):
                shares.share("legacy", "colleague@example.com", "anyone@example.com")
        self.assertFalse([q for q in log if "INSERT" in str(q[0]).upper()])

    def test_grant_write_is_committed(self):
        # Same class of bug as the calendar DDL test: an uncommitted INSERT
        # disappears on connection close, and the UI shows nothing shared.
        log = []
        conn = _FakeConn(log, [{"owner_email": "owner@example.com"},
                               {"table_name": "t", "grantee_email": "c@example.com",
                                "shared_by": "owner@example.com", "created_at": None}])
        with patch.object(shares, "connect_main", return_value=conn), \
             patch.object(shares, "_ensure_schema"):
            shares.share("t", "c@example.com", "owner@example.com")
        self.assertTrue(conn.committed)


class CascadeTests(unittest.TestCase):
    """The delete path must take the grants with it."""

    def test_dropping_a_dataset_revokes_every_grant(self):
        log = []
        conn = _FakeConn(log, [])
        with patch.object(db, "get_pg_conn", return_value=conn), \
             patch.object(shares, "connect_main", return_value=conn), \
             patch.object(shares, "_ensure_schema"):
            db.delete_pg_table("sales_2026")
        revoked = [q for q in log if str(q[0]).strip().upper().startswith("DELETE")
                   and "dataset_shares" in str(q[0])]
        self.assertTrue(revoked, "delete_pg_table left dataset_shares grants behind")
        self.assertEqual(("sales_2026",), revoked[0][1])

    def test_cleanup_is_inside_the_data_layer_not_the_router(self):
        # A router-level call would be one more endpoint to forget. This asserts
        # the wiring lives where the drop does, so a future delete path inherits it.
        import inspect
        src = inspect.getsource(db.delete_pg_table)
        self.assertIn("revoke_all_for", src)
        self.assertLess(src.index("DROP TABLE"), src.index("revoke_all_for"))


class SharedWithMeTests(unittest.TestCase):
    def test_blank_address_grants_nothing(self):
        self.assertEqual(shares.shared_with_me_rows(""), [])
        self.assertEqual(shares.shared_with_me_rows(None), [])

    def test_query_joins_the_registry_so_a_dead_grant_cannot_render(self):
        log = []
        conn = _FakeConn(log, [])
        with patch.object(shares, "connect_main", return_value=conn), \
             patch.object(shares, "_ensure_schema"):
            shares.shared_with_me_rows("me@example.com")
        joined = [q for q in log if "_import_registry" in str(q[0])]
        self.assertTrue(joined, "shared-with-me must skip grants whose dataset is gone")
        self.assertEqual(("me@example.com",), joined[0][1])


if __name__ == "__main__":
    unittest.main()
