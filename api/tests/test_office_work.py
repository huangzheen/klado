"""Office updates are explicit, bounded, and always belong to the caller."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from routers import auth


class OfficeWorkTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        self.user = {"id": 42, "role": "user"}

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.current_user = self.user
            return await call_next(request)

        app.include_router(auth.router)
        self.client = TestClient(app)
        self.store = patch.object(auth.town_store, "report", return_value={"id": 1}).start()
        self.addCleanup(patch.stopall)

    def test_all_supported_states_are_persisted_for_caller(self):
        for state in ("working", "waiting", "done", "error", "idle"):
            r = self.client.post("/office-work", json={"state": state, "task": "  Task  ", "summary": "Outcome"})
            self.assertEqual(r.status_code, 201)
            self.store.assert_called_with(self.user, state, "Task", "Outcome")

    def test_anonymous_cannot_report(self):
        self.user = None
        self.assertEqual(self.client.post("/office-work", json={"state":"done", "task":"Task"}).status_code, 401)
        self.store.assert_not_called()

    def test_cannot_impersonate_another_account(self):
        self.assertEqual(self.client.post("/office-work", json={"user_id": 1, "state":"done", "task":"Task"}).status_code, 422)
        self.store.assert_not_called()

    def test_invalid_and_unbounded_reports_are_rejected(self):
        for payload in ({"state":"unknown", "task":"Task"}, {"state":"done", "task":" "},
                        {"state":"done", "task":"x"*161}, {"state":"done", "task":"Task", "summary":"x"*1201}):
            self.assertEqual(self.client.post("/office-work", json=payload).status_code, 422)
        self.store.assert_not_called()
