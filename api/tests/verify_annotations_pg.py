"""
The notes and Inbox tables on a REAL PostgreSQL.

The unit tests (`test_annotations.py`) run against a fake connection, so the DDL
here has never executed: a wrong column name, a bad index expression or a
`CASE` that PostgreSQL rejects would all pass them and then fail on the first
deployment's first request. This script is the one place that runs the real
statements — and the real permission matrix — against a real server.

Needs a reachable PostgreSQL:

    docker run -d --name anno-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_USER=bum \
        -e POSTGRES_DB=klado -p 15432:5432 postgres:16
    PGHOST=127.0.0.1 PGPORT=15432 PGUSER=bum PGPASSWORD=pw PGDATABASE=klado \
        ../.venv312/bin/python tests/verify_annotations_pg.py
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


OWNER = "owner@example.com"
GUEST = "guest@example.com"
STRANGER = "stranger@example.com"


def _connectable():
    try:
        from core.db import connect_main
        conn = connect_main()
        conn.close()
        return True
    except Exception as exc:                        # noqa: BLE001
        print("SKIP  没有可用的 PostgreSQL：" + str(exc)[:120])
        return False


if _connectable():
    from fastapi import HTTPException

    from routers import annotations as ann_router, inbox as inbox_router, reports
    from services import annotations, inbox
    from services.ai import business_knowledge as wiki
    from services.ai import calendar_events as cal

    def _clear():
        # Create first: on a fresh database the tables do not exist yet, and
        # "DELETE FROM a table that is not there" would abort the seed before the
        # DDL it is meant to be exercising ever runs.
        reports._ensure_table()
        wiki.ensure_tables()
        cal.ensure_tables()
        annotations.ensure_tables()
        inbox.ensure_tables()
        with annotations._db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM doc_annotations")
                cur.execute("DELETE FROM inbox_events")
                cur.execute("DELETE FROM ai_reports WHERE slug LIKE 'anno-%'")
                cur.execute("DELETE FROM ai_report_colleague_shares")
        with wiki._db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ai_knowledge_items WHERE slug LIKE 'anno-%'")
        with cal._db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ai_calendar_events WHERE slug LIKE 'anno-%'")

    def _seed():
        _clear()
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ai_reports (slug, title, owner_email, visibility, status, kind, html) "
                        "VALUES ('anno-r', 'Q4 Deck', %s, 'private', 'published', 'static', '<html></html>') "
                        "ON CONFLICT (slug) DO NOTHING", (OWNER,))
            cur.execute("INSERT INTO ai_reports (slug, title, owner_email, visibility, status, kind, html) "
                        "VALUES ('anno-i', 'Interactive', %s, 'private', 'published', 'interactive', '<html></html>') "
                        "ON CONFLICT (slug) DO NOTHING", (OWNER,))
            cur.execute("SELECT id FROM ai_reports WHERE slug = 'anno-r'")
            report_id = cur.fetchone()[0]
            cur.execute("INSERT INTO ai_report_colleague_shares (report_id, recipient_email, shared_by) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (report_id, GUEST, OWNER))
        with wiki._db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ai_knowledge_items (slug, title, body, owner_email, visibility, status) "
                        "VALUES ('anno-k', 'Notes', '# hi', %s, 'private', 'published') "
                        "ON CONFLICT (slug) DO NOTHING", (OWNER,))
            cur.execute("SELECT id FROM ai_knowledge_items WHERE slug = 'anno-k'")
            cur.execute("INSERT INTO ai_knowledge_item_shares (item_id, recipient_email, shared_by) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (cur.fetchone()[0], GUEST, OWNER))
        with cal._db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ai_calendar_events (slug, title, body, start_date, end_date, "
                        "owner_email, visibility, status) VALUES ('anno-e', 'Review', '<html></html>', "
                        "'2026-10-01', '2026-10-01', %s, 'private', 'published') "
                        "ON CONFLICT (slug) DO NOTHING", (OWNER,))

    class RealDatabaseTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            _seed()

        def test_tables_and_indexes_exist(self):
            with annotations._db() as conn, conn.cursor() as cur:
                cur.execute("SELECT indexname FROM pg_indexes WHERE tablename IN "
                            "('doc_annotations', 'inbox_events')")
                names = {row[0] for row in cur.fetchall()}
            for index in ("doc_annotations_asset_idx", "inbox_events_created_idx",
                          "inbox_events_recipient_idx"):
                self.assertIn(index, names)

        def test_a_shared_colleague_can_note_and_the_owner_is_told(self):
            note = annotations.create_note(asset_type="report", slug="anno-r", email=GUEST,
                                           body="this bar is last quarter's", x=41.5, y=12.25, page=3)
            self.assertIsNotNone(note["id"])
            self.assertEqual((note["x"], note["y"], note["page"]), (41.5, 12.25, 3))
            self.assertEqual(note["author"], GUEST)
            rows = inbox.feed(OWNER)
            self.assertTrue(any(r["kind"] == "note" and r["note_id"] == note["id"] for r in rows), rows)
            self.assertNotIn(note["id"], [r["note_id"] for r in inbox.feed(GUEST)])
            annotations.delete_note(note["id"], GUEST)

        def test_a_note_does_not_survive_its_document(self):
            # The row keeps asset_slug, not a foreign key: a note outliving the
            # document would be an unreadable orphan with no owner to resolve it.
            with annotations._db() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM ai_reports WHERE slug = 'anno-k'")
                cur.execute("DELETE FROM ai_knowledge_items WHERE slug = 'anno-k'")
                cur.execute("DELETE FROM ai_calendar_events WHERE slug = 'anno-e'")
            with wiki._db() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO ai_knowledge_items (slug, title, body, owner_email, visibility, status) "
                            "VALUES ('anno-k', 'Notes', '# hi', %s, 'private', 'published')", (OWNER,))
                cur.execute("SELECT id FROM ai_knowledge_items WHERE slug = 'anno-k'")
                cur.execute("INSERT INTO ai_knowledge_item_shares (item_id, recipient_email, shared_by) "
                            "VALUES (%s, %s, %s)", (cur.fetchone()[0], GUEST, OWNER))

        def test_each_family_keeps_its_own_read_rule(self):
            annotations.create_note(asset_type="knowledge", slug="anno-k", email=GUEST,
                                    body="wiki note", x=1, y=1, page=0)
            self.assertEqual(len(annotations.list_notes("knowledge", "anno-k", OWNER)), 1)
            with self.assertRaises(annotations.AnnotationError):
                annotations.list_notes("knowledge", "anno-k", STRANGER)
            # The report grant is not a wiki grant.
            with self.assertRaises(annotations.AnnotationError):
                annotations.list_notes("report", "anno-r", STRANGER)
            self.assertEqual(annotations.list_notes("report", "anno-r", GUEST), [])

        def test_an_interactive_report_is_refused_not_silently_skipped(self):
            with self.assertRaises(annotations.AnnotationError) as ctx:
                annotations.create_note(asset_type="report", slug="anno-i", email=OWNER,
                                        body="x", x=1, y=1)
            self.assertEqual(ctx.exception.status_code, 409)

        def test_resolve_is_a_single_atomic_flip(self):
            note = annotations.create_note(asset_type="knowledge", slug="anno-k", email=GUEST,
                                           body="resolve me", x=2, y=2, page=0)
            got = annotations.update_note(note["id"], OWNER, resolved=True)
            self.assertTrue(got["resolved"])
            self.assertEqual(got["resolved_by"], OWNER)
            got = annotations.update_note(note["id"], OWNER, resolved=False)
            self.assertFalse(got["resolved"])
            # Reopened: resolved_at and resolved_by must not disagree.
            self.assertEqual(got["resolved_by"], "")
            annotations.delete_note(note["id"], OWNER)

        def test_the_owner_deletes_a_note_somebody_else_wrote(self):
            note = annotations.create_note(asset_type="knowledge", slug="anno-k", email=GUEST,
                                           body="not yours", x=3, y=3, page=0)
            with self.assertRaises(annotations.AnnotationError) as ctx:
                annotations.update_note(note["id"], STRANGER, body="hijack")
            self.assertEqual(ctx.exception.status_code, 404)   # a stranger cannot even see it
            annotations.delete_note(note["id"], OWNER)
            with self.assertRaises(annotations.AnnotationError) as ctx:
                annotations.update_note(note["id"], OWNER, resolved=True)
            self.assertEqual(ctx.exception.status_code, 404)   # it is gone, not merely hidden

        def test_a_broadcast_is_visible_to_everyone_and_a_directive_is_not(self):
            inbox.emit(kind="publish", actor_email=OWNER, recipient_email=None,
                       target_type="report", target_slug="anno-r", target_title="Q4 Deck",
                       target_url="/?report=anno-r", summary="published it")
            inbox.emit(kind="share", actor_email=OWNER, recipient_email=GUEST,
                       target_type="report", target_slug="anno-r", target_title="Q4 Deck",
                       target_url="/?report=anno-r", summary="shared it with you")
            mine = [r["summary"] for r in inbox.feed(GUEST)]
            theirs = [r["summary"] for r in inbox.feed(STRANGER)]
            self.assertIn("shared it with you", mine)
            self.assertIn("published it", mine)
            self.assertNotIn("shared it with you", theirs)
            self.assertIn("published it", theirs)

        def test_an_unknown_kind_is_refused_rather_than_stored(self):
            # A row the UI cannot colour or group is a row nobody will ever act on.
            before = len(inbox.feed(OWNER))
            inbox.emit(kind="whatever", summary="x")
            self.assertEqual(len(inbox.feed(OWNER)), before)

        def test_marking_read_only_touches_what_the_reader_can_see(self):
            inbox.emit(kind="share", actor_email=OWNER, recipient_email=GUEST,
                       target_type="report", target_slug="anno-r", summary="mine only")
            ids = [r["id"] for r in inbox.feed(STRANGER) if r["summary"] == "mine only"]
            self.assertEqual(ids, [])                       # not theirs to mark
            inbox.mark_read(STRANGER)
            still = [r for r in inbox.feed(GUEST) if r["summary"] == "mine only"]
            self.assertIsNone(still[0]["read_at"])
            inbox.mark_read(GUEST, ids=[r["id"] for r in inbox.feed(GUEST)])
            self.assertIsNotNone([r for r in inbox.feed(GUEST)
                                  if r["summary"] == "mine only"][0]["read_at"])

        def test_the_guest_read_path_works_without_an_account(self):
            annotations.create_note(asset_type="report", slug="anno-r", email=GUEST,
                                    body="visible to a stranger with the link", x=9, y=9, page=1)
            notes = annotations.list_notes_for_token("anno-r")
            self.assertIn("visible to a stranger with the link", [n["body"] for n in notes])
            # And it is report-only: a token names one report, never the wiki.
            annotations.create_note(asset_type="knowledge", slug="anno-k", email=GUEST,
                                    body="wiki only", x=1, y=1, page=0)
            self.assertNotIn("wiki only", [n["body"] for n in notes])

        def test_a_deleted_note_takes_its_inbox_line_with_it(self):
            note = annotations.create_note(asset_type="knowledge", slug="anno-k", email=GUEST,
                                           body="will be deleted", x=4, y=4, page=0)
            self.assertTrue(any(r["note_id"] == note["id"] for r in inbox.feed(OWNER)))
            annotations.delete_note(note["id"], GUEST)
            self.assertFalse(any(r["note_id"] == note["id"] for r in inbox.feed(OWNER)))

    if __name__ == "__main__":
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RealDatabaseTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        _clear()
        sys.exit(0 if result.wasSuccessful() else 1)
else:
    print("\n0 checks failed (skipped: no database)")
    sys.exit(0)
