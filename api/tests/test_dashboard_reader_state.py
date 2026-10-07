"""Dashboard reader state — the filter rail and the annotation layer, after HTML moved here.

⚠️ WHY THIS FILE IS ABOUT MOVING, NOT ABOUT NEW FEATURES. Neither the filter schema
validator nor the selection folder nor the tolerant reader is new: all three lived in
`routers/reports.py` and were moved, whole, into `services/doc_state.py` on 2026-10-05
when HTML started landing in Dashboard instead of Workspace. The tests here are therefore
about the things a MOVE can get wrong, which are the things a copy would hide:

* **two homes.** The logic must be reachable from both routers and defined once. A test
  that only checks behaviour would pass with a second copy that had already drifted, so
  `SharedOwnershipTests` asserts the *location* of each function.
* **the wrong table.** A reader's saved view must not follow a document from one module
  to the other, and a dashboard pick must never be written into `ai_report_filter_
  selections`. The two tables use different key column names on purpose.
* **the missing column.** A page that arrived from Workspace already knows its `kind`,
  and the annotation layer reads it. A SELECT that forgot `kind` reads as `static`, which
  quietly OFFERS notes on an interactive page whose right-click belongs to its own
  runtime — the failure is "a feature appeared", not "a feature is missing".
* **the missing list.** The wall cannot ship a schema per card, so the card carries
  `kind` + `has_filters` and the rail fetches the schema itself. Without the two flags
  the page shows its slices and no way to choose values.

Everything runs against fake connections — no server, no database.
"""
import importlib.util
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import resolve_frontend_dir
from routers import dashboard as dash
from routers import reports as rep
from services import annotations as ann
from services import dashboard_store as store
from services import doc_state

FRONTEND_DIR = Path(resolve_frontend_dir())
INDEX = FRONTEND_DIR / "index.html"

OWNER = "owner@example.com"
MATE = "mate@example.com"
STRANGER = "stranger@example.com"
PAGE = "<html><head><title>d</title></head><body><h1>Sales</h1></body></html>"
DYNAMIC_PAGE = ('<html><head><title>d</title></head><body>'
                '<section data-filter-when="period=2026-06">June</section>'
                '<section data-filter-when="period=2026-07">July</section>'
                '</body></html>')

SCHEMA = {"filters": [
    {"key": "period", "type": "select",
     "label": {"en": "Period", "zh": "时间段"},
     "options": [{"value": "2026-06", "label": {"en": "Jun", "zh": "六月"}},
                 {"value": "2026-07", "label": {"en": "Jul", "zh": "七月"}}],
     "default": "2026-06"}]}


def _row(**kw):
    base = {"id": 7, "slug": "q3-page", "title": "Q3", "owner_email": OWNER,
            "visibility": "private", "status": "published", "kind": "static",
            "html": PAGE, "filters_json": "", "state_json": None,
            "updated_at": None}
    base.update(kw)
    return base


def _request(kind: str = "browser"):
    """A Request-shaped stub. `kind` is what `identity.current_identity` reads off state."""
    return SimpleNamespace(
        state=SimpleNamespace(current_user={"email": OWNER}, auth_kind=kind, lang=None),
        headers={"accept-language": ""},
        app=SimpleNamespace(),
    )


def _client():
    app = FastAPI()
    app.include_router(dash.router, prefix="/api/dashboard")
    return TestClient(app)


# ── the logic has ONE home ────────────────────────────────────────────────────

class SharedOwnershipTests(unittest.TestCase):
    """Where the code LIVES, which a behaviour test cannot see.

    ⚠️ The failure this guards is the one a copy produces: reports keeps working, the
    dashboard copy works, and the two disagree about an edge case nobody tested. Asserting
    the definition site is blunt on purpose — the claim "there is one implementation" has
    no other observable form.
    """

    SHARED = ("clean_filter_schema", "effective_selection", "load_filter_schema",
              "tolerant_schema", "state_text", "no_json_constant", "saved_selection")

    def test_every_shared_rule_is_defined_in_doc_state_only(self):
        for name in self.SHARED:
            with self.subTest(rule=name):
                self.assertTrue(hasattr(doc_state, name),
                                "doc_state.%s is missing" % name)
                # `reports` re-exports the first few for its own call sites; what must NOT
                # happen is a `def` there. An import binds a reference, a def creates a
                # second implementation.
                source = inspect.getsource(rep)
                self.assertNotIn("\ndef %s(" % name, source,
                                 "reports.py defines %s — that is a second copy" % name)

    def test_the_dashboard_router_uses_the_shared_reader_not_a_second_one(self):
        source = inspect.getsource(dash)
        for name in ("clean_filter_schema", "effective_selection", "load_filter_schema",
                     "state_text"):
            with self.subTest(rule=name):
                self.assertIn("doc_state.%s" % name, source)

    def test_the_reports_rail_and_the_dashboard_rail_are_the_same_function(self):
        # The runtime assembly is ~40 lines and already covered by the report suite. A
        # dashboard copy is 40 lines that drift on the first bug fix; the dashboard
        # module therefore CALLS the report one.
        self.assertIs(dash._reports_inline_filter_runtime, rep._inline_filter_runtime)


# ── the two selection tables are separate ─────────────────────────────────────

class SelectionTableTests(unittest.TestCase):
    def test_a_reader_addresses_the_table_it_was_told_about(self):
        calls = []

        class _Cur:
            def execute(self, sql, params=None):
                calls.append((" ".join(str(sql).split()), params))

            def fetchone(self):
                return None

        doc_state.saved_selection(_Cur(), "ai_dashboard_filter_selections", 7, OWNER)
        sql, params = calls[0]
        self.assertIn("ai_dashboard_filter_selections", sql)
        self.assertEqual(params, (7, OWNER))

    def test_the_two_tables_do_not_share_rows(self):
        # A reader who set a view on a report must not find it applied to a dashboard:
        # they are different documents that happen to accept the same schema, and a pick
        # is a statement about ONE of them.
        self.assertNotEqual(store.SELECTIONS_TABLE, "public.ai_report_filter_selections")
        self.assertTrue(store.SELECTIONS_TABLE.endswith("ai_dashboard_filter_selections"))

    def test_the_key_column_matches_the_shared_reader(self):
        # The reader addresses a row by `doc_id`. Naming the dashboard column
        # `dashboard_id` instead would make every dashboard lookup return "no saved view"
        # — a rail that forgets its own settings, with no error anywhere.
        source = inspect.getsource(store)
        block = source[source.index("CREATE TABLE IF NOT EXISTS {SELECTIONS_TABLE}"):
                       source.index("CREATE TABLE IF NOT EXISTS {SELECTIONS_TABLE}") + 600]
        self.assertIn("doc_id", block)
        self.assertNotIn("dashboard_id  INTEGER", block)


# ── a page's kind, and the flags the wall needs to offer the rail ─────────────

class CardFlagTests(unittest.TestCase):
    def test_a_page_with_a_schema_advertises_that_it_has_filters(self):
        card = store.card(_row(kind="dynamic", filters_json=json.dumps(SCHEMA)))
        self.assertEqual(card["kind"], "dynamic")
        self.assertTrue(card["has_filters"])

    def test_a_plain_page_does_not_claim_filters_it_does_not_have(self):
        card = store.card(_row(kind="static", filters_json=""))
        self.assertEqual(card["kind"], "static")
        self.assertFalse(card["has_filters"],
                         "a page with no schema must not show a filter button")

    def test_the_wall_never_carries_the_schema_itself(self):
        # 200 cards × 12 filters is a list payload nobody should send. The schema
        # arrives with the single GET the rail makes.
        card = store.card(_row(kind="dynamic", filters_json=json.dumps(SCHEMA)))
        self.assertNotIn("filters", card)
        self.assertNotIn("schema", card)


class KindDerivationTests(unittest.TestCase):
    def test_a_schema_makes_a_page_dynamic_whatever_it_looked_like(self):
        self.assertEqual(dash._dashboard_kind(PAGE, SCHEMA), "dynamic")

    def test_a_page_with_data_filter_when_blocks_is_dynamic_even_with_no_schema(self):
        # With an empty schema a page whose HTML still declares slices is still dynamic —
        # its slices just all render at once. Storing `static` here is a card lying about
        # itself, and the reader's rail is what exposes it.
        self.assertEqual(dash._dashboard_kind(DYNAMIC_PAGE, {"filters": []}), "dynamic")

    def test_a_plain_page_stays_static(self):
        self.assertEqual(dash._dashboard_kind(PAGE, {"filters": []}), "static")


# ── annotations: the dashboard is a first-class target ────────────────────────

class AnnotationTargetTests(unittest.TestCase):
    def test_dashboard_is_a_known_asset_type(self):
        self.assertIn("dashboard", ann.TARGETS)

    def test_it_points_at_the_dashboard_table(self):
        self.assertEqual(ann.TARGETS["dashboard"][0], "ai_dashboards")

    def test_an_interactive_page_keeps_its_own_annotation_state(self):
        # The same rule the report path has, for the same reason: two competing
        # right-click menus in one document is worse than either one. And the failure
        # mode is a feature APPEARING, so a missing `kind` in the SELECT would not be
        # caught by any test that only checks the happy path.
        for kind in ("interactive", "dynamic"):
            with self.subTest(kind=kind):
                with patch.object(ann, "_db") as db:
                    cur = db.return_value.cursor.return_value.__enter__.return_value
                    cur.fetchone.return_value = _row(kind=kind)
                    # The read rule is mocked too, and it matters: without it the page
                    # 404s first and the 409 under test is never reached — the assertion
                    # would then be green for a reason that has nothing to do with `kind`.
                    with patch.object(ann, "_may_read_fn",
                                      return_value=lambda row, email: True):
                        with self.assertRaises(ann.AnnotationError) as caught:
                            ann.resolve_target("dashboard", "q3-page", OWNER)
                    self.assertEqual(caught.exception.status_code, 409)

    def test_a_static_page_may_carry_notes(self):
        with patch.object(ann, "_db") as db:
            cur = db.return_value.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = _row(kind="static")
            with patch.object(ann, "_may_read_fn", return_value=lambda row, email: True):
                row = ann.resolve_target("dashboard", "q3-page", OWNER)
        self.assertEqual(row["slug"], "q3-page")

    def test_the_resolve_query_asks_for_kind(self):
        # `kind` is what the rule above reads. It was selected for `report` only, and the
        # day `dashboard` joined, a missing key reads as `static` — which turns the rule
        # off silently instead of failing.
        source = inspect.getsource(ann.resolve_target)
        self.assertIn('_has_kind = asset_type in ("report", "dashboard")', source)
        self.assertNotIn("if asset_type == 'report' else", source)

    def test_an_unknown_type_still_names_every_type_it_accepts(self):
        with self.assertRaises(ann.AnnotationError) as caught:
            ann._asset_type("nonsense")
        message = str(caught.exception)
        for name in ("report", "knowledge", "event", "dashboard"):
            self.assertIn(name, message,
                          "the 400 lists the accepted types; %r is missing" % name)

    def test_a_dashboard_note_links_to_its_own_document_page(self):
        # `/d/{slug}` is a page of its own. There is no `?dashboard=` deep link in the
        # SPA, so reusing the report's query form would land the reader on the wall.
        url = ann.doc_url("dashboard", "q3-page")
        self.assertTrue(url.endswith("d/q3-page"), url)


# ── the runtime the standalone reader injects ─────────────────────────────────

class ReaderInjectionTests(unittest.TestCase):
    def test_a_dynamic_page_gets_the_filter_runtime(self):
        out = dash._inline_filter_runtime(DYNAMIC_PAGE, "q3-page", SCHEMA, {})
        self.assertIn("__reportFilters", out)
        self.assertIn('"period"', out)

    def test_a_plain_page_is_served_unchanged(self):
        # A no-op for an ordinary page. If this ever stops being a no-op, every static
        # dashboard ships a runtime it does not use.
        self.assertEqual(dash._inline_filter_runtime(PAGE, "q3-page", {"filters": []}, {}), PAGE)

    def test_a_static_page_gets_the_note_layer_and_an_interactive_one_does_not(self):
        static = dash._inline_annotation_layer("<html><body>x</body></html>", _row(kind="static"))
        self.assertIn("annotations", static.lower())
        for kind in ("interactive", "dynamic"):
            with self.subTest(kind=kind):
                html = "<html><body>x</body></html>"
                self.assertEqual(dash._inline_annotation_layer(html, _row(kind=kind)), html)

    def test_the_note_layer_says_dashboard_not_report(self):
        out = dash._inline_annotation_layer("<html><body>x</body></html>", _row(kind="static"))
        self.assertIn('"dashboard"', out)


# ── the fields that are accepted and then thrown away ─────────────────────────

class SilentlyDroppedFieldTests(unittest.TestCase):
    """Which body fields are real, and which are accepted-then-discarded.

    ⚠️ The claim under test is a NEGATIVE one, and negatives rot: nothing fails when a
    document quietly stops listing a field that is being dropped, and the first person to
    find out is an agent whose `submitter` came back 200 and never appeared on the card.

    `format-dashboard` §6.1 lists these in prose. A list in prose is a promise, not a
    check — so the promise is pinned here, and ADDING a field to the model fails this test
    until the prose is updated. That is the direction that matters: a new real field must
    make someone decide what the doc says.
    """

    #: Fields a reader of the report contract would reasonably send, that the dashboard
    #: model has no home for. Sending one is a 200 that changes nothing.
    STILL_DROPPED = ("category", "author", "author_email", "project_slug",
                     "folder_id", "source_report_id", "public_link_token")

    #: Fields that were in this list before 2026-10-05 and are now REAL. Kept so the
    #: regression that motivated them is not undone quietly.
    NOW_REAL = ("summary_en", "submitter", "langs")

    #: Deliberately NOT body fields. `kind` is DERIVED from the document and the schema,
    #: and `filters_json` has its own endpoint. Letting a caller set them in the publish
    #: body would let a page claim a kind its HTML does not have — a card that lies about
    #: itself, and a card is exactly what a reader skims before deciding to open it.
    NOT_IN_BODY = ("kind", "filters_json", "state_json", "state_updated_at")

    def test_the_dropped_list_does_not_overlap_the_real_fields(self):
        fields = set(dash.DashboardIn.model_fields)
        for name in self.STILL_DROPPED:
            with self.subTest(field=name):
                self.assertNotIn(name, fields,
                                 "%s is now a real field — update §6.1" % name)

    def test_the_fields_that_used_to_be_dropped_are_real_now(self):
        # These were accepted and discarded for the whole life of the module. A
        # regression here means an agent's `submitter` is back to being a lie.
        fields = set(dash.DashboardIn.model_fields)
        for name in self.NOW_REAL:
            with self.subTest(field=name):
                self.assertIn(name, fields)

    def test_derived_state_is_not_settable_from_the_publish_body(self):
        fields = set(dash.DashboardIn.model_fields)
        for name in self.NOT_IN_BODY:
            with self.subTest(field=name):
                self.assertNotIn(name, fields,
                                 "%s must be derived or have its own endpoint" % name)

    def test_the_card_carries_what_the_wall_reads(self):
        card = store.card(_row(submitter="Zhen Huang", summary_en="s", langs="en,zh"))
        self.assertEqual(card["submitter"], "Zhen Huang")
        self.assertEqual(card["langs"], "en,zh")


# ── the migration script ──────────────────────────────────────────────────────

class MigrationScriptTests(unittest.TestCase):
    """What the one-shot migration carries, and what it refuses to write.

    ⚠️ Written after a REAL production run lost data, which is the only review that
    counts. The script's carry list omitted `filters_json`, so the two `dynamic` pages
    arrived with `kind='dynamic'` and an empty schema: a card advertising a filter rail
    its document does not have. Nothing failed. The row looked migrated — the kind came
    across, the schema did not — because `kind` and `filters_json` travel independently
    and nothing compared the two.
    """

    @staticmethod
    def _script():
        path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_html_to_dashboard.py"
        spec = importlib.util.spec_from_file_location("migrate_html_to_dashboard", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_every_capability_a_page_can_have_is_on_the_carry_list(self):
        # A page that had a filter rail must still have one afterwards. The omission was
        # invisible because the row looked complete.
        mod = self._script()
        for name in ("kind", "filters_json", "state_json", "summary_en", "submitter", "langs"):
            with self.subTest(column=name):
                self.assertIn(name, mod.SAME_NAME,
                              "%s is not migrated — a page arrives without it" % name)

    def test_a_page_that_claims_to_be_dynamic_must_carry_its_schema(self):
        mod = self._script()
        mod.assert_self_consistent({"slug": "ok", "kind": "dynamic", "filters_json": '{"filters":[]}'})
        with self.assertRaises(RuntimeError) as caught:
            mod.assert_self_consistent({"slug": "lying", "kind": "dynamic", "filters_json": ""})
        self.assertIn("lying", str(caught.exception))

    def test_a_static_page_with_no_schema_is_fine(self):
        mod = self._script()
        mod.assert_self_consistent({"slug": "plain", "kind": "static", "filters_json": ""})
        mod.assert_self_consistent({"slug": "none", "kind": None, "filters_json": None})

    def test_office_documents_are_never_carried(self):
        # `doc_object` is where a .pptx/.xlsx's bytes live. It must not be on the carry
        # list: `ai_dashboards` has nowhere to put them, and a script that tried would
        # either 500 or, worse, store an empty column and report success.
        mod = self._script()
        self.assertNotIn("doc_object", mod.SAME_NAME)
        self.assertNotIn("doc_type", mod.SAME_NAME)
        # …and the classification that keeps them out is still in the script.
        self.assertIn("doc_type", Path(mod.__file__).read_text(encoding="utf-8"))


# ── the routes exist and keep the module's own rules ──────────────────────────

class RouteTests(unittest.TestCase):
    def test_the_state_and_filter_routes_are_all_there(self):
        # ⚠️ `router.routes` carries NO prefix — the prefix is applied when the router is
        # mounted. A test that writes "/api/dashboard/..." here is asserting about a
        # string FastAPI never produces, and it fails for a reason that has nothing to do
        # with the route being missing.
        paths = {r.path for r in dash.router.routes}
        for path in ("/{slug}/state", "/{slug}/filters", "/{slug}/filters/selection"):
            self.assertIn(path, paths)

    def test_a_trailing_slash_alias_exists_for_state(self):
        # This app answers a sub-route with a 404 (not a 307) when a trailing slash is
        # present, and `…/state/` is an easy thing to write in a template literal — a
        # silent 404 there reads as "my save vanished".
        paths = {r.path for r in dash.router.routes}
        self.assertIn("/{slug}/state/", paths)

    def test_a_page_nobody_owns_is_404_not_403(self):
        # ⚠️ Asserted on `_fail()`, not on the exception. `DashboardNotFound.status` is
        # the inherited 400 — the 404 is the ROUTER's mapping, and a test that pinned the
        # exception would be asserting a constant while the thing a reader sees (the
        # response code) could change freely. 403 would confirm to another account that
        # somebody has a page under this slug.
        exc = dash._fail(store.DashboardNotFound("x"))
        self.assertEqual(exc.status_code, 404)

    def test_an_organisation_refusal_stays_403_and_does_not_become_400(self):
        # A user shown 400 retypes the address forever, because from where they sit it
        # looks like a typo.
        self.assertEqual(dash._fail(store.DashboardPolicyError("x")).status_code, 403)


# ── the shared rail, on the page ──────────────────────────────────────────────

class SharedRailTests(unittest.TestCase):
    """One rail for two viewers, and the API prefix is not hard-coded to either."""

    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")

    def test_there_is_exactly_one_filter_rail_in_the_document(self):
        self.assertEqual(self.html.count('id="rpt-filters"'), 1)
        self.assertEqual(self.html.count('id="rpt-filters-body"'), 1)
        self.assertEqual(self.html.count('id="rpt-filters-toggle"'), 1)

    def test_the_rail_is_not_inside_either_viewer(self):
        # It starts outside them and `filterRailHost()` moves it into the open one. Left
        # inside the report viewer, the dashboard viewer has no way to show the same
        # control, and a COPY would mean two sets of ids.
        rail = self.html.index('id="rpt-filters"')
        for viewer in ('id="rpt-viewer"', 'id="dsh-viewer"'):
            start = self.html.index(viewer)
            # the viewer's markup ends at the matching close of .rpt-viewer
            end = self.html.index("\n</div>", start)
            self.assertFalse(start < rail < end,
                             "the rail is inside %s; it must be shared" % viewer)

    def test_the_rail_is_hosts_moved_into_the_open_viewer(self):
        self.assertIn("function filterRailHost(", self.html)
        self.assertIn("main.insertBefore(rail, frame)", self.html)

    def test_both_viewers_ask_for_the_rail(self):
        self.assertIn("reportsPage.openFilters(slug, item, {api: '/api/dashboard', frameId: 'dsh-frame'})",
                      self.html)
        self.assertIn("openFilters(", self.html)

    def test_the_selection_goes_to_whichever_module_owns_the_schema(self):
        # A dashboard pick saved against `/api/reports` is a write to a document that may
        # not exist, and it fails with a 404 that reads as "my view is not saving".
        self.assertIn("state.api + '/' + encodeURIComponent(state.slug) + '/filters/selection'",
                      self.html)

    def test_the_selection_reaches_the_frame_that_is_on_screen(self):
        self.assertIn("const frame = $(_filterState.frameId || 'rpt-frame');", self.html)

    def test_closing_a_dashboard_takes_the_shared_rail_with_it(self):
        # Left mounted, it reappears over the next report opened — a filter rail over a
        # document that never had filters.
        self.assertIn("reportsPage.hideFilters();", self.html)


if __name__ == "__main__":
    unittest.main()
