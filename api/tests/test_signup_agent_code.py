"""
The welcome 授权码 — every new account gets an agent access code by email.

Two behaviours are pinned here, both of them "quiet failure" shaped:

1. `POST /register/complete` issues a code and mails it. This used to be missing
   entirely (the flow only created the account), so a colleague finished signing up and
   simply never received a code — nobody noticed until a user reported it on 2026-09-29.
   The test asserts the *send*, not that a row exists: a code that is never mailed is the
   exact bug being fixed. It also asserts `/register/start`'s sibling lesson — an external
   channel must be exercised directly, because an early return hides what follows it.

2. Neither a mail failure nor a database hiccup may fail the registration. By the time
   the welcome code runs the account exists and is signed in; turning a sign-up into an
   error because SMTP was down would be strictly worse than a code that has to be handed
   over from the admin page.

3. `POST /admin/agent-tokens/{id}/mail` re-sends an existing code without rotating it,
   and refuses (instead of mailing a prefix) when the full code cannot be read back.
"""
import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException, Response

from routers import auth


class _Req:
    """Just enough of a Starlette Request for the code paths under test.

    ⚠️ The Host header is part of that "enough": `_asset_base_url` derives every email
    link from the request, and an address-less request now yields "" (the old code fell
    back to a hardcoded production host, which no longer exists). Leave it out and this
    suite would silently stop covering the mail-link path it was written for.
    """

    def __init__(self, user=None):
        self.headers = {"host": "klado.local"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.url = type("U", (), {"scheme": "https"})()
        self.state = type("S", (), {"current_user": user})()


def _body(email="newbie@example.com"):
    return auth.RegisterComplete(email=email, code="123456", password="a-good-password",
                                 display_name="Newbie")


def _store(created_user=None, **over):
    """A stand-in for services.auth_store, with only the calls the flow makes."""
    store = mock.Mock()
    store.get_user_by_email.return_value = None
    store.verify_code.return_value = (True, "ok")
    store.create_user.return_value = created_user or {
        "id": 7, "email": "newbie@example.com", "role": "user"}
    store.issue_session.return_value = "session-token"
    store.create_agent_token.return_value = ("klado_agent_" + "x" * 43, {"id": 3})
    store._public.side_effect = lambda u: {"id": u["id"], "email": u["email"]}
    for k, v in over.items():
        setattr(store, k, v)
    return store


class SignupIssuesAgentCodeTests(unittest.TestCase):
    def _run(self, store, send=None, send_error=None):
        payload = {}
        with mock.patch.object(auth, "auth_store", store), \
                mock.patch.object(auth, "mailer") as mailer:
            if send_error is not None:
                mailer.send_agent_code.side_effect = send_error
            else:
                mailer.send_agent_code.return_value = send or "sent"
            payload = asyncio.run(auth.register_complete(_body(), _Req(), Response()))
        return payload, mailer

    def test_registration_emails_a_welcome_code(self):
        store = _store()
        payload, mailer = self._run(store)

        store.create_agent_token.assert_called_once_with(
            7, auth.SIGNUP_AGENT_LABEL, created_by="newbie@example.com")
        args, kwargs = mailer.send_agent_code.call_args
        self.assertEqual(args[0], "newbie@example.com")
        self.assertEqual(args[1], "klado_agent_" + "x" * 43)
        self.assertTrue(args[2])
        self.assertTrue(kwargs.get("asset_base"))
        self.assertEqual(payload["agent_mail"], "sent")
        self.assertTrue(payload["ok"])

    def test_a_dead_mailbox_does_not_fail_the_registration(self):
        # unconfigured SMTP is the common case in a fresh environment: the account must
        # still be created and signed in, and the caller is told the mail did not go out.
        payload, _ = self._run(_store(), send="unconfigured")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent_mail"], "unconfigured")
        self.assertEqual(payload["token"], "session-token")

    def test_a_refusing_smtp_server_does_not_fail_the_registration(self):
        payload, _ = self._run(_store(), send="failed")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent_mail"], "failed")

    def test_a_store_error_does_not_fail_the_registration(self):
        # The code is minted first; if the INSERT (or a schema lag) blows up, the sign-up
        # still succeeds rather than surfacing as a 500 after the account was created.
        store = _store()
        store.create_agent_token.side_effect = RuntimeError("relation does not exist")
        payload, mailer = self._run(store)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent_mail"], "error")
        mailer.send_agent_code.assert_not_called()

    def test_an_smtp_exception_does_not_fail_the_registration(self):
        payload, _ = self._run(_store(), send_error=OSError("connection refused"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent_mail"], "error")

    def test_the_plaintext_code_is_not_shipped_in_the_response(self):
        # The SPA reloads as soon as registration succeeds, so a code in this body would
        # be thrown away unread — and it would be a credential in a response nobody keeps.
        payload, _ = self._run(_store())
        self.assertNotIn("klado_agent_" + "x" * 43, repr(payload))
        self.assertNotIn("agent_code", payload)


class AdminResendTests(unittest.TestCase):
    ADMIN = {"id": 1, "email": "admin@example.com", "role": "admin"}
    CODE = "klado_agent_" + "y" * 43

    def _row(self, **over):
        row = {"id": 5, "user_id": 9, "label": auth.SIGNUP_AGENT_LABEL,
               "user_email": "newbie@example.com", "code": self.CODE, "revoked_at": None}
        row.update(over)
        return row

    def _call(self, row, mail="sent", user=ADMIN):
        store = mock.Mock()
        store.get_agent_token.return_value = row
        with mock.patch.object(auth, "auth_store", store), \
                mock.patch.object(auth, "mailer") as mailer:
            mailer.send_agent_code.return_value = mail
            out = asyncio.run(auth.admin_mail_agent_token(5, _Req(user=user)))
        return out, mailer

    def test_resends_the_existing_code_unchanged(self):
        out, mailer = self._call(self._row())
        args, _ = mailer.send_agent_code.call_args
        self.assertEqual(args[0], "newbie@example.com")
        self.assertEqual(args[1], self.CODE)          # same code — no rotation
        self.assertEqual(out["mail"], "sent")
        self.assertEqual(out["to"], "newbie@example.com")

    def test_reports_a_dead_mailbox_with_a_hand_it_over_hint(self):
        out, _ = self._call(self._row(), mail="unconfigured")
        self.assertEqual(out["mail"], "unconfigured")
        self.assertIn("hint", out)

    def test_unknown_code_is_404(self):
        with self.assertRaises(HTTPException) as caught:
            self._call(None)
        self.assertEqual(caught.exception.status_code, 404)

    def test_revoked_code_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._call(self._row(revoked_at="2026-09-29T00:00:00"))
        self.assertEqual(caught.exception.status_code, 400)

    def test_unreadable_code_is_refused_rather_than_mailing_a_prefix(self):
        # Issued before `code_enc` existed, or SECRET_KEY was rotated: `code` is None and
        # `display` is a 12-character prefix that the API rejects. Mailing it would look
        # like success and hand over something unusable.
        with self.assertRaises(HTTPException) as caught:
            self._call(self._row(code=None))
        self.assertEqual(caught.exception.status_code, 400)

    def test_deleted_owner_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._call(self._row(user_email=""))
        self.assertEqual(caught.exception.status_code, 400)

    def test_admins_only(self):
        with self.assertRaises(HTTPException) as caught:
            self._call(self._row(), user={"id": 2, "email": "u@example.com", "role": "user"})
        self.assertEqual(caught.exception.status_code, 403)
        with self.assertRaises(HTTPException) as caught:
            self._call(self._row(), user=None)
        self.assertEqual(caught.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()