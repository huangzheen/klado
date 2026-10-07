"""Calendar events — the schedule rules, the permission tiers, and the agent boundary.

Same properties the wiki and the Workspace are held to (one permission model for all
three), plus the things that are only true of an event:

* **a schedule has to be a schedule** — a missing start, or an end before the start, is
  refused rather than stored and drawn as a zero-width bar nobody can click;
* **an attachment is a reference** — a slug that does not exist, or that this account may
  not read, is dropped instead of stored (otherwise the event page carries a dead link, or
  leaks the *title* of a page the reader is not allowed to see);
* **a partner is a reader** — the @-mention list and the read-access list are one list;
* **an agent may add partners, but may not publish** — widening an audience to a named
  colleague is what creating an event from Feishu means; widening it to every signed-in
  account is the decision a machine is not allowed to make;
* **the cover and the export are the Workspace's, not copies of it** — one resolver
  (`services/report_cover.py`) and one exporter (`services/knowledge_export.py`) serve both,
  and the tests assert the sharing rather than a matching second implementation;
* **a PUT merges** — a field the body leaves out keeps its stored value, so editing a
  deadline cannot destroy the event's page or trip the bilingual-summary rule;
* **the page template is filled from the event, not from the page** — the deadline, the
  schedule, the partners and the attachment LIST come from the stored event, and the
  attachment links are built here with the mount point resolved. A page without slots is
  returned byte-for-byte, so every event published before the template keeps rendering.
"""
import base64
import inspect
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers import calendar as router_module
from services import knowledge_export, report_cover
from services.ai import calendar_page
from services.ai import calendar_events as store
from services.ai.calendar_events import CalendarError, CalendarEvent, Attachment, Todo

OWNER = "owner@example.com"
MATE = "mate@example.com"
STRANGER = "other@example.com"

DECK = ('<html><body><div class="deck">'
        '<section class="slide" data-lang="en">Page 1</section>'
        '<section class="slide" data-lang="zh">第 1 页</section>'
        '</div></body></html>')
DECK_EN = ('<html><body><div class="deck">'
           '<section class="slide" data-lang="en">Page 1</section>'
           '</div></body></html>')


def _event(**overrides) -> CalendarEvent:
    base = dict(id=1, slug="q3-review", title="Q3 渠道复盘", start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 12), deadline=date(2026, 9, 15),
                summary="一句话", summary_en="One line", summary_zh="一句话",
                category="Review", tags="q3,channel", kind="review", body=DECK,
                owner_email=OWNER, visibility="private", status="published",
                size_bytes=100, created_at=None, updated_at=None,
                partners=[MATE], attachments=[Attachment("report", "q3-deck", "Q3 Deck")])
    base.update(overrides)
    return CalendarEvent(**base)


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": request.headers.get("X-Test-User", OWNER)}
        request.state.auth_kind = request.headers.get("X-Test-Kind", "browser")
        return await call_next(request)

    app.include_router(router_module.router, prefix="/api/calendar")
    return TestClient(app, raise_server_exceptions=False)


# ── the schedule ─────────────────────────────────────────────────────────────

class ScheduleTests(unittest.TestCase):
    def test_dates_are_days_not_timestamps(self):
        # The calendar is a month grid; a time of day would be precision the grid cannot
        # show, and would silently make two events on one day compare unequal.
        self.assertEqual(store._as_date("2026-09-01", field_name="d"), date(2026, 9, 1))
        self.assertEqual(store._as_date("2026-09-01T13:45:00", field_name="d"), date(2026, 9, 1))
        self.assertIsNone(store._as_date("", field_name="d"))
        self.assertIsNone(store._as_date(None, field_name="d"))

    def test_a_nonsense_date_names_the_field_and_the_value(self):
        with self.assertRaises(CalendarError) as ctx:
            store._as_date("next tuesday", field_name="start_date")
        self.assertIn("start_date", str(ctx.exception))
        self.assertIn("next tuesday", str(ctx.exception))


# ── attachments ──────────────────────────────────────────────────────────────

class AttachmentTests(unittest.TestCase):
    def test_the_object_form_and_the_shorthand_agree(self):
        self.assertEqual(store.parse_attachments([{"kind": "report", "slug": "q3-deck"}]),
                         [Attachment("report", "q3-deck", "")])
        self.assertEqual(store.parse_attachments(["knowledge:channel-notes"]),
                         [Attachment("knowledge", "channel-notes", "")])

    def test_an_unknown_kind_is_refused_not_ignored(self):
        with self.assertRaises(CalendarError) as ctx:
            store.parse_attachments([{"kind": "spreadsheet", "slug": "x"}])
        self.assertIn("report", str(ctx.exception))

    def test_duplicates_collapse_and_the_cap_is_enforced(self):
        self.assertEqual(len(store.parse_attachments(["report:a", "report:a", "report:b"])), 2)
        with self.assertRaises(CalendarError):
            store.parse_attachments([f"report:s{i}" for i in range(store.MAX_ATTACHMENTS + 1)])

    def test_a_slug_this_account_cannot_read_is_dropped(self):
        # Storing it would put a dead link on the event page — and the *title* of a page the
        # account may not read would travel into an event it may read.
        class _Cur:
            def __init__(self): self.asked = []
            def execute(self, sql, params): self.asked.append(params[0])
            def fetchone(self):
                return {"slug": "q3-deck", "title": "Q3 Deck"} if self.asked[-1] == "q3-deck" else None

        cur = _Cur()
        resolved = store.resolve_attachments(
            cur, [Attachment("report", "q3-deck"), Attachment("knowledge", "secret-notes")], OWNER)
        self.assertEqual([a.slug for a in resolved], ["q3-deck"])
        self.assertEqual(resolved[0].title, "Q3 Deck")


# ── partners and readers ─────────────────────────────────────────────────────

class PartnerTests(unittest.TestCase):
    def test_partners_are_lowercased_deduplicated_and_never_yourself(self):
        cleaned = []
        for raw in ("Mate@example.com", "mate@example.com", OWNER, STRANGER):
            value = store._clean_partner(raw)
            if value and value != OWNER.lower() and value not in cleaned:
                cleaned.append(value)
        self.assertEqual(cleaned, [MATE, STRANGER])

    def test_a_non_email_is_refused_rather_than_stored(self):
        with self.assertRaises(CalendarError):
            store._clean_partner("帮我加一下小李")

    def test_a_partner_reads_a_private_event_and_a_stranger_does_not(self):
        row = {"id": 1, "owner_email": OWNER, "visibility": "private", "status": "published"}
        with patch.object(store, "_partner_exists", side_effect=lambda i, e: e == MATE):
            self.assertTrue(store.may_read(row, OWNER))
            self.assertTrue(store.may_read(row, MATE))
            self.assertFalse(store.may_read(row, STRANGER))

    def test_the_sql_predicate_matches_the_python_rule(self):
        # Reads through the calendar go through `may_read`; reads through a list query go
        # through READABLE_PREDICATE. A gap between them is a silent permission hole.
        sql = store.READABLE_PREDICATE
        for fragment in ("owner_email = %s", "visibility = 'public'", "status = 'published'",
                         "ai_calendar_event_partners", "partner_email = %s"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sql)
        self.assertEqual(sql.count("%s"), 2)

    def test_a_draft_is_never_readable_by_partners_even_when_public(self):
        row = {"id": 1, "owner_email": OWNER, "visibility": "public", "status": "draft"}
        with patch.object(store, "_partner_exists", return_value=True):
            self.assertFalse(store.may_read(row, MATE))


# ── bilingual summary (the same rule as a Workspace report) ──────────────────

class SummaryTests(unittest.TestCase):
    def test_the_languages_come_from_the_page_itself(self):
        self.assertEqual(store.detect_langs(DECK), ["en", "zh"])
        self.assertEqual(store.detect_langs(DECK_EN), ["en"])
        self.assertTrue(store.is_bilingual(store.detect_langs(DECK)))

    def test_a_bilingual_event_needs_both_lines(self):
        with self.assertRaises(CalendarError) as ctx:
            store.bilingual_summaries("One line", "", "", [], DECK)
        self.assertIn("summary_en", str(ctx.exception))
        self.assertIn("summary_zh", str(ctx.exception))

    def test_a_complete_pair_passes_and_fills_the_plain_field(self):
        self.assertEqual(store.bilingual_summaries("", "One line", "一句话", [], DECK),
                         ("One line", "One line", "一句话"))

    def test_a_single_language_event_keeps_using_the_plain_summary(self):
        self.assertEqual(store.bilingual_summaries("One line", "", "", [], DECK_EN),
                         ("One line", "", ""))


# ── router surface ───────────────────────────────────────────────────────────

class RouterTests(unittest.TestCase):
    def test_list_passes_the_window_and_scope_to_the_store(self):
        captured = {}

        def fake(email, *, scope="mine", date_from=None, date_to=None, limit=500):
            captured.update(scope=scope, date_from=date_from, date_to=date_to,
                            email=email)
            return [_event()]

        with patch.object(store, "list_events", side_effect=fake):
            response = _client().get("/api/calendar/events",
                                     params={"scope": "mine", "from": "2026-09-01",
                                             "to": "2027-02-28"},
                                     headers={"X-Test-User": MATE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["date_from"], date(2026, 9, 1))
        self.assertEqual(captured["date_to"], date(2027, 2, 28))
        self.assertEqual(captured["email"], MATE)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["start_date"], "2026-09-01")
        self.assertEqual(body["events"][0]["partners"], [MATE])
        self.assertEqual(body["events"][0]["attachments"], [{"kind": "report", "slug": "q3-deck",
                                                             "title": "Q3 Deck"}])
        # A colleague is not the owner: the UI must not offer Edit / Delete.
        self.assertFalse(body["events"][0]["can_manage"])

    def test_a_bad_window_is_400_not_a_silent_full_list(self):
        with patch.object(store, "list_events") as listing:
            response = _client().get("/api/calendar/events", params={"from": "last month"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("from", response.json()["detail"])
        listing.assert_not_called()

    def test_someone_elses_event_is_404_not_403(self):
        with patch.object(store, "get_event", return_value=None):
            self.assertEqual(_client().get("/api/calendar/events/secret").status_code, 404)

    def test_create_passes_the_caller_identity_and_returns_the_page(self):
        with patch.object(store, "upsert_event", return_value=_event()) as created:
            response = _client().post("/api/calendar/events", json={
                "title": "Q3 渠道复盘", "start_date": "2026-09-01", "body": DECK,
                "partners": [MATE]}, headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(created.call_args.args[1], OWNER)
        self.assertTrue(response.json()["can_manage"])
        self.assertIn("body", response.json())

    def test_a_title_collision_is_409_and_lists_the_events(self):
        conflict = {"slug": "q3-review", "title": "Q3 渠道复盘", "owner_email": STRANGER,
                    "visibility": "public"}
        with patch.object(store, "upsert_event",
                          side_effect=store.CalendarConflict("Q3 渠道复盘", [conflict])):
            response = _client().post("/api/calendar/events",
                                      json={"title": "Q3 渠道复盘", "start_date": "2026-09-01",
                                            "body": DECK})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["conflicts"][0]["slug"], "q3-review")

    def test_a_bad_schedule_is_400(self):
        with patch.object(store, "upsert_event",
                          side_effect=CalendarError("end_date cannot be before start_date")):
            response = _client().post("/api/calendar/events",
                                      json={"title": "x", "start_date": "2026-09-10",
                                            "end_date": "2026-09-01", "body": DECK})
        self.assertEqual(response.status_code, 400)
        self.assertIn("before start_date", response.json()["detail"])

    def test_the_agent_may_write_events_and_name_partners(self):
        # The @-mention is the reason an agent creates an event at all; refusing it here
        # would make the Feishu flow useless.
        with patch.object(store, "upsert_event", return_value=_event()) as created:
            response = _client().post("/api/calendar/events", json={
                "title": "Q3 渠道复盘", "start_date": "2026-09-01", "body": DECK,
                "partners": [MATE]}, headers={"X-Test-Kind": "agent"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(created.call_args.args[0]["partners"], [MATE])

    def test_publishing_and_pulling_are_browser_only(self):
        with patch("routers.reports.settings") as settings:
            settings.AUTH_ENABLED = True
            for method, path in (("post", "/api/calendar/events/q3-review/publish"),
                                 ("post", "/api/calendar/events/q3-review/pull"),
                                 ("delete", "/api/calendar/events/q3-review/public")):
                with self.subTest(path=path):
                    client = _client()
                    headers = {"X-Test-Kind": "agent"}
                    response = (client.delete if method == "delete" else client.post)(path, headers=headers)
                    self.assertEqual(response.status_code, 403)

    def test_the_event_page_is_served_standalone(self):
        with patch.object(store, "get_event", return_value=_event()):
            response = _client().get("/api/calendar/events/q3-review/raw")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("data-lang", response.text)
        # The same document the detail viewer's iframe loads, so it must not be framed away.
        self.assertEqual(response.headers.get("x-frame-options"), "SAMEORIGIN")
        # "Standalone" has to mean the DOCUMENT and not the app: the SPA shell is what
        # /e/<slug> exists to avoid. This is the bug that had to be fixed twice — once for
        # the Inbox preview in general, once for the wiki page that boots inside the shell.
        for marker in ('<nav class="nav"', 'class="sidebar"', 'NAV_TAB_FOR_PAGE',
                       'class="rpt-toolbar"', 'id="page-calendar"', 'id="page-knowledge"'):
            self.assertNotIn(marker, response.text, marker)

    def test_delete_reports_whether_anything_was_removed(self):
        with patch.object(store, "delete_event", return_value=False):
            self.assertEqual(_client().delete("/api/calendar/events/nope").status_code, 404)
        with patch.object(store, "delete_event", return_value=True):
            self.assertEqual(_client().delete("/api/calendar/events/mine").status_code, 200)


class PageTodoAlignmentTests(unittest.TestCase):
    """The to-do lines a page hand-writes, read back for the editor.

    Reported 2026-09-30: an owner opened an event whose page writes its own to-do list, saw
    three lines with no owner and no date, added one to-do — and the three were gone. The page
    replacing authored lines once the event has a to-do IS deliberate (`render` says why: the
    field is the source of truth), but nothing had shown the owner those lines, so a correct
    rule read as data loss. `page_todos` is the bridge: the same lines, for the panel.
    """

    AUTHORED = ('<div class="deck"><section class="slide" data-lang="en">'
                '<ul class="ev-todo"><li>Alpha</li><li><b>Beta</b> &amp; more</li></ul>'
                '</section></div>')

    def test_a_page_that_writes_its_own_list_is_read_back(self):
        self.assertEqual(calendar_page.authored_todos(self.AUTHORED), ["Alpha", "Beta & more"])

    def test_nothing_is_read_back_without_a_list(self):
        for body in ("", "<div class='deck'><p>none</p></div>",
                     '<ul class="ev-todo" data-ev="todo"></ul>'):
            self.assertEqual(calendar_page.authored_todos(body), [], body)

    def test_the_shape_carries_them_only_while_the_event_has_none_of_its_own(self):
        none_ = router_module._shape(_event(body=self.AUTHORED, todos=[]), OWNER)
        self.assertEqual(none_["page_todos"], ["Alpha", "Beta & more"])
        some = router_module._shape(
            _event(body=self.AUTHORED, todos=[Todo(text="Alpha", due="2026-10-02")]), OWNER)
        self.assertEqual(some["page_todos"], [],
                         "the event's own to-dos win on the page — the authored lines are gone")
        self.assertEqual([t["text"] for t in some["todos"]], ["Alpha"])

    def test_the_page_really_does_replace_them_once_a_todo_exists(self):
        """The premise of the bridge above, asserted rather than assumed."""
        kept = calendar_page.render(self.AUTHORED, _event(body=self.AUTHORED, todos=[]))
        self.assertIn("Alpha", kept)
        replaced = calendar_page.render(
            self.AUTHORED, _event(body=self.AUTHORED, todos=[Todo(text="New line")]))
        self.assertIn("New line", replaced)
        self.assertNotIn("Alpha", replaced)


class ProjectDescriptionTests(unittest.TestCase):
    """The 项目描述 box: authored prose until 2026-09-30, an event field after it.

    The question that produced this ("这段文本在哪里可以编辑？") had no answer in the app — the box
    could only be written into the page body, so the panel showed nothing to edit while the page
    displayed prose. These pin both halves: the server fills the slide of the field's own
    language, and the panel reads the page's text back while the field is still empty.
    """

    AUTHORED = ('<div class="deck">'
                '<section class="slide" data-lang="en"><p class="ev-desc">English prose</p></section>'
                '<section class="slide" data-lang="zh"><p class="ev-desc">中文原稿</p></section></div>')

    def test_each_language_fills_its_own_slide(self):
        out = calendar_page.render(self.AUTHORED, _event(
            body=self.AUTHORED, description_en="Filled EN", description_zh="填入的中文"))
        self.assertIn("Filled EN", out)
        self.assertIn("填入的中文", out)
        self.assertNotIn("English prose", out)     # the field wins, same rule as the to-do list
        self.assertNotIn("中文原稿", out)

    def test_an_empty_field_leaves_that_slide_alone(self):
        out = calendar_page.render(self.AUTHORED, _event(body=self.AUTHORED, description_zh="只填中文"))
        self.assertIn("English prose", out)        # nothing was sent for EN → unchanged
        self.assertIn("只填中文", out)
        self.assertNotIn("中文原稿", out)

    def test_the_panel_reads_the_page_while_the_field_is_empty(self):
        shape = router_module._shape(_event(body=self.AUTHORED, todos=[]), OWNER)
        self.assertEqual(shape["description_en"], "")
        self.assertEqual(shape["page_desc_en"], "English prose")
        self.assertEqual(shape["page_desc_zh"], "中文原稿")
        filled = router_module._shape(_event(body=self.AUTHORED, description_en="X"), OWNER)
        self.assertEqual(filled["page_desc_en"], "", "the bridge stops once the field is set")
        self.assertEqual(filled["page_desc_zh"], "中文原稿", "…per language, not for both at once")

    def test_the_text_is_escaped_not_treated_as_markup(self):
        out = calendar_page.render(self.AUTHORED, _event(
            body=self.AUTHORED, description_en='<img src=x onerror="boom()">'))
        self.assertIn("&lt;img", out)
        self.assertNotIn("<img src=x", out)

    def test_a_put_that_omits_it_keeps_the_stored_value(self):
        given = router_module.EventIn(title="t", start_date="2026-09-01")
        merged = router_module._merged_payload(
            given, _event(description_en="kept", description_zh="保留"))
        self.assertEqual(merged["description_en"], "kept")
        self.assertEqual(merged["description_zh"], "保留")

    def test_every_path_that_must_know_the_column_does(self):
        """A column added to the dataclass but forgotten in a read or write path fails only on
        the deployed database — the fake-connection tests cannot see it. Cheap structural check.
        """
        source = inspect.getsource(store)
        for col in ("description_en", "description_zh"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col}", source, col)
            self.assertIn(col, store._SELECT_COLS, col)
            self.assertIn(col, inspect.getsource(store._row_to_event), col)
            self.assertIn(col, inspect.getsource(store.upsert_event), col)
            self.assertIn(col, inspect.getsource(router_module._shape), col)
            self.assertIn(col, inspect.getsource(router_module._merged_payload), col)


class EnsureTablesTests(unittest.TestCase):
    def test_the_schema_is_committed(self):
        """⚠️ psycopg2 runs DDL inside the transaction.

        Closing the connection without a commit rolls the CREATE TABLE back, so the tables
        never exist and every request 500s with "relation does not exist" — while the
        deployment itself looks perfectly healthy. The init path swallows the same failure
        in the lifespan, so nothing surfaces until the first real query. Asserting the
        commit is the cheap way to keep that from coming back.
        """
        calls = []

        class _Cur:
            def execute(self, sql, params=None):
                calls.append("execute")
            def close(self):
                calls.append("cursor.close")
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        class _Conn:
            def cursor(self):
                return _Cur()
            def commit(self):
                calls.append("commit")
            def close(self):
                calls.append("conn.close")

        previous = store._schema_ready
        try:
            store._schema_ready = False
            with patch.object(store, "connect_main", return_value=_Conn()):
                store.ensure_tables()
        finally:
            store._schema_ready = previous
        self.assertIn("commit", calls)
        self.assertLess(calls.index("commit"), calls.index("conn.close"))
        self.assertGreater(calls.count("execute"), 1)


class SlugTests(unittest.TestCase):
    def test_slugs_are_url_safe_and_bounded(self):
        self.assertEqual(store.clean_slug("Q3 Channel Deep Dive!", "x"), "q3-channel-deep-dive")
        self.assertTrue(len(store.clean_slug("x" * 200, "x")) <= 70)

    def test_an_all_cjk_title_keeps_its_own_characters(self):
        # ⚠️ This used to assert the opposite (`event-<hash>`), and the hash was the whole
        # point of the old assertion: with `[^a-z0-9._-]+` a Chinese title slugged to
        # nothing, so every event fell back to the hash. Those urls are unguessable,
        # unreadable in the address bar, and impossible for a reader to type from a chat
        # message. CJK is URL-safe (percent-encoded on the wire), so it is kept — which
        # makes this test a guard on that decision, not a leftover.
        slug = store.clean_slug("", "渠道月度复盘")
        self.assertEqual(slug, "渠道月度复盘")
        # Still unique, which was the only real requirement the hash was serving.
        self.assertNotEqual(slug, store.clean_slug("", "价格段矩阵"))

    def test_an_empty_title_still_falls_back_to_a_stable_hash(self):
        # The hash is NOT dead code: it is the genuinely-empty-title case, where there is
        # no text of any script left to keep.
        slug = store.clean_slug("", "")
        self.assertTrue(slug.startswith("event-"))
        self.assertEqual(slug, store.clean_slug(None, ""))


if __name__ == "__main__":
    unittest.main()

# ── attachments that name a file in the library ──────────────────────────────

class FileAttachmentTests(unittest.TestCase):
    """`kind: "file"` — the third kind, added when the event page learned to take uploads."""

    def test_the_bucket_allowlist_is_the_one_that_serves_the_bytes(self):
        # A file outside the serve-time allowlist opens a 404 the moment the owner clicks
        # it, so the two lists are one rule and are compared here rather than trusted.
        from routers import storage
        self.assertEqual(set(store.FILE_BUCKETS), set(storage._ALLOWED_BUCKETS))

    def test_a_file_reference_keeps_its_case(self):
        # Every other kind lowercases its slug; an OSS key is case-sensitive, so doing that
        # to a path would silently turn a working attachment into a 404.
        parsed = store.parse_attachments([{"kind": "file", "path": "datacenter-raw/Q3 Deck.pdf"}])
        self.assertEqual(parsed, [Attachment("file", "datacenter-raw/Q3 Deck.pdf", "")])

    def test_a_path_outside_the_library_is_refused(self):
        for bad in ("secret-bucket/x.pdf", "datacenter-raw", "datacenter-raw/../../etc/passwd",
                    "/datacenter-raw/x.pdf", ""):
            with self.assertRaises(CalendarError):
                store.parse_attachments([{"kind": "file", "path": bad}])

    def test_a_file_is_kept_without_a_database_lookup(self):
        class _Cur:                       # a file has no row to look up
            def execute(self, *a): raise AssertionError("a file must not be queried")
            def fetchone(self): raise AssertionError("a file must not be queried")

        resolved = store.resolve_attachments(
            _Cur(), [Attachment("file", "datacenter-raw/notes/Q3.pdf")], OWNER)
        self.assertEqual(resolved, [Attachment("file", "datacenter-raw/notes/Q3.pdf", "Q3.pdf")])
        named = store.resolve_attachments(
            _Cur(), [Attachment("file", "datacenter-raw/notes/Q3.pdf", "Q3 渠道复盘")], OWNER)
        self.assertEqual(named[0].title, "Q3 渠道复盘")


# ── the cover: the Workspace's mechanism, shared rather than copied ──────────

class CoverTests(unittest.TestCase):
    JPEG = {"image_base64": base64.b64encode(b"JPEG").decode(), "mime": "image/jpeg",
            "width": 1280, "height": 720, "prompt": "p", "model": "image-01"}

    def _body(self, **kw):
        base = dict(cover_base64="", cover_url="", cover_prompt="", cover_ratio="16:9",
                    cover_seed=None, cover_extra="", cover_with_title=False,
                    generate_cover=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_a_report_and_an_event_resolve_a_cover_with_the_same_function(self):
        # This is the whole claim of "reuse the Workspace's cover mechanism": one resolver,
        # so the report side cannot drift away from the calendar side.
        import routers.reports as reports
        with patch.object(report_cover, "generate_cover", return_value=self.JPEG):
            via_service = report_cover.resolve_cover(self._body(generate_cover=True), "Q3")
            via_report = reports._cover_from_body(self._body(generate_cover=True), "Q3")
        self.assertEqual(via_report, via_service)
        self.assertEqual(via_report["data"], b"JPEG")

    def test_an_external_url_is_kept_as_a_url_and_not_fetched(self):
        cover = report_cover.resolve_cover(self._body(cover_url="  https://x/y.jpg  "), "Q3")
        self.assertEqual(cover["url"], "https://x/y.jpg")
        self.assertIsNone(cover["data"])

    def test_nothing_sent_means_keep_whatever_is_stored(self):
        self.assertIsNone(report_cover.resolve_cover(self._body(), "Q3"))

    def test_a_refused_provider_is_a_502_with_the_reason(self):
        with patch.object(store, "get_event", return_value=_event()), \
             patch.object(report_cover, "generate_cover", side_effect=RuntimeError("quota")):
            response = _client().post("/api/calendar/events/q3-review/cover", json={},
                                      headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 502)
        self.assertIn("quota", response.json()["detail"])

    def test_the_payload_is_shaped_like_a_report_card(self):
        with_data = router_module._shape(_event(has_cover=True, cover_mime="image/jpeg",
                                               cover_w=1280, cover_h=720), OWNER)
        self.assertTrue(with_data["has_cover"])
        self.assertIn("/api/calendar/events/q3-review/cover", with_data["cover_url"])
        self.assertEqual((with_data["cover_w"], with_data["cover_h"]), (1280, 720))
        external = router_module._shape(_event(cover_url="https://x/y.jpg"), OWNER)
        self.assertTrue(external["has_cover"])
        self.assertEqual(external["cover_url"], "https://x/y.jpg")

    def test_generating_stores_it_on_the_event(self):
        saved = {}

        def fake_set(slug, email, cover=None, *, clear=False):
            saved.update(slug=slug, email=email, cover=cover, clear=clear)
            return _event(has_cover=True)

        with patch.object(store, "get_event", return_value=_event()), \
             patch.object(store, "set_cover", side_effect=fake_set), \
             patch.object(report_cover, "generate_cover", return_value=self.JPEG):
            response = _client().post("/api/calendar/events/q3-review/cover",
                                      json={"ratio": "16:9"}, headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved["email"], OWNER)
        self.assertEqual(saved["cover"]["data"], b"JPEG")

    def test_only_the_owner_may_set_or_clear_a_cover(self):
        # A partner reads the event, so 403 would confirm it exists and is theirs to lose.
        # POST checks ownership from the event it just read; DELETE asks the store to do it
        # (`_owned_event_id`), so both are exercised — neither may answer anything but 404.
        with patch.object(store, "get_event", return_value=_event(partners=[MATE])):
            self.assertEqual(_client().post("/api/calendar/events/q3-review/cover", json={},
                                            headers={"X-Test-User": MATE}).status_code, 404)
        with patch.object(store, "set_cover", return_value=None):
            self.assertEqual(_client().delete("/api/calendar/events/q3-review/cover",
                                              headers={"X-Test-User": MATE}).status_code, 404)

    def test_clearing_is_its_own_call(self):
        saved = {}

        def fake_set(slug, email, cover=None, *, clear=False):
            saved.update(clear=clear, cover=cover)
            return _event()

        with patch.object(store, "set_cover", side_effect=fake_set):
            self.assertEqual(_client().delete("/api/calendar/events/q3-review/cover",
                                              headers={"X-Test-User": OWNER}).status_code, 200)
        self.assertTrue(saved["clear"])

    def test_an_unknown_cover_field_is_refused_not_dropped(self):
        # The report side learned this the hard way: a cover sent under a name nobody
        # declares used to be dropped silently, and the publisher believed it went through.
        with patch.object(store, "upsert_event", return_value=_event()):
            response = _client().post("/api/calendar/events",
                                      json={"title": "Q3", "start_date": "2026-09-01",
                                            "body": DECK, "cover_picture_base": "x"},
                                      headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 400)
        self.assertIn("cover_picture_base", response.json()["detail"])


# ── export: the wiki's exporter, shared ──────────────────────────────────────

class ExportTests(unittest.TestCase):
    def test_the_export_is_the_wikis_exporter(self):
        fake = AsyncMock(return_value=(b"%PDF-1.4", "application/pdf", "pdf"))
        with patch.object(store, "get_event", return_value=_event()), \
             patch.object(knowledge_export, "export_document", new=fake):
            response = _client().post("/api/calendar/events/q3-review/export/pdf",
                                      json={"html": "<h1>Q3</h1>", "title": "Q3 渠道复盘"},
                                      headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-disposition"],
                         'attachment; filename="q3-review.pdf"')
        self.assertEqual(fake.await_args.args[0], "pdf")
        self.assertEqual(fake.await_args.args[1], "<h1>Q3</h1>")
        self.assertEqual(fake.await_args.kwargs["auth_headers"], {})   # no creds in this client

    def test_an_unknown_kind_is_400(self):
        with patch.object(store, "get_event", return_value=_event()):
            response = _client().post("/api/calendar/events/q3-review/export/pptx", json={},
                                      headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 400)

    def test_an_event_you_cannot_read_is_404(self):
        with patch.object(store, "get_event", return_value=None):
            self.assertEqual(_client().post("/api/calendar/events/secret/export/pdf",
                                            json={}).status_code, 404)

    def test_a_render_failure_is_422_not_500(self):
        with patch.object(store, "get_event", return_value=_event()), \
             patch.object(knowledge_export, "export_document",
                          new=AsyncMock(side_effect=knowledge_export.ExportError("no chromium"))):
            response = _client().post("/api/calendar/events/q3-review/export/picture", json={},
                                      headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 422)
        self.assertIn("no chromium", response.json()["detail"])


# ── a PUT merges: what the body leaves out is kept ───────────────────────────

class PartialUpdateTests(unittest.TestCase):
    def test_a_put_that_omits_the_page_keeps_the_stored_one(self):
        # Otherwise editing a deadline from the UI has to round-trip the whole 16:9 page —
        # or destroy it by omission.
        with patch.object(store, "get_event", return_value=_event()), \
             patch.object(store, "upsert_event", return_value=_event()) as written:
            response = _client().put("/api/calendar/events/q3-review",
                                     json={"deadline": "2026-09-30"},
                                     headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 200)
        data = written.call_args.args[0]
        self.assertEqual(data["body"], DECK)
        self.assertEqual(data["deadline"], "2026-09-30")
        self.assertEqual(data["partners"], [MATE])            # not wiped either
        self.assertEqual(data["attachments"][0]["slug"], "q3-deck")
        self.assertEqual(data["summary_en"], "One line")      # no bogus bilingual 400

    def test_an_empty_list_still_clears(self):
        with patch.object(store, "get_event", return_value=_event()), \
             patch.object(store, "upsert_event", return_value=_event()) as written:
            _client().put("/api/calendar/events/q3-review", json={"partners": []},
                          headers={"X-Test-User": OWNER})
        self.assertEqual(written.call_args.args[0]["partners"], [])

    def test_a_partner_who_can_read_it_still_cannot_edit_it(self):
        with patch.object(store, "get_event", return_value=_event(partners=[MATE])), \
             patch.object(store, "upsert_event") as written:
            response = _client().put("/api/calendar/events/q3-review",
                                     json={"deadline": "2026-09-30"},
                                     headers={"X-Test-User": MATE})
        self.assertEqual(response.status_code, 404)
        written.assert_not_called()


# ── the to-do list: text + who + by when, so a line can be a milestone ───────

def _todo_dict(text, assignee="", due="", done=False) -> dict:
    return {"text": text, "assignee": assignee, "due": due, "done": done}


class TodoTests(unittest.TestCase):
    """The to-do list used to be prose in the page body — the one thing on the page the
    event did not know about. It is a field now because a line carries an owner and a
    date, which is what lets the calendar draw it."""

    def test_the_shorthand_and_the_object_form_agree(self):
        parsed = store.parse_todos(["write the deck"])
        self.assertEqual(parsed, [Todo(text="write the deck", assignee="", due="", done=False)])
        self.assertEqual(store.parse_todos([{"text": "write the deck"}]), parsed)

    def test_a_row_with_no_text_is_dropped_not_refused(self):
        # The editor posts its draft rows; a row added but never typed into is normal, and
        # refusing the save would throw away the rest of the edit.
        parsed = store.parse_todos([{"text": ""}, {"text": "  "}, {"text": "keep me"}])
        self.assertEqual([t.text for t in parsed], ["keep me"])

    def test_an_assignee_has_to_be_an_address(self):
        # Stored as typed it would render as a name nobody can act on.
        with self.assertRaises(CalendarError):
            store.parse_todos([{"text": "x", "assignee": "Zhang San"}])
        self.assertEqual(store.parse_todos([{"text": "x", "assignee": "Li.Ming@example.com"}])[0].assignee,
                         "li.ming@example.com")

    def test_a_bad_due_date_is_refused_rather_than_ignored(self):
        # Silently dropping it would lose the milestone without telling anyone.
        with self.assertRaises(CalendarError):
            store.parse_todos([{"text": "x", "due": "next Friday"}])
        self.assertEqual(store.parse_todos([{"text": "x", "due": date(2026, 10, 12)}])[0].due,
                         "2026-10-12")

    def test_the_cap_is_enforced(self):
        with self.assertRaises(CalendarError):
            store.parse_todos([{"text": "t%d" % i} for i in range(store.MAX_TODOS + 1)])

    def test_round_trip_through_the_column(self):
        rows = [Todo("a", "li.ming@example.com", "2026-10-12", False),
                Todo("b", "", "", True)]
        self.assertEqual(store.parse_todos(store.todos_json(rows)), rows)

    def test_a_put_that_omits_the_list_keeps_the_stored_one(self):
        with patch.object(store, "get_event", return_value=_event(todos=[Todo("keep me")])), \
             patch.object(store, "upsert_event", return_value=_event()) as written:
            response = _client().put("/api/calendar/events/q3-review",
                                     json={"deadline": "2026-09-30"},
                                     headers={"X-Test-User": OWNER})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(written.call_args.args[0]["todos"], [_todo_dict("keep me")])

    def test_an_empty_list_still_clears(self):
        with patch.object(store, "get_event", return_value=_event(todos=[Todo("keep me")])), \
             patch.object(store, "upsert_event", return_value=_event()) as written:
            _client().put("/api/calendar/events/q3-review", json={"todos": []},
                          headers={"X-Test-User": OWNER})
        self.assertEqual(written.call_args.args[0]["todos"], [])

    def test_the_shaped_payload_carries_the_list(self):
        payload = router_module._shape(_event(todos=[Todo("a")]), OWNER)
        self.assertEqual(payload["todos"], [{"text": "a", "assignee": "", "due": "", "done": False}])

    # ── the page slot ────────────────────────────────────────────────────────
    def test_the_slot_fills_one_row_per_item_with_owner_and_date(self):
        html = calendar_page.slot_html("todo", _event(todos=[
            Todo("Align sell-in", "zhen.huang@example.com", "2026-10-12", False)]))
        self.assertIn('<li class="ev-todo-row">', html)
        self.assertIn("Align sell-in", html)
        self.assertIn("Zhen Huang", html)                      # the name, not the address
        self.assertIn('title="zhen.huang@example.com"', html)      # …which is still there
        self.assertIn('datetime="2026-10-12"', html)

    def test_a_done_item_is_marked_and_keeps_its_place(self):
        html = calendar_page.slot_html("todo", _event(todos=[Todo("done thing", done=True)]))
        self.assertIn("is-done", html)
        self.assertIn("done thing", html)                      # not hidden, not reordered

    def test_a_hand_written_list_loses_to_the_event_s_when_there_is_one(self):
        # ⚠️ The one box where the field WINS over what the author wrote: pages published
        # before 2026-09-30 hard-code their `<li>`s and carry no `data-ev="todo"`, so if the
        # slot were the only way in, the owner would edit the to-do list in the panel and see
        # nothing change on their own page.
        authored = ('<html><head></head><body><div class="deck">'
                    '<ul class="ev-todo"><li>my own line</li></ul></div></body></html>')
        out = calendar_page.render(authored, _event(todos=[Todo("from the event")]))
        body = out[out.index("<body>"):]
        self.assertNotIn("my own line", body)
        self.assertIn("from the event", body)
        self.assertEqual(body.count("<li"), 1)

    def test_a_hand_written_list_survives_when_the_event_has_no_todos(self):
        # …and the other half of that rule: an empty `todos` must never wipe an author's
        # list, or every old event would lose its to-dos the day this shipped.
        authored = ('<html><head></head><body><div class="deck">'
                    '<ul class="ev-todo"><li>my own line</li></ul></div></body></html>')
        out = calendar_page.render(authored, _event(todos=[]))
        body = out[out.index("<body>"):]
        self.assertIn("my own line", body)
        self.assertEqual(body.count("<li"), 1)

    def test_the_override_covers_a_page_with_no_slots_at_all(self):
        # A body written before the template existed may have no `data-ev` anywhere; the
        # to-do field still has to reach it.
        page = '<html><head></head><body><ul class="ev-todo"><li>old</li></ul></body></html>'
        out = calendar_page.render(page, _event(todos=[Todo("new")]))
        self.assertIn("new", out)
        # ⚠️ Assert on the authored markup, not the bare word "old": `render()` injects the
        # template stylesheet, so a plain `assertNotIn("old", out)` also fires on any comment
        # that happens to contain "old" (e.g. "the old version", "holding") — that is a prose
        # landmine, not a regression (hit 2026-09-30 while rewriting event-page.css).
        self.assertNotIn("<li>old</li>", out)
        self.assertNotIn(">old<", out)

    def test_the_empty_slot_is_filled_rather_than_left_blank(self):
        empty = '<html><head></head><body><ul class="ev-todo" data-ev="todo"></ul></body></html>'
        out = calendar_page.render(empty, _event(todos=[]))
        self.assertIn("No to-do items yet", out)
        self.assertNotIn('data-ev="todo"></ul>', out)

    def test_todo_text_is_escaped(self):
        html = calendar_page.slot_html("todo", _event(todos=[Todo('<img src=x onerror=alert(1)>')]))
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)


# ── the event page template (`services/ai/calendar_page.py`) ─────────────────

class EventPageTemplateTests(unittest.TestCase):
    """Fixed boxes filled from the event — see `verify_event_page_template_ui.py` for the
    geometry (that the boxes do not move when an author writes more text)."""

    PAGE = '<html><head></head><body><div class="deck"><div data-ev="deadline"></div></div></body></html>'

    def test_a_page_without_slots_is_returned_untouched(self):
        # Every event published before the template existed must render exactly as published.
        body = '<html><head></head><body><div class="deck"><section class="slide">hi</section></div></body></html>'
        self.assertIs(calendar_page.render(body, _event()), body)

    def test_only_an_empty_slot_is_filled(self):
        # A slot with content is the author's own text and is left alone; the contract is
        # "an empty element carrying data-ev".
        authored = '<html><head></head><body><div data-ev="deadline">1999-01-01</div></body></html>'
        self.assertIn("1999-01-01", calendar_page.render(authored, _event()))
        filled = calendar_page.render(self.PAGE, _event())
        # The slot itself now carries the value (the stylesheet mentions slots in comments,
        # so match the element, not "2026-09-15 somewhere in the document").
        self.assertIn('<div data-ev="deadline"><span class="ev-deadline">2026-09-15</span></div>',
                      filled)

    def test_an_unknown_slot_is_left_alone(self):
        page = '<html><head></head><body><div data-ev="nonsense"></div></body></html>'
        self.assertIn('data-ev="nonsense"', calendar_page.render(page, _event()))

    def test_the_data_boxes_all_come_from_the_event(self):
        filled = calendar_page.render(
            '<html><head></head><body>'
            '<div data-ev="kicker"></div><div data-ev="schedule"></div>'
            '<div data-ev="partners"></div><div data-ev="attachments"></div></body></html>', _event())
        # ⚠️ The kicker is RETIRED (2026-09-30): the head line carries the page's own TITLE
        # instead. The slot is still emptied rather than left as the author wrote it — an old
        # page must not keep a stale `KIND · category` line next to the title.
        self.assertIn('<div data-ev="kicker"></div>', filled)
        self.assertNotIn("REVIEW · Review", filled)
        self.assertIn("2026-09-01 → 2026-09-12", filled)
        self.assertIn("mate@example.com", filled)
        self.assertIn("Q3 Deck", filled)

    def test_attachment_links_are_built_here_and_stay_relative(self):
        # ⚠️ A leading slash resolves against the HOST root, where the platform gateway answers
        # `{"code":1002,…}` — the same trap as the file library's Download button.
        for kind, slug, expected in (
                ("report", "q3-deck", "r/q3-deck"),
                ("knowledge", "channel-notes", "?kb=channel-notes"),
                ("file", "datacenter-raw/a b.pdf",
                 "api/storage/serve?path=datacenter-raw%2Fa%20b.pdf&download=1")):
            href = calendar_page.attachment_href(Attachment(kind, slug))
            self.assertEqual(href, expected)
            self.assertFalse(href.startswith("/"), href)
        html = calendar_page.slot_html("attachments",
                                       _event(attachments=[Attachment("knowledge", "k", "K")]))
        self.assertIn('target="_blank"', html)                   # never replace the event page

    def test_slot_values_are_escaped(self):
        html = calendar_page.slot_html("title", _event(title='<img src=x onerror=alert(1)>'))
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        names = calendar_page.slot_html("partners", _event(partners=['a"b@example.com']))
        self.assertNotIn('a"b', names)

    def test_partners_lead_with_a_name_and_keep_the_address_small(self):
        html = calendar_page.slot_html("partners", _event(partners=["zhen.huang@example.com"]))
        self.assertIn("Zhen Huang", html)
        self.assertIn("<small>zhen.huang@example.com</small>", html)

    def test_nothing_is_left_as_an_empty_box(self):
        ev = _event(deadline=None, partners=[], attachments=[])
        self.assertIn("is-none", calendar_page.slot_html("deadline", ev))
        self.assertEqual(calendar_page.slot_html("partners", ev).count("ev-empty"), 1)
        self.assertEqual(calendar_page.slot_html("attachments", ev).count("ev-empty"), 1)

    def test_the_template_css_is_injected_after_the_authors_own_css(self):
        page = ('<html><head><style>.ev-title{color:red}</style></head>'
                '<body><div data-ev="title"></div></body></html>')
        out = calendar_page.render(page, _event())
        self.assertGreater(out.index("data-event-page"), out.index(".ev-title{color:red}"))
        self.assertLess(out.index("data-event-page"), out.index("</head>"))


# ── the event type is a closed enum ──────────────────────────────────────────

class KindTests(unittest.TestCase):
    """`kind` is not free text, and the coercion is not silent about which side it lands on.

    Asked for 2026-09-30: "现在事件的类型是不是固定的字段？agent只可以在里面选？我觉得现在事件
    类型还不够丰富" — so the list grew to a product-marketing centre's week, and the two retired
    values (`event`, `milestone`) are still ACCEPTED so an older event cannot be rewritten by
    the act of saving it.
    """

    def test_the_offered_types_are_the_ones_the_calendar_draws(self):
        # `frontend/out/index.html`'s KIND_META must key off exactly this tuple: the server
        # coerces anything else, so a type added on one side only is a type that disappears
        # the next time someone saves the event.
        self.assertEqual(store.KINDS, (
            "meeting", "review", "launch", "promotion", "campaign", "media", "content",
            "offline", "training", "research", "planning", "other"))
        self.assertEqual(store.LEGACY_KINDS, ("event", "milestone"))
        self.assertEqual(set(store.KINDS) & set(store.LEGACY_KINDS), set())

    def test_the_meeting_type_exists(self):
        # "还有个类型是「会议」，一定要加上"
        self.assertIn("meeting", store.KINDS)

    def test_milestone_is_no_longer_offered(self):
        # The milestone this product wants is a to-do's `due` INSIDE an event, not an event of
        # its own — so it must not be offered even though old rows still carry it.
        self.assertNotIn("milestone", store.KINDS)
        self.assertNotIn("event", store.KINDS)

    def test_every_offered_type_survives_the_coercion(self):
        for kind in store.KINDS:
            self.assertEqual(store.coerce_kind(kind), kind)

    def test_a_retired_type_is_kept_rather_than_rewritten(self):
        # ⚠️ The lockout this avoids: opening an older event in the panel and saving it must not
        # quietly change what it is.
        self.assertEqual(store.coerce_kind("milestone"), "milestone")
        self.assertEqual(store.coerce_kind("event"), "event")

    def test_an_invented_type_falls_into_other_not_into_a_real_one(self):
        # The old fallback was `event`, which handed back a plausible-looking event carrying a
        # type the caller never sent.
        for invented in ("webinar", "Trade Show", "MEETING", "launch2", "", None, "other "):
            self.assertEqual(store.coerce_kind(invented), "other", invented)

    def test_the_model_default_and_the_coercion_agree(self):
        # Otherwise an event created without a type would be `other` on the way in and on the
        # way out of two different code paths.
        from routers.calendar import EventIn
        self.assertEqual(EventIn(title="x", start_date="2026-09-01", body="<html></html>").kind,
                         "other")
