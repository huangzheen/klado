"""`/api/auth/office-seats` — the office's seat list, and what it is allowed to say.

    ../.venv312/bin/python -m unittest tests.test_office_seats

The room on the landing page is a function of this one response, so the assertions
here are about two things and nothing else:

1. **The numbers are facts.** A desk per active account, ordered by registration so
   a seat number does not move when somebody joins, and a presence derived from when
   that account's agent credential was last used. A test that only checked "the
   endpoint returns 200 and a list" would pass on a query that returned every
   account ever deleted, or one that called every stale desk "active".

2. **The exposure is the one `/colleagues` already accepts.** The landing page needs
   to draw twenty desks, so it needs names. It does NOT need the account metadata
   the admin table shows, and it must never carry an account that cannot sign in.
   A field that leaks is not a field the UI happens not to render.

⚠️ There is NO DATABASE HERE, and that is the point rather than a limitation. The
CI image points the app at a closed loopback port, so a test that opens a real
connection either fails or, worse, passes against whatever happens to be running
on a developer's machine. Every case below is driven through a fake cursor, which
also means the SQL itself can be asserted — the filters are the behaviour, and a
fake that cannot be seen reading them proves nothing.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_ENV", "local")

from klado_shared import accounts  # noqa: E402


class _FakeCursor:
    """Runs a row list back for the query it recognises, and records every statement.

    `rows` is what the join would have produced. `filters` are the predicates the
    fake actually applies, so a test can prove the query says what it claims: a fake
    that ignored the WHERE clause could not tell "active accounts only" from
    "every account ever", and that is precisely the distinction under test.
    """

    def __init__(self, rows=(), filters=()):
        self._rows = list(rows)
        self._filters = filters
        self._result = []
        self.executed = []
        # ⚠️ `rowcount` is not optional. `set_avatar` treats 0 as "no such account"
        # and returns None, which is how the router turns a write into a 400 — a
        # fake without the attribute fails with an AttributeError instead, and an
        # AttributeError in a fake reads like a bug in the code under test.
        self.rowcount = 1

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        if "FROM app_users u" not in flat:
            self._result = []
            return
        rows = self._rows
        for col, want in self._filters:
            rows = [r for r in rows if r.get(col) == want]
        self._result = rows

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _row(uid, name="Someone", role="user", agent_last_at=None, hits=0,
         created=None, avatar_animal="tiger", avatar_cloth="blue"):
    # ⚠️ `created_at` is a datetime, not a string: the code calls `.isoformat()` on
    # it. A fake that hands back a string fails every test with a confusing
    # AttributeError, which reads like a bug in the code under test.
    return {
        "id": uid, "display_name": name, "email": f"{name.lower()}@example.com",
        "role": role,
        "created_at": created or datetime(2026, 1, 1, tzinfo=timezone.utc),
        "agent_hits": hits, "agent_last_at": agent_last_at,
        "avatar_animal": avatar_animal, "avatar_cloth": avatar_cloth,
    }


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


class _Harness:
    """`patch` pair for one call, plus the cursor so a test can read the SQL back."""

    def __init__(self, rows=(), filters=()):
        self.cursor = _FakeCursor(rows, filters)
        self._patches = [
            patch.object(accounts, "_db", return_value=_FakeConn(self.cursor)),
            patch.object(accounts, "_ensure_schema", return_value=None),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False

    def call(self, **kw):
        return accounts.office_seats(**kw)

    @property
    def sql(self):
        return self.cursor.executed[0][0] if self.cursor.executed else ""


class PresenceTests(unittest.TestCase):
    """`presence` is the whole product claim, and it is derived from a timestamp at
    two boundaries. Both are pinned, because a state machine that is only ever
    exercised on its happy path is a state machine nobody has tested."""

    def test_a_seat_with_no_agent_visit_is_never(self):
        with _Harness([_row(1), _row(2)]) as h:
            out = h.call()
        self.assertEqual([s["presence"] for s in out["seats"]], ["never", "never"])

    def test_a_visit_inside_the_window_is_active(self):
        with _Harness([_row(1, agent_last_at=_ago(minutes=2), hits=7)]) as h:
            out = h.call()
        self.assertEqual(out["seats"][0]["presence"], "active")

    def test_a_visit_just_outside_the_window_is_already_idle(self):
        """One minute past the window. `>=` against the boundary is the kind of
        off-by-one that reads as fine in review and shows up as a desk that stays lit
        for an hour, so the boundary is asserted from both sides."""
        win = accounts.OFFICE_ACTIVE_WINDOW_MIN
        with _Harness([_row(1, agent_last_at=_ago(minutes=win - 1))]) as h:
            self.assertEqual(h.call()["seats"][0]["presence"], "active")
        with _Harness([_row(1, agent_last_at=_ago(minutes=win + 1))]) as h:
            self.assertEqual(h.call()["seats"][0]["presence"], "idle")

    def test_an_old_visit_is_idle_not_active(self):
        with _Harness([_row(1, agent_last_at=_ago(days=4), hits=99)]) as h:
            out = h.call()
        self.assertEqual(out["seats"][0]["presence"], "idle")
        # The count is still carried: "been here 40 times" is a fact even when the
        # agent is not at the desk right now.
        self.assertEqual(out["seats"][0]["agent_hits"], 99)

    def test_presence_has_exactly_three_values(self):
        # A fourth value would be a state the room cannot draw: a seat that is in
        # the response but not in the legend reads as a bug to the reader.
        rows = [_row(1), _row(2, agent_last_at=_ago(days=1)),
                _row(3, agent_last_at=_ago(minutes=1))]
        with _Harness(rows) as h:
            out = h.call()
        self.assertEqual({s["presence"] for s in out["seats"]}, {"never", "idle", "active"})

    def test_never_never_carries_a_timestamp(self):
        with _Harness([_row(1)]) as h:
            out = h.call()
        self.assertIsNone(out["seats"][0]["agent_last_at"])

    def test_the_window_travels_in_the_payload(self):
        """The caption under the legend states a number of minutes. It is rendered
        from what the server sent, because a number restated in the frontend is a
        number that will one day disagree with the rule that produced the count —
        and the reader is being asked to trust that sentence."""
        with _Harness([_row(1)]) as h:
            out = h.call()
        self.assertEqual(out["active_window_min"], accounts.OFFICE_ACTIVE_WINDOW_MIN)
        self.assertGreater(out["active_window_min"], 0)


class WorkFreshnessTests(unittest.TestCase):
    def test_old_work_is_stale_even_when_agent_is_online(self):
        row = _row(1, agent_last_at=_ago(minutes=1))
        row["updates"] = [{"state":"working", "created_at":_ago(hours=2).isoformat(), "task":"Old task"}]
        with _Harness([row]) as h:
            seat = h.call()["seats"][0]
        self.assertEqual(seat["presence"], "active")
        self.assertTrue(seat["work_stale"])
        self.assertEqual(seat["updates"], row["updates"])

    def test_recent_work_is_fresh_and_history_query_is_bounded(self):
        row = _row(1, agent_last_at=_ago(minutes=1))
        row["updates"] = [{"state":"done", "created_at":_ago(minutes=1).isoformat(), "task":"Delivered"}]
        with _Harness([row]) as h:
            seat = h.call()["seats"][0]
        self.assertFalse(seat["work_stale"])
        self.assertIn("FROM office_updates WHERE user_id = u.id ORDER BY id DESC LIMIT 12", h.sql)
        self.assertIn("GREATEST(g.last_at, work.last_at)", h.sql)


class QueryTests(unittest.TestCase):
    """The filters ARE the behaviour, so they are read off the SQL rather than
    inferred from a result. An assertion that only looks at the output cannot tell a
    correct filter from a fake that ignores it."""

    def test_presence_counts_only_agent_rows(self):
        with _Harness([_row(1)]) as h:
            h.call()
        self.assertIn("WHERE kind = 'agent' GROUP BY user_id", h.sql,
                      "存在性必须只看 kind='agent'；混进 browser 就是把"
                      "「人用过系统」说成「agent 来过」")

    def test_deleted_and_disabled_accounts_are_excluded_in_the_sql(self):
        with _Harness([_row(1)]) as h:
            h.call()
        self.assertIn("u.disabled = FALSE", h.sql)
        self.assertIn("u.deleted_at IS NULL", h.sql,
                      "回收站里的账号持有宽限期，但它不能登录，也就不是一个在职的人")

    def test_seat_order_is_registration_order(self):
        """Seat number is stable: joining order, not reverse-chronological.

        The admin table sorts newest-first, which is right for a table you are
        scanning and wrong for a room — a seat that moves every time somebody
        registers is a seat nobody can learn.
        """
        rows = [_row(1, created=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                _row(2, created=datetime(2026, 3, 1, tzinfo=timezone.utc))]
        with _Harness(rows) as h:
            seats = h.call()["seats"]
        self.assertEqual([s["user_id"] for s in seats], [1, 2])
        self.assertEqual([s["joined_at"] for s in seats],
                         sorted(s["joined_at"] for s in seats))

    def test_limit_asks_for_one_more_than_the_cap(self):
        """The extra row is how the code learns it truncated. Without it the room
        would draw twelve desks for fourteen accounts and the two missing people
        would simply not exist."""
        with _Harness([_row(1)]) as h:
            h.call(limit=5)
        self.assertEqual(h.cursor.executed[0][1], [6])

    def test_the_cap_is_bounded(self):
        for bad in (0, -1, 10_000):
            with self.subTest(limit=bad), _Harness([]) as h:
                h.call(limit=bad)
            asked = h.cursor.executed[0][1][0]
            self.assertGreaterEqual(asked, 2)
            self.assertLessEqual(asked, 501)


class TruncationTests(unittest.TestCase):
    def test_truncated_when_the_rows_exceed_the_cap(self):
        with _Harness([_row(i) for i in range(1, 5)]) as h:
            out = h.call(limit=3)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["seats"]), 3)

    def test_not_truncated_when_they_fit(self):
        with _Harness([_row(i) for i in range(1, 4)]) as h:
            out = h.call(limit=3)
        self.assertFalse(out["truncated"])
        self.assertEqual(len(out["seats"]), 3)

    def test_an_empty_deployment_is_an_empty_room_not_an_error(self):
        """No accounts is a real state on a fresh install, and the frontend draws a
        room with no desks rather than a failure page."""
        with _Harness([]) as h:
            out = h.call()
        self.assertEqual(out["seats"], [])
        self.assertFalse(out["truncated"])


class ExposureTests(unittest.TestCase):
    """The landing page draws names, not an account table. Every allowed key is
    listed, so a field added out of habit fails here instead of reaching every
    signed-in reader's page."""

    ALLOWED = {"user_id", "name", "role", "joined_at", "agent_hits",
               "agent_last_at", "presence", "avatar", "updates", "work_stale"}

    def test_only_the_allowed_fields_are_exposed(self):
        with _Harness([_row(1, agent_last_at=_ago(minutes=1))]) as h:
            out = h.call()
        self.assertEqual(set(out["seats"][0]), self.ALLOWED)

    def test_the_avatar_is_a_pair_and_nothing_else(self):
        """The avatar is a costume, not an account. It must arrive as exactly two
        controlled values — an animal and a shirt — so the SPA never has to parse
        a string the server invented, and a future field added to the costume
        cannot ride along inside it unnoticed."""
        with _Harness([_row(1, avatar_animal="cat", avatar_cloth="red")]) as h:
            out = h.call()
        self.assertEqual(out["seats"][0]["avatar"], {"animal": "cat", "cloth": "red"})

    def test_an_avatar_outside_the_vocabulary_falls_back(self):
        """A value this build has never heard of must still draw. A hand-edited
        row, a half-applied migration where one ALTER landed and the other did
        not, or a server newer than the cached SPA — all of them arrive here, and
        a blank animal in the room is the one outcome none of them may produce."""
        with _Harness([_row(1, avatar_animal="dragon", avatar_cloth="chartreuse")]) as h:
            out = h.call()
        av = out["seats"][0]["avatar"]
        self.assertIn(av["animal"], accounts.AVATAR_ANIMALS)
        self.assertIn(av["cloth"], accounts.AVATAR_CLOTHS)

    def test_a_missing_avatar_is_the_default_pair(self):
        """The columns are NOT NULL with a default, but a row written before the
        migration — or by a harness that does not select them — must still render."""
        with _Harness([_row(1, avatar_animal=None, avatar_cloth=None)]) as h:
            out = h.call()
        av = out["seats"][0]["avatar"]
        self.assertEqual(av["animal"], accounts.AVATAR_ANIMALS[0])
        self.assertEqual(av["cloth"], accounts.AVATAR_CLOTHS[1])

    def test_no_email_is_exposed(self):
        # The fake carries an email, so this fails if the query ever starts
        # selecting it — which is the whole point of a fake that has the field.
        with _Harness([_row(1)]) as h:
            out = h.call()
        self.assertNotIn("email", out["seats"][0])
        self.assertNotIn("example.com", str(out["seats"][0]))

    def test_a_name_is_always_present(self):
        rows = [_row(1, name="Ada"), _row(2, name="")]
        with _Harness(rows) as h:
            out = h.call()
        for seat in out["seats"]:
            self.assertTrue(seat["name"].strip(),
                            "没有名字的工位是一张没有账号的桌子")


class FrontendCouplingTests(unittest.TestCase):
    """Two facts that live in two files, and are worth a test because the failure
    mode is silent: a stale threshold shows a confident wrong number, and a pair
    whose English half starts with punctuation never splits at all."""

    FRONT = Path(__file__).resolve().parents[2] / "frontend" / "out" / "office.js"

    def setUp(self):
        if not self.FRONT.exists():
            self.skipTest("frontend/out/office.js is not present")
        self.js = self.FRONT.read_text(encoding="utf-8")

    def test_the_window_is_read_from_the_payload_not_recomputed(self):
        self.assertIn("active_window_min", self.js,
                      "前端必须用服务端给的窗口")
        self.assertNotRegex(self.js, r"\b\d+\s*\*\s*60\b",
                            "前端自己换算了一遍活动窗口，两边迟早对不上")

    def test_no_simulated_work_state_survives(self):
        """The seven work states are gone from the data and must be gone from the
        code that renders it.

        ⚠️ The check is on CODE, not on the whole file: the file's header still
        NAMES the removed functions, because explaining why they went is worth more
        than a grep that stays quiet. What must not exist is a call or a class
        assignment — a leftover `AgentOffice.setState` would let something put an
        agent on a desk that has never had one. So this looks for the two spellings
        that can only be live code.
        """
        body = re.sub(r"/\*.*?\*/", "", self.js, flags=re.S)      # comments out
        body = re.sub(r"^\s*//.*$", "", body, flags=re.M)          # line comments
        for gone in ("setState(", "setAgent(", "clearAgent(",
                     "st-coding", "st-search", "st-writing", "st-waiting"):
            self.assertNotIn(gone, body, f"模拟器残留：{gone}")

    def test_the_three_states_are_the_only_ones_defined(self):
        body = re.sub(r"/\*.*?\*/", "", self.js, flags=re.S)
        found = set(re.findall(r"\b(never|idle|active)\s*:", body))
        self.assertEqual(found, {"never", "idle", "active"},
                         "存在性状态必须正好是三个；多出来的那个画不出来")

    def test_every_english_pair_half_starts_with_a_latin_letter(self):
        """i18n.js's pair pattern requires the English half to begin `[A-Za-z]`, so a
        pair opening with a quote, a digit or a CJK glyph is not a pair AT ALL: the
        walker leaves the whole string and the English page renders the Chinese
        half. `audit_untranslatable_pairs.py` is the real guard for this; this
        assertion is here because that guard only scans two files and this one is
        about the coupling, not the string."""
        for m in re.finditer(r"[\u4e00-\u9fff][^'\"\n]*?\s/\s([^'\"\n]*)", self.js):
            en = m.group(1)
            self.assertTrue(en[:1].isascii() and en[:1].isalpha(),
                            f"英文侧不以拉丁字母开头，walker 不会拆它：{en[:40]!r}")


class AvatarSetterTests(unittest.TestCase):
    """`set_avatar` is the only self-service write on this account table, and its
    whole risk is that it either lets a caller edit somebody else or quietly
    rewrites a bad value into a default. Both are pinned here."""

    def test_a_valid_pair_is_written(self):
        cur = _FakeCursor()
        with patch.object(accounts, "_db", return_value=_FakeConn(cur)), \
             patch.object(accounts, "_ensure_schema", return_value=None), \
             patch.object(accounts, "get_user_by_id", return_value={"id": 7, "avatar_animal": "cat", "avatar_cloth": "green"}):
            out = accounts.set_avatar(7, "cat", "green")
        sql, params = cur.executed[-1]
        self.assertIn("avatar_animal = %s", sql)
        self.assertEqual(list(params), ["cat", "green", 7])
        self.assertEqual(out["avatar_animal"], "cat")

    def test_an_unknown_animal_is_rejected_not_coerced(self):
        """Returning None is the whole contract. Falling back to tiger would put
        an account in an animal it never chose, with no UI that could get it back
        out — the failure would be invisible and permanent."""
        cur = _FakeCursor()
        with patch.object(accounts, "_db", return_value=_FakeConn(cur)), \
             patch.object(accounts, "_ensure_schema", return_value=None):
            self.assertIsNone(accounts.set_avatar(7, "dragon", "red"))
        self.assertEqual(cur.executed, [], "a rejected avatar must not reach the database at all")

    def test_an_unknown_shirt_is_rejected(self):
        cur = _FakeCursor()
        with patch.object(accounts, "_db", return_value=_FakeConn(cur)), \
             patch.object(accounts, "_ensure_schema", return_value=None):
            self.assertIsNone(accounts.set_avatar(7, "tiger", "chartreuse"))
        self.assertEqual(cur.executed, [])

    def test_the_vocabularies_are_the_ones_the_spa_draws(self):
        """The SPA keeps its own copy of both lists (a round trip before the room
        can be drawn is not worth it, and the server validates anyway). The two
        copies are only safe while they agree, so the order and the membership are
        asserted here rather than trusted."""
        js = (Path(__file__).resolve().parents[2] / "frontend" / "out" / "office.js").read_text()
        for name in ("ANIMALS", "CLOTHS"):
            for key in getattr(accounts, "AVATAR_" + name):
                self.assertIn(f"'{key}'", js, f"{key} is in the server list but not in the SPA's")

    def test_the_router_takes_no_user_id_from_the_body(self):
        """The body model has no id field, which is the only thing that makes
        "self-service" mean self-service rather than "self-service in principle".
        A future field added here would be a privilege escalation with a test
        name that still says the right thing."""
        router = (Path(__file__).resolve().parents[2] / "api" / "routers" / "auth.py").read_text()
        # ⚠️ The block ends at the next COLUMN-ZERO line, not at the next `class`.
        # `AvatarPatch` is the last model in the file, so a "until the next class"
        # match runs off the end and swallows the health handler — which is where
        # the stray `try` came from. Bounding on indentation is what actually means
        # "the body of this class".
        block = re.search(r"^class AvatarPatch\(BaseModel\):\n(.*?)(?=\n\S)", router, re.S | re.M)
        self.assertIsNotNone(block, "AvatarPatch model not found")
        # The docstring is stripped FIRST. It explains exactly why there is no id
        # field, so a naive field scan reads the words "user id" out of the
        # explanation and fails on the very comment that justifies the design — a
        # test that is defeated by the documentation of its own invariant.
        body = re.sub(r'"""(?:.|\n)*?"""', "", block.group(1))
        fields = set(re.findall(r"^\s{4}(\w+)\s*:", body, re.M))
        self.assertEqual(fields, {"animal", "cloth"},
                         "the avatar body must carry a costume and nothing else — no id, no user_id")


if __name__ == "__main__":
    unittest.main()
