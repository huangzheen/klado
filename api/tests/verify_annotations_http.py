"""
The notes and Inbox features over real HTTP, with the real auth middleware.

`verify_annotations_pg.py` proves the SQL; this proves the *contracts around* it,
which is where the interesting failures live:

* an **agent credential** may read a document's notes (that is the whole point) and
  is **refused** when writing one — a rule that lives in `_require_browser`, i.e.
  in the route, not in the middleware;
* a **stranger** gets **404**, not 403, so the status code cannot be used to probe
  for other people's documents;
* a **guest on a `/s/{token}` link** sees the notes through the token-scoped GET
  and is not offered a write route at all.

Needs a reachable PostgreSQL (see verify_annotations_pg.py for the docker line)
and a free port.

    POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=15432 POSTGRES_USER=bum \
      POSTGRES_PASSWORD=pw POSTGRES_DB=klado \
      ../.venv312/bin/python tests/verify_annotations_http.py
"""
import base64
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests   # noqa: E402
import uvicorn    # noqa: E402

PORT = int(os.environ.get("ANNO_HTTP_PORT", "18899"))
BASE = f"http://127.0.0.1:{PORT}"

OWNER = ("owner", "anno-owner@example.com")
GUEST = ("guest", "anno-guest@example.com")
STRANGER = ("stranger", "anno-stranger@example.com")


def _auth(who):
    return {"Authorization": "Basic " + base64.b64encode(f"{who[1]}:anno-pass-1".encode()).decode()}


def _browser(who):
    """A browser session, which is what a note write requires."""
    session = requests.Session()
    response = session.post(f"{BASE}/api/auth/login", json={"email": who[1], "password": "anno-pass-1"})
    if response.status_code != 200:
        # Local auth is on by default; a disabled AUTH_ENABLED is the only other case.
        print("login refused (%s) — is the auth module enabled?" % response.status_code)
        return None
    return session


def _reachable():
    try:
        from core.db import connect_main
        connect_main().close()
        return True
    except Exception as exc:                        # noqa: BLE001
        print("SKIP  没有可用的 PostgreSQL：" + str(exc)[:120])
        return False


if not _reachable():
    print("\n0 checks failed (skipped: no database)")
    sys.exit(0)

import main                     # noqa: E402
from routers import reports     # noqa: E402
from services import annotations, inbox   # noqa: E402
from services import auth_store  # noqa: E402

server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(80):
    try:
        if requests.get(f"{BASE}/api/health", timeout=1).status_code == 200:
            break
    except Exception:                              # noqa: BLE001
        time.sleep(0.25)
else:
    print("server did not come up")
    sys.exit(1)


def _prepare():
    """Three accounts, one report shared with one of them, and an anyone-link."""
    auth_store._ensure_schema()
    with auth_store._db() as conn, conn.cursor() as cur:
        for name, email in (OWNER, GUEST, STRANGER):
            cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
            cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                        "VALUES (%s, %s, %s, 'user')",
                        (email, auth_store._hash("anno-pass-1"), name))
    reports._ensure_table()
    with reports._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai_reports WHERE slug = 'anno-http'")
        cur.execute("DELETE FROM ai_report_colleague_shares")
        cur.execute("DELETE FROM ai_report_anyone_links")
        cur.execute("INSERT INTO ai_reports (slug, title, owner_email, visibility, status, kind, html) "
                    "VALUES ('anno-http', 'HTTP deck', %s, 'private', 'published', 'static', "
                    "'<!doctype html><html><head><title>t</title></head><body><p>hi</p></body></html>')",
                    (OWNER[1],))
        cur.execute("SELECT id FROM ai_reports WHERE slug = 'anno-http'")
        cur.execute("INSERT INTO ai_report_colleague_shares (report_id, recipient_email, shared_by) "
                    "VALUES (%s, %s, %s)", (cur.fetchone()[0], GUEST[1], OWNER[1]))
        cur.execute("INSERT INTO ai_report_anyone_links (report_id, link_id) "
                    "SELECT id, 'anno-http-link-00000001' FROM ai_reports WHERE slug = 'anno-http'")


LINK_ID = "anno-http-link-00000001"   # the token format requires 20–40 chars
_prepare()
TOKEN = reports._anyone_token(LINK_ID)
OWNER_SESSION = _browser(OWNER)
GUEST_SESSION = _browser(GUEST)


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        with annotations._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM doc_annotations WHERE asset_slug = 'anno-http'")
        with inbox._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM inbox_events")

    def test_an_agent_credential_can_read_the_notes(self):
        OWNER_SESSION.post(f"{BASE}/api/annotations",
                           json={"asset_type": "report", "slug": "anno-http",
                                 "body": "written by a person", "x": 10, "y": 20, "page": 0})
        got = requests.get(f"{BASE}/api/annotations?type=report&slug=anno-http", headers=_auth(GUEST))
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual([n["body"] for n in got.json()["notes"]], ["written by a person"])
        self.assertEqual(got.json()["notes"][0]["author"], OWNER[1])

    def test_an_agent_credential_may_not_write_one(self):
        got = requests.post(f"{BASE}/api/annotations", headers=_auth(GUEST),
                            json={"asset_type": "report", "slug": "anno-http", "body": "x",
                                  "x": 1, "y": 1, "page": 0})
        self.assertEqual(got.status_code, 403, got.text)

    def test_anyone_without_access_gets_404_not_403(self):
        got = requests.get(f"{BASE}/api/annotations?type=report&slug=anno-http", headers=_auth(STRANGER))
        self.assertEqual(got.status_code, 404, got.text)

    def test_anonymous_gets_401_not_404(self):
        got = requests.get(f"{BASE}/api/annotations?type=report&slug=anno-http")
        self.assertEqual(got.status_code, 401, got.text)

    def test_a_guest_on_the_share_link_sees_them(self):
        OWNER_SESSION.post(f"{BASE}/api/annotations",
                           json={"asset_type": "report", "slug": "anno-http",
                                 "body": "for the person outside", "x": 5, "y": 5, "page": 0})
        got = requests.get(f"{BASE}/s/{TOKEN}/annotations?type=report&slug=anno-http")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertIn("for the person outside", [n["body"] for n in got.json()["notes"]])
        # The token family is GET only — there is no write path for a guest.
        self.assertEqual(requests.post(f"{BASE}/s/{TOKEN}/annotations",
                                       json={"asset_type": "report", "slug": "anno-http"}).status_code, 405)
        # And the shared document itself still carries the layer, in read-only form.
        page = requests.get(f"{BASE}/s/{TOKEN}")
        self.assertEqual(page.status_code, 200, page.text[:200])
        self.assertIn("s/%s/annotations" % TOKEN, page.text)
        self.assertIn('"writable": false', page.text)

    def test_the_shared_document_and_the_event_page_carry_the_layer(self):
        as_owner = OWNER_SESSION.get(f"{BASE}/r/anno-http")
        self.assertEqual(as_owner.status_code, 200)
        self.assertIn("data-doc-annotations", as_owner.text)
        self.assertIn('"writable": true', as_owner.text)

    def test_an_interactive_report_is_served_without_a_note_layer(self):
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ai_reports WHERE slug = 'anno-http-i'")
            cur.execute("INSERT INTO ai_reports (slug, title, owner_email, visibility, status, kind, html) "
                        "VALUES ('anno-http-i', 'Interactive', %s, 'private', 'published', 'interactive', "
                        "'<html><body><p>x</p></body></html>')", (OWNER[1],))
        page = OWNER_SESSION.get(f"{BASE}/r/anno-http-i")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("data-doc-annotations", page.text)
        got = requests.get(f"{BASE}/api/annotations?type=report&slug=anno-http-i", headers=_auth(GUEST))
        self.assertEqual(got.status_code, 404, got.text)

    def test_the_event_page_carries_the_layer_too(self):
        from services.ai import calendar_events
        calendar_events.ensure_tables()
        with calendar_events._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ai_calendar_events WHERE slug = 'anno-http-e'")
            cur.execute("INSERT INTO ai_calendar_events (slug, title, body, start_date, end_date, "
                        "owner_email, visibility, status) VALUES ('anno-http-e', 'Review', "
                        "'<html><body><p>x</p></body></html>', '2026-10-01', '2026-10-01', %s, "
                        "'private', 'published')", (OWNER[1],))
        page = OWNER_SESSION.get(f"{BASE}/e/anno-http-e")
        self.assertEqual(page.status_code, 200, page.text[:200])
        self.assertIn("data-doc-annotations", page.text)
        self.assertIn('"type": "event"', page.text)
        # A partner who cannot edit it can still read it and note on it.
        with calendar_events._db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM ai_calendar_events WHERE slug = 'anno-http-e'")
            cur.execute("INSERT INTO ai_calendar_event_partners (event_id, partner_email) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (cur.fetchone()[0], GUEST[1]))
        got = requests.get(f"{BASE}/api/annotations?type=event&slug=anno-http-e", headers=_auth(GUEST))
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(requests.get(f"{BASE}/api/annotations?type=event&slug=anno-http-e",
                                      headers=_auth(STRANGER)).status_code, 404)

    def test_the_inbox_reaches_the_document_owner_only(self):
        note = GUEST_SESSION.post(f"{BASE}/api/annotations",
                                  json={"asset_type": "report", "slug": "anno-http",
                                        "body": "please fix this number", "x": 3, "y": 4, "page": 2}).json()
        feed = OWNER_SESSION.get(f"{BASE}/api/inbox").json()
        mine = [i for i in feed["items"] if i["note_id"] == note["id"]]
        self.assertEqual(len(mine), 1, feed)
        self.assertEqual(mine[0]["kind"], "note")
        self.assertIn("anno-http", mine[0]["target_url"])
        theirs = requests.get(f"{BASE}/api/inbox", headers=_auth(STRANGER)).json()
        self.assertEqual([i for i in theirs["items"] if i["note_id"] == note["id"]], [])
        OWNER_SESSION.delete(f"{BASE}/api/annotations/{note['id']}")   # the owner may delete any


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(HttpContractTests))
    server.should_exit = True
    time.sleep(0.4)
    sys.exit(0 if result.wasSuccessful() else 1)
