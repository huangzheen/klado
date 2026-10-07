"""Dynamic reports (kind='dynamic'): the agent's filter schema and a reader's picks.

Pure functions plus the HTML injection — no database, no HTTP. The schema is a PUBLISHED
contract (knowledge doc `klado-v2:format-dynamic-report`), so these tests are that
contract's teeth: every rejection here is a mistake that would otherwise reach a reader as
"some slices never show up", days later, in a document that looks fine.

Two properties are worth more than the rest and are asserted directly:

* the server REJECTS a schema it cannot honour instead of quietly repairing it — a
  silently adjusted filter would disagree with the document's `data-filter-when` blocks;
* the document itself never grows a control. A dynamic report is read-only, and the
  sidebar lives in the app (`reportsPage.filters`), not in the page.
"""
import json
import unittest

from fastapi import HTTPException

from routers import reports
from services import doc_state  # 抽取后：筛选/注解逻辑的归属


def _four_types():
    return {"version": 1, "filters": [
        {"key": "period", "type": "select", "label": {"en": "Period", "zh": "时间段"},
         "options": [{"value": "2026-06", "label": {"en": "Jun 2026", "zh": "2026年6月"}},
                     {"value": "2026-07", "label": "Jul 2026"}],
         "default": "2026-06"},
        {"key": "metric", "type": "multi", "label": {"en": "Metric"},
         "options": ["gto", "spto", "quantity"], "default": ["gto"]},
        {"key": "price", "type": "range", "label": {"en": "Price band"}, "min": 0, "max": 20000,
         "step": 500, "unit": "RMB", "default": {"min": 3000, "max": 9000}},
        {"key": "window", "type": "date_range", "label": {"en": "Date range"},
         "min_date": "2026-01-01", "max_date": "2026-12-31",
         "default": {"from": "2026-06-01", "to": "2026-06-30"}},
    ]}


class FilterSchemaTests(unittest.TestCase):
    def test_a_four_type_schema_survives_validation(self):
        schema = doc_state.clean_filter_schema(_four_types())
        self.assertEqual(schema["version"], 1)
        self.assertEqual([f["key"] for f in schema["filters"]],
                         ["period", "metric", "price", "window"])
        self.assertEqual([f["type"] for f in schema["filters"]],
                         ["select", "multi", "range", "date_range"])
        self.assertEqual(schema["filters"][0]["default"], "2026-06")
        self.assertEqual(schema["filters"][1]["default"], ["gto"])
        self.assertEqual(schema["filters"][2]["default"], {"min": 3000, "max": 9000})
        self.assertEqual(schema["filters"][3]["default"], {"from": "2026-06-01", "to": "2026-06-30"})

    def test_bare_option_strings_are_accepted_and_labelled_with_themselves(self):
        schema = doc_state.clean_filter_schema({"filters": [
            {"key": "metric", "type": "multi", "label": "Metric", "options": ["gto", "spto"]}]})
        options = schema["filters"][0]["options"]
        self.assertEqual([o["value"] for o in options], ["gto", "spto"])
        self.assertEqual(options[0]["label"], {"en": "gto", "zh": "gto"})
        self.assertEqual(schema["filters"][0]["label"], {"en": "Metric", "zh": "Metric"})
        self.assertEqual(schema["filters"][0]["default"], ["gto"])

    def test_a_bare_filter_list_is_accepted(self):
        schema = doc_state.clean_filter_schema([{"key": "p", "type": "select", "label": "P",
                                                "options": ["a", "b"]}])
        self.assertEqual(schema["filters"][0]["default"], "a")

    def test_missing_or_empty_filters_is_a_valid_empty_schema(self):
        self.assertEqual(doc_state.clean_filter_schema({})["filters"], [])
        self.assertEqual(doc_state.clean_filter_schema({"filters": []})["filters"], [])
        self.assertEqual(doc_state.clean_filter_schema(None)["filters"], [])

    def test_key_must_be_lower_snake_case_and_unique(self):
        with self.assertRaises(HTTPException) as ctx:
            doc_state.clean_filter_schema({"filters": [{"key": "Sales Period", "label": "x",
                                                       "options": ["a"]}]})
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "label": "x", "options": ["a"]},
                {"key": "p", "label": "y", "options": ["b"]}]})

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            doc_state.clean_filter_schema({"filters": [{"key": "p", "type": "slider", "label": "P",
                                                       "options": ["a"]}]})
        self.assertIn("type must be one of", str(ctx.exception.detail))

    def test_a_default_that_is_not_a_real_value_is_rejected(self):
        # A default the document does not render opens the card on a blank page and looks
        # like a broken report once the agent is long gone.
        with self.assertRaises(HTTPException) as ctx:
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "type": "select", "label": "P", "options": ["a"], "default": "zzz"}]})
        self.assertIn("default", str(ctx.exception.detail))
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "m", "type": "multi", "label": "M", "options": ["a"], "default": ["b"]}]})

    def test_duplicate_option_values_are_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "label": "P", "options": ["a", "a"]}]})
        self.assertIn("repeated", str(ctx.exception.detail))

    def test_option_count_is_capped(self):
        with self.assertRaises(HTTPException) as ctx:
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "label": "P", "options": [str(i) for i in range(reports.MAX_FILTER_OPTIONS + 1)]}]})
        self.assertIn("sidebar", str(ctx.exception.detail))

    def test_too_many_filters_are_rejected(self):
        filters = [{"key": f"f{i}", "label": "F", "options": ["a"]}
                   for i in range(reports.MAX_FILTERS + 1)]
        with self.assertRaises(HTTPException) as ctx:
            doc_state.clean_filter_schema({"filters": filters})
        self.assertIn(str(reports.MAX_FILTERS), str(ctx.exception.detail))

    def test_range_bounds_and_step_are_validated(self):
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "type": "range", "label": "P", "min": 10, "max": 10}]})
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "type": "range", "label": "P", "min": 0, "max": 10, "step": 0}]})
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "p", "type": "range", "label": "P", "min": "cheap", "max": 10}]})
        # Numeric strings are coerced rather than rejected: a hand-written or
        # JSON-from-Feishu schema sends numbers as text often enough to matter.
        coerced = doc_state.clean_filter_schema({"filters": [
            {"key": "p", "type": "range", "label": "P", "min": "0", "max": "10", "step": "2"}]})
        self.assertEqual(coerced["filters"][0]["min"], 0)
        self.assertEqual(coerced["filters"][0]["step"], 2)

    def test_range_default_outside_the_bounds_is_clamped_not_rejected(self):
        schema = doc_state.clean_filter_schema({"filters": [
            {"key": "p", "type": "range", "label": "P", "min": 0, "max": 100,
             "default": {"min": -50, "max": 500}}]})
        self.assertEqual(schema["filters"][0]["default"], {"min": 0, "max": 100})

    def test_date_bounds_must_be_iso_and_ordered(self):
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "w", "type": "date_range", "label": "W",
                 "min_date": "2026-1-1", "max_date": "2026-12-31"}]})
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [
                {"key": "w", "type": "date_range", "label": "W",
                 "min_date": "2026-12-31", "max_date": "2026-01-01"}]})

    def test_a_label_in_one_language_is_enough(self):
        schema = doc_state.clean_filter_schema({"filters": [
            {"key": "p", "label": {"zh": "时间段"}, "options": ["a"]}]})
        self.assertEqual(schema["filters"][0]["label"], {"en": "时间段", "zh": "时间段"})
        with self.assertRaises(HTTPException):
            doc_state.clean_filter_schema({"filters": [{"key": "p", "label": {}, "options": ["a"]}]})


class EffectiveSelectionTests(unittest.TestCase):
    def setUp(self):
        self.schema = doc_state.clean_filter_schema(_four_types())

    def test_an_untouched_reader_gets_every_default(self):
        effective, dropped = doc_state.effective_selection(self.schema, {})
        self.assertEqual(effective, {"period": "2026-06", "metric": ["gto"],
                                     "price": {"min": 3000, "max": 9000},
                                     "window": {"from": "2026-06-01", "to": "2026-06-30"}})
        self.assertEqual(dropped, [])

    def test_a_retired_value_falls_back_and_is_reported(self):
        effective, dropped = doc_state.effective_selection(self.schema, {
            "period": "2019-01", "metric": ["gto", "retired"], "price": {"min": 1, "max": 2}})
        self.assertEqual(effective["period"], "2026-06")
        self.assertEqual(effective["metric"], ["gto"])
        self.assertEqual(effective["price"], {"min": 1, "max": 2})
        self.assertEqual(sorted(dropped), ["metric", "period"])

    def test_a_retired_filter_key_is_dropped(self):
        effective, dropped = doc_state.effective_selection(self.schema, {"gone": "1"})
        self.assertNotIn("gone", effective)
        self.assertIn("gone", dropped)

    def test_range_is_clamped_and_reversed_input_is_swapped(self):
        effective, dropped = doc_state.effective_selection(self.schema, {"price": {"min": 9000, "max": 3000}})
        self.assertEqual(effective["price"], {"min": 3000, "max": 9000})
        self.assertEqual(dropped, [])
        effective, _ = doc_state.effective_selection(self.schema, {"price": {"min": -100, "max": 999999}})
        self.assertEqual(effective["price"], {"min": 0, "max": 20000})

    def test_a_malformed_range_falls_back_to_the_default(self):
        effective, dropped = doc_state.effective_selection(self.schema, {"price": {"min": "cheap"}})
        self.assertEqual(effective["price"], {"min": 3000, "max": 9000})
        self.assertEqual(dropped, ["price"])

    def test_date_range_is_clamped_and_reported_when_malformed(self):
        effective, _ = doc_state.effective_selection(self.schema, {"window": {"from": "2026-09-09", "to": "2026-06-01"}})
        self.assertEqual(effective["window"], {"from": "2026-06-01", "to": "2026-09-09"})
        effective, dropped = doc_state.effective_selection(self.schema, {"window": {"from": "9/9/26", "to": "2026-06-01"}})
        self.assertEqual(effective["window"], {"from": "2026-06-01", "to": "2026-06-30"})
        self.assertEqual(dropped, ["window"])

    def test_an_empty_schema_never_raises(self):
        for raw in ({}, {"anything": 1}, None, "nonsense", []):
            effective, dropped = doc_state.effective_selection({"filters": []}, raw)
            self.assertEqual(effective, {})

    def test_a_multi_selection_of_an_empty_list_is_kept(self):
        # "no option picked" is a real view (the reader is comparing something), and the
        # runtime hides every slice that names a value of that filter.
        effective, dropped = doc_state.effective_selection(self.schema, {"metric": []})
        self.assertEqual(effective["metric"], [])
        self.assertEqual(dropped, [])

    def test_a_single_string_is_accepted_where_a_list_is_expected(self):
        effective, _ = doc_state.effective_selection(self.schema, {"metric": "spto"})
        self.assertEqual(effective["metric"], ["spto"])


class DynamicKindTests(unittest.TestCase):
    def test_slice_blocks_make_a_report_dynamic_without_a_declaration(self):
        html = '<div class="deck"><section class="slide" data-filter-when="period=2026-06">x</section></div>'
        self.assertEqual(reports._report_kind(html), "dynamic")

    def test_the_declared_kind_wins_and_is_the_fast_path(self):
        plain = '<section class="slide">x</section>'
        self.assertEqual(reports._report_kind(plain, "dynamic"), "dynamic")
        self.assertEqual(reports._report_kind('<h1 contenteditable>x</h1>', "dynamic"), "dynamic")
        self.assertEqual(reports._report_kind(plain, "interactive"), "interactive")
        self.assertEqual(reports._report_kind(plain, "static"), "static")

    def test_the_meta_marker_is_honoured(self):
        marked = '<meta name="report-kind" content="dynamic">' + \
                 '<section class="slide" data-filter-when="p=a">x</section>'
        self.assertEqual(reports._report_kind(marked), "dynamic")

    def test_a_dynamic_report_is_never_reclassified_as_interactive(self):
        # The rail is the only control surface, so the document must stay read-only: a stray
        # <input> in a dynamic report must not hand it the /state editor contract.
        html = ('<meta name="report-kind" content="dynamic">'
                '<section class="slide" data-filter-when="p=a"><input value="x"></section>')
        self.assertEqual(reports._report_kind(html), "dynamic")

    def test_an_uploaded_document_cannot_claim_a_kind(self):
        with self.assertRaises(HTTPException) as ctx:
            reports._report_kind("<html></html>", "document")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_ordinary_reports_are_unaffected(self):
        self.assertEqual(reports._report_kind('<div class="deck"><section class="slide">x</section></div>'), "static")
        self.assertEqual(reports._report_kind('<h1 contenteditable>x</h1>'), "interactive")


class StoredSchemaReadTests(unittest.TestCase):
    """Reading a stored schema must never fail — a 500 there kills the whole rail.

    Found by the browser harness (2026-09-30): a row whose `options` were bare strings made
    `GET .../filters` raise `'str' object has no attribute 'get'`. The write path always
    stores the expanded form, but "always" is a claim about code I can see, and the reader
    is the wrong place to enforce it.
    """

    def test_bare_string_options_are_expanded_on_read(self):
        stored = json.dumps({"filters": [
            {"key": "period", "type": "select", "label": "Period", "options": ["2026-06"],
             "default": "2026-06"},
            {"key": "metric", "type": "multi", "label": {"en": "Metric"}, "options": ["gto", "spto"],
             "default": ["gto"]}]}, ensure_ascii=False)
        schema = doc_state.load_filter_schema({"slug": "s", "filters_json": stored})
        self.assertEqual([o["value"] for o in schema["filters"][0]["options"]], ["2026-06"])
        self.assertEqual(schema["filters"][0]["label"], {"en": "Period", "zh": "Period"})
        effective, dropped = doc_state.effective_selection(
            schema, {"period": "2026-06", "metric": ["spto"]})
        self.assertEqual(effective, {"period": "2026-06", "metric": ["spto"]})
        self.assertEqual(dropped, [])

    def test_a_row_without_filters_or_with_junk_reads_as_empty(self):
        self.assertEqual(doc_state.load_filter_schema({"filters_json": ""})["filters"], [])
        self.assertEqual(doc_state.load_filter_schema({"filters_json": "not json"})["filters"], [])
        self.assertEqual(doc_state.load_filter_schema({"filters_json": "[1,2]"})["filters"], [])
        self.assertEqual(doc_state.load_filter_schema({"filters_json": '{"filters": "x"}'})["filters"], [])

    def test_a_filter_without_a_key_is_skipped_rather_than_guessed(self):
        stored = json.dumps({"filters": [
            {"type": "select", "label": "no key", "options": ["a"]},
            {"key": "ok", "type": "select", "label": "Ok", "options": ["a"]}]})
        schema = doc_state.load_filter_schema({"slug": "s", "filters_json": stored})
        self.assertEqual([f["key"] for f in schema["filters"]], ["ok"])
        self.assertEqual(schema["filters"][0]["label"], {"en": "Ok", "zh": "Ok"})


class FilterRuntimeInjectionTests(unittest.TestCase):
    def setUp(self):
        self.schema = doc_state.clean_filter_schema(_four_types())
        self.effective, _ = doc_state.effective_selection(self.schema, {})
        self.html = ('<html><head><title>t</title></head><body>'
                     '<div class="deck"><section class="slide" data-filter-when="period=2026-06">'
                     'Jun</section></div></body></html>')

    def test_a_report_without_filters_is_served_byte_for_byte(self):
        plain = '<html><head></head><body><h1>Static</h1></body></html>'
        self.assertEqual(reports._inline_filter_runtime(plain, "s", {"filters": []}, {}), plain)

    def test_the_state_is_embedded_as_json_and_the_runtime_is_added(self):
        out = reports._inline_filter_runtime(self.html, "q3-review", self.schema, self.effective)
        self.assertIn("data-report-filters-state", out)
        payload = json.loads(out.split("window.__reportFilters = ", 1)[1].split(";</script>", 1)[0])
        self.assertEqual(payload["slug"], "q3-review")
        self.assertEqual(payload["readOnly"], False)
        self.assertEqual(payload["selection"]["period"], "2026-06")
        self.assertEqual([f["key"] for f in payload["schema"]["filters"]],
                         ["period", "metric", "price", "window"])
        if reports._deck_asset("report-filters.js"):
            self.assertIn("data-report-filters>", out)
        if reports._deck_asset("report-filters.css"):
            self.assertIn("<style data-report-filters>", out)
            self.assertIn(".report-filter-off", out)

    def test_the_runtime_lands_before_the_deck_runtime(self):
        # The deck counts only the pages its own runtime can see, and the filter runtime is
        # what marks them — so it has to have run first.
        body = reports._inline_filter_runtime(self.html, "q3-review", self.schema, self.effective)
        body = reports._inline_deck_runtime(body)
        self.assertLess(body.index("<script data-report-filters-state>"),
                        body.index("<script data-report-deck>"))

    def test_a_guest_gets_the_read_only_flag(self):
        out = reports._inline_filter_runtime(self.html, "q3-review", self.schema, self.effective, read_only=True)
        self.assertIn('"readOnly": true', out)

    def test_re_publishing_does_not_stack_copies_of_the_runtime(self):
        once = reports._inline_filter_runtime(self.html, "q3-review", self.schema, self.effective)
        twice = reports._inline_filter_runtime(once, "q3-review", self.schema, self.effective)
        # The runtime's own comment block mentions the state tag, so count the INJECTION
        # (tag immediately followed by the assignment) rather than the bare attribute name.
        marker = "data-report-filters-state>window.__reportFilters"
        self.assertEqual(once.count(marker), 1)
        self.assertEqual(twice.count(marker), 1)
        self.assertEqual(twice.count("<script data-report-filters>"), 1)

    def test_the_runtime_never_contains_a_literal_script_tag(self):
        # The runtime is inlined verbatim and the server strips the previous copy by
        # re-matching that tag — so a literal "<script" in the runtime's OWN text makes the
        # strip match inside the file, and a re-published report stacks a second copy.
        # Measured before the fix: 2 runtime tags after one re-inject. report-deck.js is
        # clean for the same reason; this keeps it that way.
        js = reports._deck_asset("report-filters.js")
        if js:
            self.assertNotIn("<script", js)

    def test_the_injected_state_cannot_close_its_own_script_tag(self):
        # A slug or label containing `</script>` would otherwise break out of the block.
        schema = doc_state.clean_filter_schema({"filters": [
            {"key": "p", "label": {"en": "a</script><b>"}, "options": ["x"]}]})
        out = reports._inline_filter_runtime(self.html, "q3-review", schema, {"p": "x"})
        head = out.split("data-report-filters-state>", 1)[1].split("</script>", 1)[0]
        self.assertNotIn("</script", head)
        self.assertIn("<\\/script", head)


if __name__ == "__main__":
    unittest.main()