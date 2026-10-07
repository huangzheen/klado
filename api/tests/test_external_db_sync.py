"""Saved database connections are private; the copy they produce is not.

⚠️ The product rule these tests exist to hold: *a connection belongs to one person.
The local COPY may be shared; the source credentials may never be.* Most of this file
is therefore about what CANNOT happen, because the feature works perfectly well without
those tests and keeps working perfectly well if they are deleted.

What is asserted here, and why each one is a real failure mode:

* **Every statement that touches a connection or a schedule filters on `owner_email`.**
  Checked over the module's SQL as a static invariant rather than by trying each
  endpoint as two users, because an endpoint added later would simply have no test.
  The insertion side is the exception and is spelled out: an INSERT supplies
  `owner_email` as a value, which is the same guarantee from the other direction.
* **No `admin` escape.** An operator administers accounts. Being one is not a reason
  to hold somebody's database password, so no public function here takes an `admin`
  flag — a second one added later is the hole this pins shut.
* **No response shape can carry a password.** `public_view` / `job_view` are the only
  two things the router hands out, so they are asserted key by key.
* **A refused import writes nothing.** The name-collision guard has to fire BEFORE
  the TRUNCATE, or it is a message rather than a protection.

`test_external_db_sync_ui.py` is the frontend half; the browser cannot see any of this.

    ../.venv312/bin/python -m unittest tests.test_external_db_sync
"""
import ast
import io
import re
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from services import data_center_db as db
from services import dataset_groups
from services import external_sync as es

MODULE_PATH = Path(es.__file__)


def _module_source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


def _function_node(name: str):
    for node in ast.parse(_module_source()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone — the test is checking nothing")


def _string_literals_in(name: str) -> list[str]:
    """Every string literal inside one function, docstring excluded.

    Excluding the docstring is the whole point: a function that explains 「there is no
    plaintext path here」 must not fail a check about plaintext paths.
    """
    node = _function_node(name)
    doc = ast.get_docstring(node, clean=False)
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if doc and sub.value == doc:
                continue
            out.append(sub.value)
    return out


def _code_without_docstring(name: str) -> str:
    """The function's source with its docstring and `#` comments removed.

    ⚠️ Needed because `run_due_jobs` *explains* the advisory lock in its docstring —
    a check for `pg_try_advisory_lock` over the raw text therefore passed against a
    function that had stopped taking it. Third time in this project that a rule was
    satisfied by the sentence describing it.
    """
    src = _module_source()
    node = _function_node(name)
    text = ast.get_source_segment(src, node) or ""
    doc = ast.get_docstring(node, clean=False)
    if doc:
        text = text.replace(doc, "")
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def _bound_names(func_node) -> set[str]:
    """Every name a function BINDS — including tuple unpacking.

    ⚠️ `_viewer` returns a pair, so the call site is `email, admin = _viewer(...)`,
    a `Tuple` target. Collecting only `ast.Name` targets sees nothing there, and the
    `admin` guard passed against a handler that bound `admin` on its very first line.
    """
    out: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                out |= _target_names(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            out |= _target_names(node.target)
    return out


def _target_names(target) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n for elt in target.elts for n in _target_names(elt)}
    return set()


class CredentialStorageTests(unittest.TestCase):
    def test_a_password_is_never_stored_in_the_clear(self):
        enc = es.encrypt_credential("s3cr3t")
        self.assertTrue(enc.startswith("enc:"))
        self.assertNotIn("s3cr3t", enc)

    def test_it_round_trips(self):
        self.assertEqual(es.decrypt_credential(es.encrypt_credential("s3cr3t")), "s3cr3t")

    def test_a_rotated_key_yields_nothing_rather_than_the_password(self):
        """⚠️ The dangerous alternative is returning the stored blob or raising, because
        the caller would then authenticate with it against a real database. Empty is
        the only answer that cannot log in anywhere."""
        from core.config import settings
        enc = es.encrypt_credential("s3cr3t")
        original = settings.SECRET_KEY
        try:
            settings.SECRET_KEY = "a-different-key"
            self.assertEqual(es.decrypt_credential(enc), "")
        finally:
            settings.SECRET_KEY = original

    def test_a_value_outside_our_envelope_is_refused_not_used_as_a_password(self):
        """`plain:…` is what the agent-code path degrades to when crypto is missing.
        Reading it here would turn a display fallback into a login attempt."""
        self.assertEqual(es.decrypt_credential("plain:guess"), "")
        self.assertEqual(es.decrypt_credential("enc:AAAA"), "")
        self.assertEqual(es.decrypt_credential(""), "")

    def test_a_blank_password_stores_nothing(self):
        self.assertEqual(es.encrypt_credential(""), "")

    def test_no_plaintext_fallback_exists_for_a_password(self):
        """`klado_shared/accounts.py::_encrypt_code` degrades to `plain:` when the
        crypto import fails. That is right for a display code and wrong here, so the
        absence of such a branch is itself pinned — a well-meaning copy of that
        function would put a database password in a dump.

        ⚠️ Matched over STRING LITERALS, not over text. Two narrower mistakes came
        first: a whole-module `assertNotIn` failed on the module docstring explaining
        the rule, and scoping it to the function body still failed on
        `def encrypt_credential(plain: str)` — the parameter name. A fallback branch
        is a literal that STARTS with `plain:`, and that is the only thing worth
        looking for.
        """
        for name in ("encrypt_credential", "decrypt_credential"):
            for literal in _string_literals_in(name):
                self.assertFalse(
                    literal.startswith("plain:"),
                    f"{name} builds a plaintext envelope {literal!r} — a database "
                    f"password must never have a degraded path")


class ResponseShapeTests(unittest.TestCase):
    """The two functions the router serialises. If a key is added here, it is now on
    the wire — so the assertion is about the whole key set, not about a substring."""

    def test_a_connection_view_carries_no_credential(self):
        view = es.public_view({
            "id": 1, "label": "prod", "host": "h", "port": 5432, "db_name": "d",
            "db_user": "u", "db_schema": "public", "password_enc": "enc:AAAA",
            "password": "hunter2", "created_at": None,
        })
        self.assertEqual(sorted(view), [
            "created_at", "database", "has_password", "host", "id", "label",
            "port", "schema", "user"])
        self.assertNotIn("hunter2", str(view))

    def test_the_job_view_carries_no_credential_either(self):
        job = es.job_view({
            "id": 1, "connection_id": 2, "slug": "s", "target_table": "t",
            "source_table": "o", "mode": "overwrite", "interval_minutes": 60,
            "enabled": True, "last_run_at": None, "last_status": "", "last_error": "",
            "last_rows": None, "connection_label": "prod", "password": "hunter2",
        })
        self.assertNotIn("password", str(sorted(job)))
        self.assertNotIn("hunter2", str(job))

    def test_a_failed_pull_records_the_type_not_a_connection_string(self):
        """A psycopg2 message can quote the server's error text, and this string is
        displayed. Truncation is the control; the type is what makes it useful."""
        with mock.patch.object(es, "connection_spec", return_value={"host": "h"}), \
             mock.patch.object(es.dataset_groups, "import_external_postgres",
                               side_effect=RuntimeError("x" * 5000)), \
             mock.patch.object(es, "_job_row", return_value={
                 "id": 9, "owner_email": "me", "connection_id": 1, "slug": "s",
                 "target_table": "t", "source_table": "o", "mode": "overwrite"}), \
             mock.patch.object(es.db, "get_pg_conn") as conn:
            conn.return_value.__enter__.return_value.cursor.return_value.__enter__ \
                .return_value = mock.MagicMock()
            out = es._execute({"id": 9, "owner_email": "me", "connection_id": 1,
                               "slug": "s", "target_table": "t", "source_table": "o",
                               "mode": "overwrite"})
        self.assertEqual(out["status"], "failed")
        self.assertLessEqual(len(out["error"]), 340)
        self.assertIn("RuntimeError", out["error"])


class IsolationInvariantTests(unittest.TestCase):
    """Static, because the endpoints that would break this are the ones not written yet.

    ⚠️ Read the module's SQL rather than its behaviour: a new endpoint with a missing
    `AND owner_email=%s` has no test to fail, but it does have a string literal.
    """

    def _sql_literals(self):
        """`(function_name, sql)` for every statement about a private table.

        ⚠️ Most of this module's SQL is an f-string, and `ast.Constant` sees only its
        PIECES — so the first version of this guard found two string constants (the
        table names) and reported "no SELECT found", which reads exactly like a
        passing test that quietly stopped checking. The table names are substituted
        from the module's own constants; anything else becomes `{?}`.
        """
        src = _module_source()
        tree = ast.parse(src)
        for func in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                    if text not in (es.CONN_TABLE, es.JOB_TABLE) and (
                            es.CONN_TABLE in text or es.JOB_TABLE in text):
                        yield func.name, text
                elif isinstance(node, ast.JoinedStr):
                    out = []
                    for part in node.values:
                        if isinstance(part, ast.Constant):
                            out.append(part.value)
                        elif isinstance(part, ast.FormattedValue) and \
                                isinstance(part.value, ast.Name) and part.value.id in (
                                    "CONN_TABLE", "JOB_TABLE"):
                            out.append(getattr(es, part.value.id))
                        else:
                            out.append("{?}")
                    text = "".join(out)
                    if es.CONN_TABLE in text or es.JOB_TABLE in text:
                        yield func.name, text

    # The ONE statement in the module that is deliberately not owner-filtered: the
    # scheduler has to find everybody's due jobs, because it is not acting as a user.
    # It is an allowlist of one, and the test below pins that it is still one — an
    # exception that can grow is not an exception.
    SYSTEM_READERS = {"due_jobs"}

    def test_every_statement_about_a_connection_or_a_schedule_names_the_owner(self):
        checked = 0
        exempt = set()
        for func, text in self._sql_literals():
            upper = text.upper()
            if "CREATE TABLE" in upper or "CREATE INDEX" in upper:
                continue          # DDL has no rows to filter
            if func in self.SYSTEM_READERS:
                exempt.add(func)
                continue
            checked += 1
            self.assertIn(
                "OWNER_EMAIL", upper,
                f"{func}() touches a private table without filtering by owner:\n"
                f"{text.strip()[:200]}")
        self.assertEqual(exempt, self.SYSTEM_READERS,
                         "the no-owner-filter allowlist changed — a system-level read "
                         "was added or removed, and that needs a decision, not a diff")
        # ⚠️ A guard that never sees a statement is a guard that cannot fail.
        self.assertGreaterEqual(checked, 10, "the invariant stopped seeing any SQL")

    def test_reads_writes_and_deletes_are_all_covered(self):
        """Belt and braces: the invariant above would be satisfied by ten SELECTs."""
        # ⚠️ Normalise whitespace first: the statements are indented multi-line
        # f-strings, so " SELECT " with real spaces never appears in them.
        joined = re.sub(r"\s+", " ", "\n".join(t for _, t in self._sql_literals())).upper()
        for verb in ("SELECT", "UPDATE", "DELETE", "INSERT"):
            self.assertIn(f" {verb} ", f" {joined} ", f"no {verb} found to check")

    def test_no_public_function_takes_an_admin_flag(self):
        """Operator rights are over ACCOUNTS. A second `admin=True` on this module is
        the hole that would hand every operator every saved password."""
        tree = ast.parse(_module_source())
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            args = [a.arg for a in node.args.args + node.args.kwonlyargs]
            self.assertNotIn("admin", args,
                             f"{node.name} takes an `admin` flag — an operator must not "
                             f"be able to read another person's database credential")

    def test_the_one_function_that_returns_a_password_is_named_as_such(self):
        """It exists, it is needed by the scheduler, and it is owner-filtered. Pinning
        the count means a second, less careful copy shows up as a failure."""
        src = _module_source()
        self.assertEqual(src.count("def connection_spec("), 1)
        self.assertIn("WHERE id=%s AND owner_email=%s", src)

    def test_the_tick_is_serialised_by_the_database(self):
        """Klado runs one uvicorn process today, so a second replica is a compose-file
        property rather than a code one. Two concurrent pulls would each TRUNCATE and
        rewrite the copy while somebody reads it."""
        tick = _code_without_docstring("run_due_jobs")
        self.assertIn("pg_try_advisory_lock", tick,
                      "the tick no longer takes a lock — a second replica would run "
                      "every pull a second time")
        self.assertIn("pg_advisory_unlock", tick, "the lock is never released")


class RouterIsolationTests(unittest.TestCase):
    """The HTTP surface. Same rule, checked where the rule can be broken by accident."""

    ROUTER = Path(db.__file__).resolve().parents[1] / "routers" / "data_center.py"

    def test_no_handler_binds_admin(self):
        """⚠️ `_viewer` returns `(email, is_admin)`. Binding the second name anywhere in
        these handlers is the mistake this exists to catch — it reads as harmless
        because other routes in this file legitimately do use it, and the call site is
        a TUPLE assignment, which a name-only check does not see."""
        tree = ast.parse(self.ROUTER.read_text(encoding="utf-8"))
        names = {"list_db_connections", "create_db_connection", "update_db_connection",
                 "delete_db_connection", "list_db_sync_jobs", "create_db_sync_job",
                 "update_db_sync_job", "delete_db_sync_job", "run_db_sync_job"}
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in names:
                found.add(node.name)
                bound = _bound_names(node) | {a.arg for a in node.args.args}
                self.assertNotIn("admin", bound,
                                 f"{node.name} binds `admin` — an operator must not "
                                 f"reach another person's connection")
        self.assertEqual(found, names, "a handler was renamed or removed; the list of "
                                       "protected routes must be updated deliberately")

    def test_every_handler_takes_the_owner_from_the_session(self):
        src = self.ROUTER.read_text(encoding="utf-8")
        block = src.split("class ConnectionSave", 1)
        self.assertEqual(len(block), 2, "the saved-connection routes are gone")
        body = block[1].split("# ─── 数据集预览")[0]
        self.assertEqual(body.count("_viewer(request)"), 9,
                         "a handler stopped resolving the caller, or one was added "
                         "without the count being updated deliberately")
        # ⚠️ Every one of them must hand `email` on. A handler that resolves the
        # caller and then ignores it is the shape this rule actually fails in.
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name in {
                    "list_db_connections", "create_db_connection", "update_db_connection",
                    "delete_db_connection", "list_db_sync_jobs", "create_db_sync_job",
                    "update_db_sync_job", "delete_db_sync_job", "run_db_sync_job"}:
                calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
                passes_owner = any(
                    isinstance(c.func, ast.Attribute) and c.func.attr in {
                        "list_connections", "save_connection", "delete_connection",
                        "list_jobs", "create_job", "update_job", "delete_job", "run_job"}
                    and c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "email"
                    for c in calls)
                self.assertTrue(passes_owner,
                                f"{node.name} does not pass the caller as the owner")


class RefusedImportWritesNothingTests(unittest.TestCase):
    """The guard in `write_member_table`, with the database stubbed out.

    ⚠️ The order is the whole point: a collision that is detected AFTER the TRUNCATE
    is a message, not a protection. So these tests stub the write and assert it was
    never reached — the same reason the CI suite cannot simply call the real function
    (there is no database in CI).
    """

    def setUp(self):
        self.g = {"id": 11, "slug": "container-a"}
        patcher = mock.patch.object(dataset_groups, "_require_group", return_value=self.g)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dataset_groups, "ensure_schema")
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dataset_groups, "get_group_by_id",
                                    return_value={"slug": "container-b"})
        patcher.start(); self.addCleanup(patcher.stop)

    def _dataset(self, group_id):
        return {"table_name": "orders", "owner_email": "me@example.test",
                "dataset_group_id": group_id}

    def test_a_name_taken_by_another_container_is_refused_before_any_write(self):
        columns = [("id", "INTEGER")]
        records = [{"id": 1}]
        with mock.patch.object(db, "get_dataset", return_value=self._dataset(22)), \
             mock.patch.object(db, "insert_pg_data") as insert, \
             mock.patch.object(db, "get_pg_conn") as conn:
            with self.assertRaises(dataset_groups.GroupError) as ctx:
                dataset_groups.write_member_table("container-a", "orders", columns,
                                                  records, mode="overwrite",
                                                  owner_email="me@example.test")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("container-b", str(ctx.exception), "the message must say where it is")
        insert.assert_not_called()          # ⚠️ the load-bearing assertion
        conn.assert_not_called()            # no re-file UPDATE either

    def test_a_loose_table_is_refused_rather_than_silently_emptied(self):
        """Nothing signals this loss: the table never appears anywhere new, so
        `overwrite` just empties a table the person may not have been looking at."""
        with mock.patch.object(db, "get_dataset", return_value=self._dataset(None)), \
             mock.patch.object(db, "insert_pg_data") as insert, \
             mock.patch.object(db, "get_pg_conn") as conn:
            with self.assertRaises(dataset_groups.GroupError) as ctx:
                dataset_groups.write_member_table("container-a", "orders",
                                                  [("id", "INTEGER")], [{"id": 1}],
                                                  mode="overwrite",
                                                  owner_email="me@example.test")
        self.assertEqual(ctx.exception.status, 409)
        insert.assert_not_called()
        conn.assert_not_called()

    def test_refreshing_the_same_table_in_the_same_container_still_works(self):
        """⚠️ This is the path a scheduled pull runs every cycle. A guard that refused
        it would make the feature impossible rather than safe — so the guard has to
        let it through, and this is the test that says so."""
        written = {}
        with mock.patch.object(db, "get_dataset", return_value=self._dataset(11)), \
             mock.patch.object(db, "insert_pg_data",
                               side_effect=lambda n, r, **k: written.setdefault("rows", len(r))), \
             mock.patch.object(db, "get_pg_conn") as conn:
            out = dataset_groups.write_member_table("container-a", "orders",
                                                    [("id", "INTEGER")],
                                                    [{"id": 1}, {"id": 2}],
                                                    mode="overwrite",
                                                    owner_email="me@example.test")
        self.assertFalse(out["created"], "the table already existed")
        self.assertEqual(written["rows"], 2)
        conn.assert_called()

    def test_somebody_elses_table_is_left_to_the_existing_owner_check(self):
        """The cross-OWNER case is already refused by `db._check_import_owner` with a
        message that is tested elsewhere. This guard must not swallow it into a
        different one, or it would be reporting a different problem."""
        with mock.patch.object(db, "get_dataset",
                               return_value=self._dataset(22) | {"owner_email": "them@example.test"}), \
             mock.patch.object(db, "insert_pg_data") as insert, \
             mock.patch.object(db, "get_pg_conn"):
            # Falls through to the write, where the real owner check lives.
            dataset_groups.write_member_table("container-a", "orders",
                                              [("id", "INTEGER")], [{"id": 1}],
                                              mode="overwrite",
                                              owner_email="me@example.test")
        insert.assert_called_once()


class ScheduleArithmeticTests(unittest.TestCase):
    def test_the_interval_is_bounded(self):
        self.assertEqual(es._clamp_interval(60), 60)
        self.assertEqual(es._clamp_interval("1440"), 1440)
        for bad in (0, 1, -5, 10 ** 9):
            with self.assertRaises(es.SyncError, msg=bad):
                es._clamp_interval(bad)

    def test_a_non_numeric_interval_is_refused_not_coerced(self):
        with self.assertRaises(es.SyncError):
            es._clamp_interval("daily")

    def test_the_next_run_is_the_one_the_scheduler_will_actually_use(self):
        """⚠️ One rule, one place — and the arithmetic has to match `due_jobs()`.

        The scheduler asks for a job when
        `last_run_at <= NOW() - interval` (or `created_at` for one that has never
        run), so the next run is that same anchor plus the interval. Computing it in
        the browser instead would show "in 23 hours" for a schedule created 25 hours
        ago that has never run — it is due now, and the row would be lying about the
        one number a person reads to decide whether to trust it.
        """
        from datetime import timedelta
        now = datetime(2026, 10, 6, 9, 0, 0)
        self.assertEqual(
            es._next_run_at({"enabled": True, "interval_minutes": 60,
                             "last_run_at": now}),
            (now + timedelta(minutes=60)).isoformat())
        # Never run: the anchor is `created_at`, and the result is in the PAST —
        # which is correct, and is exactly the case the browser gets wrong.
        stale = es._next_run_at({"enabled": True, "interval_minutes": 1440,
                                 "last_run_at": None, "created_at": now - timedelta(days=2)})
        self.assertEqual(stale, (now - timedelta(days=2) + timedelta(days=1)).isoformat())
        self.assertLess(datetime.fromisoformat(stale), now)

    def test_a_paused_schedule_has_no_next_run(self):
        """A time it will never keep is worse than no time."""
        self.assertIsNone(es._next_run_at({"enabled": False, "interval_minutes": 60,
                                           "last_run_at": datetime(2026, 10, 6, 9, 0)}))

    def test_a_schedule_with_no_anchor_at_all_has_no_next_run(self):
        # A row that somehow has neither timestamp: say so rather than raise.
        self.assertIsNone(es._next_run_at({"enabled": True, "interval_minutes": 60}))

    def test_the_view_carries_it_and_still_carries_no_credential(self):
        view = es.job_view({
            "id": 1, "connection_id": 2, "slug": "s", "target_table": "t",
            "source_table": "s", "mode": "overwrite", "interval_minutes": 1440,
            "enabled": True, "last_run_at": datetime(2026, 10, 6, 9, 0),
        })
        self.assertTrue(view["next_run_at"])
        self.assertNotIn("password", view)
        self.assertNotIn("host", view)

    def test_a_job_runs_as_its_owner(self):
        """⚠️ The pull has to authenticate as the person who saved the connection, and
        the resulting copy has to be filed under them too. Passing a different email
        anywhere here is what would put one person's rows in another person's dataset.
        """
        seen = {}

        def fake_import(spec, slug, table_name, source_table, mode="overwrite",
                        owner_email=None):
            seen.update(spec=spec, slug=slug, table=table_name, source=source_table,
                        mode=mode, owner=owner_email)
            return {"rows": 7}

        with mock.patch.object(es, "connection_spec", return_value={"password": "p"}), \
             mock.patch.object(es.dataset_groups, "import_external_postgres",
                               side_effect=fake_import), \
             mock.patch.object(es.db, "get_pg_conn") as conn:
            conn.return_value.__enter__.return_value.cursor.return_value.__enter__ \
                .return_value = mock.MagicMock()
            out = es._execute({"id": 3, "owner_email": "me@example.test",
                               "connection_id": 2, "slug": "c", "target_table": "t",
                               "source_table": "o", "mode": "overwrite"})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["rows"], 7)
        self.assertEqual(seen["owner"], "me@example.test")
        self.assertEqual(seen["mode"], "overwrite")
        self.assertEqual(seen["table"], "t")

    def test_the_credential_reaches_the_connection_but_not_the_result(self):
        with mock.patch.object(es, "connection_spec",
                               return_value={"host": "h", "password": "hunter2"}), \
             mock.patch.object(es.dataset_groups, "import_external_postgres",
                               return_value={"rows": 1}), \
             mock.patch.object(es.db, "get_pg_conn") as conn:
            conn.return_value.__enter__.return_value.cursor.return_value.__enter__ \
                .return_value = mock.MagicMock()
            out = es._execute({"id": 4, "owner_email": "me@example.test",
                               "connection_id": 2, "slug": "c", "target_table": "t",
                               "source_table": "o", "mode": "overwrite"})
        self.assertNotIn("hunter2", str(out))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
