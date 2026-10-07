"""The console's Agent Skill page — the package an outside agent is installed with.

Three layers, each faking only the layer *below* it:

* **`klado_shared/agent_skill.py`** is driven against the **real** `agent.zip` in the
  repository. Nothing is faked, because the thing under test is the relationship between
  the package and what this module reports about it — a fixture zip would only prove the
  fixture agrees with itself. The consistency with `agent_skill/` is somebody else's
  test to make (`test_agent_skill_package.py`); this file asserts the reporting.
* **the route** runs through the real app with only the *session* faked, so the router
  and `require_operator` both execute.
* **the frontend** is asserted statically, and the two browser-level facts (the panes, and
  that a source file is not rendered as prose) are asserted for real in
  `shot_admin_console_ui.py`.

⚠️ Two defects this file exists to keep fixed, both found on 2026-10-03 while building
the page:

* The route was `{entry}` and 404'd on six of the seven files, because they live in
  sub-directories. It reads as a broken page — the list renders, every file below the
  root fails — rather than as a routing mistake.
* The 说明书 page had a row of stat cards, two of whose numbers could not mean anything
  to a reader (see `test_the_manual_page_has_no_stat_cards`).
"""
import io
import os
import re
import sys
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
CONSOLE_DIR = os.path.join(REPO_DIR, "api-admin")
FRONTEND = os.path.join(CONSOLE_DIR, "frontend")

for _p in (REPO_DIR, API_DIR, CONSOLE_DIR):
    while _p in sys.path:
        sys.path.remove(_p)
for _p in (CONSOLE_DIR, API_DIR, REPO_DIR):
    sys.path.insert(0, _p)

from fastapi.testclient import TestClient                  # noqa: E402

import test_admin_console as console_tests                 # noqa: E402
from klado_shared import agent_skill                       # noqa: E402

console_main = console_tests.console_main
console_access = console_tests.console_access
OPERATOR = console_tests.OPERATOR
PLAIN_USER = console_tests.PLAIN_USER

PAGE = io.open(os.path.join(FRONTEND, "index.html"), encoding="utf-8").read()
CSS = io.open(os.path.join(FRONTEND, "console.css"), encoding="utf-8").read()
ZIP = os.path.join(REPO_DIR, "frontend", "out", "agent.zip")


def _client():
    return TestClient(console_main.app)


class _as:
    """The console app with only the *session* faked.

    ⚠️ `current_operator` and not an Authorization header: the console's session is a
    signed token the middleware verifies, and minting a real one here would make this
    test depend on the signing key. The gate under test is `require_operator`, and this
    is how `test_admin_console.py` drives it too.
    """

    def __init__(self, user):
        self._patch = patch.object(console_access, "current_operator", return_value=user)

    def __enter__(self):
        self._patch.start()
        return _client()

    def __exit__(self, *exc):
        self._patch.stop()
        return False


# ── the shared reader ─────────────────────────────────────────────────────────

class PackageReadTests(unittest.TestCase):
    """What this module says about `agent.zip`, against the real archive."""

    def test_the_package_is_where_this_test_thinks_it_is(self):
        self.assertTrue(os.path.isfile(ZIP),
                        f"技能包不存在：{ZIP}（跑 bash scripts/build_agent_skill.sh）")

    def test_skill_md_comes_first_and_paths_drop_the_package_root(self):
        """`SKILL.md` is the file an agent reads first, so it is the file an operator
        should see first — and the list shows `SKILL.md`, not `klado/SKILL.md`, because
        every reference inside the package is by name."""
        entries = agent_skill.list_entries()
        self.assertTrue(entries)
        self.assertEqual("SKILL.md", entries[0]["path"])
        self.assertFalse([e for e in entries if e["path"].startswith("klado/")])

    def test_directories_are_not_rows(self):
        """The zip stores directories as zero-length entries. A row that cannot be opened
        teaches nothing, and the path already carries the hierarchy."""
        entries = agent_skill.list_entries()
        self.assertTrue(all(e["bytes"] > 0 for e in entries),
                        [e["path"] for e in entries if e["bytes"] == 0])

    def test_the_version_is_read_out_of_the_file_not_copied_here(self):
        """A copy of the stamp in this module is a copy that can be wrong, which is why
        `test_agent_skill_package.py` reads it from the file too."""
        body = agent_skill.get_entry("SKILL.md")
        self.assertIsNotNone(body["package_version"])
        self.assertIn(body["package_version"], body["content"])
        self.assertEqual(body["package_version"], agent_skill.summary()["package_version"])

    def test_summary_totals_agree_with_the_list(self):
        """The page shows a file count and a size; they are computed from the listing on
        purpose, so they cannot disagree with it — asserted so that stays true."""
        entries = agent_skill.list_entries()
        s = agent_skill.summary()
        self.assertEqual(len(entries), s["entries"])
        self.assertEqual(sum(e["bytes"] for e in entries), s["total_bytes"])
        self.assertEqual(os.path.getsize(ZIP), s["zip_bytes"])

    def test_a_nested_entry_is_readable(self):
        """The regression: the route is `{entry:path}` because six of the seven files are
        nested. This asserts the *lookup* half — the name has to match the archive
        exactly, sub-directories and all."""
        nested = [e for e in agent_skill.list_entries() if "/" in e["path"]]
        self.assertTrue(nested, "包里应当有子目录下的文件")
        found = agent_skill.get_entry(nested[0]["path"])
        self.assertIsNotNone(found, nested[0]["path"])
        self.assertEqual(nested[0]["path"], found["path"])
        # ⚠️ `bytes` is the zip's byte count and `len()` is characters: this corpus is
        # Chinese, so the two are not equal and comparing them measures nothing. What
        # matters is that the whole file came back rather than being cut.
        self.assertFalse(found["truncated"])
        self.assertLessEqual(len(found["content"]), nested[0]["bytes"])
        self.assertGreater(len(found["content"]), nested[0]["bytes"] * 0.5)

    def test_a_name_the_package_does_not_have_is_not_invented(self):
        """`..` is harmless here because a zip is read and never extracted, and because
        the name is only ever a lookup key. The point of the assertion is that neither
        fact is load-bearing for safety — the lookup is what rejects it."""
        for bogus in ("nope.md", "../../etc/passwd", "klado/../../etc/passwd",
                      "SKILL.md.bak", ""):
            self.assertIsNone(agent_skill.get_entry(bogus), bogus)

    def test_a_missing_package_is_a_state_and_keeps_the_shape(self):
        """Not an exception. The page renders "not built" from this, so every key
        `summary()` returns has to be here too — otherwise the frontend grows a second
        shape to handle."""
        # The key set is computed with the real package in place: `summary()` would
        # itself raise under the patch, which is the whole point of the missing state
        # being a *return value* rather than an exception.
        expected = set(agent_skill.summary()) | {"reason"}
        with patch.dict(os.environ, {"AGENT_SKILL_ZIP": "/nonexistent/agent.zip"}):
            missing = agent_skill.missing()
            self.assertIsNotNone(missing)
            self.assertFalse(missing["built"])
            self.assertTrue(missing["reason"])
            self.assertEqual(set(missing), expected,
                             "「未构建」与「已构建」两种状态必须返回同一组键")

    def test_source_is_the_zip_not_the_directory(self):
        """Reading `agent_skill/` instead would be a second answer to "what did the agent
        get", and the two can differ — a deploy can ship a stale zip."""
        source = io.open(agent_skill.__file__, encoding="utf-8").read()
        self.assertIn("agent.zip", source)
        self.assertNotIn('"agent_skill"', source,
                         "本模块不能直接读 agent_skill 目录，只能读构建产物")

    def test_no_write_path(self):
        source = io.open(agent_skill.__file__, encoding="utf-8").read()
        for verb in ("write_text", "write_bytes", "mkdir", "extractall", "extract("):
            self.assertNotIn(verb, source, f"只读模块里出现了 {verb}")


# ── the route ─────────────────────────────────────────────────────────────────

class SkillRouteTests(unittest.TestCase):
    def test_an_operator_gets_the_list_and_the_facts_in_one_response(self):
        """One request, so the header cannot show a count that disagrees with the list."""
        with _as(OPERATOR) as c:
            r = c.get("/api/admin-console/agent-skill")
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertIn("entries", body)
        self.assertIn("summary", body)
        self.assertEqual(body["summary"]["entries"], len(body["entries"]))
        self.assertEqual("SKILL.md", body["entries"][0]["path"])

    def test_a_nested_entry_is_reachable_over_http(self):
        """⚠️ The regression this page shipped with. `{entry}` matches one path segment and
        six of the seven files are nested, so this was a 404 for every file below the
        root while the list rendered perfectly. A test that only walked the root would
        have been green throughout."""
        with _as(OPERATOR) as c:
            listing = c.get("/api/admin-console/agent-skill").json()
            nested = [e for e in listing["entries"] if "/" in e["path"]]
            self.assertTrue(nested, "包里应当有子目录下的文件")
            for entry in nested:
                r = c.get("/api/admin-console/agent-skill/" + entry["path"])
                self.assertEqual(200, r.status_code, entry["path"])
                self.assertEqual(entry["path"], r.json()["path"])

    def test_the_entry_parameter_spans_paths(self):
        """Stated directly, because the symptom (a 404 that reads like a broken page) does
        not point at the route, and a future tidy-up back to `{entry}` is one character.

        ⚠️ Matched against the **decorator**, not the file. The first version asserted
        `"{entry:path}" in source`, which passed even after the route was changed back —
        the docstring above the function says the same words. A guard that keeps passing
        while the thing it guards is wrong is worse than no guard, because it is now
        evidence for the wrong version.
        """
        source = io.open(os.path.join(CONSOLE_DIR, "admin_console", "agent_skill.py"),
                         encoding="utf-8").read()
        decorators = re.findall(r'@router\.get\("([^"]+)"\)', source)
        self.assertIn("/agent-skill/{entry:path}", decorators,
                      "子目录下的条目读不到：路由必须是 {entry:path}")
        self.assertIn("/agent-skill", decorators)

    def test_a_name_the_package_lacks_is_404_not_an_empty_document(self):
        with _as(OPERATOR) as c:
            for bogus in ("nope.md", "../../etc/passwd"):
                r = c.get("/api/admin-console/agent-skill/" + bogus)
                self.assertEqual(404, r.status_code, bogus)

    def test_search_filters_by_path(self):
        with _as(OPERATOR) as c:
            r = c.get("/api/admin-console/agent-skill?search=dashboards")
        self.assertEqual(200, r.status_code)
        entries = r.json()["entries"]
        self.assertTrue(entries)
        self.assertTrue(all("dashboards" in e["path"] for e in entries))

    def test_the_page_is_read_only(self):
        """Same argument as the 说明书 page: this is a page an operator needs *most* when
        writes are switched off."""
        source = io.open(os.path.join(CONSOLE_DIR, "admin_console", "agent_skill.py"),
                         encoding="utf-8").read()
        for verb in ('router.post', 'router.put', 'router.patch', 'router.delete'):
            self.assertNotIn(verb, source, f"页面里出现了 {verb}")

    def test_a_signed_in_non_operator_is_refused(self):
        with _as(PLAIN_USER) as c:
            self.assertEqual(403, c.get("/api/admin-console/agent-skill").status_code)
            self.assertEqual(403, c.get("/api/admin-console/agent-skill/SKILL.md").status_code)

    def test_a_signed_out_caller_is_refused(self):
        _c = _client()
        self.assertEqual(401, _c.get("/api/admin-console/agent-skill").status_code)


# ── the frontend ──────────────────────────────────────────────────────────────

class SkillPageTests(unittest.TestCase):
    def test_the_page_is_in_the_navigation_and_the_shell(self):
        self.assertIn("key: 'skill'", PAGE)
        self.assertIn('id="page-skill"', PAGE)
        self.assertIn("skill: renderSkill", PAGE)

    def test_a_source_file_is_not_rendered_as_prose(self):
        """The four dashboard references are working HTML pages and a Python publisher
        script. Rendering them through the markdown renderer would show the operator a
        document that is not the thing they ship — and would put a file body through a
        parser that is only safe because it escapes first, for no gain."""
        self.assertIn("kk-src", PAGE)
        self.assertIn("d.kind === 'markdown'", PAGE)
        self.assertIn("esc(d.content)", PAGE)

    def test_every_interpolated_body_is_escaped_or_rendered(self):
        """`kkMarkdown` escapes its whole input before any rule runs; the source branch is
        a single `esc()`. Asserted on the shape of the branch so neither can be replaced
        by a raw interpolation."""
        branch = PAGE.split("const body = d.kind === 'markdown'")[1][:400]
        self.assertIn("kkMarkdown(d.content)", branch)
        self.assertIn("esc(d.content)", branch)
        self.assertNotIn("${d.content}", branch)

    def test_a_document_body_cannot_inject_a_tag(self):
        """The skill package is a build artifact, so its content is as trusted as the
        repository — but the page renders it as HTML, and a page that can execute what it
        displays is a page whose contents decide what the console can do."""
        self.assertIn('data-i18n-skip', PAGE)      # the two body containers carry it
        self.assertIn('class="kk-render"', PAGE)
        self.assertIn('class="kk-src"', PAGE)
        # Nothing interpolates a raw body outside the two known-rendered branches.
        self.assertNotIn("${skView.detail.content}", PAGE)
        self.assertNotIn("${d.content}", PAGE)

    def test_the_missing_package_is_a_rendered_state(self):
        self.assertIn("s.built", PAGE)
        self.assertIn("尚未构建", PAGE)

    def test_opening_a_file_only_replaces_the_right_pane(self):
        """The same defect the 说明书 page had (2026-10-03): a full re-render replaces
        both scroll containers and throws away the reader's place. Guarded here
        statically because the browser assertion lives in `shot_admin_console_ui.py`."""
        open_fn = PAGE.split("async function skOpen")[1].split("\n}")[0]
        self.assertIn("kk-main", open_fn)
        self.assertNotIn("renderSkill(host)", open_fn)


class ManualPageStatCardTests(unittest.TestCase):
    """User, 2026-10-03: 「我给你的这部分都不需要，请删除」 — the row of stat cards and the
    blue note under it, on the 说明书 page.

    The numbers in them were not wrong (19 / 1 / 144.0 KB were all true). They were three
    cards whose every value is either a constant or a sum the reader cannot act on, sitting
    above the thing they came to read. The one number worth keeping went to the title bar
    instead, where it describes the page you are on.
    """

    def test_the_manual_page_has_no_stat_cards(self):
        block = PAGE.split("async function renderManual")[1].split("async function skOpen")[0] \
            if "async function skOpen" in PAGE else \
            PAGE.split("async function renderManual")[1]
        self.assertNotIn("kd-stats", block, "说明书页不应再有统计卡")
        self.assertNotIn("kd-stat", block, "说明书页不应再有统计卡")

    def test_the_manual_page_has_no_explanatory_note(self):
        block = PAGE.split("async function renderManual")[1]
        self.assertNotIn("kd-note info", block,
                         "说明书页不应再有那条蓝色说明")

    def test_the_total_size_moved_to_the_title_bar(self):
        self.assertIn('id="pg-meta"', PAGE)
        self.assertIn("正文总量", PAGE)
        self.assertIn("pageMeta", PAGE)

    def test_the_title_bar_number_is_cleared_on_every_navigation(self):
        """Otherwise a page that stops having a number leaves the last one's behind — a
        stale total above a page it does not describe."""
        go_fn = PAGE.split("async function go(key)")[1].split("\n}")[0]
        self.assertIn("setPageMeta(meta)", go_fn)
        setter = PAGE.split("function setPageMeta")[1].split("\n}")[0]
        self.assertIn("textContent = p ? pageMeta(p) : ''", setter)


class SkillStyleTests(unittest.TestCase):
    def test_no_colour_literal_in_the_new_rules(self):
        """`theme.css` is the single place a colour is defined. A literal here would only
        work in one of the two themes."""
        block = CSS.split("/* A file shown as source rather than as prose.")[1]
        block = block.split("@media")[0]
        for literal in re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", block):
            self.fail(f"console.css 新增规则里出现颜色字面量：{literal}")

    def test_every_custom_property_used_is_defined_in_the_theme(self):
        theme = io.open(os.path.join(FRONTEND, "theme.css"), encoding="utf-8").read()
        for token in set(re.findall(r"var\((--[a-zA-Z0-9_-]+)\)",
                                    CSS.split("/* A file shown as source rather than")[1]
                                    .split("@media")[0])):
            # ⚠️ `re.M`: the token is defined somewhere in a 150-line file, and without
            # MULTILINE `^` only ever matches the first line — the check then fails on a
            # file that defines the token perfectly well.
            self.assertIsNotNone(
                re.search(rf"^\s*{re.escape(token)}\s*:", theme, re.M),
                f"{token} 在 theme.css 里没有定义")


if __name__ == "__main__":
    unittest.main()
