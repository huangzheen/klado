"""互动报告 → 静态快照：把服务端 state 烘焙进 HTML，然后剥掉互动层。

⚠️ 静态报告的定义是「纯 HTML/CSS 快照」（knowledge doc format-interactive-report §0）——
读者看的是快照、改不了。互动报告的内容却存在**两个地方**：HTML（初始渲染）和服务端
`state_json`（此后每一次标注）。所以快照如果不把标注折进文档就是**不诚实的**：读者看到的
是标注之前的样子，而卡片上却写着"已发布"。

这个脚本覆盖三条最容易做错的线：
  ① 烘焙 —— state 里的值必须真的落到 HTML（`data-deck-field` 是模型渲染出口）；
  ② 剥离 —— 转换后服务端自己的分类器必须判定为 `static`（`_report_kind`），否则
     "静态报告"名不副实，读者仍能编辑；
  ③ 不伤原报告 —— 转换是新增一份，原互动报告保持 interactive。

    .venv312/bin/python api/tests/test_report_to_static.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.reports import (  # noqa: E402
    _bake_state_into_html, _initial_model, _report_kind, detect_langs)

MODEL = {
    "version": 1,
    "fields": {"titleEn": "Product overview", "titleZh": "产品线全景"},
    "state": {"note": "initial note"},
    "tables": {"matrix": [["A1", "A2"], ["B1", "B2"]]},
}

# A report shaped like the real one: a deck whose text/table are rendered from the
# model, an editable annotation, an editable form control, a colour state, and the
# script that saves all of it back to the state endpoint.
INTERACTIVE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="report-kind" content="interactive">
<style>.slide{background:#fff}</style>
</head><body>
<div class="deck" data-deck-title="Overview">
  <section class="slide" data-lang="en">
    <h1 data-deck-field="fields.titleEn" contenteditable="true">stale title</h1>
    <p data-deck-field="state.note" contenteditable="true">stale note</p>
    <input id="owner" value="stale">
    <table data-deck-table="tables.matrix" data-deck-header-rows="1"></table>
    <span data-deck-field="state.colour">grey</span>
  </section>
  <section class="slide" data-lang="zh">
    <h1 data-deck-field="fields.titleZh" contenteditable="true">旧标题</h1>
  </section>
</div>
<script type="application/json" data-report-deck-model>
""" + json.dumps(MODEL) + """
</script>
<script>
  const save = () => fetch('api/reports/x/state', {method:'POST', body: JSON.stringify(s)});
  document.addEventListener('input', save);
</script>
</body></html>"""


class ToStaticTest(unittest.TestCase):
    def setUp(self):
        self.state = {
            "note": "Q4 launch risk",
            "colour": "red",
        }
        self.model = _initial_model(INTERACTIVE)
        self.html, self.baked = _bake_state_into_html(INTERACTIVE, self.state, self.model)

    # ── ① 烘焙 ───────────────────────────────────────────────────────────────
    def test_model_is_read_from_the_authored_json(self):
        self.assertEqual(self.model.get("version"), 1)
        self.assertEqual(self.model["fields"]["titleEn"], "Product overview")

    def test_state_is_written_into_the_document(self):
        self.assertIn("Q4 launch risk", self.html)
        self.assertIn("red", self.html)

    def test_stale_authored_text_is_replaced(self):
        self.assertNotIn("stale note", self.html)
        self.assertNotIn("stale title", self.html)

    def test_untouched_model_values_still_render(self):
        # Fields the state says nothing about must keep the MODEL's value, not the
        # placeholder text the author left in the HTML.
        self.assertIn("Product overview", self.html)

    def test_table_rows_come_from_the_model(self):
        self.assertIn("B1", self.html)
        self.assertIn("<th", self.html)          # header row honoured
        self.assertNotIn('data-deck-table="tables.matrix"', self.html)

    def test_both_languages_are_baked(self):
        self.assertIn("产品线全景", self.html)   # zh slide bound field
        self.assertEqual(sorted(detect_langs(self.html)), ["en", "zh"])

    # ── ② 剥离：结果必须真的被判为 static ────────────────────────────────────
    def test_result_is_classified_static(self):
        # The authoritative check: the SAME classifier the publish path uses.
        self.assertEqual(_report_kind(self.html), "static")

    def test_editable_affordances_are_gone(self):
        self.assertNotIn("contenteditable", self.html)
        self.assertNotIn("<input", self.html)
        self.assertNotIn('data-deck-field', self.html)

    def test_save_script_is_gone_but_model_survives(self):
        # The save script talks to a /state endpoint this snapshot will never have.
        self.assertNotIn("api/reports/x/state", self.html)
        # ⚠️ The MODEL script must survive: the deck runtime and the PPTX exporter
        # both read it. Blanket-removing every <script> would silently break export.
        self.assertIn("data-report-deck-model", self.html)
        self.assertIn("Product overview", self.html.split("data-report-deck-model")[1][:400])

    def test_kind_marker_is_flipped(self):
        self.assertIn('content="static"', self.html)

    def test_deck_pagination_is_preserved(self):
        # Pagination is NOT an authoring control (see `_report_kind`), so a static
        # report is still a 16:9 deck — removing it would change the deliverable.
        self.assertIn('class="deck"', self.html)
        self.assertIn('class="slide"', self.html)
        self.assertEqual(self.html.count('class="slide"'), 2)

    # ── ③ 健壮性：畸形输入不能炸，也不能产出假静态 ────────────────────────────
    def test_empty_state_is_fine(self):
        html, baked = _bake_state_into_html(INTERACTIVE, {}, self.model)
        self.assertEqual(_report_kind(html), "static")
        self.assertIn("Product overview", html)     # model still rendered
        self.assertGreaterEqual(baked, 3)

    def test_malformed_state_json_is_ignored(self):
        for bad in (None, "not json", "[1,2]", '"str"'):
            model = _initial_model(INTERACTIVE)
            html, _ = _bake_state_into_html(INTERACTIVE, bad or {}, model)
            self.assertEqual(_report_kind(html), "static")

    def test_missing_model_is_not_fatal(self):
        html, _ = _bake_state_into_html("<p>plain</p>", {"note": "x"}, {})
        self.assertEqual(_report_kind(html), "static")

    def test_unresolvable_path_is_left_alone(self):
        # A binding the model does not carry must not blank the node out.
        src = ('<div class="deck"><section class="slide">'
               '<p data-deck-field="nope.missing">keep me</p></section></div>')
        html, _ = _bake_state_into_html(src, {}, {"version": 1})
        self.assertIn("keep me", html)

    def test_a_report_that_stays_interactive_is_detectable(self):
        # The endpoint refuses to publish a "static" copy that the classifier still
        # calls interactive. Prove the guard has something to catch.
        stubborn = ('<div class="deck"><section class="slide">'
                    '<div contenteditable="true">x</div></section></div>')
        self.assertEqual(_report_kind(stubborn), "interactive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
