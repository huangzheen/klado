"""The two triage decisions a reader owns: mark unread again, and delete.

There is no PostgreSQL in this checkout, so these run against a small fake
connection. The fake is not a list-returning mock: it **applies** the visibility
rule and the reader state, because the assertions that matter are all of the
negative kind — "this row is not in YOUR feed", "this one is in nobody else's",
"marking it unread did not touch your colleague's list". A canned list would pass
every one of those without testing anything.

Two things are pinned down here:

* **Delete is per reader, and the log is never written to.** A broadcast is one row
  that every signed-in user sees, so a real DELETE would erase a colleague's
  published report from everybody's history the moment one person tidied up. The
  fake has a hard time returning a usable answer if the service ever reaches for
  `DELETE FROM inbox_events`, which is the point: that statement must not appear.
* **Unread is per reader too**, and it is NOT `inbox_events.read_at`. That column is
  one value shared by every reader of a broadcast, so writing it from a per-reader
  control puts the message back in bold in somebody else's Inbox.

The fake also asserts the SQL TEXT: a query that stopped filtering in the database
(after fetching, say) would still return the right rows here, because the fake
filters in Python. So the composed statements are checked directly as well.
"""
import re
import unittest
from unittest import mock

from services import inbox

ME = "me@example.com"
MATE = "mate@example.com"
STRANGER = "stranger@example.com"
NOW = "2026-10-05T09:00:00Z"


def _event(eid, recipient, read_at=None):
    return {"id": eid, "kind": "share", "actor_email": "someone@example.com",
            "recipient_email": recipient, "target_type": "report",
            "target_slug": "s%d" % eid, "target_title": "Doc %d" % eid,
            "target_url": "/?report=s%d" % eid, "summary": "", "note_id": None,
            "created_at": "2026-10-01T09:00:00Z", "read_at": read_at}


class _Cursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # ── the semantics the service asks for, implemented once ────────────────
    def _visible(self, email):
        out = []
        for eid, row in self.db.rows.items():
            if not (row["recipient_email"] is None or row["recipient_email"] == email):
                continue
            state = self.db.state.get((email, eid))
            if state is not None and state.get("dismissed_at"):
                continue
            out.append(eid)
        return out

    def _effective_read(self, email, eid):
        state = self.db.state.get((email, eid))
        if state is not None:
            return state.get("read_at")
        return self.db.rows[eid].get("read_at")

    def execute(self, sql, params=()):
        flat = " ".join(str(sql).split())
        self.db.statements.append((flat, tuple(params or ())))
        self._rows = []
        self.rowcount = 0
        params = tuple(params or ())

        if flat.startswith("SELECT id, kind"):
            email = params[0]
            limit = params[-1]
            before = params[2] if len(params) == 4 else None
            ids = [i for i in self._visible(email) if before is None or i < before]
            ids.sort(reverse=True)
            rows = []
            for eid in ids[:limit]:
                row = dict(self.db.rows[eid])
                row["read_at"] = self._effective_read(email, eid)
                rows.append(row)
            self._rows = rows
            return self

        if flat.startswith("SELECT COUNT(*)"):
            email = params[0]
            self._rows = [[sum(1 for i in self._visible(email)
                               if self._effective_read(email, i) is None)]]
            return self

        if flat.startswith("SELECT email, display_name"):
            # The feed's decoration lookup. It is best-effort by design (see
            # services/inbox.py), so the fake simply has no users to offer.
            self._rows = []
            return self

        if flat.startswith("UPDATE inbox_events SET read_at"):
            email = params[-1]
            ids = self._visible(email)
            # ⚠️ Match on the id list specifically. Testing for `= ANY(%s)` alone also
            # matched the `recipient_email = %s` that is in EVERY variant, and the
            # fake then marked everything read — which reads as a service bug and is not.
            if "id = ANY(%s)" in flat:
                ids = [i for i in ids if i in params[0]]
            changed = 0
            for eid in ids:
                if self.db.rows[eid].get("read_at") is None:
                    self.db.rows[eid]["read_at"] = NOW
                    changed += 1
            self.rowcount = changed
            return self

        # The override clear that makes "mark read" win over "mark unread".
        if flat.startswith("DELETE FROM inbox_reader_state"):
            email = params[0]
            gone = []
            for (reader, eid), state in list(self.db.state.items()):
                if reader != email or state.get("dismissed_at"):
                    continue          # a hidden row must stay hidden
                if "event_id = ANY(%s)" in flat:
                    if eid in params[1]:
                        gone.append((reader, eid))
                elif state.get("read_at") is None:
                    gone.append((reader, eid))
            for key in gone:
                del self.db.state[key]
            self.rowcount = len(gone)
            return self

        if flat.startswith("INSERT INTO inbox_reader_state"):
            email = params[0]
            column = re.search(r"INSERT INTO inbox_reader_state \(reader_email, event_id, (\w+)\)",
                               flat).group(1)
            ids = self._visible(email)
            if "id = ANY(%s)" in flat:
                ids = [i for i in ids if i in params[2]]
            for eid in ids:
                state = self.db.state.setdefault((email, eid), {})
                state[column] = None if column == "read_at" else NOW
            self.rowcount = len(ids)
            return self

        raise AssertionError("unexpected statement: " + flat[:140])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    cursor_factory = None

    def __init__(self, db):
        self.db = db

    def cursor(self, *a, **k):
        return _Cursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _DB:
    def __init__(self):
        self.rows = {
            1: _event(1, ME),                 # mine, unread
            2: _event(2, ME, read_at=NOW),    # mine, read
            3: _event(3, None),               # broadcast, unread
            4: _event(4, None, read_at=NOW),  # broadcast, read
            5: _event(5, STRANGER),           # a third person's
        }
        self.state = {}
        self.statements = []

    def sql_for(self, prefix):
        return [s for s, _ in self.statements if s.startswith(prefix)]


class InboxTriageTests(unittest.TestCase):
    def setUp(self):
        self.db = _DB()
        patches = [
            mock.patch.object(inbox, "_db", lambda: _Conn(self.db)),
            mock.patch.object(inbox, "ensure_tables", lambda: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    # ── what "delete" means ────────────────────────────────────────────────
    def test_a_deleted_row_leaves_this_readers_feed_and_count(self):
        self.assertEqual([r["id"] for r in inbox.feed(ME)], [4, 3, 2, 1])
        inbox.dismiss(ME, ids=[3])
        self.assertEqual([r["id"] for r in inbox.feed(ME)], [4, 2, 1])
        self.assertEqual(inbox.unread_count(ME), 1)      # only id 1 left unread

    def test_deleting_a_broadcast_does_not_delete_it_for_anybody_else(self):
        inbox.dismiss(MATE, ids=[3])                     # a broadcast (id 3)
        self.assertNotIn(3, [r["id"] for r in inbox.feed(MATE)])
        self.assertIn(3, [r["id"] for r in inbox.feed(ME)])
        self.assertIn(3, [r["id"] for r in inbox.feed(STRANGER)])

    def test_a_row_addressed_to_someone_else_is_never_deletable_by_you(self):
        # The candidate ids come from a SELECT inside the INSERT, so this is refused
        # by the database rather than by a check the caller could forget.
        self.assertEqual(inbox.dismiss(ME, ids=[5]), 0)
        self.assertIn(5, [r["id"] for r in inbox.feed(STRANGER)])

    def test_delete_all_clears_everything_the_reader_can_see(self):
        inbox.dismiss(ME)
        self.assertEqual(inbox.feed(ME), [])
        self.assertEqual(inbox.unread_count(ME), 0)
        # The stranger keeps their own row and both broadcasts.
        self.assertEqual([r["id"] for r in inbox.feed(STRANGER)], [5, 4, 3])

    def test_deleting_never_writes_to_the_log(self):
        inbox.dismiss(ME)
        inbox.dismiss(MATE, ids=[4])
        self.assertEqual(
            [s for s in self.db.sql_for("DELETE") if "inbox_events" in s], [],
            "a delete that touches inbox_events erases a shared row for everyone")
        self.assertEqual(sorted(self.db.rows), [1, 2, 3, 4, 5])

    def test_the_hidden_rows_come_from_the_database_not_from_python(self):
        inbox.dismiss(ME, ids=[3])
        inbox.feed(ME)                                   # ask again after the delete
        inbox.unread_count(ME)
        feed_sql = self.db.sql_for("SELECT id, kind")[0]
        self.assertIn("LEFT JOIN inbox_reader_state", feed_sql)
        self.assertIn("s.dismissed_at IS NULL", feed_sql)
        count_sql = self.db.sql_for("SELECT COUNT(*)")[0]
        self.assertIn("s.dismissed_at IS NULL", count_sql)

    # ── what "mark as unread" means ────────────────────────────────────────
    def test_marking_unread_puts_the_row_back_for_this_reader_only(self):
        # A BROADCAST is the only row two people can both see, so it is the only
        # place where "for this reader only" is a claim anybody could check.
        inbox.mark_unread(ME, ids=[4])                   # read broadcast
        self.assertIsNone([r for r in inbox.feed(ME) if r["id"] == 4][0]["read_at"])
        self.assertEqual(inbox.unread_count(ME), 3)      # 1 and 3 were already unread
        # The row's own read_at is untouched, so no other reader can see this…
        self.assertEqual(self.db.rows[4]["read_at"], NOW)
        self.assertIsNotNone([r for r in inbox.feed(MATE) if r["id"] == 4][0]["read_at"])
        self.assertEqual(inbox.unread_count(MATE), 1)    # only the broadcast id 3

    def test_the_list_and_the_badge_are_answered_by_the_same_expression(self):
        inbox.mark_unread(ME, ids=[2])
        bold = [r["id"] for r in inbox.feed(ME) if r["read_at"] is None]
        self.assertEqual(sorted(bold), [1, 2, 3])
        self.assertEqual(inbox.unread_count(ME), len(bold))
        feed_sql = self.db.sql_for("SELECT id, kind")[0]
        count_sql = self.db.sql_for("SELECT COUNT(*)")[0]
        self.assertIn(inbox._EFFECTIVE_READ, feed_sql)
        self.assertIn(inbox._EFFECTIVE_READ, count_sql)

    def test_marking_unread_never_writes_the_shared_read_column(self):
        inbox.mark_unread(ME, ids=[1])
        self.assertEqual(
            [s for s in self.db.sql_for("UPDATE") if "read_at = NULL" in s], [],
            "mark-as-unread must not write inbox_events.read_at: that column is shared")
        self.assertIsNone(self.db.rows[1]["read_at"])

    def test_clicking_a_message_again_beats_its_own_unread_mark(self):
        inbox.mark_unread(MATE, ids=[4])
        self.assertIsNone([r for r in inbox.feed(MATE) if r["id"] == 4][0]["read_at"])
        inbox.mark_read(MATE, ids=[4])
        self.assertIsNotNone([r for r in inbox.feed(MATE) if r["id"] == 4][0]["read_at"])
        self.assertEqual(inbox.unread_count(MATE), 1)    # back to the broadcast id 3

    def test_mark_all_read_clears_an_unread_mark_without_unhiding_anything(self):
        inbox.mark_unread(ME, ids=[1])
        inbox.dismiss(ME, ids=[3])
        inbox.mark_read(ME)
        self.assertEqual(inbox.unread_count(ME), 0)
        self.assertEqual([r["id"] for r in inbox.feed(ME)], [4, 2, 1])
        # The hidden row is still hidden, and still recorded as hidden.
        self.assertNotIn(3, [r["id"] for r in inbox.feed(ME)])
        self.assertIn((ME, 3), self.db.state)
        self.assertIsNotNone(self.db.state[(ME, 3)]["dismissed_at"])
        self.assertNotIn((ME, 1), self.db.state)

    def test_a_third_persons_row_is_never_marked_unread_by_you(self):
        self.assertEqual(inbox.mark_unread(ME, ids=[5]), 0)
        self.assertNotIn((ME, 5), self.db.state)


class InboxTriageStatementTests(unittest.TestCase):
    """The composed SQL, read straight off the constants.

    The fake above filters in Python, so on its own it would happily pass a query
    that stopped filtering in the database. These assert the statements themselves.
    """

    def test_the_visibility_rule_asks_for_two_readers_in_a_fixed_order(self):
        self.assertEqual(_VISIBLE_PARAMS, 2)
        self.assertIn("inbox_events.recipient_email IS NULL OR inbox_events.recipient_email = %s",
                      inbox._VISIBLE)
        self.assertIn("s.reader_email = %s", inbox._VISIBLE)

    def test_the_effective_read_distinguishes_no_override_from_an_explicit_null(self):
        # `COALESCE` would be the natural way to write this and it would be WRONG:
        # an override that says "unread" IS a NULL, and COALESCE would drop it.
        self.assertNotIn("COALESCE", inbox._EFFECTIVE_READ)
        self.assertIn("s.reader_email IS NULL", inbox._EFFECTIVE_READ)

    def test_the_table_is_created_with_a_cascade_from_the_log(self):
        with open(inbox.__file__, encoding="utf-8") as handle:
            src = handle.read()
        # Up to the closing paren of the COLUMN list — a non-greedy `.*?\)` would stop
        # at the `)` inside `REFERENCES inbox_events(id)` and read half the statement.
        ddl = re.search(r"CREATE TABLE IF NOT EXISTS inbox_reader_state\s*\(.*?\n\s*\)",
                        src, re.S).group(0)
        self.assertIn("REFERENCES inbox_events(id) ON DELETE CASCADE", ddl)
        self.assertIn("PRIMARY KEY (reader_email, event_id)", ddl)


_VISIBLE_PARAMS = inbox._VISIBLE.count("%s")


if __name__ == "__main__":
    unittest.main()
