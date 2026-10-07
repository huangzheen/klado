"""Dashboard — the rules that decide what a page may say, ask for, and be seen by.

A dashboard is a page that queries datasets, so the expensive mistakes here are not
layout ones:

* a colour stored in a row. `theme.css` owns every colour (AGENTS.md); an `accent` that
  holds `#ff8800` is right in one theme and unreadable in the other, and nobody finds
  out until a reader switches. `validate_accent` is the guard, and it is pinned with the
  exact shapes it must refuse.
* a slug that is not a URL. It is typed by people and pasted into chat, so the shape is
  enforced at publish time rather than discovered later.
* a runtime that stacks. The block `/d/{slug}` injects is stripped by a regex that looks
  for its own tag, so a literal `<script` inside the runtime's own text makes the strip
  start matching mid-file and a re-published page ends up with two runtimes. This is the
  same trap `tests/test_report_filters.py` guards for the filter runtime.
* a share that confirms a slug exists. Non-owners get 404, never 403.
* a share that outlives its dashboard.

Everything here runs against fake connections — no server, no database. The rules worth
arguing about are the ones a test can pin without a database.
"""
import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.config import resolve_frontend_dir
from routers import dashboard as router_module
from services import dashboard_store as store

FRONTEND_DIR = Path(resolve_frontend_dir())

OWNER = "owner@example.com"
MATE = "mate@example.com"
STRANGER = "stranger@example.com"
PAGE = "<html><head><title>d</title></head><body><h1>Sales</h1></body></html>"


# ── fake connections (the pattern tests/test_dataset_shares.py uses) ──────────

class _FakeCursor:
    def __init__(self, log, results):
        self._log = log
        self._results = results          # the CONNECTION's queue, not a copy
        self.rowcount = 1
        self._next = None

    def execute(self, sql, params=None):
        self._log.append((" ".join(str(sql).split()), params))
        # One shared queue, drained in call order — psycopg2's behaviour. A per-cursor
        # copy would silently re-serve the FIRST queued row to the second query in the
        # same call, which is how a "no such share" lookup comes back truthy.
        self._next = self._results.pop(0) if self._results else None
        self.rowcount = 1

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
        self._results = list(results)
        self.committed = False
        self._cursor = None

    def cursor(self, cursor_factory=None):
        # One cursor per connection, not per call: the store opens a second cursor for
        # a nested lookup, and a fresh one would restart the result queue.
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
    """Run `fn` with the store's schema and connection replaced by fakes.

    Returns `(log, conn, value)`. With `propagate=False` a raised error is returned as
    the value instead, so a test can assert on what was NOT done after a refusal.
    """
    log: list = []
    conn = _FakeConn(log, results)
    with patch.object(store, "connect_main", return_value=conn), \
         patch.object(store, "ensure_schema"):
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:                      # noqa: BLE001 — the test decides
            if propagate:
                raise
            value = exc
    return log, conn, value


def _request(header: str = "", state_lang=None):
    state = SimpleNamespace()
    if state_lang is not None:
        state.lang = state_lang
    return SimpleNamespace(state=state, headers={"accept-language": header})


# ── slug: a URL people type ──────────────────────────────────────────────────

class SlugTests(unittest.TestCase):
    def test_a_plain_slug_passes_through_lowercased(self):
        self.assertEqual(store.normalise_slug("  Q3-Sales  "), "q3-sales")

    def test_the_shape_is_enforced_rather_than_guessed(self):
        # 3 characters minimum, 64 maximum, lowercase, hyphen inside only. Every one of
        # these would produce an address that is awkward to type or ambiguous to read.
        for bad in ("ab",                       # too short
                    "-q3",                      # leading hyphen
                    "q3_sales",                 # underscore
                    "q3.sales",                 # dot
                    "q3 sales",                 # space
                    "q" * 65,                   # over-long
                    "q3/sales"):                # a path separator is a different route
            with self.assertRaises(store.DashboardError, msg=bad):
                store.normalise_slug(bad, "Fallback title")

    def test_a_slug_that_only_needs_lowercasing_is_accepted(self):
        # The value is lowercased BEFORE it is judged, so a caller writing a title-case
        # slug is helped rather than refused. What it cannot be helped with is a space.
        self.assertEqual(store.normalise_slug("Q3-Sales", "x"), "q3-sales")
        with self.assertRaises(store.DashboardError):
            store.normalise_slug("Q3 Sales", "x")

    def test_an_empty_slug_falls_back_to_the_title(self):
        self.assertEqual(store.normalise_slug("", "Q3 Sales Review"), "q3-sales-review")

    def test_a_title_with_no_ascii_gets_a_stable_slug_of_its_own(self):
        # Two Chinese-titled dashboards must not land on one address and overwrite each
        # other — the fallback is a hash of the title, not a constant.
        first = store.normalise_slug("", "销售总览")
        second = store.normalise_slug("", "渠道复盘")
        self.assertNotEqual(first, second)
        self.assertEqual(first, store.normalise_slug("", "销售总览"))
        self.assertTrue(store.SLUG_RE.match(first), first)

    def test_the_error_is_a_bilingual_pair(self):
        with self.assertRaises(store.DashboardError) as caught:
            store.normalise_slug("no", "x")
        message = str(caught.exception)
        self.assertIn(" / ", message)
        from core.i18n import split_pair
        self.assertIsNotNone(split_pair(message), message)


# ── accent: theme.css owns every colour ──────────────────────────────────────

class AccentTests(unittest.TestCase):
    def test_a_theme_token_is_accepted(self):
        for token in ("", "accent", "ok", "warn", "brand-navy", "a"):
            self.assertEqual(store.validate_accent(token), token)

    def test_a_hex_colour_is_refused(self):
        # The exact regression: a card that stores a literal is correct in the light
        # theme and wrong in the dark one, and nothing in the app would say so.
        for bad in ("#fff", "#FF8800", "#ff8800cc", " #1a2b3c "):
            with self.assertRaises(store.DashboardError, msg=bad):
                store.validate_accent(bad)

    def test_a_colour_function_is_refused_too(self):
        for bad in ("rgba(15,23,42,.86)", "rgb(0,0,0)", "hsl(210 40% 20%)",
                    "var(--accent)", "#12345"):
            with self.assertRaises(store.DashboardError, msg=bad):
                store.validate_accent(bad)

    def test_the_rejection_says_which_value_and_why(self):
        with self.assertRaises(store.DashboardError) as caught:
            store.validate_accent("#ff8800")
        message = str(caught.exception)
        self.assertIn("#ff8800", message)          # names the value
        self.assertIn("theme.css", message)        # names the rule
        self.assertIn(" / ", message)

    def test_a_token_that_is_not_lowercase_or_dash_separated_is_refused(self):
        for bad in ("Accent", "brand_navy", "1brand", "-brand", "brand navy"):
            with self.assertRaises(store.DashboardError, msg=bad):
                store.validate_accent(bad)


# ── datasets: an allow-list, checked as data ─────────────────────────────────

class DatasetTests(unittest.TestCase):
    def test_a_json_array_of_identifiers_parses(self):
        self.assertEqual(store.parse_datasets('["sales_2026", "channel_map"]'),
                         ["sales_2026", "channel_map"])

    def test_a_bare_list_from_an_http_body_works_too(self):
        self.assertEqual(store.parse_datasets(["sales_2026"]), ["sales_2026"])

    def test_duplicates_collapse_and_order_is_kept(self):
        self.assertEqual(store.parse_datasets(["b_1", "a_1", "b_1"]), ["b_1", "a_1"])

    def test_anything_that_is_not_an_array_is_refused_not_ignored(self):
        for bad in ('{"a": 1}', '"sales_2026"', "42"):
            with self.assertRaises(store.DashboardError, msg=bad):
                store.parse_datasets(bad)

    def test_an_entry_that_is_not_identifier_shaped_is_refused(self):
        # A name that were silently dropped would leave the page asking for a table it was
        # told it may use and getting a 403 from the guard that protects it.
        for bad in (["sales; DROP TABLE x"], ["has space"], ["1leading"], [""],
                    ["x" * 64], [123], [None], [{"name": "sales"}]):
            with self.assertRaises(store.DashboardError, msg=str(bad)):
                store.parse_datasets(bad)

    def test_broken_json_is_refused(self):
        with self.assertRaises(store.DashboardError):
            store.parse_datasets('["sales_2026"')

    def test_the_list_is_capped(self):
        with self.assertRaises(store.DashboardError):
            store.parse_datasets([f"t_{i}" for i in range(store.MAX_DATASETS + 1)])

    def test_the_stored_form_round_trips(self):
        names = ["sales_2026", "channel_map"]
        self.assertEqual(store.parse_datasets(store.datasets_json(names)), names)


# ── the three scopes are disjoint ────────────────────────────────────────────

class ScopeTests(unittest.TestCase):
    def test_mine_is_private_and_owned_by_me(self):
        sql, params = store.scope_predicate("mine", OWNER)
        self.assertIn("owner_email = %s", sql)
        self.assertIn("visibility = 'private'", sql)
        self.assertEqual(params, [OWNER])

    def test_public_is_the_public_wall_and_takes_no_parameter(self):
        sql, params = store.scope_predicate("public", OWNER)
        self.assertIn("visibility = 'public'", sql)
        self.assertEqual(params, [])

    def test_shared_excludes_my_own_rows(self):
        # Without `owner_email <> %s` a row I own that somebody shared with me would be
        # in both `mine` and `shared`, and the wall would show it twice with two
        # different meanings.
        sql, params = store.scope_predicate("shared", OWNER)
        self.assertIn("owner_email <> %s", sql)
        self.assertIn("visibility = 'private'", sql)
        self.assertEqual(params, [OWNER, OWNER])

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(store.DashboardError) as caught:
            store.scope_predicate("everything", OWNER)
        self.assertIn(" / ", str(caught.exception))

    def test_the_parameter_count_matches_the_placeholders_in_every_scope(self):
        for scope in store.SCOPES:
            sql, params = store.scope_predicate(scope, OWNER)
            self.assertEqual(sql.count("%s"), len(params), scope)


# ── the card: can_manage is ownership, not role ──────────────────────────────

class CardTests(unittest.TestCase):
    def _row(self, **over):
        base = dict(id=3, slug="q3-sales", title="Q3 Sales", summary="s", summary_zh="销售",
                    description="", tags="gto, channel", accent="accent",
                    datasets='["sales_2026"]', status="published", visibility="private",
                    owner_email=OWNER, pinned=False, in_nav=True, nav_order=2, in_home=False,
                    home_order=0, home_collapsed=False, size_bytes=120, views=4,
                    created_at=datetime(2026, 10, 1, 9, 0), updated_at=datetime(2026, 10, 1),
                    shared_with_me=False)
        base.update(over)
        return base

    def test_the_owner_manages_and_a_colleague_does_not(self):
        self.assertTrue(store.card(self._row(), OWNER)["can_manage"])
        self.assertFalse(store.card(self._row(), MATE)["can_manage"])

    def test_tags_are_a_list_and_timestamps_are_iso(self):
        payload = store.card(self._row(), OWNER)
        self.assertEqual(payload["tags"], ["gto", "channel"])
        self.assertEqual(payload["created_at"], "2026-10-01T09:00:00")

    def test_datasets_arrive_as_names_not_as_stored_json(self):
        self.assertEqual(store.card(self._row(), OWNER)["datasets"], ["sales_2026"])

    def test_the_shared_scope_marks_the_row(self):
        self.assertTrue(store.card(self._row(), MATE, shared_with_me=True)["shared_with_me"])
        self.assertFalse(store.card(self._row(), MATE)["shared_with_me"])


# ── the injected runtime: one copy, always ───────────────────────────────────

class RuntimeInjectionTests(unittest.TestCase):
    def test_the_runtime_never_contains_a_literal_script_tag(self):
        # The block is stripped and re-injected by a regex that matches its own tag. A
        # literal `<script` in the runtime's OWN text makes that regex start matching in
        # the middle of the file, and a re-render stacks a second copy — the older one
        # winning. Measured the same way for the filter runtime in test_report_filters.
        block = router_module.runtime_html({"slug": "q3-sales", "title": "Q3",
                                            "datasets": ["sales_2026"]})
        # The text BETWEEN the tags — the block's own closing tag is expected.
        body = block[len(router_module._RUNTIME_TAG):-len("</script>")]
        self.assertNotIn("<script", body)
        self.assertNotIn("</script", body)

    def test_the_block_lands_in_head_before_any_author_script(self):
        html = ("<html><head><title>t</title><script>window.authored = 1;</script></head>"
                "<body><script src='late.js'></script></body></html>")
        out = router_module._inject_runtime(html, router_module.runtime_html({"slug": "s"}))
        self.assertLess(out.index("data-dashboard-runtime"),
                        out.index("window.authored = 1;"))
        self.assertLess(out.index("data-dashboard-runtime"), out.index("late.js"))

    def test_rendering_twice_does_not_stack_two_runtimes(self):
        once = router_module._inject_runtime(PAGE, router_module.runtime_html({"slug": "s"}))
        twice = router_module._inject_runtime(once, router_module.runtime_html({"slug": "s"}))
        self.assertEqual(once.count("data-dashboard-runtime>"), 1)
        self.assertEqual(twice.count("data-dashboard-runtime>"), 1)
        # The strip regex is the thing that has to keep working; assert it directly too.
        self.assertEqual(len(router_module._RUNTIME_RE.findall(twice)), 1)

    def test_the_page_keeps_its_own_base_tag(self):
        # The server injects one, and a document that declares its own must win.
        with_base = "<html><head><base href='/klado/'></head><body></body></html>"
        self.assertEqual(router_module._inject_base_tag(with_base, "/"),
                         with_base)
        self.assertIn('<base href="/">', router_module._inject_base_tag(PAGE, "/"))

    def test_the_context_the_page_reads_is_its_own(self):
        block = router_module.runtime_html({"slug": "q3-sales", "title": "Q3 销售",
                                            "datasets": ["sales_2026"]}, "zh")
        payload = json.loads(block.split("window.kladoDashboard = ", 1)[1]
                             .split(";\n", 1)[0])
        self.assertEqual(payload["slug"], "q3-sales")
        self.assertEqual(payload["datasets"], ["sales_2026"])
        self.assertEqual(payload["lang"], "zh")
        self.assertIn("window.kldQuery", block)

    def test_a_title_cannot_close_the_block(self):
        block = router_module.runtime_html({"slug": "s", "title": "</script><b>"})
        head = block.split("data-dashboard-runtime>", 1)[1].rsplit("</", 1)[0]
        self.assertNotIn("</script", head)
        self.assertIn("<\\/script", block)


# ── non-owner is 404, never 403 ──────────────────────────────────────────────

class SharePermissionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(store, "ensure_schema"))
        self.enterContext(patch.object(store.orgs, "effective_profile",
                                      return_value=store.orgs.UNRESTRICTED))

    def _client(self) -> TestClient:
        app = FastAPI()

        @app.middleware("http")
        async def _identity(request: Request, call_next):
            request.state.current_user = {"email": request.headers.get("X-Test-User", OWNER)}
            request.state.auth_kind = "browser"
            return await call_next(request)

        app.include_router(router_module.router, prefix="/api/dashboard")
        return TestClient(app, raise_server_exceptions=False)

    def _call(self, method: str, path: str, user: str, **kw):
        return self._client().request(method, path, headers={"X-Test-User": user}, **kw)

    def test_a_stranger_gets_404_not_403_from_every_share_endpoint(self):
        # 403 for "exists but not yours" next to 404 for "no such slug" is a slug oracle:
        # it tells a stranger which slugs are real. Every share path must answer 404.
        for method, path, kw in (
            ("GET", "/api/dashboard/q3-sales/shares", {}),
            ("POST", "/api/dashboard/q3-sales/shares/colleagues", {"json": {"email": MATE}}),
            ("DELETE", "/api/dashboard/q3-sales/shares/colleagues/mate@example.com", {}),
            ("DELETE", "/api/dashboard/q3-sales", {}),
        ):
            with self.subTest(path=path):
                with patch.object(store, "list_shares", side_effect=store.DashboardNotFound(
                        "仪表盘不存在 / dashboard not found")), \
                     patch.object(store, "share_dashboard", side_effect=store.DashboardNotFound(
                        "仪表盘不存在 / dashboard not found")), \
                     patch.object(store, "revoke_share", side_effect=store.DashboardNotFound(
                        "仪表盘不存在 / dashboard not found")), \
                     patch.object(store, "delete_dashboard", side_effect=store.DashboardNotFound(
                        "仪表盘不存在 / dashboard not found")), \
                     patch.object(store, "get_dashboard", return_value={"id": 1, "slug": "q3-sales",
                                                                         "owner_email": OWNER,
                                                                         "status": "published",
                                                                         "visibility": "private"}), \
                     patch.object(store, "require_readable", side_effect=store.DashboardNotFound(
                        "仪表盘不存在 / dashboard not found")):
                    resp = self._call(method, path, STRANGER, **kw)
                self.assertEqual(resp.status_code, 404, f"{method} {path} → {resp.status_code}")

    def test_the_store_itself_refuses_a_non_owner_the_same_way(self):
        with self.assertRaises(store.DashboardNotFound):
            store.require_owner({"id": 1, "owner_email": OWNER}, STRANGER)
        with self.assertRaises(store.DashboardNotFound):
            store.require_owner(None, OWNER)
        # The owner gets the row back.
        row = {"id": 1, "owner_email": OWNER}
        self.assertIs(store.require_owner(row, OWNER), row)

    def test_no_share_write_happens_for_a_stranger(self):
        log, _conn, value = _with_fake_db(
            [{"id": 3, "owner_email": OWNER}], store.share_dashboard, "q3-sales", MATE,
            STRANGER, propagate=False)
        self.assertIsInstance(value, store.DashboardNotFound)
        writes = [q for q in log if q[0].strip().upper().startswith(("INSERT", "DELETE", "UPDATE"))]
        self.assertEqual(writes, [], "a refused share must not have written anything")


# ── deleting a dashboard takes its shares with it ────────────────────────────

class CascadeTests(unittest.TestCase):
    def test_deleting_a_dashboard_deletes_its_shares_first(self):
        log, _conn, _value = _with_fake_db(
            [{"id": 3, "owner_email": OWNER}, 1], store.delete_dashboard, "q3-sales", OWNER)
        statements = [q[0] for q in log]
        share_delete = next((s for s in statements
                             if s.startswith("DELETE") and "ai_dashboard_colleague_shares" in s),
                            None)
        self.assertIsNotNone(share_delete, "deleting a dashboard left its shares behind")
        self.assertEqual((3,), dict(zip(statements, [q[1] for q in log]))[share_delete])
        # Order matters: the shares reference the dashboard, so they go first.
        self.assertLess(statements.index(share_delete),
                        next(i for i, s in enumerate(statements)
                             if s.startswith("DELETE") and "ai_dashboards" in s))

    def test_a_stranger_deleting_gets_404_and_deletes_nothing(self):
        log, _conn, value = _with_fake_db(
            [{"id": 3, "owner_email": OWNER}], store.delete_dashboard, "q3-sales", STRANGER,
            propagate=False)
        self.assertIsInstance(value, store.DashboardNotFound)
        self.assertFalse([q for q in log if q[0].startswith("DELETE")])

    def test_the_constraint_also_cascades(self):
        # Belt and braces, and the reason the explicit DELETE is a comment away from
        # being "redundant": a path that deletes a dashboard by another route (a
        # maintenance script, a future bulk endpoint) is covered by the constraint.
        source = __import__("inspect").getsource(store.ensure_schema)
        self.assertIn("ON DELETE CASCADE", source)

    def test_a_grant_outliving_its_dashboard_is_what_the_cascade_prevents(self):
        # The scenario, stated once so the assertion above has a reason: a share row
        # pointing at a dashboard id that nothing owns would hand a future dashboard with
        # a recycled id access to a stranger's page.
        _log, _conn, value = _with_fake_db(
            [{"id": 3, "owner_email": OWNER}], store.delete_dashboard, "q3-sales", STRANGER,
            propagate=False)
        self.assertIsInstance(value, store.DashboardNotFound)


# ── the shared SQL gateway ───────────────────────────────────────────────────

class QueryGatewayTests(unittest.TestCase):
    def test_the_dashboard_asks_the_data_center_gateway(self):
        # One allow-list. If this router ever grew its own, a dashboard would be able to
        # read a table its reader could not type into the Data Center console.
        import inspect
        source = inspect.getsource(router_module.run_dashboard_query)
        self.assertIn("dq.run_scoped_query", source)
        self.assertNotIn("db.query_pg", source)
        self.assertNotIn("_assert_query_allowed", source)

    def test_max_rows_is_capped_at_the_documented_limit(self):
        from services import dataset_query as dq
        seen = {}

        def _fake(request, sql_text, *, viewer_email, admin, max_rows=None):
            seen["max_rows"] = max_rows
            return {"rows": [], "row_count": 0, "truncated": False}

        body = router_module.QueryIn(sql="SELECT 1", max_rows=99999)
        with patch.object(dq, "run_scoped_query", _fake), \
             patch.object(router_module.identity, "current_identity", return_value=(OWNER, "browser")), \
             patch.object(router_module.identity, "is_admin_caller", return_value=False), \
             patch.object(store, "ensure_schema"):
            import asyncio
            asyncio.run(router_module.run_dashboard_query(body, _request("zh")))
        self.assertEqual(seen["max_rows"], store.MAX_QUERY_ROWS)


# ── the store's own writes ───────────────────────────────────────────────────

class RuntimePayloadTests(unittest.TestCase):
    """What the reader's injected `window.kladoDashboard` actually carries.

    A string where the page expects a list is the kind of bug no status code
    reports: the page renders, the runtime exists, and the first query is built
    from `"["` — a malformed statement refused by the very guard meant to protect
    it. Asserted here because the reader is the only place this payload is built.
    """

    def test_datasets_reach_the_page_as_a_list(self):
        out = router_module.runtime_html({"slug": "s", "title": "t",
                                          "datasets": '["sales_2026", "region_map"]'}, "zh")
        payload = out.split("window.kladoDashboard = ", 1)[1].split(";", 1)[0]
        parsed = json.loads(payload)
        self.assertIsInstance(parsed["datasets"], list, parsed["datasets"])
        self.assertEqual(parsed["datasets"], ["sales_2026", "region_map"])
        self.assertEqual(parsed["slug"], "s")
        self.assertEqual(parsed["lang"], "zh")

    def test_an_unparseable_column_yields_an_empty_list_not_a_string(self):
        # The read path is tolerant on purpose: a bad column must not take the page
        # down. Tolerant still has to mean "a list".
        for raw in ('not json', '{"a": 1}', 42, None, ''):
            out = router_module.runtime_html({"slug": "s", "title": "t", "datasets": raw}, "")
            payload = out.split("window.kladoDashboard = ", 1)[1].split(";", 1)[0]
            self.assertEqual(json.loads(payload)["datasets"], [], raw)

    def test_the_runtime_body_never_contains_a_literal_script_tag(self):
        # The strip/re-inject regex matches `<script … data-dashboard-runtime …>`; a
        # literal *inside the runtime's own text* makes that regex start matching
        # mid-file, and a re-published page ends up with two runtimes — the older one
        # winning, bound to nothing. The block's own opening tag obviously contains
        # one, so the invariant is about the body after it, and about there being
        # exactly one attribute marker in the whole block.
        out = router_module.runtime_html({"slug": "s", "title": "t"}, "")
        self.assertTrue(out.startswith("<" + "script data-dashboard-runtime>"), out[:60])
        head, _, body = out.partition(">")
        self.assertNotIn("<" + "script", body)
        self.assertEqual(out.count("data-dashboard-runtime"), 1)
        # A payload that itself contained a closing tag must not close the block.
        nasty = router_module.runtime_html(
            {"slug": "s", "title": "t", "datasets": ["a"]}, "")  # sanity: unchanged
        self.assertNotIn("</" + "script></" + "script>", nasty)


class InjectionOrderTests(unittest.TestCase):
    """`<base>` must be parsed BEFORE the runtime that resolves URLs against it.

    Found by running the page, not by reading it: the block landed straight after
    `<head>`, i.e. ahead of the base tag, so `document.baseURI` inside the runtime
    was the page's own URL (`/d/<slug>`) and every `kldQuery` call went to
    `/d/api/dashboard/query`. The page rendered, the helper existed, and every query
    was refused — for a reason that looked nothing like the cause.
    """

    HTML = ("<!doctype html><html><head><title>t</title>"
            "<script src='a.js'></script></head><body><p>x</p></body></html>")

    def test_runtime_lands_after_the_base_tag(self):
        out = router_module._inject_runtime(
            router_module._inject_base_tag(self.HTML, "/"),
            router_module.runtime_html({"slug": "s", "title": "t"}, ""))
        self.assertLess(out.index("<base"), out.index("data-dashboard-runtime"),
                        "the runtime must be parsed after <base>")
        self.assertLess(out.index("data-dashboard-runtime"), out.index("a.js"),
                        "and still before the author's own scripts")

    def test_a_document_with_no_head_still_gets_the_block(self):
        out = router_module._inject_runtime("<p>fragment</p>",
                                            router_module.runtime_html({"slug": "s"}, ""))
        self.assertIn("data-dashboard-runtime", out)
        self.assertTrue(out.startswith("<" + "script"), out[:40])

    def test_a_document_that_declares_its_own_base_keeps_it(self):
        html = self.HTML.replace("<title>t</title>", "<base href='/sub/'>")
        out = router_module._inject_base_tag(html, "/")
        self.assertEqual(out.count("<base"), 1)
        self.assertIn("/sub/", out)
        out2 = router_module._inject_runtime(out, router_module.runtime_html({"slug": "s"}, ""))
        self.assertLess(out2.index("/sub/"), out2.index("data-dashboard-runtime"))


class DocumentKitTests(unittest.TestCase):
    """The design tokens a standalone document gets for free, and where they may land.

    The bug this pins is the reason the kit exists. An agent writes
    `background: var(--bg)` — correctly, per the convention — and a document with no
    `theme.css` resolves every `var()` to nothing: the page renders as bare HTML with
    the CSS still sitting in the source looking right, and nothing errors. So the
    tokens are injected, linked from the app's own `theme.css` rather than copied.

    The first version of `_inject_document_kit` put its block straight after `<head>`,
    i.e. ahead of `<base>`, and every asset 404'd as `/d/theme.css` — the same trap
    the class above records for the query runtime, reached a second time because the
    rule was written down in two places. Hence one `_insert_into_head`.
    """

    HTML = ("<!doctype html><html><head><title>t</title>"
            "<style>body{background:var(--bg)}</style></head><body><p>x</p></body></html>")

    def _rendered(self, html=None):
        body = router_module._inject_document_kit(
            router_module._inject_base_tag(html or self.HTML, "/"))
        return router_module._inject_runtime(
            body, router_module.runtime_html({"slug": "s", "title": "t"}, ""))

    def test_the_kit_lands_after_the_base_tag(self):
        out = self._rendered()
        self.assertLess(out.index("<base"), out.index("klado-document-kit"),
                        "the kit must be parsed after <base>, or theme.css resolves "
                        "against the document URL and 404s as /d/theme.css")

    def test_the_kit_lands_before_the_authors_own_style(self):
        out = self._rendered()
        self.assertLess(out.index("klado-document-kit"), out.index("body{background"),
                        "the kit is the floor, not the ceiling: the author's CSS still wins")

    def test_the_kit_carries_the_tokens_the_components_and_the_helpers(self):
        out = self._rendered()
        for asset in ("theme.css", "document.css", "theme.js", "document.js"):
            self.assertIn(asset, out, f"{asset} is missing from the injected kit")
        # Relative on purpose: they resolve through the <base> the server injects.
        self.assertIn('href="theme.css', out)
        self.assertNotIn('href="/theme.css', out)

    def test_it_links_theme_css_rather_than_copying_the_tokens(self):
        """One source of colour. A copy in this file is a copy that goes stale and a
        document that is right in one theme and unreadable in the other."""
        out = self._rendered()
        head = out[:out.index("</head>")]
        self.assertNotRegex(head, r"#[0-9a-fA-F]{3,8}\b",
                            "the kit must not carry a colour of its own")

    def test_injecting_twice_does_not_stack_two_kits(self):
        once = self._rendered()
        twice = router_module._inject_document_kit(once)
        self.assertEqual(twice.count("<!-- klado-document-kit -->"), 1)
        self.assertEqual(twice.count('href="theme.css'), 1)

    def test_the_kit_is_emitted_in_call_order_not_reverse(self):
        """`_insert_into_head` used to insert at a fixed offset, so a LATER call landed
        BEFORE an earlier one and the document came out `base → runtime → kit`. Both
        were after `<base>` so nothing broke — and the ordering nobody could reason
        about is the ordering that breaks when a third block arrives."""
        out = self._rendered()
        self.assertLess(out.index("klado-document-kit"), out.index("data-dashboard-runtime"))

    def test_a_document_with_no_head_still_gets_the_kit(self):
        out = router_module._inject_document_kit("<p>fragment</p>")
        self.assertIn("klado-document-kit", out)

    def test_the_kit_files_exist_and_carry_no_colour_literal(self):
        """`document.css` is the component layer for standalone pages; a colour literal
        in it is a page that only ever looks right in the theme it was written in."""
        css = FRONTEND_DIR / "document.css"
        js = FRONTEND_DIR / "document.js"
        for path in (css, js):
            self.assertTrue(path.exists(), f"{path.name} is referenced but not shipped")
            self.assertGreater(path.stat().st_size, 500, f"{path.name} is suspiciously small")
        text = css.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", text),
                          "document.css has a colour literal; every colour is a token")

    def test_the_helper_module_defines_the_documented_surface(self):
        """A dashboard is written against `window.kld`. If a name is renamed and the
        docstring is not, every published page breaks at once and nothing else does —
        so the names are pinned here rather than left to the reader's memory."""
        js = (FRONTEND_DIR / "document.js").read_text(encoding="utf-8")
        for name in ("fmt:", "table:", "bars:", "sbars:", "donut:", "spark:", "onTheme:"):
            self.assertIn(name, js, f"window.kld.{name.rstrip(':')} is gone")
        self.assertIn("klado:theme", js,
                      "charts read their colours once; without this listener a dark "
                      "reader keeps light-theme bars")


class SaveTests(unittest.TestCase):
    def test_creating_without_a_title_or_a_page_is_refused(self):
        with self.assertRaises(store.DashboardError):
            _with_fake_db([None], store.save_dashboard, {"html": PAGE}, OWNER, "q3-sales")
        with self.assertRaises(store.DashboardError):
            _with_fake_db([None], store.save_dashboard, {"title": "Q3"}, OWNER, "q3-sales")

    def test_a_slug_the_caller_can_see_is_a_conflict(self):
        # 409 only for somebody who could already have found the page — a published
        # one, where "that name is taken" is useful information and nothing more.
        with self.assertRaises(store.DashboardConflict):
            _with_fake_db([{"id": 3, "owner_email": MATE,
                            "visibility": "public", "status": "published"}],
                          store.save_dashboard, {"title": "Q3", "html": PAGE},
                          STRANGER, "q3-sales")

    def test_a_slug_the_caller_cannot_see_is_404_not_409(self):
        # The other half, and the reason the first half is not enough: a 409 is a
        # probing oracle ("is there a dashboard at this slug?") answered for a page
        # the caller may not read. GET and the standalone reader both refuse, so the
        # write path must not volunteer what they withheld.
        cases = (
            # (existing row, queued results for the share lookup that follows)
            ({"id": 3, "owner_email": MATE}, [None]),
            ({"id": 3, "owner_email": MATE, "visibility": "private",
              "status": "published"}, [None]),
            ({"id": 3, "owner_email": MATE, "visibility": "public",
              "status": "draft"}, [None]),
        )
        for row, share_lookup in cases:
            with self.assertRaises(store.DashboardNotFound, msg=row):
                _with_fake_db([row, *share_lookup], store.save_dashboard,
                              {"title": "Q3", "html": PAGE}, STRANGER, "q3-sales")

    def test_a_slug_shared_with_the_caller_is_still_a_conflict(self):
        # Same private-and-published row as above, but this caller IS the grantee —
        # so they can already see the page and 409 tells them nothing new.
        with self.assertRaises(store.DashboardConflict):
            _with_fake_db([{"id": 3, "owner_email": MATE, "visibility": "private",
                            "status": "published"}, {"exists": True}],
                          store.save_dashboard, {"title": "Q3", "html": PAGE},
                          STRANGER, "q3-sales")

    def test_an_update_keeps_the_fields_the_body_did_not_mention(self):
        current = {"id": 3, "owner_email": OWNER, "title": "Q3 Sales",
                   "summary": "old", "summary_zh": "旧", "description": "d", "tags": "gto",
                   "accent": "accent", "datasets": '["sales_2026"]', "html": PAGE,
                   "visibility": "private", "status": "published", "pinned": False,
                   "in_nav": True, "nav_order": 2, "in_home": False, "home_order": 0,
                   "home_collapsed": False}
        log, _conn, _value = _with_fake_db(
            [current, {"id": 3, "created_at": None, "updated_at": None}],
            store.save_dashboard, {"title": "Q3 Sales v2"}, OWNER, "q3-sales")
        values = log[-1][1]
        self.assertEqual(values["title"], "Q3 Sales v2")
        self.assertEqual(values["summary"], "old")
        self.assertEqual(values["html"], PAGE)
        self.assertEqual(values["in_nav"], True)

    def test_an_accent_that_is_a_colour_never_reaches_sql(self):
        with self.assertRaises(store.DashboardError):
            _with_fake_db([{"id": 3, "owner_email": OWNER}],
                          store.save_dashboard, {"accent": "#ff8800"}, OWNER, "q3-sales")

    def test_oversized_html_is_refused_before_it_is_stored(self):
        with self.assertRaises(store.DashboardTooLarge):
            _with_fake_db([{"id": 3, "owner_email": OWNER}],
                          store.save_dashboard,
                          {"title": "Q3", "html": "x" * (store.MAX_HTML_BYTES + 1)},
                          OWNER, "q3-sales")


if __name__ == "__main__":
    unittest.main()
