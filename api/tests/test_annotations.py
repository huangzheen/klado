"""
Reader notes: the annotation table, its permission boundary, and the Inbox lines
it produces.

There is no local PostgreSQL in this checkout, so the tests run against a small
fake connection (`_FakeDB`) that answers the exact statements the service issues.
The fake is deliberately dumb: it stores rows and hands them back. Everything that
matters for this feature is above it — which family's `may_read` was consulted,
what a note's author may do, who gets an Inbox line, and the fact that a refusal
is a 404 rather than a 403.
"""
import re
import unittest
from unittest import mock

from services import annotations, inbox
from services.ai import business_knowledge as wiki
from services.ai import calendar_events as cal


# ── the fake ─────────────────────────────────────────────────────────────────

ASSET_TABLES = {
    "report": "ai_reports",
    "knowledge": "ai_knowledge_items",
    "event": "ai_calendar_events",
}


def _row(**kwargs):
    from datetime import datetime, timezone
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    base = {"id": 1, "slug": "x", "title": "T", "owner_email": "owner@example.com",
            "visibility": "private", "status": "published", "kind": "static",
            "created_at": now, "updated_at": now}
    base.update(kwargs)
    return base


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._rows = []
        self.rowcount = 0
        flat = " ".join(str(sql).split())
        params = params or ()

        match = re.search(r"FROM (ai_reports|ai_knowledge_items|ai_calendar_events) WHERE slug = %s", flat)
        if match:
            asset = self.db.assets.get((match.group(1), params[0]))
            self._rows = [dict(asset)] if asset else []
            return self

        if "ai_report_colleague_shares" in flat:
            self._rows = [[1]] if (params[0], params[1]) in self.db.report_shares else []
            return self
        if "ai_knowledge_item_shares" in flat:
            self._rows = [[1]] if (params[0], params[1]) in self.db.wiki_shares else []
            return self
        if "ai_calendar_event_partners" in flat:
            self._rows = [[1]] if (params[0], params[1]) in self.db.event_partners else []
            return self

        if flat.startswith("SELECT COUNT(*)") and "doc_annotations" in flat:
            # A dict, because the service reads it by column name on a RealDictCursor —
            # the shape a list cannot represent, and the one that actually bit us.
            self._rows = [{"note_count": len(self.db.notes)}]
            return self

        if flat.startswith("SELECT * FROM doc_annotations WHERE id ="):
            note = self.db.notes.get(int(params[0]))
            self._rows = [dict(note)] if note else []
            return self

        if flat.startswith("SELECT * FROM doc_annotations WHERE asset_type = 'report'"):
            notes = [dict(n) for n in self.db.notes.values() if n["asset_slug"] == params[0]]
            notes.sort(key=lambda n: (n["page"] or 0, n["id"]))
            self._rows = notes
            return self

        if flat.startswith("SELECT * FROM doc_annotations WHERE asset_type"):
            kind, slug = params[0], params[1]
            rows = [dict(n) for n in self.db.notes.values()
                    if n["asset_type"] == kind and n["asset_slug"] == slug]
            rows.sort(key=lambda n: (n["page"] or 0, n["id"]))
            self._rows = rows
            return self

        if flat.startswith("INSERT INTO doc_annotations"):
            self.db._seq += 1
            note = _row(id=self.db._seq, asset_type=params[0], asset_slug=params[1],
                        asset_id=7, body=params[3], x_pct=params[4], y_pct=params[5],
                        page=params[6], author_email=params[7], resolved_at=None,
                        resolved_by=None, kind=None)
            self.db.notes[note["id"]] = note
            self._rows = [dict(note)]
            return self

        if flat.startswith("UPDATE doc_annotations SET body"):
            note = self.db.notes.get(int(params[1]))
            if note:
                note["body"] = params[0]
                self.rowcount = 1
            return self

        if "resolved_at = CASE WHEN" in flat:
            note = self.db.notes.get(int(params[3]))
            if note:
                note["resolved_at"] = _row()["created_at"] if params[0] else None
                note["resolved_by"] = params[2] if params[0] else None
                self.rowcount = 1
            return self

        if flat.startswith("DELETE FROM doc_annotations"):
            self.db.notes.pop(int(params[0]), None)
            self.rowcount = 1
            return self

        raise AssertionError("unexpected statement: " + flat[:120])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    cursor_factory = None

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def cursor(self, *a, **k):
        return _FakeCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _DB:
    def __init__(self):
        self.assets = {}
        self.notes = {}
        self.report_shares = set()
        self.wiki_shares = set()
        self.event_partners = set()
        self._seq = 0

    def add(self, asset, **kwargs):
        self.assets[(ASSET_TABLES[asset], kwargs.get("slug", "x"))] = _row(**kwargs)
        return self.assets[(ASSET_TABLES[asset], kwargs.get("slug", "x"))]


def _patched(db):
    """Point every module that opens a connection at the same fake."""
    import routers.reports as reports
    import services.ai.business_knowledge as bk
    import services.ai.calendar_events as ce

    conn = lambda: _FakeConn(db)          # noqa: E731 — one fake for all four modules
    patches = [
        mock.patch.object(annotations, "_db", conn),
        mock.patch.object(reports, "_db", conn),
        mock.patch.object(bk, "_db", conn),
        mock.patch.object(ce, "_db", conn),
        mock.patch.object(annotations, "ensure_tables", lambda: None),
    ]
    for p in patches:
        p.start()
    return patches


def _stop(patches):
    for p in reversed(patches):
        p.stop()


OWNER = "owner@example.com"
GUEST = "guest@example.com"
STRANGER = "nobody@example.com"
OTHER = "other@example.com"


# ── tests ────────────────────────────────────────────────────────────────────

class NoteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = _DB()
        self.patches = _patched(self.db)
        self.emitted = []
        self.emit_patch = mock.patch.object(
            annotations.inbox, "emit",
            side_effect=lambda **kw: self.emitted.append(kw))
        self.emit_patch.start()
        self.addCleanup(self.emit_patch.stop)
        forget = mock.patch.object(annotations.inbox, "forget_note")
        forget.start()
        self.addCleanup(forget.stop)
        self.addCleanup(_stop, self.patches)
        self.db.add("report", slug="r1", title="Q4 Channel Deep Dive", owner_email=OWNER)
        self.db.report_shares.add((1, GUEST))

    def test_anyone_who_can_read_may_leave_a_note(self):
        note = annotations.create_note(asset_type="report", slug="r1", email=GUEST,
                                      body="this cell is wrong", x=41.25, y=12.5, page=3)
        self.assertEqual(note["author"], GUEST)
        self.assertEqual(note["page"], 3)
        self.assertEqual((note["x"], note["y"]), (41.25, 12.5))
        self.assertEqual(len(annotations.list_notes("report", "r1", OWNER)), 1)

    def test_a_stranger_gets_404_not_403(self):
        # 403 would confirm that somebody else has a document under this slug.
        with self.assertRaises(annotations.AnnotationError) as ctx:
            annotations.list_notes("report", "r1", STRANGER)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_the_owner_is_notified_once_and_the_author_is_not(self):
        annotations.create_note(asset_type="report", slug="r1", email=GUEST, body="hi", x=1, y=1)
        self.assertEqual(len(self.emitted), 1)
        line = self.emitted[0]
        self.assertEqual(line["kind"], "note")
        self.assertEqual(line["recipient_email"], OWNER)
        self.assertEqual(line["actor_email"], GUEST)
        self.assertIn("Q4 Channel Deep Dive", line["summary"])
        self.assertIn("report=", line["target_url"])

        self.emitted.clear()
        annotations.create_note(asset_type="report", slug="r1", email=OWNER, body="mine", x=1, y=1)
        self.assertEqual(self.emitted, [])

    def test_coordinates_are_clamped_to_the_page(self):
        # A note off the page is not a note: the bubble would sit outside the slide.
        note = annotations.create_note(asset_type="report", slug="r1", email=GUEST,
                                      body="b", x=-40, y=1200, page=0)
        self.assertEqual((note["x"], note["y"]), (0.0, 100.0))
        note = annotations.create_note(asset_type="report", slug="r1", email=GUEST,
                                      body="b", x="nonsense", y=float("nan"), page=0)
        self.assertEqual((note["x"], note["y"]), (0.0, 0.0))

    def test_empty_and_oversized_notes_are_refused(self):
        for body in ("", "   ", "x" * (annotations.MAX_BODY_LEN + 1)):
            with self.assertRaises(annotations.AnnotationError) as ctx:
                annotations.create_note(asset_type="report", slug="r1", email=GUEST, body=body,
                                        x=1, y=1)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_interactive_reports_are_left_to_their_own_state(self):
        self.db.add("report", slug="interactive-demo", kind="interactive")
        with self.assertRaises(annotations.AnnotationError) as ctx:
            annotations.list_notes("report", "interactive-demo", OWNER)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_document_card_accepts_notes_like_any_other_document(self):
        # An Office card (`kind='document'`) is a read-only file with no markup of its own —
        # exactly what this layer is for. Asked for 2026-10-01: "这些类型的文档也需要可以加notes".
        self.db.add("report", slug="contract", kind="document", title="Q4 contract",
                    owner_email=OWNER)
        note = annotations.create_note(asset_type="report", slug="contract", email=GUEST,
                                      body="this is mis-spelled", x=10, y=90, page=2)
        self.assertEqual([n["body"] for n in annotations.list_notes("report", "contract", OWNER)],
                         ["this is mis-spelled"])
        # The preview document is a *paginated* one, so the note has to name its sheet: page 2
        # is what makes the bubble land on the second slide rather than 90% down a scroll.
        self.assertEqual(note["page"], 2)

    def test_a_dynamic_report_still_keeps_its_own_right_click(self):
        self.db.add("report", slug="sliced", kind="dynamic")
        with self.assertRaises(annotations.AnnotationError) as ctx:
            annotations.create_note(asset_type="report", slug="sliced", email=OWNER,
                                    body="hi", x=1, y=1)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_note_carries_its_author_and_its_time(self):
        # The Feishu agent's view of a document: who said it, and when. Both are what makes a
        # note actionable rather than a floating opinion.
        note = annotations.create_note(asset_type="report", slug="r1", email=GUEST,
                                      body="look at row 4", x=5, y=6, page=2)
        self.assertEqual(note["author"], GUEST)
        self.assertTrue(note["created_at"])           # ISO string, straight from the row
        line = annotations.note_to_agent_line(note)
        self.assertIn(GUEST, line)
        self.assertIn("look at row 4", line)
        self.assertNotIn("None", line)
        # ⚠️ The page number is printed as stored. `page` is 1-based — the number the reader saw
        # in the "Page 2" menu — so an agent told "page 3" would be looking at the wrong page.
        self.assertIn("page 2", line)
        self.assertNotIn("page 3", line)

    def test_the_inbox_line_uses_the_same_page_number_the_reader_saw(self):
        self.db.add("report", slug="deck", kind="document", title="Q4 deck", owner_email=OWNER)
        annotations.create_note(asset_type="report", slug="deck", email=GUEST,
                                body="page two please", x=5, y=6, page=2)
        summary = self.emitted[0]["summary"]
        self.assertIn("第 2 页", summary)
        self.assertNotIn("第 3 页", summary)


class NotePermissionTests(unittest.TestCase):
    def setUp(self):
        self.db = _DB()
        self.patches = _patched(self.db)
        self.emitted = []
        self.emit_patch = mock.patch.object(annotations.inbox, "emit",
                                            side_effect=lambda **kw: self.emitted.append(kw))
        self.emit_patch.start()
        self.addCleanup(self.emit_patch.stop)
        forget = mock.patch.object(annotations.inbox, "forget_note")
        forget.start()
        self.addCleanup(forget.stop)
        self.addCleanup(_stop, self.patches)
        self.db.add("report", slug="r1", owner_email=OWNER)
        self.db.report_shares.add((1, GUEST))
        self.note = annotations.create_note(asset_type="report", slug="r1", email=GUEST,
                                            body="first", x=10, y=10)

    def test_the_author_may_edit_and_delete_their_own_note(self):
        annotations.update_note(self.note["id"], GUEST, body="second")
        self.assertEqual(self.db.notes[self.note["id"]]["body"], "second")
        annotations.delete_note(self.note["id"], GUEST)
        self.assertEqual(self.db.notes, {})

    def test_the_document_owner_may_delete_any_note_on_it(self):
        annotations.delete_note(self.note["id"], OWNER)
        self.assertEqual(self.db.notes, {})

    def test_another_reader_may_neither_edit_nor_delete(self):
        # A colleague who can read the document, but neither wrote this note nor
        # owns the document.
        self.db.report_shares.add((1, OTHER))
        self.assertEqual(annotations.list_notes("report", "r1", OTHER), [self.note])
        with self.assertRaises(annotations.AnnotationError) as ctx:
            annotations.update_note(self.note["id"], OTHER, body="hijacked")
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(annotations.AnnotationError) as ctx:
            annotations.delete_note(self.note["id"], OTHER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.db.notes[self.note["id"]]["body"], "first")

    def test_resolving_is_triage_and_any_reader_may_do_it(self):
        # "I handled this" and "actually I did not" are both things a reader has to
        # be able to say, so this is not restricted to the author.
        annotations.update_note(self.note["id"], OWNER, resolved=True)
        self.assertIsNotNone(self.db.notes[self.note["id"]]["resolved_at"])
        annotations.update_note(self.note["id"], OWNER, resolved=False)
        self.assertIsNone(self.db.notes[self.note["id"]]["resolved_at"])

    def test_resolving_notifies_the_author_but_resolving_your_own_does_not(self):
        self.emitted.clear()          # setUp's create already notified the owner
        annotations.update_note(self.note["id"], OWNER, resolved=True)
        self.assertEqual([e["recipient_email"] for e in self.emitted], [GUEST])
        self.assertEqual(self.emitted[0]["kind"], "note_resolved")
        self.emitted.clear()
        annotations.update_note(self.note["id"], GUEST, resolved=False)
        self.assertEqual(self.emitted, [])

    def test_deleting_a_note_drops_its_inbox_line(self):
        forgotten = []
        self.forget_patch = mock.patch.object(annotations.inbox, "forget_note",
                                              side_effect=forgotten.append)
        self.forget_patch.start()
        self.addCleanup(self.forget_patch.stop)
        annotations.delete_note(self.note["id"], GUEST)
        # A feed row pointing at a note that no longer exists would link to
        # something the reader cannot open.
        self.assertEqual(forgotten, [self.note["id"]])


class ThreeFamiliesTests(unittest.TestCase):
    """One table, three read rules — each delegated, none re-implemented."""

    def setUp(self):
        self.db = _DB()
        self.patches = _patched(self.db)
        self.addCleanup(_stop, self.patches)
        mock.patch.object(annotations.inbox, "emit").start()

    def test_each_family_uses_its_own_share_table(self):
        self.db.add("knowledge", slug="k1", owner_email=OWNER)
        self.db.add("event", slug="e1", owner_email=OWNER)
        with self.assertRaises(annotations.AnnotationError):
            annotations.list_notes("knowledge", "k1", GUEST)
        with self.assertRaises(annotations.AnnotationError):
            annotations.list_notes("event", "e1", GUEST)

        self.db.wiki_shares.add((1, GUEST))
        self.db.event_partners.add((1, GUEST))
        self.assertEqual(annotations.list_notes("knowledge", "k1", GUEST), [])
        self.assertEqual(annotations.list_notes("event", "e1", GUEST), [])

    def test_a_report_share_does_not_open_the_wiki(self):
        # The whole point of delegating: one family's grant must not leak into another.
        self.db.add("report", slug="r1", owner_email=OWNER)
        self.db.add("knowledge", slug="k1", owner_email=OWNER)
        self.db.report_shares.add((1, GUEST))
        self.assertEqual(annotations.list_notes("report", "r1", GUEST), [])
        with self.assertRaises(annotations.AnnotationError):
            annotations.list_notes("knowledge", "k1", GUEST)

    def test_a_public_snapshot_is_readable_by_any_signed_in_account(self):
        self.db.add("report", slug="pub", owner_email=OWNER, visibility="public")
        self.assertEqual(annotations.list_notes("report", "pub", STRANGER), [])

    def test_an_unknown_asset_type_is_a_400(self):
        with self.assertRaises(annotations.AnnotationError) as ctx:
            annotations.list_notes("invoice", "x", OWNER)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_the_token_reader_gets_notes_without_an_account(self):
        # `/s/{token}` has already authenticated the capability in the route; this
        # function must not re-check permissions, or the guest would need a login.
        self.db.notes = {5: _row(id=5, asset_type="report", asset_slug="r1", body="guest note",
                                 x_pct=1.0, y_pct=2.0, page=0, author_email=OWNER,
                                 resolved_at=None, resolved_by=None)}
        notes = annotations.list_notes_for_token("r1")
        self.assertEqual([n["body"] for n in notes], ["guest note"])


class DocUrlTests(unittest.TestCase):
    def test_every_asset_kind_gets_a_mounted_deep_link(self):
        # A link the browser follows does not go through the SPA's fetch patch, so it
        # must carry the mount point itself — `/api/…` at the host root answers 200
        # with 49 bytes of gateway JSON.
        for kind, marker in (("report", "?report="), ("knowledge", "?kb="),
                             ("event", "?event="), ("dataset", "?dc=")):
            url = annotations.doc_url(kind, "a b")
            self.assertTrue(url.endswith("/" + marker + "a%20b"), url)


class NotesAuthPathTests(unittest.TestCase):
    """Which paths the middleware resolves a caller for, and which it lets past."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def test_the_api_surfaces_require_a_session(self):
        for path in ("/api/annotations", "/api/annotations/", "/api/inbox",
                     "/api/annotations/12", "/api/inbox/read"):
            self.assertTrue(self.main.needs_identity(path), path)
            self.assertFalse(self.main._is_public_path(path), path)

    def test_the_shared_link_reads_stay_login_free(self):
        # `/s/{token}` is the app's only login-free entry point and the token in the
        # path IS the credential. `/s/{token}/annotations` joined that family; if it
        # ever needs an identity, every shared link silently 401s.
        for path in ("/s/some-token/annotations", "/s/some-token/state", "/s/some-token"):
            self.assertFalse(self.main.needs_identity(path), path)

    def test_the_ordinary_document_paths_still_need_one(self):
        for path in ("/r/any-slug", "/e/any-slug", "/api/annotations"):
            self.assertTrue(self.main.needs_identity(path), path)

    def test_an_agent_credential_may_read_but_not_write(self):
        # The middleware lets every GET through; the write gate is `_require_browser`
        # inside the route, which is where "a note is signed by a person" lives.
        self.assertFalse(self.main._agent_may_write("/api/annotations"), )
        self.assertFalse(self.main._agent_may_write("/api/annotations/12"))
        # A shared report is still the agent's own write surface.
        self.assertTrue(self.main._agent_may_write("/api/reports/x"))


class InboxVisibilityTests(unittest.TestCase):
    """
    The visibility rule, with a fake that actually applies the WHERE clause.

    A mock that returns a fixed list would pass the "my rows are here" assertion
    and fail the "someone else's row is not" one — which is the assertion that
    matters, so the fake filters.
    """

    def setUp(self):
        self.rows = [
            {"id": 1, "kind": "share", "recipient_email": "me@example.com"},
            {"id": 2, "kind": "publish", "recipient_email": None},
            {"id": 3, "kind": "share", "recipient_email": "someone-else@example.com"},
        ]
        self.sql = ""
        self.params = ()
        outer = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                outer.sql = " ".join(str(sql).split())
                outer.params = params
                return self

            def fetchall(self):
                email = outer.params[0]
                return [r for r in outer.rows
                        if r["recipient_email"] is None or r["recipient_email"] == email]

            def fetchone(self):
                return self.fetchall()[:1]

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self, *a, **k):
                return Cursor()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        self.conn = Conn()
        patches = [
            mock.patch.object(inbox, "_db", lambda: self.conn),
            mock.patch.object(inbox, "ensure_tables", lambda: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_a_row_addressed_to_someone_else_is_never_in_your_feed(self):
        ids = [r["id"] for r in inbox.feed("me@example.com")]
        self.assertIn(1, ids)          # mine
        self.assertIn(2, ids)          # a broadcast
        self.assertNotIn(3, ids)       # addressed to a third person

    def test_the_query_filters_in_the_database_not_in_python(self):
        # Filtering after the fetch would break paging, the count, and the limit.
        inbox.feed("me@example.com")
        # ⚠️ Two halves, not one string. The feed LEFT JOINs `inbox_reader_state` (the
        # per-reader "hidden" state), so the table is written out on both sides of the
        # OR and a single literal no longer matches. Split on purpose: it still fails the
        # moment either half of the visibility rule goes missing, and it no longer
        # fails just because a JOIN was added.
        self.assertIn("recipient_email IS NULL OR", self.sql)
        self.assertIn("recipient_email = %s", self.sql)
        self.assertIn("ORDER BY id DESC", self.sql)


if __name__ == "__main__":
    unittest.main()
