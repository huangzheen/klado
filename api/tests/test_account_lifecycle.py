"""Closing an account: what is promised, and what must not be broken on the way.

The lifecycle has three phases and each one is a promise to a person:

    soft_delete → "your data is still here for 30 days"
    restore     → "putting it back gives you exactly what you had"
    purge       → "now it is gone, including other people's dashboards"

The tests below are mostly about the ways those promises break quietly. The four that
earned their keep while this was being written:

* `restore()` deleting the shares the account *gave*. Its datasets were never dropped,
  so every link it handed out still resolves — deleting the grant rows would break
  working links while leaving the data they point at untouched.
* soft delete flipping `disabled`. That flag is an operator's switch with its own
  meaning; folding it in means a restore silently re-enables an account an admin had
  turned off for an unrelated reason.
* a purge that touches `mail_settings`. That table is one deployment-wide row holding
  the SMTP password, with `updated_by` — not `owner_email`. Keying a purge on it would
  either crash or, worse, take the installation's mail configuration down because one
  unrelated account closed.
* `purge_after` being computed at read time. A config change would then retroactively
  move a deadline that was already shown to somebody.

Everything here runs against fake connections — no server, no database.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from services import account_lifecycle as al
from services import auth_store


# ── fake connections (the pattern tests/test_dashboard.py uses) ────────────────

class _FakeCursor:
    def __init__(self, log, results):
        self._log = log
        self._results = results          # the CONNECTION's queue, not a copy
        self.rowcount = 0
        self._next = None

    def execute(self, sql, params=None):
        self._log.append((" ".join(str(sql).split()), params))
        # One shared queue drained in call order — psycopg2's behaviour. A per-cursor
        # copy would re-serve the first queued row to the second query in the same
        # call, which is how "no such share" comes back truthy.
        self._next = self._results.pop(0) if self._results else None
        self.rowcount = 1

    def fetchone(self):
        # A queued entry may be a list of rows (fetchall) or a single row (fetchone).
        # Handing back the list here would make `row["n"]` a subscript on a list.
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
    def __init__(self, log, results):
        self._log = log
        self._results = list(results)
        self.committed = False
        self._cursor = None

    def cursor(self, cursor_factory=None):
        # One cursor per connection, not per call.
        if self._cursor is None:
            self._cursor = _FakeCursor(self._log, self._results)
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_fake_db(results, fn, *args, propagate: bool = True, **kwargs):
    """Run `fn` with both connection helpers replaced by one fake connection."""
    log: list = []
    conn = _FakeConn(log, results)
    with patch.object(al, "connect_main", return_value=conn), \
         patch.object(auth_store, "_db", return_value=conn), \
         patch.object(auth_store, "_ensure_schema"):
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:                       # noqa: BLE001 — the test decides
            if propagate:
                raise
            value = exc
    return log, conn, value


def _sqls(log):
    return [sql for sql, _ in log]


USER = {"id": 7, "email": "person@example.com", "display_name": "P",
        "role": "user", "disabled": False, "created_at": None, "last_login_at": None,
        "deleted_at": None, "purge_after": None, "deleted_by": ""}


# ── the tables the cascade is built from ──────────────────────────────────────

class CascadeTableListTests(unittest.TestCase):
    def test_the_content_tables_are_all_there(self):
        tables = {t for t, _, _ in al.OWNED_CONTENT}
        self.assertEqual(tables, {"ai_reports", "ai_dashboards",
                                  "ai_calendar_events", "ai_knowledge_items",
                                  # The knowledge projects a closed account's pages were
                                  # filed into. Without this the address could be
                                  # registered again and inherit the old card wall.
                                  "ai_knowledge_projects"})

    def test_the_derived_knowledge_table_is_never_cascaded(self):
        # ai_knowledge_documents is reconciled from api/knowledge_docs/*.md on every
        # startup. It has no owner column, and deleting its rows would lose a derived
        # copy the next restart writes straight back. It is the product's content.
        self.assertNotIn("ai_knowledge_documents",
                         {t for t, _, _ in al.OWNED_CONTENT} |
                         {t for t, _, _ in al.OWNED_SIDECARS})

    def test_mail_settings_is_never_cascaded(self):
        # One deployment-wide row with `updated_by`, not `owner_email`. A purge keyed
        # on it would crash on the missing column, or take the installation's SMTP
        # configuration down because one unrelated account closed.
        self.assertNotIn("mail_settings", {t for t, _, _ in al.OWNED_SIDECARS})

    def test_every_entry_that_names_an_email_column_names_a_real_one(self):
        real = {
            "ai_reports": "owner_email", "ai_dashboards": "owner_email",
            "ai_calendar_events": "owner_email", "ai_knowledge_items": "owner_email",
            "ai_report_colleague_shares": "recipient_email",
            "ai_report_filter_selections": "user_email",
            "ai_knowledge_item_shares": "recipient_email",
            # Folders hang off a project the *owner* may still have, so this is
            # purged by the recipient's address and not by the project cascade.
            "ai_knowledge_folder_shares": "recipient_email",
            "ai_knowledge_projects": "owner_email",
            "inbox_events": "recipient_email", "login_codes": "email",
            "file_shares": ("grantee_email", "shared_by"),
        }
        for table, column, _ in list(al.OWNED_CONTENT) + list(al.OWNED_SIDECARS):
            if column is None:
                continue
            if table in ("dataset_shares", "file_shares"):
                # dataset_shares appears twice on purpose: once for the grants it was
                # given, once for the grants it gave. Both columns are real.
                self.assertIn(column, ("grantee_email", "shared_by"))
                continue
            self.assertIn(table, real, f"{table} has no known email column")
            self.assertEqual(real[table], column, f"{table} column drifted")

    def test_the_keyed_by_user_id_tables_declare_no_email_column(self):
        # account_modules / agent_tokens join on user_id. Sending them an email column
        # would silently match nothing, and the rows would survive the purge.
        for table, column, _ in al.OWNED_SIDECARS:
            if table in ("account_modules", "agent_tokens"):
                self.assertIsNone(column, f"{table} is keyed by user_id")


# ── phase 1: the close ────────────────────────────────────────────────────────

class SoftDeleteTests(unittest.TestCase):
    def test_a_close_writes_the_deadline_and_the_actor(self):
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=dict(USER)), \
                 patch.object(auth_store, "get_user_by_email", return_value=dict(USER)):
                return al.soft_delete(7, actor="admin@x.com")
        log, _conn, view = _with_fake_db([], run)
        joined = " | ".join(_sqls(log))
        self.assertIn("UPDATE app_users", joined)
        self.assertIn("purge_after", joined)
        self.assertIn("deleted_by", joined)
        self.assertTrue(view["pending_deletion"])

    def test_the_grace_period_is_thirty_days(self):
        self.assertEqual(al.GRACE_DAYS, 30)

    def test_closing_does_not_disable_the_account(self):
        # `disabled` is an operator's switch with its own meaning. Folding it in here
        # means restore() would silently re-enable an account an admin had turned off
        # for an unrelated reason.
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=dict(USER)), \
                 patch.object(auth_store, "get_user_by_email", return_value=dict(USER)):
                return al.soft_delete(7)
        log, _conn, _v = _with_fake_db([], run)
        self.assertNotIn("disabled", " ".join(_sqls(log)))

    def test_a_second_close_does_not_move_the_deadline(self):
        # Clicking twice must not quietly shorten a window the user was told about.
        closed = dict(USER, deleted_at=datetime.now(timezone.utc),
                      purge_after=datetime.now(timezone.utc) + timedelta(days=3))
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=closed):
                return al.soft_delete(7)
        log, _conn, view = _with_fake_db([], run)
        self.assertEqual(_sqls(log), [], "an already-closed account must not be re-written")
        self.assertTrue(view["pending_deletion"])

    def test_an_agent_code_stops_working_at_the_close_not_at_the_purge(self):
        # A code that outlived the account would be a way back in after the fact.
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=dict(USER)), \
                 patch.object(auth_store, "get_user_by_email", return_value=dict(USER)):
                return al.soft_delete(7, actor="a")
        log, _conn, _v = _with_fake_db([], run)
        joined = " ".join(_sqls(log))
        self.assertIn("UPDATE agent_tokens", joined)
        self.assertIn("revoked_at", joined)

    def test_an_unissued_sign_in_code_is_consumed(self):
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=dict(USER)), \
                 patch.object(auth_store, "get_user_by_email", return_value=dict(USER)):
                return al.soft_delete(7)
        log, _conn, _v = _with_fake_db([], run)
        joined = " ".join(_sqls(log))
        self.assertIn("UPDATE login_codes", joined)
        self.assertIn("consumed_at", joined)

    def test_an_unknown_account_is_refused(self):
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=None):
                return al.soft_delete(999)
        _log, _conn, err = _with_fake_db([], run, propagate=False)
        self.assertIsInstance(err, al.LifecycleError)
        self.assertIn("/", str(err), "the message must stay bilingual")


# ── phase 2: the undo ─────────────────────────────────────────────────────────

class RestoreTests(unittest.TestCase):
    closed = dict(USER, deleted_at=datetime.now(timezone.utc),
                  purge_after=datetime.now(timezone.utc) + timedelta(days=20))

    def test_restoring_deletes_nothing(self):
        # Soft delete removed nothing, so restoring must remove nothing either. In
        # particular the shares this account *gave* point at datasets that were never
        # dropped — deleting those grant rows would break working links.
        def run():
            with patch.object(auth_store, "get_user_by_id", side_effect=[dict(self.closed), dict(USER)]):
                return al.restore(7)
        log, _conn, _v = _with_fake_db([], run)
        self.assertEqual([s for s in _sqls(log) if s.strip().upper().startswith("DELETE")], [])
        joined = " ".join(_sqls(log))
        self.assertNotIn("dataset_shares", joined)
        self.assertNotIn("ai_report_colleague_shares", joined)

    def test_restoring_clears_the_marks_and_leaves_disabled_alone(self):
        def run():
            with patch.object(auth_store, "get_user_by_id", side_effect=[dict(self.closed), dict(USER)]):
                return al.restore(7)
        log, _conn, _v = _with_fake_db([], run)
        updates = [s for s in _sqls(log) if s.strip().upper().startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("disabled", updates[0])

    def test_restoring_an_account_that_was_never_closed_is_a_no_op(self):
        def run():
            with patch.object(auth_store, "get_user_by_id", return_value=dict(USER)):
                return al.restore(7)
        log, _conn, _v = _with_fake_db([], run)
        self.assertEqual(_sqls(log), [])


# ── the deadline ──────────────────────────────────────────────────────────────

class DaysLeftTests(unittest.TestCase):
    def test_no_deadline_means_no_answer(self):
        self.assertIsNone(al.days_left({"purge_after": None}))

    def test_it_rounds_up_so_the_window_is_never_understated(self):
        # `timedelta.days` truncates: 29 hours left would read "1 day", telling
        # somebody deciding whether there is still time a day less than they have.
        soon = datetime.now(timezone.utc) + timedelta(days=1, hours=23)
        self.assertEqual(al.days_left({"purge_after": soon}), 2)

    def test_it_never_goes_negative(self):
        past = datetime.now(timezone.utc) - timedelta(days=3)
        self.assertEqual(al.days_left({"purge_after": past}), 0)

    def test_a_whole_number_of_days_reads_exactly(self):
        soon = datetime.now(timezone.utc) + timedelta(days=5)
        self.assertIn(al.days_left({"purge_after": soon}), (5, 6))

    def test_an_iso_string_is_accepted(self):
        # `_public()` serialises timestamps, so a row read back out of a response
        # arrives as text. Reading it must not raise.
        soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.assertEqual(al.days_left({"purge_after": soon}), 2)

    def test_a_naive_timestamp_is_read_as_utc(self):
        naive = (datetime.now(timezone.utc) + timedelta(days=4)).replace(tzinfo=None)
        self.assertEqual(al.days_left({"purge_after": naive}), 4)

    def test_unparseable_text_is_not_a_crash(self):
        self.assertIsNone(al.days_left({"purge_after": "not-a-date"}))


# ── the recycle bin ───────────────────────────────────────────────────────────

class RecycleBinTests(unittest.TestCase):
    def test_only_closed_accounts_are_listed(self):
        row = {"id": 7, "email": "a@x.com", "display_name": "", "role": "user",
               "disabled": True, "created_at": None, "last_login_at": None,
               "deleted_at": datetime.now(timezone.utc),
               "purge_after": datetime.now(timezone.utc) + timedelta(days=9),
               "deleted_by": "admin@x.com"}
        _log, _conn, out = _with_fake_db([[row]], al.list_pending)
        self.assertIn("deleted_at IS NOT NULL", _sqls(_log)[0])
        self.assertIn("ORDER BY deleted_at DESC", _sqls(_log)[0])
        self.assertEqual(out[0]["email"], "a@x.com")
        self.assertIn("days_left", out[0])

    def test_expired_rows_can_be_left_out(self):
        _log, _conn, _v = _with_fake_db([[]], al.list_pending, include_expired=False)
        self.assertIn("purge_after > NOW()", " ".join(_sqls(_log)))

    def test_a_queue_is_not_frozen_by_one_bad_account(self):
        # One account's stuck storage object must not stop the rest, or a single
        # failure freezes the recycle bin forever.
        calls = []

        def fake_purge(uid):
            calls.append(uid)
            if uid == 1:
                raise RuntimeError("object locked")
            return {"ok": True}

        with patch.object(al, "purge_due", return_value=[{"id": 1, "email": "a@x.com"},
                                                         {"id": 2, "email": "b@x.com"}]), \
             patch.object(al, "purge_account", side_effect=fake_purge):
            done = al.sweep()
        self.assertEqual(calls, [1, 2])
        self.assertFalse(done[0]["purged"])
        self.assertTrue(done[1]["purged"])


# ── who is affected ───────────────────────────────────────────────────────────

class DependentsTests(unittest.TestCase):
    """`dependents()` runs its queries in a fixed order, so the fake's result queue
    must be filled in that same order or every later assertion reads someone else's
    row. With datasets present that is:

        datasets → dashboards → shares → reports → own_dashboards → knowledge
                → calendar → files

    Without datasets the dashboard and share queries are skipped entirely, leaving six.
    """

    def _user(self):
        return patch.object(auth_store, "get_user_by_id", return_value=dict(USER))

    #: no datasets → dashboards and shares are skipped: six slots
    NO_DATA_SET = [[],                                                       # datasets
                   [{"slug": "r", "title": "R", "visibility": "private"}],    # reports
                   [],                                                        # own_dashboards
                   [],                                                        # knowledge
                   [],                                                        # calendar
                   [{"n": 2}]]                                                # files

    #: with one dataset → eight slots
    WITH_DATA_SET = [[{"table_name": "sales_q3", "display_name": "Q3", "row_count": 10}],
                     [],                                                        # dashboards
                     [],                                                        # shares
                     [],                                                        # reports
                     [],                                                        # own_dashboards
                     [],                                                        # knowledge
                     [],                                                        # calendar
                     [{"n": 0}]]                                                # files

    def test_a_lone_account_with_nothing_shared_is_not_blocking(self):
        with self._user():
            _log, _conn, out = _with_fake_db(list(self.NO_DATA_SET), al.dependents, 7)
        self.assertEqual(out["total"], 3)          # 1 report + 2 files
        self.assertFalse(out["blocking"])

    def test_somebody_elses_dashboard_over_my_dataset_is_reported(self):
        # A dashboard is a live query, not a copy: it names the dataset by name, so
        # this is the thing that breaks when the table goes.
        results = [
            [{"table_name": "sales_q3", "display_name": "Q3", "row_count": 10}],   # mine
            [{"slug": "their-dash", "title": "Theirs", "owner_email": "them@x.com",
              "datasets": '["sales_q3", "other"]'}],                              # dashboards
            [{"grantee_email": "them@x.com", "table_name": "sales_q3"}],           # shares
            [], [], [], [],                                                      # r/own/k/c
            [{"n": 0}],                                                           # files
        ]
        with self._user():
            _log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertEqual(len(out["dashboards"]), 1)
        self.assertEqual(out["dashboards"][0]["datasets"], ["sales_q3"])
        self.assertTrue(out["blocking"])

    def test_a_dashboard_naming_someone_elses_dataset_is_not_a_dependant(self):
        results = list(self.WITH_DATA_SET)
        results[1] = [{"slug": "d", "title": "T", "owner_email": "them@x.com",
                       "datasets": '["their_own_table"]'}]
        with self._user():
            _log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertEqual(out["dashboards"], [])

    def test_a_corrupt_dataset_column_does_not_break_the_count(self):
        # The column stores JSON text; anything unreadable must degrade to "no
        # datasets named" rather than taking the whole impact dialog down.
        results = list(self.WITH_DATA_SET)
        results[1] = [{"slug": "d", "title": "T", "owner_email": "them@x.com",
                       "datasets": "<<not json>>"}]
        results[3] = [{"slug": "r", "title": "R", "visibility": "private"}]     # reports
        with self._user():
            _log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertEqual(out["dashboards"], [])
        self.assertIn("reports", out)
        self.assertEqual(out["total"], 2)      # 1 dataset + 1 report, no files

    def test_the_accounts_own_dashboards_are_listed_and_counted(self):
        # `dashboards` lists OTHER people's pages that depend on this account's data.
        # The pages that simply vanish with the account are the other half, and
        # leaving them out under-reports exactly what the operator is about to lose.
        results = list(self.NO_DATA_SET)
        results[2] = [{"slug": "mine", "title": "My page", "visibility": "private"}]
        with self._user():
            _log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertEqual(len(out["own_dashboards"]), 1)
        self.assertEqual(out["total"], 4)      # 1 report + 1 page + 2 files
        self.assertFalse(out["blocking"], "one's own page is not a reason to hesitate")

    def test_a_public_report_is_flagged_as_blocking(self):
        results = list(self.NO_DATA_SET)
        results[1] = [{"slug": "p", "title": "P", "visibility": "public"}]
        with self._user():
            _log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertTrue(out["blocking"])
        self.assertEqual(len(out["public_items"]), 1)
        # 1 public report + the 2 files NO_DATA_SET carries. The public report is
        # counted ONCE: `public_items` is a derived view of `reports`, not extra rows.
        self.assertEqual(out["total"], 3)

    def test_public_items_are_counted_once_not_twice(self):
        # They used to be re-queried, which both cost a round trip and added every
        # public report a second time to the number the confirm dialog shows.
        results = list(self.NO_DATA_SET)
        results[1] = [{"slug": "p", "title": "P", "visibility": "public"}]
        with self._user():
            log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertEqual(len([s for s in _sqls(log) if "FROM public.ai_reports" in s]), 1)
        self.assertEqual(out["total"], 3)      # would be 4 if double-counted

    def test_files_count_as_files_not_as_one_bucket(self):
        results = list(self.NO_DATA_SET)
        results[1] = []                        # no reports
        results[5] = [{"n": 200}]              # files
        with self._user():
            _log, _conn, out = _with_fake_db(results, al.dependents, 7)
        self.assertEqual(out["total"], 200)


# ── the purge ─────────────────────────────────────────────────────────────────

class PurgeTests(unittest.TestCase):
    def test_children_are_deleted_before_their_parent(self):
        # They cascade in SQL, but doing it explicitly means the reported count is the
        # number of rows actually removed, and a table without the FK cannot orphan.
        _log, _conn, _v = _with_fake_db([], al._purge_owned_content, "p@x.com", 7)
        joined = _sqls(_log)

        def at(stmt):
            """Index of the exact statement — the parent query starts with the same
            table name, so a substring search finds the child first both times."""
            hits = [i for i, s in enumerate(joined) if s.startswith(stmt)]
            self.assertTrue(hits, f"{stmt!r} was never issued; got {joined}")
            return hits[0]

        self.assertLess(at("DELETE FROM public.ai_dashboard_colleague_shares"),
                        at("DELETE FROM public.ai_dashboards"))
        self.assertLess(at("DELETE FROM public.ai_report_colleague_shares"),
                        at("DELETE FROM public.ai_reports"))
        self.assertLess(at("DELETE FROM public.ai_report_anyone_links"),
                        at("DELETE FROM public.ai_reports"))
        self.assertLess(at("DELETE FROM public.ai_knowledge_item_shares"),
                        at("DELETE FROM public.ai_knowledge_items"))

    def test_no_delete_ever_names_mail_settings(self):
        _log, _conn, _v = _with_fake_db([], al._purge_sidecars, "p@x.com", 7)
        self.assertNotIn("mail_settings", " ".join(_sqls(_log)))

    def test_content_is_removed_before_the_account_row(self):
        # If a storage object cannot be deleted, the account row must stay and the
        # sweep retries — rather than the account vanishing while its data lingers.
        order = []

        def fake_content(email, uid):
            order.append("content")
            return {}

        def fake_sidecars(email, uid):
            order.append("sidecars")
            return {}

        with patch.object(auth_store, "get_user_by_id", return_value=dict(USER)), \
             patch.object(al, "_purge_datasets", return_value=0), \
             patch.object(al, "_purge_files", return_value=0), \
             patch.object(al, "_purge_owned_content", side_effect=fake_content), \
             patch.object(al, "_purge_sidecars", side_effect=fake_sidecars), \
             patch.object(al, "_detach_from_others", return_value={}), \
             patch.object(auth_store, "_ensure_schema"), \
             patch.object(auth_store, "_db") as db:
            db.return_value.__enter__.return_value = _FakeConn([], [])
            al.purge_account(7)
        self.assertEqual(order, ["content", "sidecars"])


if __name__ == "__main__":
    unittest.main()
