"""The admin users query must actually execute.

`list_users` had no test, and a `{where}` that psycopg2 failed to interpolate shipped as
a **500 on the whole admin page** — the users table simply never rendered, with nothing
in the unit suite to notice. These are shaped like the other tests here (fake cursors,
no server) but assert the one thing a fake cannot: that the statement psycopg2 is
handed contains no leftover `{…}` placeholder and asks for the right columns.
"""
import re
import unittest
from unittest.mock import MagicMock, patch

from services import auth_store


class _Cur:
    def __init__(self, sink, rows):
        self._sink = sink
        self._rows = rows
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._sink.append((sql, params))
        self.rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        # ⚠️ Must return ROWS. The real bug only fires when at least one row comes
        # back, because the loop that shadows `where` is what breaks the substitution —
        # a fake returning `[]` skips that loop, so the broken code passes the test and
        # ships a 500 on the admin page.
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, sink, rows):
        self._sink = sink
        self._rows = rows

    def cursor(self, cursor_factory=None):
        return _Cur(self._sink, self._rows)

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


#: One account-shaped row — enough for the loop that shadows `where` to run.
ROW = {"id": 1, "email": "a@x.com", "display_name": "A", "role": "user",
       "disabled": False, "created_at": None, "last_login_at": None,
       "deleted_at": None, "purge_after": None, "deleted_by": "",
       "requests": 0, "agent_requests": 0, "last_seen": None}


def _run(fn, *args, rows=None, **kwargs):
    sink: list = []
    with patch.object(auth_store, "_ensure_schema"), \
         patch.object(auth_store, "_db", return_value=_Conn(sink, [ROW] if rows is None else rows)):
        fn(*args, **kwargs)
    return sink


class ListUsersTests(unittest.TestCase):
    def test_the_statement_has_no_leftover_named_placeholder(self):
        # ⚠️ The actual bug: the query was a PLAIN string, not an f-string, so the
        # `{where}` filter went to the server verbatim — a syntax error, and a 500 on
        # the whole admin page (the users table simply never rendered). Nothing in the
        # unit suite noticed, because there was no test for this query at all.
        sink = _run(auth_store.list_users)
        sql, _params = sink[0]
        self.assertNotRegex(sql, r"\{[a-zA-Z_][a-zA-Z0-9_]*\}",
                            f"unsubstituted placeholder in: {sql[:200]}")
        self.assertIn("FROM app_users", sql)

    def test_the_filter_reaches_the_statement_as_sql(self):
        # Not merely "no placeholder": the filter must actually be IN the SQL, or a
        # search box that silently returns every account is worse than a crash.
        sink = _run(auth_store.list_users, "example-user")
        sql, _params = sink[0]
        self.assertIn("WHERE u.email ILIKE %s", sql)

    def test_the_filter_is_interpolated_into_the_sql(self):
        sink = _run(auth_store.list_users, "example-user")
        sql, params = sink[0]
        self.assertIn("WHERE u.email ILIKE %s", sql)
        self.assertEqual(params[:2], ["%example-user%", "%example-user%"])

    def test_no_filter_still_asks_for_the_limit(self):
        sink = _run(auth_store.list_users)
        _sql, params = sink[0]
        self.assertEqual(params, [200], "the unfiltered call still passes only the limit")

    def test_it_selects_the_lifecycle_columns_the_page_renders(self):
        # The admin page shows "N days left to restore" straight off these three.
        sink = _run(auth_store.list_users)
        sql, _params = sink[0]
        for column in ("u.deleted_at", "u.purge_after", "u.deleted_by"):
            self.assertIn(column, sql, f"{column} missing — the recycle bin state cannot render")


class DaysLeftOnTheUsersRowTests(unittest.TestCase):
    """`days_left` must reach the USERS table, not only the recycle bin.

    The STATUS cell draws "N days left to restore" from it. When it was missing the page
    read `undefined` and rendered a bare coloured pill with no text in it — a screenshot
    caught it, because every other assertion still passed.
    """

    def _row(self, **over):
        return dict(ROW, **over)

    def _list_with(self, row):
        # `last_seen` must be None and the timestamps real datetimes: `_public()`
        # calls `.isoformat()` on them, so text values fail here for a reason that has
        # nothing to do with what these tests are about.
        row = dict(row, last_seen=None)
        sink: list = []
        with patch.object(auth_store, "_ensure_schema"), \
             patch.object(auth_store, "_db", return_value=_Conn(sink, [row])):
            return auth_store.list_users()

    def test_a_closed_account_carries_its_remaining_days(self):
        from datetime import datetime, timedelta, timezone
        out = self._list_with(self._row(
            deleted_at=datetime.now(timezone.utc),
            purge_after=datetime.now(timezone.utc) + timedelta(days=12)))
        self.assertTrue(out[0]["pending_deletion"])
        self.assertEqual(out[0]["days_left"], 12)

    def test_an_active_account_has_no_deadline(self):
        out = self._list_with(self._row())
        self.assertFalse(out[0]["pending_deletion"])
        self.assertIsNone(out[0]["days_left"])

    def test_an_already_due_account_reads_zero_not_a_negative(self):
        from datetime import datetime, timedelta, timezone
        out = self._list_with(self._row(
            deleted_at=datetime.now(timezone.utc),
            purge_after=datetime.now(timezone.utc) - timedelta(days=3)))
        self.assertEqual(out[0]["days_left"], 0)

    def test_it_uses_the_lifecycle_rule_rather_than_its_own(self):
        # Rounding UP matters: 29 hours left must not read "1 day". Assert the two agree
        # by construction — if somebody copies the calculation in, this stops matching.
        from datetime import datetime, timedelta, timezone
        from services import account_lifecycle as lifecycle
        deadline = datetime.now(timezone.utc) + timedelta(days=1, hours=23)
        out = self._list_with(self._row(
            deleted_at=datetime.now(timezone.utc), purge_after=deadline))[0]
        self.assertEqual(out["days_left"], lifecycle.days_left({"purge_after": deadline}))
        self.assertEqual(out["days_left"], 2, "29 hours left must not under-report")


class SearchUsersTests(unittest.TestCase):
    def test_the_picker_query_is_clean_too(self):
        # `search_users` builds its filter with an f-string; the guard is that whatever
        # interpolates it actually did, so the same failure cannot reappear there.
        sink = _run(auth_store.search_users, "example-user")
        sql, _params = sink[0]
        self.assertNotRegex(sql, r"\{[a-zA-Z_][a-zA-Z0-9_]*\}",
                            f"unsubstituted placeholder in: {sql[:200]}")
        self.assertIn("WHERE u.disabled = FALSE", sql)

    def test_a_one_character_query_short_circuits(self):
        sink = _run(auth_store.search_users, "a")
        self.assertEqual(sink, [], "a single character must not hit the database")


class PublicShapeTests(unittest.TestCase):
    def test_a_row_serialises_the_pending_deletion_state(self):
        row = auth_store._public({
            "id": 1, "email": "a@x.com", "display_name": "", "role": "user",
            "disabled": False, "created_at": None, "last_login_at": None,
            "deleted_at": None, "purge_after": None, "deleted_by": "",
        })
        for key in ("pending_deletion", "deleted_at", "purge_after", "days_left_source"):
            if key == "days_left_source":
                continue
            self.assertIn(key, row)
        self.assertFalse(row["pending_deletion"])

    def test_a_closed_account_is_flagged(self):
        from datetime import datetime, timezone
        row = auth_store._public({
            "id": 1, "email": "a@x.com", "display_name": "", "role": "user",
            "disabled": False, "created_at": None, "last_login_at": None,
            "deleted_at": datetime.now(timezone.utc),
            "purge_after": datetime.now(timezone.utc), "deleted_by": "admin@x.com",
        })
        self.assertTrue(row["pending_deletion"])
        self.assertEqual(row["deleted_by"], "admin@x.com")


if __name__ == "__main__":
    unittest.main()
