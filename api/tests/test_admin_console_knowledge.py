"""The console's 项目说明书 page — the manual an agent reads, and how it is presented.

Three layers, each faking only the layer *below* it:

* **`klado_shared/knowledge.py`** is driven against a scripted fake connection, so the SQL
  that is actually sent is the thing under test. A fake that answered the same rows for
  every statement would pass against a query that had dropped its language filters.
* **the route** runs through the real app with only the *session* faked, so the middleware,
  the router and `require_operator` all execute.
* **the frontend** is asserted statically, because a browser is not available here and a
  claim about the page that nobody opened is not a claim.

⚠️ The renderer assertions are about *ordering*, not about output. `kkMarkdown` escapes
its whole input before any rule runs; a version that escaped at emit time instead would
still render every case correctly and would still be one unescaped branch away from
executing a document body. Ordering is the invariant, so the assertion pins the order.
"""
import os
import pathlib
import re
import sys
import unittest
from unittest.mock import patch

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
CONSOLE_DIR = os.path.join(REPO_DIR, "api-admin")
FRONTEND = os.path.join(CONSOLE_DIR, "frontend")

# The same fixed order as `test_admin_console.py` — two top-level `main.py` files make
# `sys.path` order load-bearing, and getting it wrong fails in a full run rather than
# in this file.
for _p in (REPO_DIR, API_DIR, CONSOLE_DIR):
    while _p in sys.path:
        sys.path.remove(_p)
for _p in (CONSOLE_DIR, API_DIR, REPO_DIR):
    sys.path.insert(0, _p)

from fastapi.testclient import TestClient                  # noqa: E402

import test_admin_console as console_tests                 # noqa: E402
from klado_shared import knowledge                         # noqa: E402

console_main = console_tests.console_main
console_access = console_tests.console_access
OPERATOR = console_tests.OPERATOR
PLAIN_USER = console_tests.PLAIN_USER


# ── the fake database ─────────────────────────────────────────────────────────
#
# ⚠️ Deliberately NOT `test_admin_console._Cursor`. That one answers `fetchone()` and has
# no `description`, because the organizations list is five correlated subqueries read as
# scalars. This module's queries are read with `cursor.description` and shaped with
# `dict(zip(columns, row))`, so a fake borrowed from over there would fail with an
# `AttributeError` on every single test and the suite would prove nothing.

class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.log.append((" ".join((sql or "").split()), params))
        self._conn.index += 1
        script = self._conn.script
        self._rows_i = script[self._conn.index - 1] if self._conn.index <= len(script) else []
        self.rowcount = len(self._rows_i)
        # A psycopg2 `description` is a sequence of column objects whose `name` is what
        # the production code reads; one-tuples are faithful enough and keep a failure
        # readable. It comes from the *scripted* row, so an empty result set has no
        # columns — which is why every script below carries real rows, including the
        # aggregate rows whose columns are the thing under test.
        columns = list(self._rows_i[0].keys()) if self._rows_i else []
        self.description = [(name,) for name in columns]
        return self

    def _tuple_row(self, row):
        columns = [d[0] for d in (self.description or [])]
        return tuple(row.get(name) for name in columns)

    def fetchone(self):
        # A psycopg2 row is a *tuple* in `description` order, and the production
        # `summary()` does `dict(zip(columns, fetchone()))`; a fake handing back the
        # scripted dict would pair every column name with its own name, and `int('total')`
        # is what that looks like.
        return self._tuple_row(self._rows_i[0]) if self._rows_i else None

    def fetchall(self):
        return [self._tuple_row(row) for row in self._rows_i]


class _Conn:
    def __init__(self, script, log):
        self.script, self.log = list(script), log
        # ⚠️ The statement cursor is shared across `cursor()` calls on purpose. A counter
        # that reset per cursor would let the second query read the first one's rows and
        # pass a test that had deleted every filter — the exact failure the console
        # suite's own docstring warns about.
        self.index = 0

    def cursor(self, *a, **kw):
        return _Cursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass


def strip_comments(source: str, *, python: bool = False) -> str:
    """Source with its comments removed, so an assertion is about code and not prose.

    ⚠️ Needed because these modules explain themselves heavily. An assertion of the form
    "this file must not contain X" that also matches X inside a comment that *describes* X
    is a test that fails on a correct file, and the fix is then to weaken the assertion
    rather than to keep the guard.
    """
    if python:
        out = []
        for line in source.split("\n"):
            quote, cut = None, len(line)
            for i, ch in enumerate(line):
                if quote:
                    if ch == quote:
                        quote = None
                elif ch in "'\"":
                    quote = ch
                elif ch == "#":
                    cut = i
                    break
            out.append(line[:cut])
        source = "\n".join(out)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"/\*(?:.|\n)*?\*/", "", source)
    source = re.sub(r"//[^\n]*", "", source)
    return source


def _rows(**kwargs):
    """One scripted statement result, as a list of dicts (what `_dict_rows` consumes)."""
    return [kwargs]


# ── the shared query ─────────────────────────────────────────────────────────

class ManualQueryTests(unittest.TestCase):
    """The SQL, and the decisions that are not SQL."""

    DOCUMENT = dict(
        document_id="klado-v2:format-chart-style",
        title="format-chart-style",
        document_type="format",
        source="api/knowledge_docs",
        version="1.6",
        is_active=True,
        content_chars=4096,
        created_at=None, updated_at=None,
        metadata={"tags": ["chart", "图表", "配色"], "retrieval_role": None,
                  "runtime_contract": "internal-tools-only",
                  "source_path": "knowledge/format-chart-style.md",
                  "content_sha256": "abc123"},
    )

    def _run(self, script, call, *args, **kwargs):
        log: list = []
        with patch.object(knowledge, "connect_main", return_value=_Conn(script, log)):
            result = call(*args, **kwargs)
        return result, log

    def test_the_list_query_reads_the_metadata_columns_it_filters_on(self):
        """Tags and the source path live inside a jsonb column. If the filter named the
        column without the `->>` operator the query would fail at runtime — and a test
        that only checked the function's return value would never reach the database.

        ⚠️ A `search` term is required to see them: those columns appear in the WHERE
        clause, and an unfiltered query has no WHERE clause at all. Asserting on the
        unfiltered SQL would have passed against a search path that was never built."""
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents,
                           search="chart")
        sql = log[0][0]
        self.assertIn("metadata->>'source_path'", sql)
        self.assertIn("metadata->>'tags'", sql)

    def test_the_query_does_not_filter_on_is_active(self):
        """Retirement is a **hard delete** (`knowledge_store.delete_document` issues
        `DELETE`), not a flag — so this table never holds a retired row, and a
        `WHERE is_active` filter would only ever hide a row someone deactivated by hand.

        ⚠️ This test used to be named `test_retired_documents_are_listed_not_filtered_away`
        and claimed "the manual keeps retired documents so an older agent's citation still
        resolves". That was false, and the page grew a 已退役 stat card on it which read 0
        straight after 19 documents were actually retired. The name asserted a product
        behaviour that does not exist, so a future reader would preserve it on faith. The
        assertions are unchanged; only the claim they were making is corrected."""
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents)
        self.assertIn("is_active DESC", log[0][0])
        self.assertNotIn("WHERE is_active", log[0][0])

    def test_a_state_the_writer_cannot_produce_is_not_offered(self):
        """`is_active` is a column but a constant: `upsert_document` is the only writer
        and it sets TRUE, and retirement deletes the row. Shipping it in the payload let
        the console render a 已退役 badge that no document can ever earn. The field is
        gone from the document shape, and `retired` is gone from the summary for the same
        reason — both were numbers that could only ever be one value."""
        import inspect
        document, _ = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents)
        self.assertNotIn("is_active", document[0])
        source = strip_comments(open(knowledge.__file__, encoding="utf-8").read(),
                                python=True)
        self.assertNotIn("AS retired", source)
        self.assertNotIn("WHERE NOT is_active", source)

        # …and the console must not have grown a badge for it either.
        page = open(pathlib.Path(knowledge.__file__).resolve().parents[1]
                    / "api-admin" / "frontend" / "index.html", encoding="utf-8").read()
        for label in ("已退役", "is_active", "s.retired"):
            # ⚠️ assertFalse, not assertNotIn: the latter puts the whole 2 000-line page in
            # the failure message, which buries the actual finding in a wall of markup.
            self.assertFalse(label in page,
                             f"console 仍引用 {label}，而该状态已不可能出现")

    def test_there_is_no_language_filter_left_to_misuse(self):
        """The manual is Chinese-only since 2026-10-03: the canonical English-side files
        are deleted and retired. A surviving `zh` / `canonical` filter would imply there
        was a variant to switch back to, and an operator pressing it would get either an
        empty page or a list that no longer means anything."""
        import inspect
        self.assertNotIn("language", inspect.signature(knowledge.list_documents).parameters)
        source = strip_comments(open(knowledge.__file__, encoding="utf-8").read(),
                                python=True)
        self.assertNotIn("document_id LIKE", source)
        self.assertNotIn("document_id NOT LIKE", source)

    def test_a_type_filter_is_bound_not_spliced(self):
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents,
                           document_type="rule")
        sql, params = log[0]
        self.assertIn("document_type = %s", sql)
        self.assertIn("rule", params)
        # `all` and empty both mean no filter, and neither may add a clause.
        for value in ("", "all"):
            _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents,
                               document_type=value)
            self.assertNotIn("document_type =", log[0][0], value)

    def test_search_is_parameterized(self):
        """The search term reaches the database as a bound parameter. An operator pasting
        a quote into the search box is a Tuesday."""
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents,
                           search="o'brien --; drop table")
        sql, params = log[0]
        self.assertNotIn("drop table", sql)
        self.assertTrue(any("o'brien --; drop table" in str(p) for p in params))

    def test_a_list_query_does_not_carry_the_content(self):
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents)
        self.assertNotIn("content,", log[0][0])
        self.assertNotIn(" content,", log[0][0])

    def test_a_detail_query_does_carry_the_content(self):
        one, log = self._run([_rows(**self.DOCUMENT, content="## 正文")],
                             knowledge.get_document, "klado-v2:format-chart-style")
        self.assertIn("content,", log[0][0])
        self.assertEqual(one["content"], "## 正文")
        self.assertFalse(one["truncated"])

    def test_tags_come_from_jsonb_as_a_list(self):
        """⚠️ No comma-splitting here. The business-knowledge table stores tags as one
        comma-joined string and needs a split; this one stores a real jsonb array, and a
        copy of that split would mangle any tag containing a comma."""
        one, _ = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents)
        self.assertEqual(one[0]["tags"], ["chart", "图表", "配色"])
        missing, _ = self._run([_rows(**dict(self.DOCUMENT, metadata=None))],
                               knowledge.list_documents)
        self.assertEqual(missing[0]["tags"], [])

    def test_the_limit_is_clamped_rather_than_trusted(self):
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents,
                           limit=10 ** 9)
        self.assertEqual(log[0][1][-1], knowledge.MAX_LIMIT)
        _, log = self._run([_rows(**self.DOCUMENT)], knowledge.list_documents,
                           limit="not a number")
        self.assertEqual(log[0][1][-1], knowledge.DEFAULT_LIMIT)

    def test_a_missing_row_is_none_and_an_empty_identifier_never_queries(self):
        log: list = []
        with patch.object(knowledge, "connect_main", return_value=_Conn([], log)):
            self.assertIsNone(knowledge.get_document("nope"))
            self.assertIsNone(knowledge.get_document("   "))
        self.assertEqual(len(log), 1, "only the real lookup should have reached the database")

    def test_a_known_truncation_is_reported(self):
        """A body past the cap comes back short *and says so*. Silently showing the
        first 256 KB as if it were the whole document is the failure mode."""
        big = dict(self.DOCUMENT, content="x" * (knowledge.MAX_CONTENT_CHARS + 10))
        one, _ = self._run([_rows(**big)], knowledge.get_document, "x")
        self.assertEqual(len(one["content"]), knowledge.MAX_CONTENT_CHARS)
        self.assertTrue(one["truncated"])

    def test_the_summary_counts_only_what_the_page_shows(self):
        """Every stat on the page has to come from this row, or the header and the list
        disagree — which is the failure this project's own `overview` docstring warns
        about."""
        # ⚠️ Three scripted results, because `summary()` runs three statements: the
        # totals row, the type breakdown, the contract breakdown. The type breakdown is
        # ONE grouped statement, not one per type.
        script = [
            _rows(total=19, active=19, routers=2, types=2, contracts=1,
                  total_chars=150000),
            _rows(document_type="format", documents=13),
            _rows(contract="internal-tools-only", documents=19),
        ]
        summary, log = self._run(script, knowledge.summary)
        self.assertEqual(len(log), 3)
        self.assertIn("retrieval_role' = 'router'", log[0][0])
        self.assertEqual(summary["totals"]["total"], 19)
        self.assertEqual(summary["by_type"],
                         [{"document_type": "format", "documents": 13}])
        self.assertEqual(summary["by_contract"],
                         [{"contract": "internal-tools-only", "documents": 19}])

    def test_the_summary_states_no_verdict_on_validity(self):
        """⚠️ The main app's `/api/ai/knowledge/manifest` decides whether the manual is
        valid. A second verdict here would be a second answer about the agent's
        instructions — the one thing `klado_shared` exists to prevent — so this module
        reports the fields the judgement is made on and says nothing about the result."""
        source = strip_comments(open(knowledge.__file__, encoding="utf-8").read(),
                                python=True)
        for word in ("valid", "duplicate", "issue"):
            self.assertNotIn(word, source.lower(),
                             f"the console must not re-judge the manual ({word})")

    def test_the_module_has_no_write_statement(self):
        """The console reads the manual; a deploy writes it. A stray INSERT or UPDATE
        here would bypass the reconciler, `_retired.txt` and the content hashes at once.

        Comments are stripped first (they explain the read-only rule at length) and
        `(?<![.\\w])` keeps the check off `columns.insert(1, "content")`."""
        source = strip_comments(open(knowledge.__file__, encoding="utf-8").read(),
                                python=True)
        for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "GRANT"):
            self.assertIsNone(re.search(rf"(?<![.\w]){verb}\b", source, re.I),
                              f"klado_shared/knowledge.py must stay read-only ({verb})")

    def test_the_module_never_mentions_the_business_knowledge_corpus(self):
        """⚠️ The ruling behind this page's shape. It was asked for as the manual, and an
        earlier version answered with what people had written down — so the module must
        not carry a query for that table at all, and a later change cannot quietly put it
        back. Checked on the code, not the prose, because the docstring explains the
        exclusion at length and must keep doing so."""
        source = strip_comments(open(knowledge.__file__, encoding="utf-8").read(),
                                python=True)
        self.assertNotIn("ai_knowledge_items", source)
        self.assertNotIn("visibility", source)


# ── the corpus on disk ────────────────────────────────────────────────────────

class ManualCorpusTests(unittest.TestCase):
    """The manual is Chinese-only, and the files on disk are the single writer of that.

    ⚠️ This is the guard on the 2026-10-03 migration, not on the query. When the 19
    canonical English-side files were deleted, deleting them alone would have left **93
    dead cross-references** in the documents that survived — an agent told "see
    `klado-v2:runtime-contract`" for a document that no longer existed. A migration whose
    interesting part is a rewrite of 93 pointers is exactly the kind with no test, and
    the failure is silent: the document still reads fine, the link is just gone.
    """

    DOCS = os.path.join(API_DIR, "knowledge_docs")

    def _documents(self):
        """`(document_id, path)` for every real file; `_`-prefixed files are metadata."""
        out = []
        for path in sorted(pathlib.Path(self.DOCS).glob("*.md")):
            if path.name.startswith("_"):
                continue
            for line in path.read_text(encoding="utf-8").split("\n"):
                if line.startswith("document_id:"):
                    out.append((line.split(":", 1)[1].strip(), path))
                    break
        return out

    def _retired(self):
        path = pathlib.Path(self.DOCS) / "_retired.txt"
        if not path.is_file():
            return set()
        return {line.split("#", 1)[0].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.split("#", 1)[0].strip()}

    def test_every_document_on_disk_is_a_chinese_one(self):
        """No canonical file may come back. There is no English side to switch to, and a
        stray one would be a near-duplicate: 16 of the 19 deleted pairs were byte-identical
        apart from their `document_id`."""
        not_zh = [d for d, _ in self._documents() if not d.endswith(knowledge.CHINESE_SUFFIX)]
        self.assertEqual(not_zh, [], f"非中文说明书又回来了: {not_zh}")

    def test_every_backticked_cross_reference_resolves(self):
        """The invariant the 93 rewrites exist to keep. A reference to a document that is
        not in the corpus is an instruction the agent cannot follow."""
        known = {d for d, _ in self._documents()} | self._retired()
        pattern = re.compile(r"`(klado-v2:[a-z0-9-]+(?::zh)?)`")
        missing = set()
        for doc_id, path in self._documents():
            for hit in pattern.findall(path.read_text(encoding="utf-8")):
                if hit not in known:
                    missing.add((doc_id, hit))
        self.assertEqual(sorted(missing), [], f"指向不存在文档的引用: {sorted(missing)}")

    def test_the_deleted_ids_are_retired_rather_than_merely_deleted(self):
        """⚠️ Deleting a `.md` file does NOT delete the row: the startup reconciler only
        inserts and updates, so a deleted file leaves its document readable forever. The
        `_retired.txt` list is the deletion. This asserts the *other* direction — that the
        list still exists, because the file deletion and the list are two halves of one
        act and one of them is easy to lose in a revert."""
        self.assertTrue((pathlib.Path(self.DOCS) / "_retired.txt").is_file(),
                        "_retired.txt 是删除动作的载体，不能跟着文件一起被删掉")
        # Spot-check one id that was definitely deleted, and one that must still exist.
        self.assertIn("klado-v2:runtime-contract", self._retired())
        self.assertNotIn("klado-v2:runtime-contract:zh", self._retired())
        self.assertIn("klado-v2:runtime-contract:zh", {d for d, _ in self._documents()})

    def test_the_corpus_has_both_sides_of_every_survivor(self):
        """Every surviving document has a file, so a row cannot exist without a writer."""
        docs = self._documents()
        self.assertGreater(len(docs), 0)
        for doc_id, path in docs:
            self.assertTrue(path.is_file(), doc_id)


# ── the route ────────────────────────────────────────────────────────────────

class ManualRouteTests(unittest.TestCase):
    """The gate, the status codes, and the surface itself."""

    def client_as(self, user, script):
        client = TestClient(console_main.app)
        log: list = []

        class _P:
            def __enter__(self_inner):
                self_inner.a = patch.object(console_access, "current_operator",
                                            return_value=user)
                self_inner.b = patch.object(knowledge, "connect_main",
                                            return_value=_Conn(script, log))
                self_inner.a.start(); self_inner.b.start()

            def __exit__(self_inner, *exc):
                self_inner.a.stop(); self_inner.b.stop()
                return False

        return client, _P()

    DOCUMENT = ManualQueryTests.DOCUMENT

    def test_the_two_routes_live_under_the_console_prefix(self):
        paths = {r.path for r in console_main.app.routes if "manual" in getattr(r, "path", "")}
        self.assertEqual(paths, {"/api/admin-console/manual",
                                 "/api/admin-console/manual/{document_id}"})
        # ⚠️ And the knowledge routes are gone: a request for the manual answered with
        # user-written pages, and leaving the old path alive would let that happen again.
        self.assertNotIn("/api/admin-console/knowledge", paths)
        self.assertFalse([p for p in (r.path for r in console_main.app.routes)
                          if p.startswith("/api/admin-console/knowledge")])

    def test_no_session_is_401_and_a_plain_user_is_403(self):
        for user, expected in ((None, 401), (PLAIN_USER, 403)):
            # Never reaches the database — the gate answers first — so the script is
            # never consumed and its length is irrelevant here.
            client, ctx = self.client_as(user, [[], [], [], []])
            with ctx:
                resp = client.get("/api/admin-console/manual")
            self.assertEqual(resp.status_code, expected, resp.text[:200])

    def test_the_page_stays_readable_while_the_console_is_read_only(self):
        """An operator investigating an incident needs to know what the agent was told
        *most* when they cannot change anything, so it is `require_operator` and never
        `require_write`."""
        import inspect
        from admin_console import knowledge as route_module
        source = inspect.getsource(route_module)
        self.assertNotIn("require_write", source)
        self.assertIn("require_operator", source)

    def test_the_index_returns_the_documents_and_the_counts_together(self):
        """⚠️ Two scripted results: the list, then `summary()`'s. Scripting one would leave
        the aggregate empty, and an empty result has no `description` — the failure would
        land inside the code under test rather than on an assertion about it."""
        script = [[self.DOCUMENT],
                  _rows(total=1, active=1, routers=0,
                        types=1, contracts=1, total_chars=4096),
                  _rows(document_type="format", documents=1),
                  _rows(contract="internal-tools-only", documents=1)]  # + summary()
        client, ctx = self.client_as(OPERATOR, script)
        with ctx:
            resp = client.get("/api/admin-console/manual")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        body = resp.json()
        self.assertEqual(body["documents"][0]["document_id"],
                         "klado-v2:format-chart-style")
        self.assertEqual(body["summary"]["totals"]["total"], 1)
        # The list withholds content; that is the contract the detail route exists for.
        self.assertNotIn("content", body["documents"][0])

    def test_an_unknown_type_is_not_an_error(self):
        """`document_type` is an open vocabulary: a deploy can add one, so an unknown
        value filters to zero rows rather than 400. The page's chips come from the
        breakdown this API returns, so it can only send values the server itself named."""
        # 4 statements: the (empty) list, then the three inside `summary()`.
        client, ctx = self.client_as(OPERATOR, [
            [],
            _rows(total=0, active=0, routers=0, types=0, contracts=0,
                  total_chars=0),
            [], []])
        with ctx:
            resp = client.get("/api/admin-console/manual?type=nope")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(resp.json()["documents"], [])

    def test_a_missing_document_is_404(self):
        client, ctx = self.client_as(OPERATOR, [[]])
        with ctx:
            resp = client.get("/api/admin-console/manual/nope")
        self.assertEqual(resp.status_code, 404, resp.text[:200])

    def test_the_detail_route_returns_the_content_the_list_withholds(self):
        client, ctx = self.client_as(OPERATOR, [_rows(**self.DOCUMENT, content="## 规则")])
        with ctx:
            resp = client.get("/api/admin-console/manual/klado-v2%3Aformat-chart-style")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        body = resp.json()
        self.assertEqual(body["content"], "## 规则")
        self.assertEqual(body["source_path"], "knowledge/format-chart-style.md")

    def test_a_colon_in_the_document_id_survives_the_route(self):
        """Every id in this corpus contains a colon, and the `:zh` variants end in one
        more. A percent-encoded colon must still match the path parameter, or half the
        manual is unreachable by id."""
        client, ctx = self.client_as(OPERATOR, [_rows(**self.DOCUMENT, content="x")])
        with ctx:
            resp = client.get(
                "/api/admin-console/manual/klado-v2%3Aformat-chart-style%3Azh")
        self.assertEqual(resp.status_code, 200, resp.text[:200])


# ── the page ─────────────────────────────────────────────────────────────────

def _read(name: str) -> str:
    with open(os.path.join(FRONTEND, name), encoding="utf-8") as handle:
        return handle.read()


class ManualPageTests(unittest.TestCase):
    """What the console must show, and the two orderings the renderer depends on."""

    HTML = None
    CSS = None

    @classmethod
    def setUpClass(cls):
        cls.HTML = _read("index.html")
        cls.CSS = _read("console.css")

    def test_the_page_is_in_the_nav_with_both_languages(self):
        self.assertRegex(
            self.HTML,
            r"\{ key: 'manual',\s*zh: '[^']*[一-鿿][^']*',\s*en: '[^']+'",
            "the nav entry must carry both a Chinese and an English label")

    def test_the_page_has_a_container_and_a_dispatch_entry(self):
        self.assertIn('<section id="page-manual"', self.HTML)
        self.assertRegex(self.HTML, r"manual:\s*renderManual")
        self.assertRegex(self.HTML, r"(?m)^async function renderManual\(")

    def test_the_page_never_calls_the_old_knowledge_route(self):
        """The page was rebuilt around the manual; a surviving `/knowledge` call would be
        a route that no longer exists, and `kldFail` would render it as a load error."""
        self.assertNotIn("'/knowledge", self.HTML)
        self.assertNotIn("page-knowledge", self.HTML)

    def test_the_page_never_asks_for_a_write(self):
        """The page is an operator's window onto a deploy-owned corpus. A POST/PUT/DELETE
        anywhere in it would be an edit path with none of the reconciler's checks."""
        body = re.search(r"async function renderManual\(host\) \{.*?\n\}",
                         self.HTML, re.S).group(0)
        for verb in ("method: 'POST'", "method: 'PUT'", "method: 'DELETE'"):
            self.assertNotIn(verb, body)

    def test_the_page_still_names_the_source_file(self):
        """The file is the only writer of this corpus, so the page keeps saying so — but
        as where a document comes from, not as an explanation of a language pair that no
        longer exists."""
        self.assertIn("source_path", self.HTML)
        self.assertIn("kkPairNote", self.HTML)
        self.assertNotIn("is_chinese_variant", self.HTML)
        self.assertNotIn("canonical_id", self.HTML)

    def test_the_renderer_escapes_before_it_parses(self):
        """⚠️ The ordering, not the output. Escape-first is what makes every rule in
        `kkInline`/`kkMarkdown` safe: no rule can ever see a raw `<`, so none can emit an
        attribute. A version that escaped at emit time would render identically and still
        be one branch away from executing a document body."""
        source = re.search(r"function kkMarkdown\(src\) \{.*?\n\}", self.HTML, re.S).group(0)
        escape_at = source.index("kkEscape(")
        first_block_rule = min(
            (i for token in ("<pre><code", "<h", "<table", "<ul", "<blockquote", "<p>")
             if (i := source.find(token)) != -1),
            default=len(source))
        self.assertLess(escape_at, first_block_rule,
                        "the source must be escaped before any rule emits a tag")

    def test_the_renderer_allowlists_link_schemes(self):
        """Escaping alone is a claim about this file; the scheme allowlist is a claim
        about the reader's browser. Both, or neither is a real boundary."""
        body = re.search(r"function kkInline\(text\) \{.*?\n\}", self.HTML, re.S).group(0)
        self.assertIn("https?|mailto", body)
        self.assertIn("noopener", body)

    def test_no_raw_control_byte_survives_in_the_console_frontend(self):
        """The renderer parks code spans behind U+0000 and fences behind U+0001. Written
        as raw bytes those are invisible, and the first tool that trims control
        characters turns the restore regex into a no-op — leaving a placeholder sitting
        in a rendered page."""
        for name in ("index.html", "i18n.js", "console.css"):
            source = _read(name)
            offenders = [(i, repr(line)) for i, line in enumerate(source.split("\n"), 1)
                         if any(ord(c) < 9 or 13 < ord(c) < 32 for c in line)]
            self.assertEqual(offenders, [], name)

    def test_the_blockquote_marker_is_matched_after_escaping(self):
        """A regression pin. `kkMarkdown` escapes first, which turns `>` into `&gt;`; a
        blockquote test written the obvious way therefore matches nothing and every quote
        in every manual renders as a literal paragraph."""
        source = re.search(r"function kkMarkdown\(src\) \{.*?\n\}", self.HTML, re.S).group(0)
        self.assertIn("/^\\s*&gt;/.test(line)", source)

    def test_the_manual_css_uses_tokens_and_defines_no_selector_twice(self):
        """Both halves of the main app's stylesheet rules, applied here. A colour literal
        works in one theme and is wrong in the other, and this console has a dark theme.
        A repeated selector is worse: the later rule silently wins and the earlier one is
        dead code."""
        block = self.CSS.split("── project manual")[-1]
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)
        self.assertEqual(literals, [], "page styles must use theme tokens, not literals")
        counts = {}
        for selector in re.findall(r"^\.(kk-[\w-]+) \{", block, re.M):
            counts[selector] = counts.get(selector, 0) + 1
        repeated = {k: v for k, v in counts.items() if v > 1}
        self.assertEqual(repeated, {}, f"a repeated selector silently wins: {repeated}")


class ConsoleLanguageDefaultTests(unittest.TestCase):
    """The console opens in Chinese unless the operator has chosen otherwise."""

    @classmethod
    def setUpClass(cls):
        cls.CODE = strip_comments(_read("i18n.js"))

    def _init(self) -> str:
        return re.search(r"function init\(\) \{.*?\n  \}", self.CODE, re.S).group(0)

    def test_an_empty_storage_defaults_to_chinese(self):
        self.assertIn("stored === 'en' ? 'en' : 'zh'", self._init())

    def test_the_browser_language_does_not_participate(self):
        """⚠️ The main app still uses localStorage → browser language, and that difference
        is deliberate: the console is a tool for one team, so the useful default is the
        language the team works in. A guard is here so nobody "fixes" the divergence by
        copying the main app's line back across."""
        self.assertNotIn("navigator.language", self._init())

    def test_the_default_agrees_with_set_lang(self):
        """If `init` accepted a stored value `setLang` would refuse, a hand-edited
        localStorage entry could put the page into a language no `pickPair` branch
        matches — and every pair would render as "中文 / English" at once."""
        self.assertIn("next === 'en' ? 'en' : 'zh'", self.CODE)

    def test_a_stale_storage_value_cannot_reach_a_pair_with_no_side(self):
        self.assertNotIn("stored ||", self._init())


if __name__ == "__main__":
    unittest.main()
