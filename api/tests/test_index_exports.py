"""The names the left rail CALLS must exist on the objects it calls them on.

`node --check` parses; it does not resolve identifiers, and no static guard executes
the page. So a call to a method that does not exist is invisible to all of them — and
it is **not** a load-time throw either. The rail initialises fine, renders fine, and
only the row a user clicks is dead. Nothing anywhere says so.

That is not hypothetical. The rail opens a filed item in three different modules, and
each names its opener differently: `dashboardPage.openViewer`, `reportsPage.open`,
`knowledgePage.open`. None of them is `openSlug`, which is the name all three invite
you to type.

⚠️ **This file deliberately does NOT try to parse the whole export surface.** An
earlier version located each module's IIFE and balanced its braces, and every
plausible implementation of that was wrong in a way that looked like a product bug:

* finding the module by `find("\n})();")` stops at a NESTED callback's terminator —
  `calendarPage` reported 5 exported names when it has 20;
* counting braces from the wrong offset closes the module on its first `}`, and one
  unbalanced apostrophe inside any string in the file makes every module "unbalanced";
* a non-greedy `return \\{(.*?)\\}` stops at the first closing brace anywhere after it,
  which is a helper object rather than the export list, and then reports every real
  method as missing.

A guard that cries wolf on a working tree gets deleted, and the one real defect it
would have caught ships. So the assertion is narrow and exact: for each module the rail
talks to, its export list must mention the one name the rail calls. A `grep`-shaped
question with a browser-shaped answer.
"""
import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "frontend" / "out" / "index.html"


def _own_script_blocks() -> str:
    """The page's own JavaScript, as one string.

    Vendor bundles are excluded by their `/*! … */` banner — the convention every
    minified library here follows. They export thousands of names through shapes this
    file does not model, and including them turns a precise question into noise.
    """
    html = INDEX.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return "\n".join(b for b in blocks if not b.lstrip().startswith("/*!"))


class RailCrossModuleCallTests(unittest.TestCase):
    # module object -> the ONE method the rail calls on it
    CALLED = {
        "dashboardPage": "openViewer",
        "reportsPage": "open",
        "knowledgePage": "open",
    }

    def test_the_index_html_exists(self):
        self.assertTrue(INDEX.exists(), f"{INDEX} is missing")

    def test_each_module_the_rail_calls_exports_that_one_name(self):
        source = _own_script_blocks()
        # The export list is the `return { … };` that ends a module IIFE. The lookahead
        # is deliberately loose about what sits between the name and the `};` — this
        # asserts "this name is a member of SOME export list", which is the property
        # that matters, and a stricter parse is where the earlier version of this file
        # spent its time and produced false alarms.
        tail = r"(?=[^}]*\};\s*(?:\n\s*)?\}\)\(\))"
        for module, method in self.CALLED.items():
            self.assertRegex(
                source, r"\b" + re.escape(module) + r"\s*=\s*\(\(\)\s*=>",
                f"{module} is not a module IIFE any more — the rail cannot reach it")
            # The name must appear as an EXPORTED member, not merely somewhere in the
            # file: a local function of the same name is not something `module.name`
            # can reach.
            self.assertRegex(
                source, r"\b" + re.escape(method) + r"\b" + tail,
                f"{module}.{method}() is called by the rail but is not in its export "
                f"list — the row would be dead on click, with no error")

    def test_the_rail_publishes_itself_on_window(self):
        """The rail is initialised from the shared bootstrap, which cannot see a
        top-level `const` — that lives in the global LEXICAL scope, not on `window`.
        Without this the bootstrap's `window.railPageInit()` throws and the whole
        left rail never binds its listeners: the panel exists, is styled, and does
        nothing at all."""
        self.assertIn("window.railPage = railPage", _own_script_blocks())

    def test_the_rail_derives_its_own_base_url(self):
        """⚠️ `APP_BASE` is declared `const` in TWO script blocks, both AFTER the rail's.
        Naming it puts the rail in a temporal dead zone: the first `api()` call throws
        `APP_BASE is not defined`, `load()` catches it, and the rail renders its module
        rows from the registry while silently showing no folders — which looks exactly
        like "you have not made any folders yet".

        That is not a hypothetical: it is what the first build did, and the browser
        regression passed 11 of 18 checks because everything except the folders worked.
        """
        body = _own_script_blocks()
        match = re.search(r"const railPage = \(\(\) => \{(.*?)\n\}\)\(\);", body, re.S)
        self.assertIsNotNone(match, "could not find the rail module")
        self.assertIn("RAIL_BASE", match.group(1),
                      "the rail must compute its own base URL")
        # ⚠️ Comments are stripped first. The rail's own comment block explains WHY it
        # does not use `APP_BASE` and names it twice — matching the raw text would make
        # this test fail on its own explanation, which is how a guard gets deleted.
        code = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotRegex(code, r"\bAPP_BASE\b",
                            "the rail must not reference another block's APP_BASE")


if __name__ == "__main__":
    unittest.main()
