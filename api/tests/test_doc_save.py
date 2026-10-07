"""`POST /{slug}/document/content`: the guards around writing to a stored file.

    .venv312/bin/python api/tests/test_doc_save.py

This endpoint changes bytes the reader can no longer get back from the app, so the
checks that matter are the ones that STOP it:

* not yours, or not a document → 404, and never a 403 that confirms it exists;
* a format that cannot be edited in place → 409, before a single byte is read;
* an empty edit list → 400, so a client bug does not look like a save;
* every oid unresolvable → 409 and **nothing stored**: a save that reports success
  while leaving the file alone is the one outcome worth failing loudly for;
* someone else saved between our read and our write → 409, and the bytes we
  stored are dropped rather than left as an orphan object.

`_db` and `oss_storage` are replaced with recording fakes. That is deliberate:
these assertions are about WHICH calls happen in WHICH order and WHAT is answered,
and a live database would add a fixture to maintain without making any of them
truer. The file-level half — that the text lands in the right paragraph — is
`test_doc_edit.py`, which round-trips through real .docx / .pptx / .xlsx bytes.
"""
import asyncio
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from routers import reports as R  # noqa: E402
from services import doc_preview as dp  # noqa: E402


def _docx_bytes():
    import docx

    d = docx.Document()
    d.add_heading("结算条款速查表", level=1)
    d.add_paragraph("本页列出主要结算条款。")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


class _FakeCursor:
    """Answers exactly the two statements this endpoint runs, in order."""

    def __init__(self, owner, current_object, update_hits=True):
        self.owner = owner
        self.current_object = current_object
        self.update_hits = update_hits
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        # Full SQL, not a prefix: an assertion about the guard clause has to be
        # able to see the guard clause.
        self.executed.append((" ".join(sql.split()), params))
        if sql.strip().upper().startswith("UPDATE"):
            # `stale` is decided by whether the WHERE clause still matched: the
            # endpoint guards on `doc_object`, so a row whose object has moved is
            # a row this save must not overwrite.
            if params and params[3] == self.current_object and self.update_hits:
                self._rows = [{"id": 1}]
            else:
                self._rows = []
        else:
            self._rows = [{"id": 1, "slug": "terms", "doc_type": "docx",
                           "doc_object": self.current_object, "owner_email": self.owner,
                           "title": "结算条款速查表", "size_bytes": 100, "doc_pages": 1,
                           "visibility": "private", "kind": "document",
                           "doc_name": "terms.docx"}]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **kw):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeStore:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.removes = []

    def get_object(self, prefix, key):
        return self.objects[key]

    def put_object(self, prefix, key, data, content_type=None):
        self.objects[key] = data
        self.puts.append((key, len(data), content_type))
        return key

    def remove_object(self, prefix, key):
        self.removes.append(key)
        self.objects.pop(key, None)


class _Harness:
    """One endpoint call, with the world replaced by recorders."""

    def __init__(self, owner="me@example.com", viewer="me@example.com",
                 doc_type="docx", current_object="terms-20260101.docx",
                 source=None, drop_all=False, update_hits=True,
                 stored_keys=(None,)):
        self.cursor = _FakeCursor(owner, current_object, update_hits)
        self.store = _FakeStore()
        if source is not None:
            self.store.objects[current_object] = source
        self.viewer = viewer
        self.doc_type = doc_type
        self.drop_all = drop_all
        self.patches = [
            mock.patch.object(R, "_db", return_value=_FakeConn(self.cursor)),
            mock.patch.object(R, "oss_storage", self.store),
            mock.patch.object(R, "_identity", return_value=(viewer, "browser")),
            mock.patch.object(R, "request_lang", return_value=None),
            mock.patch.object(R, "DOC_BUCKET_PREFIX", "workspace-docs"),
            # ⚠️ Same signature as the real one, `(bucket_prefix, key)`. An
            # earlier version bound the fake's 1-arg method straight in and every
            # test that reached the cleanup path died with a TypeError that read
            # like a product bug.
            mock.patch.object(R, "_drop_object_quietly", self._drop),
        ]

    def _drop(self, object_key):
        self.store.remove_object("workspace-docs", object_key)

    @staticmethod
    def _undecorated():
        """The route WITHOUT `_with_schema`.

        ⚠️ `functools.wraps` copies `__wrapped__`, so the original is reachable —
        and it has to be. The wrapper calls `_ensure_table()`, which opens a REAL
        connection. Run on its own, this file inherited a live local postgres and
        the schema check silently succeeded; under `scripts/ci_check.py`
        (`POSTGRES_PORT=1`, no database) the same call raised, and the whole file
        errored for a reason that had nothing to do with what it asserts.

        Testing the decorated form would mean standing up a database to test
        authorization, which is not what this file is for.
        """
        return R.save_document_content.__wrapped__

    def call(self, edits, slug="terms"):
        module = dp
        # ⚠️ Saved BEFORE patching. The first version restored with
        # `module.apply_edits = dp.apply_edits` — which assigns the module's own
        # (already-patched) attribute back to itself, so the fake survived the test
        # and answered "nothing matched" for every later one. Two unrelated tests
        # then failed for reasons that had nothing to do with them, which is the
        # worst way a test can be wrong.
        real_apply = module.apply_edits
        if self.drop_all:
            def nothing_matches(data, doc_type, payload):
                return data, [], list(payload)

            module.apply_edits = nothing_matches
        try:
            for p in self.patches:
                p.start()
            body = R.DocumentEditsIn(edits=edits)
            # ⚠️ Explicit, not a default: see the sibling note in AGENTS.md about
            # FastAPI parameter objects. Here the body is constructed by hand, but
            # the same habit applies — nothing is left to a sentinel.
            return asyncio.run(self._undecorated()(slug, body, None))
        finally:
            module.apply_edits = real_apply
            for p in reversed(self.patches):
                p.stop()


def _oids_of(html, prefix="b"):
    import re

    return re.findall(r'data-oid="(%s\d+)"' % prefix, html)


class DocumentSaveAuthTests(unittest.TestCase):
    def setUp(self):
        self.source = _docx_bytes()
        self.html = dp.preview_html(self.source, "docx", "x")
        self.first = _oids_of(self.html)[0]

    def test_owner_can_save(self):
        h = _Harness(source=self.source)
        out = h.call([{"oid": self.first, "text": "改过的标题"}])
        self.assertTrue(out["ok"])
        self.assertEqual(out["applied"], [self.first])
        self.assertEqual(out["dropped"], [])

    def test_a_stranger_gets_404_not_403(self):
        # 403 would confirm the document exists. The read path already answers 404
        # for this reason (`_require_read`), and a write must not be more talkative.
        h = _Harness(source=self.source, viewer="someone-else@example.com")
        with self.assertRaises(HTTPException) as ctx:
            h.call([{"oid": self.first, "text": "x"}])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_anonymous_caller_gets_404(self):
        h = _Harness(source=self.source, viewer="")
        with self.assertRaises(HTTPException) as ctx:
            h.call([{"oid": self.first, "text": "x"}])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_nothing_is_stored_when_the_caller_is_not_the_owner(self):
        h = _Harness(source=self.source, viewer="someone-else@example.com")
        with self.assertRaises(HTTPException):
            h.call([{"oid": self.first, "text": "x"}])
        self.assertEqual(h.store.puts, [])


class DocumentSaveRefusalsTests(unittest.TestCase):
    def setUp(self):
        self.source = _docx_bytes()
        self.html = dp.preview_html(self.source, "docx", "x")
        self.first = _oids_of(self.html)[0]

    def test_pdf_is_refused_and_nothing_is_read(self):
        h = _Harness(source=self.source, doc_type="pdf")
        with mock.patch.object(R, "_document_row", return_value={
                "slug": "terms", "doc_type": "pdf", "title": "t", "size_bytes": 1,
                "doc_pages": 1, "doc_object": "k.pdf", "owner_email": "me@example.com"}):
            with self.assertRaises(HTTPException) as ctx:
                h.call([{"oid": "x", "text": "y"}])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(h.store.puts, [])

    def test_no_edits_is_a_400(self):
        h = _Harness(source=self.source)
        with self.assertRaises(HTTPException) as ctx:
            h.call([])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(h.store.puts, [])

    def test_an_edit_without_an_oid_is_not_sent_to_storage(self):
        h = _Harness(source=self.source)
        with self.assertRaises(HTTPException) as ctx:
            h.call([{"text": "没有 oid"}])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(h.store.puts, [])

    def test_when_everything_is_dropped_nothing_is_stored(self):
        # ⚠️ The failure this exists for: an editor whose oids no longer match the
        # file. Answering 200 there would tell the reader their edit is kept while
        # the bytes on disk are untouched.
        h = _Harness(source=self.source, drop_all=True)
        with self.assertRaises(HTTPException) as ctx:
            h.call([{"oid": "b9999", "text": "x"}])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(h.store.puts, [])


class DocumentSaveStorageTests(unittest.TestCase):
    def setUp(self):
        self.source = _docx_bytes()
        self.html = dp.preview_html(self.source, "docx", "x")
        self.oids = _oids_of(self.html)
        self.old_key = "terms-20260101.docx"

    def test_the_previous_file_is_kept_as_a_version(self):
        h = _Harness(source=self.source, current_object=self.old_key)
        out = h.call([{"oid": self.oids[0], "text": "改过了"}])
        self.assertEqual(len(h.store.puts), 1)
        new_key = h.store.puts[0][0]
        # ⚠️ A NEW key, and the old one still readable. Re-uploading a document
        # sweeps the old object, which is right when the owner hands over a
        # replacement on purpose and wrong for a few words changed in place.
        self.assertNotEqual(new_key, self.old_key)
        self.assertIn(self.old_key, h.store.objects)
        self.assertIn("改过了", dp.preview_html(h.store.objects[new_key], "docx", "x"))
        self.assertEqual(h.store.removes, [])

    def test_the_row_points_at_the_new_object(self):
        h = _Harness(source=self.source, current_object=self.old_key)
        h.call([{"oid": self.oids[0], "text": "改过了"}])
        new_key = h.store.puts[0][0]
        update = [sql for sql, _ in h.cursor.executed if sql.upper().startswith("UPDATE")][0]
        self.assertIn("doc_object = %s", update)
        # And the guard clause is there: without `AND doc_object = %s` the second
        # person's save silently discards the first one's file.
        self.assertIn("AND doc_object = %s", update)

    def test_a_concurrent_save_is_refused_and_its_bytes_are_dropped(self):
        # Someone else saved between our read and our write. Our `UPDATE` matched
        # no row, so the file we stored belongs to nobody — it must be removed, and
        # the reader must be told to reopen, not shown a success.
        h = _Harness(source=self.source, current_object="moved-by-someone-else.docx",
                     update_hits=False)
        with self.assertRaises(HTTPException) as ctx:
            h.call([{"oid": self.oids[0], "text": "改过了"}])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(h.store.puts), 1)
        orphan = h.store.puts[0][0]
        # ⚠️ Precisely "the object WE stored is gone", not "the store is empty":
        # the file that was already there is somebody else's live document and
        # must be untouched. An over-broad assertion would also forbid the correct
        # behaviour, and would pass if the cleanup removed the wrong key.
        self.assertEqual(h.store.removes, [orphan])
        self.assertNotIn(orphan, h.store.objects)
        self.assertIn("moved-by-someone-else.docx", h.store.objects)

    def test_a_duplicate_oid_is_reported_rather_than_silently_collapsed(self):
        h = _Harness(source=self.source)
        out = h.call([{"oid": self.oids[0], "text": "第一次"},
                      {"oid": self.oids[0], "text": "最后一次"}])
        self.assertEqual(out["dropped"], [self.oids[0]])
        html = dp.preview_html(h.store.objects[h.store.puts[0][0]], "docx", "x")
        self.assertIn("最后一次", html)
        self.assertNotIn("第一次", html)

    def test_a_partly_resolvable_save_still_writes_what_it_could(self):
        h = _Harness(source=self.source)
        out = h.call([{"oid": self.oids[0], "text": "改过了"},
                      {"oid": "b9999", "text": "对不上"}])
        self.assertEqual(out["applied"], [self.oids[0]])
        self.assertEqual(out["dropped"], ["b9999"])
        self.assertEqual(len(h.store.puts), 1)

    def test_a_corrupt_stored_file_is_a_422_and_stores_nothing(self):
        h = _Harness(source=b"not a docx at all")
        with self.assertRaises(HTTPException) as ctx:
            h.call([{"oid": "b0", "text": "x"}])
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(h.store.puts, [])


def _row(doc_type="docx", html=""):
    """A row shaped like the list query's, so `_meta` can serialize it.

    ⚠️ Built from `_meta`'s own key accesses rather than by hand-editing until it
    runs: a fixture that lists twenty keys is twenty chances to miss one, and the
    failure it produces is a `KeyError` in a test about something else.
    """
    row = {}
    tree = __import__("ast").parse(__import__("inspect").getsource(R._meta))
    for node in __import__("ast").walk(tree):
        if isinstance(node, __import__("ast").Subscript) and isinstance(node.value, __import__("ast").Name):
            if node.value.id == "row" and isinstance(node.slice, __import__("ast").Constant):
                row[node.slice.value] = None
    row.update({
        "id": 1, "slug": "s", "title": "t", "status": "published", "author": "agent",
        "owner_email": "me@example.com", "visibility": "private", "kind": "document",
        "doc_type": doc_type, "doc_object": "k", "doc_pages": 1, "size_bytes": 1,
        "html": html, "created_at": None, "updated_at": None,
        "doc_name": "k." + (doc_type or "html"),
    })
    return row


class EditableFlagTests(unittest.TestCase):
    """The list has to answer the same question `apply_edits` enforces."""

    def test_the_list_payload_carries_editable(self):
        self.assertTrue(R._meta(_row(doc_type="docx"), "me@example.com")["editable"])

    def test_a_pdf_card_is_not_editable(self):
        self.assertFalse(R._meta(_row(doc_type="pdf"), "me@example.com")["editable"])

    def test_an_html_report_is_not_editable(self):
        row = _row(doc_type="", html="<p>x</p>")
        self.assertFalse(R._meta(row, "me@example.com")["editable"])

    def test_xlsx_and_pptx_are_editable(self):
        for kind in ("xlsx", "pptx"):
            self.assertTrue(R._meta(_row(doc_type=kind), "me@example.com")["editable"], kind)

    def test_the_flag_does_not_depend_on_who_is_looking(self):
        # ⚠️ `editable` is a property of the FILE, not of the viewer. A borrowed
        # document answers `editable: true` and `can_manage: false` — the client
        # needs both, and folding them together here would make the list unable to
        # say "this could be edited, by someone else".
        row = _row(doc_type="docx")
        row["owner_email"] = "someone-else@example.com"
        payload = R._meta(row, "me@example.com")
        self.assertTrue(payload["editable"])
        self.assertFalse(payload["can_manage"])


if __name__ == "__main__":
    unittest.main()
