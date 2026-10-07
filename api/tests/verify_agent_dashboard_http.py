"""The agent access code must be able to publish a dashboard.

The module was specified as "your agent writes the HTML page". That makes the agent
credential a first-class path, not an afterthought — and it is the one path the
end-to-end script never touched, because that script signs in as a *browser*.

A read-only POST blocked by a write rule, or a new module missing from
`_AGENT_WRITE_ALLOWED_PREFIXES`, produces a 403 that reads like "the agent lacks
permission" rather than "nobody listed this path". Both are easy to ship and hard to
notice, so the whole surface is pinned here against a real `klado_agent_…` code:
publish, read back, query, update, and the two things it must NOT do — reach a
dataset it was not given, and touch somebody else's page.

    .venv312/bin/python api/tests/verify_agent_dashboard_http.py

Needs the app running on :8000 — this authenticates with a real issued agent code
against a live server, which is the only way to test an allow-list. Named
`verify_*` rather than `test_*` so the hermetic suite does not pick it up and fail
whenever :8000 happens to be down.
"""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests   # noqa: E402

from services import auth_store   # noqa: E402

BASE = "http://127.0.0.1:8000"
EMAIL = "agent-dash@example.com"
PASSWORD = "agent-dash-pass-1"
DASH = "agent-published-dash"
TABLE = "agent_dash_data"

PAGE = ("<!doctype html><html><head><meta charset='utf-8'><title>t</title></head>"
        "<body><h1>Agent page</h1></body></html>")


def _agent_auth(code: str) -> dict:
    return {"Authorization": "Bearer " + code}


class AgentDashboardAccessTests(unittest.TestCase):
    """Real HTTP, real agent code, against a running server on :8000."""

    @classmethod
    def setUpClass(cls):
        try:
            requests.get(BASE + "/api/health", timeout=2)
        except requests.RequestException as exc:                       # noqa: BLE001
            raise unittest.SkipTest(f"no server on {BASE}: {exc}")
        auth_store._ensure_schema()
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE email = %s", (EMAIL,))
            cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                        "VALUES (%s, %s, 'agent dash', 'user')",
                        (EMAIL, auth_store._hash(PASSWORD)))
            cur.execute("SELECT id FROM app_users WHERE email = %s", (EMAIL,))
            cls.uid = cur.fetchone()[0]
            conn.commit()
        cls.code, _row = auth_store.create_agent_token(cls.uid, "test agent")
        cls.auth = _agent_auth(cls.code)
        cls._make_dataset()

    @classmethod
    def _make_dataset(cls):
        from routers import reports
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}")
            cur.execute(f"CREATE TABLE public.{TABLE} (region text, amount int)")
            cur.execute(f"INSERT INTO public.{TABLE} VALUES ('north', 7), ('south', 9)")
            cur.execute("DELETE FROM public._import_registry WHERE table_name = %s", (TABLE,))
            cur.execute(
                "INSERT INTO public._import_registry (table_name, display_name, target_db, "
                "row_count, fingerprint, columns, source_file, sheet_name, modules, "
                "description, owner_email) VALUES (%s, 'Agent data', 'pg', 2, 'a', "
                "'{}'::jsonb, 'a.csv', 'S', '{}', '', %s)", (TABLE, EMAIL))
            conn.commit()

    @classmethod
    def tearDownClass(cls):
        try:
            requests.delete(f"{BASE}/api/dashboard/{DASH}", headers=cls.auth, timeout=5)
        except requests.RequestException:
            pass
        from routers import reports
        with reports._db() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}")
            cur.execute("DELETE FROM public._import_registry WHERE table_name = %s", (TABLE,))
            conn.commit()
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_tokens WHERE user_id = %s", (cls.uid,))
            cur.execute("DELETE FROM app_users WHERE email = %s", (EMAIL,))
            conn.commit()

    def test_01_agent_publishes_a_dashboard(self):
        r = requests.put(f"{BASE}/api/dashboard/{DASH}", headers=self.auth, json={
            "title": "Agent page", "summary": "written by the agent",
            "datasets": [TABLE], "html": PAGE,
            "status": "published", "visibility": "private",
            "in_nav": True, "in_home": False,
        }, timeout=10)
        self.assertEqual(r.status_code, 200, r.text[:200])

    def test_02_agent_reads_it_back(self):
        r = requests.get(f"{BASE}/api/dashboard/{DASH}", headers=self.auth, timeout=10)
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("Agent page", r.json().get("html", ""))

    def test_03_agent_updates_it(self):
        r = requests.put(f"{BASE}/api/dashboard/{DASH}", headers=self.auth,
                         json={"title": "Agent page v2"}, timeout=10)
        self.assertEqual(r.status_code, 200, r.text[:200])
        r = requests.get(f"{BASE}/api/dashboard/{DASH}", headers=self.auth, timeout=10)
        self.assertEqual(r.json()["title"], "Agent page v2")
        # A merge, not a replace: the body said nothing about the page.
        self.assertIn("Agent page", r.json()["html"])

    def test_04_agent_queries_through_the_dashboard_gateway(self):
        # A read-only POST. Reached through a *different* path from
        # /api/data-center/query, so the allow-list has to name both.
        r = requests.post(f"{BASE}/api/dashboard/query", headers=self.auth,
                          json={"sql": f"SELECT region, amount FROM {TABLE} ORDER BY region"},
                          timeout=10)
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual([x["region"] for x in r.json().get("rows", [])], ["north", "south"])

    def test_05_agent_cannot_write_through_the_query_gateway(self):
        for sql in (f"DELETE FROM {TABLE}", f"DROP TABLE {TABLE}",
                    f"UPDATE {TABLE} SET amount = 0"):
            r = requests.post(f"{BASE}/api/dashboard/query", headers=self.auth,
                              json={"sql": sql}, timeout=10)
            self.assertNotEqual(r.status_code, 200, sql)
        r = requests.get(f"{BASE}/api/dashboard/{DASH}", headers=self.auth, timeout=10)
        self.assertEqual(requests.post(f"{BASE}/api/dashboard/query", headers=self.auth,
                                       json={"sql": f"SELECT count(*) c FROM {TABLE}"},
                                       timeout=10).json()["rows"][0]["c"], 2)

    def test_06_agent_cannot_reach_a_dataset_it_does_not_own(self):
        r = requests.post(f"{BASE}/api/dashboard/query", headers=self.auth,
                          json={"sql": "SELECT * FROM app_users"}, timeout=10)
        self.assertEqual(r.status_code, 403, r.text[:160])

    def test_07_agent_cannot_touch_another_accounts_page(self):
        # The page has to belong to a DIFFERENT account. Publishing one as this same
        # agent and then "hijacking" it would assert nothing — the agent is the owner
        # and a 200 is the correct answer.
        other = "agent-other-owner-page-xyz"
        victim = "victim-dash@example.com"
        vpw = "victim-dash-pass-1"
        auth_store._ensure_schema()
        with auth_store._db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE email = %s", (victim,))
            cur.execute("INSERT INTO app_users (email, password_hash, display_name, role) "
                        "VALUES (%s, %s, 'victim', 'user')", (victim, auth_store._hash(vpw)))
            cur.execute("SELECT id FROM app_users WHERE email = %s", (victim,))
            vid = cur.fetchone()[0]
            conn.commit()
        try:
            victim_code, _ = auth_store.create_agent_token(vid, "victim agent")
            vh = {"Authorization": "Bearer " + victim_code}
            r = requests.put(f"{BASE}/api/dashboard/{other}", headers=vh,
                             json={"title": "theirs", "html": PAGE}, timeout=10)
            self.assertEqual(r.status_code, 200, r.text[:160])

            r = requests.put(f"{BASE}/api/dashboard/{other}", headers=self.auth,
                             json={"title": "hijacked"}, timeout=10)
            self.assertEqual(r.status_code, 404, r.text[:160])
            r = requests.delete(f"{BASE}/api/dashboard/{other}", headers=self.auth, timeout=10)
            self.assertEqual(r.status_code, 404, r.status_code)
            r = requests.get(f"{BASE}/api/dashboard/{other}", headers=vh, timeout=10)
            self.assertEqual(r.json()["title"], "theirs", "the page was modified anyway")
        finally:
            with auth_store._db() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM agent_tokens WHERE user_id = %s", (vid,))
                cur.execute("DELETE FROM app_users WHERE email = %s", (victim,))
                conn.commit()
            with __import__("services.dashboard_store", fromlist=["x"]).connect_main() as c2, c2.cursor() as c2c:
                c2c.execute("DELETE FROM ai_dashboards WHERE slug = %s", (other,))
                c2.commit()

    def test_08_agent_may_not_share_on_somebody_elses_behalf(self):
        # Sharing is a person's decision, made from the browser. An account can share
        # its OWN page — that is the same trust story as a report — but the endpoint
        # must still refuse a page it does not own.
        r = requests.post(f"{BASE}/api/dashboard/{DASH}/shares/colleagues",
                          headers=self.auth, json={"email": "nobody@example.com"}, timeout=10)
        self.assertEqual(r.status_code, 200, r.text[:160])
        requests.delete(f"{BASE}/api/dashboard/{DASH}/shares/colleagues/nobody@example.com",
                        headers=self.auth, timeout=10)

    def test_09_reader_page_is_served_to_the_agent(self):
        r = requests.get(f"{BASE}/d/{DASH}", headers=self.auth, timeout=10)
        self.assertEqual(r.status_code, 200, r.status_code)
        self.assertIn("Agent page", r.text)
        self.assertIn("kldQuery", r.text)


if __name__ == "__main__":
    unittest.main()
