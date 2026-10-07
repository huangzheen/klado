"""
`agent_tokens.code_enc` — the stored copy of an agent code.

Why this is tested at all: the admin page's copy button is only useful if the string it
copies is the credential the API actually accepts. Until 2026-09-27 that button copied the
12-character `display` prefix, so whatever got pasted into a robot returned 401. These
tests pin the two things that make the fix real — the stored value round-trips, and the
public view carries the decrypted code while never leaking ciphertext or hash — plus the
degraded paths, which are the ones most likely to be "fixed" into a crash later.
"""
import base64
import hashlib
import os
import re
import unittest
from unittest.mock import patch

from services import auth_store
from klado_shared import accounts

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _row(**over):
    row = {
        "id": 1, "user_id": 7, "label": "Feishu agent", "display": "klado_agent_AbCdEf",
        "code_enc": None, "created_at": None, "created_by": "a@example.com",
        "last_used_at": None, "revoked_at": None, "revoked_by": "",
    }
    row.update(over)
    return row


class AgentCodeStorageTests(unittest.TestCase):
    def test_round_trips_the_full_code(self):
        token = auth_store.AGENT_TOKEN_PREFIX + "x" * 43
        stored = auth_store._encrypt_code(token)
        self.assertTrue(stored.startswith("enc:"))
        self.assertNotIn(token, stored)          # never stored in the clear
        self.assertEqual(auth_store._decrypt_code(stored), token)

    def test_public_view_carries_the_code_and_nothing_else(self):
        token = auth_store.AGENT_TOKEN_PREFIX + "k" * 43
        pub = auth_store._agent_token_public(_row(code_enc=auth_store._encrypt_code(token)))
        self.assertEqual(pub["code"], token)
        self.assertEqual(pub["display"], "klado_agent_AbCdEf")
        # The hash stays server-side, and the ciphertext is an implementation detail.
        self.assertNotIn("code_enc", pub)
        self.assertNotIn("token_hash", pub)

    def test_row_without_a_stored_code_reports_none(self):
        # Issued before the column existed: there is nothing usable to hand out, and the
        # page must say so rather than offering a copy button that yields a dead string.
        pub = auth_store._agent_token_public(_row())
        self.assertIsNone(pub["code"])
        self.assertEqual(pub["display"], "klado_agent_AbCdEf")

    def test_corrupt_or_foreign_ciphertext_reports_none(self):
        self.assertIsNone(auth_store._decrypt_code("enc:not-a-token"))
        self.assertIsNone(auth_store._decrypt_code("enc:"))
        self.assertIsNone(auth_store._decrypt_code(None))
        self.assertIsNone(auth_store._decrypt_code(""))
        # A value encrypted under a different SECRET_KEY (the rotation case).
        other = auth_store.Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(b"agent-tokens:another-key").digest()))
        foreign = "enc:" + other.encrypt(b"klado_agent_stale").decode()
        self.assertIsNone(auth_store._decrypt_code(foreign))

    def test_secret_key_rotation_is_not_silently_ignored(self):
        token = auth_store.AGENT_TOKEN_PREFIX + "z" * 43
        stored = auth_store._encrypt_code(token)
        self.assertEqual(auth_store._decrypt_code(stored), token)
        # The way the failure is expected to surface: no code, not a wrong one.
        self.assertNotEqual(auth_store._decrypt_code("enc:AAAA"), token)


class ConsoleMaskTests(unittest.TestCase):
    """The console masks a code in the accounts table; the mask is built from the prefix.

    ⚠️ The prefix now lives in **two** places — the Python constant and a literal in the
    console's HTML — because the mask is rendered client-side. They cannot see each other,
    and a rename on either side leaves a table full of well-formed, completely wrong text:
    `klado_agent2_••••••••3f9a`. No test anywhere else would notice, because the stored
    code, the copy button and the API all keep working perfectly. The failure is purely
    cosmetic — which is exactly why it survives.
    """

    def test_console_mask_prefix_matches_the_server(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = os.path.join(here, "..", "api-admin", "frontend", "index.html")
        with open(html, encoding="utf-8") as fh:
            source = fh.read()
        m = re.search(r"const AGENT_CODE_PREFIX = '([^']*)'", source)
        self.assertIsNotNone(
            m, "AGENT_CODE_PREFIX is gone from the console frontend; the mask has to be "
               "built from the same string the server mints codes with")
        self.assertEqual(m.group(1), auth_store.AGENT_TOKEN_PREFIX)

    def test_the_mask_does_not_leak_the_body_of_the_code(self):
        """The rendered row must not contain the secret; the copy button carries it instead."""
        token = auth_store.AGENT_TOKEN_PREFIX + "S3cretBodyHere" + "tail1234"
        masked = (auth_store.AGENT_TOKEN_PREFIX + "•" * 8 + token[-4:])
        self.assertNotIn(token, masked)
        self.assertNotIn("S3cretBodyHere", masked)
        self.assertTrue(masked.endswith(token[-4:]))


class _FakeCursor:
    """Just enough of a psycopg2 cursor to script a query result.

    The backfill's correctness is entirely in its WHERE clause, so the tests below are
    about *which rows it asks for*, not about what the database does with them.
    """

    def __init__(self, script):
        self._script = script          # sql fragment -> list of rows
        self._result = None
        self.executed = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        for frag, rows in self._script.items():
            if frag in flat:
                result = list(rows)
                # ⚠️ Honour the filter instead of only matching a fragment. A fake that
                # returns the scripted rows whatever the SQL says cannot tell "asks for
                # accounts that never had a code" from "asks for accounts with no *live*
                # code" — and those differ exactly on the revoked case, which is the one
                # that matters. This line is what makes the negative test go red.
                if "revoked_at IS NULL" in flat:
                    result = [r for r in result if not r.get("revoked_at")]
                self._result = result
                return
        self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

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


class AgentCodeInvariantTests(unittest.TestCase):
    """`ensure_initial_agent_code` / `backfill_missing_agent_codes`.

    The page used to offer a 「签发」 button for accounts with no code, which made "this
    person cannot act for an agent" a state an operator had to notice and click. The
    invariant replaced it: **every active account has a code**, established at registration,
    at the first-admin bootstrap, and repaired on every start.
    """

    def _patched(self, script, token_rows_for_user=()):
        cur = _FakeCursor(script)
        issued = []

        def fake_create(user_id, label="", created_by=""):
            issued.append((user_id, label))
            return ("klado_agent_fake", {"id": len(issued), "user_id": user_id})

        return (patch.object(accounts, "_db", return_value=_FakeConn(cur)),
                patch.object(accounts, "_ensure_schema", return_value=None),
                patch.object(accounts, "create_agent_token", side_effect=fake_create),
                issued, cur)

    def test_an_account_with_no_code_at_all_gets_one(self):
        p_db, p_schema, p_create, issued, _ = self._patched(
            {"SELECT 1 FROM agent_tokens": []})
        with p_db, p_schema, p_create:
            out = accounts.ensure_initial_agent_code(7, "signup", "a@example.com")
        self.assertIsNotNone(out)
        self.assertEqual(issued, [(7, "signup")])

    def test_an_account_that_already_has_a_row_is_left_alone(self):
        """⚠️ The load-bearing case, and the one a "no *live* code" implementation gets
        wrong. Revoking a code is a deliberate act; if the check were "has no live code",
        the next restart would hand back the credential the operator just killed, and the
        revocation would be worth nothing while looking like it worked. A revoked row still
        counts as having had one."""
        for row in ({"id": 1}, {"id": 2, "revoked_at": "2026-10-03 00:00:00"}):
            with self.subTest(row=row):
                p_db, p_schema, p_create, issued, cur = self._patched(
                    {"SELECT 1 FROM agent_tokens": [row]})
                with p_db, p_schema, p_create:
                    out = accounts.ensure_initial_agent_code(7, "signup")
                self.assertIsNone(out)
                self.assertEqual(issued, [], "a code must not be re-issued")
        # …and the reason, read off the SQL rather than off the outcome, so a fake that
        # stopped being faithful could not quietly turn this into a no-op assertion.
        sql = cur.executed[0][0]
        self.assertNotIn("revoked_at", sql,
                         "ensure_initial_agent_code 按「是否有活码」判断，吊销后会被自动复活")

    def test_the_backfill_only_asks_for_active_accounts_that_never_had_one(self):
        """The WHERE clause is the whole function. `NOT EXISTS` — not a join on live rows —
        is what keeps a revoked code from being resurrected, and the closed/disabled
        filters keep it from minting credentials that cannot authenticate."""
        p_db, p_schema, p_create, issued, cur = self._patched({
            "SELECT u.id, u.email FROM app_users u": [{"id": 7, "email": "a@example.com"}],
        })
        with p_db, p_schema, p_create:
            n = accounts.backfill_missing_agent_codes()
        self.assertEqual(n, 1)
        self.assertEqual(issued, [(7, "signup")])
        sql = cur.executed[0][0]
        self.assertIn("NOT EXISTS (SELECT 1 FROM agent_tokens", sql)
        self.assertIn("deleted_at IS NULL", sql)
        self.assertIn("disabled", sql)

    def test_one_bad_row_does_not_stop_the_others(self):
        """Startup runs this. A single failure must not leave the remaining accounts
        without codes, and must certainly not stop the app from booting."""
        cur = _FakeCursor({
            "SELECT u.id, u.email FROM app_users u": [
                {"id": 7, "email": "good@example.com"},
                {"id": 8, "email": "bad@example.com"},
                {"id": 9, "email": "also@example.com"},
            ],
        })
        issued = []

        def flaky(user_id, label="", created_by=""):
            if user_id == 8:
                raise RuntimeError("boom")
            issued.append(user_id)
            return ("klado_agent_fake", {"id": user_id, "user_id": user_id})

        with patch.object(accounts, "_db", return_value=_FakeConn(cur)), \
             patch.object(accounts, "_ensure_schema", return_value=None), \
             patch.object(accounts, "create_agent_token", side_effect=flaky):
            n = accounts.backfill_missing_agent_codes()
        self.assertEqual(n, 2)
        self.assertEqual(sorted(issued), [7, 9])

    def test_registration_and_the_bootstrap_both_go_through_it(self):
        """Two doors create an account. Both must leave it with a code, or the page shows
        a permanent 「待签发」 to somebody who did nothing wrong."""
        with open(os.path.join(REPO, "api", "routers", "auth.py"), encoding="utf-8") as fh:
            auth_src = fh.read()
        with open(os.path.join(REPO, "api", "main.py"), encoding="utf-8") as fh:
            main_src = fh.read()
        self.assertIn("create_agent_token(", auth_src,
                      "注册路径不再签发授权码")
        self.assertIn("ensure_initial_agent_code(", main_src,
                      "首个超管建号时不再签发授权码")
        self.assertIn("_backfill_agent_codes()", main_src,
                      "启动时不再补发")


if __name__ == "__main__":
    unittest.main()