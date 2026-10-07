"""`GET /api/auth/colleagues` — the colleague picker's directory.

The share boxes need to suggest registered people, which means handing an authenticated
caller a slice of the account list. That is a deliberate, narrow exception, so what is
tested here is the *narrowing*:

* only the three picker fields are selected — id / created_at / last_login_at / disabled
  are account metadata and stay on the admin page;
* deactivated accounts never appear (a share recipient must be able to sign in);
* a one-character query returns nothing instead of most of the company;
* no identity, no directory.

No database is required: the router is tested against a stubbed store, and `search_users`
against a fake cursor that records the statement it was handed (the no-DB convention of
test_business_knowledge.py / test_knowledge_router.py).

    cd api && ../.venv312/bin/python -m unittest tests.test_auth_colleagues
"""
import contextlib
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers import auth as auth_router
from services import auth_store

CALLER = {"email": "me@example.com", "role": "user"}
ROW = {"email": "anne.zhao@example.com", "display_name": "Anne Zhao", "role": "user"}


def _client(identity=CALLER) -> TestClient:
    """The auth router plus a stand-in for the auth middleware."""
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = identity
        return await call_next(request)

    app.include_router(auth_router.router, prefix="/api/auth")
    return TestClient(app)


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **kw):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def _stubbed_store(rows=()):
    """The real search_users against a cursor that records instead of connecting."""
    cur = _FakeCursor(list(rows))
    with patch.object(auth_store, "_ensure_schema"), \
            patch.object(auth_store, "_db", return_value=_FakeConn(cur)):
        yield cur


def _run(query, rows=(), limit=8, exclude=None):
    with _stubbed_store(rows) as cur:
        out = auth_store.search_users(query, limit=limit, exclude=exclude)
    return out, cur.calls


class ColleaguesRouteTests(unittest.TestCase):
    def test_requires_an_identity(self):
        self.assertEqual(_client(identity=None).get("/api/auth/colleagues?q=anne").status_code, 401)

    def test_returns_what_the_store_selected(self):
        with patch.object(auth_store, "search_users", return_value=[ROW]) as called:
            res = _client().get("/api/auth/colleagues?q=anne&limit=5")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"colleagues": [ROW], "domain": auth_store.allowed_domain()})
        called.assert_called_once_with("anne", 5)

    def test_a_short_query_cannot_be_used_as_a_directory_dump(self):
        # Exercised through the real store: one character must not even reach the table.
        with _stubbed_store() as cur:
            self.assertEqual(_client().get("/api/auth/colleagues?q=a").json()["colleagues"], [])
            self.assertEqual(cur.calls, [])

    def test_a_real_query_comes_back_mapped(self):
        with _stubbed_store([dict(ROW)]):
            body = _client().get("/api/auth/colleagues?q=anne").json()
        self.assertEqual(body["colleagues"], [ROW])

    def test_limit_is_bounded(self):
        self.assertEqual(_client().get("/api/auth/colleagues?q=anne&limit=9999").status_code, 422)
        self.assertEqual(_client().get("/api/auth/colleagues?q=anne&limit=0").status_code, 422)

    def test_missing_query_parameter_is_allowed_and_returns_nothing(self):
        with _stubbed_store() as cur:
            self.assertEqual(_client().get("/api/auth/colleagues").json()["colleagues"], [])
            self.assertEqual(cur.calls, [])


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **kw):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def _stubbed_store(rows=()):
    """The real search_users against a cursor that records instead of connecting."""
    cur = _FakeCursor(list(rows))
    with patch.object(auth_store, "_ensure_schema"), \
            patch.object(auth_store, "_db", return_value=_FakeConn(cur)):
        yield cur


def _run(query, rows=(), limit=8, exclude=None):
    with _stubbed_store(rows) as cur:
        out = auth_store.search_users(query, limit=limit, exclude=exclude)
    return out, cur.calls


class SearchUsersTests(unittest.TestCase):
    def test_one_character_never_reaches_the_database(self):
        self.assertEqual(_run("a"), ([], []))

    def test_blank_query_never_reaches_the_database(self):
        self.assertEqual(_run("   ")[1], [])

    def test_none_query_never_reaches_the_database(self):
        self.assertEqual(_run(None)[1], [])

    def test_selects_only_the_three_picker_fields(self):
        out, calls = _run("anne", [ROW])
        self.assertEqual(out, [ROW])
        sql = calls[0][0]
        self.assertIn("SELECT u.email, u.display_name, u.role FROM app_users u", sql)
        # The whole point of a separate function: the account metadata the admin table shows
        # must never ride along with a suggestion.
        for forbidden in ("u.id", "u.created_at", "u.last_login_at", "u.disabled,",
                          "password_hash"):
            self.assertNotIn(forbidden, sql.split("FROM")[0])
        self.assertEqual(calls[0][1][:2], ["%anne%", "%anne%"])

    def test_matches_email_or_display_name_and_skips_disabled(self):
        sql = _run("zhao")[1][0][0]
        self.assertIn("u.email ILIKE %s OR u.display_name ILIKE %s", sql)
        self.assertIn("u.disabled = FALSE", sql)

    def test_exact_and_prefix_matches_are_ranked_first(self):
        params = _run("anne")[1][0][1]
        self.assertEqual(params[-3:-1], ["anne%", "anne%"])   # ORDER BY … ILIKE 'anne%'

    def test_exclude_is_lowercased_and_only_added_when_needed(self):
        sql, params = _run("anne", exclude=["ANNE.ZHAO@example.com", "  "])[1][0]
        self.assertIn("lower(u.email) <> ALL(%s)", sql)
        self.assertEqual(params[2], ["anne.zhao@example.com"])
        plain_sql, plain_params = _run("anne")[1][0]
        self.assertNotIn("<> ALL", plain_sql)
        self.assertEqual(len(plain_params), 5)                # 2 like + 2 order + limit

    def test_limit_is_clamped_not_an_error(self):
        self.assertEqual(_run("anne", limit=999)[1][0][1][-1], 25)
        self.assertEqual(_run("anne", limit=0)[1][0][1][-1], 1)

    def test_rows_without_a_name_or_role_still_come_back(self):
        out, _ = _run("anne", [{"email": "anne.zhao@example.com", "display_name": None, "role": None}])
        self.assertEqual(out, [{"email": "anne.zhao@example.com", "display_name": "", "role": "user"}])


if __name__ == "__main__":
    unittest.main()