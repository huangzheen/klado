"""The temporary initializer must never accept a remote or repeated password write."""
import importlib.util
import io
import re
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("initialize_admin", ROOT / "scripts/initialize_admin.py")
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


class PasswordFormTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(email="fixture@example.invalid")
        self.backend.finish.return_value = True
        self.Handler = initializer.make_handler(self.backend, 8852)

    def request(self, method="GET", fields=None, host="127.0.0.1:8852", origin="http://127.0.0.1:8852"):
        handler = self.Handler.__new__(self.Handler)
        handler.path = "/"
        body = urllib.parse.urlencode(fields or {}).encode()
        handler.headers = {"Host": host, "Origin": origin, "Content-Length": str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        getattr(handler, "do_" + method)()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def form(self):
        code, body = self.request()
        self.assertEqual(code, 200)
        nonce = re.search(rb'name="nonce" value="([^"]+)"', body).group(1).decode()
        return {"nonce": nonce, "password": "fixture-password", "confirm": "fixture-password"}

    def test_foreign_origin_cannot_write_even_with_a_valid_form(self):
        form = self.form()
        code, _ = self.request("POST", form, origin="https://foreign.example.invalid")
        self.assertEqual(code, 403)
        self.backend.finish.assert_not_called()

    def test_dns_rebinding_host_is_refused(self):
        code, _ = self.request(host="foreign.example.invalid:8852")
        self.assertEqual(code, 403)
        self.backend.finish.assert_not_called()

    def test_missing_form_token_cannot_write(self):
        form = self.form()
        del form["nonce"]
        code, _ = self.request("POST", form)
        self.assertEqual(code, 403)
        self.backend.finish.assert_not_called()

    def test_mismatch_and_short_password_never_reach_database(self):
        for password, confirm in (("short", "short"), ("fixture-password", "different")):
            with self.subTest(password=password):
                form = self.form()
                form.update(password=password, confirm=confirm)
                code, _ = self.request("POST", form)
                self.assertEqual(code, 400)
        self.backend.finish.assert_not_called()

    def test_success_is_single_use_and_password_is_never_in_response(self):
        form = self.form()
        code, body = self.request("POST", form)
        self.assertEqual(code, 200)
        self.assertNotIn(form["password"].encode(), body)
        code, _ = self.request("POST", form)
        self.assertEqual(code, 410)
        self.backend.finish.assert_called_once_with(form["password"])

    def test_expired_page_cannot_write(self):
        form = self.form()
        with patch.object(initializer.time, "monotonic", return_value=float("inf")):
            code, _ = self.request("POST", form)
        self.assertEqual(code, 410)
        self.backend.finish.assert_not_called()

    def test_backend_errors_never_expose_password(self):
        form = self.form()
        self.backend.finish.side_effect = RuntimeError(form["password"])
        code, body = self.request("POST", form)
        self.assertEqual(code, 500)
        self.assertNotIn(form["password"].encode(), body)


class AccountProtectionTests(unittest.TestCase):
    def test_pending_account_has_no_usable_password(self):
        from klado_shared import accounts
        self.assertFalse(accounts._verify("fixture-password", initializer.PENDING_HASH))

    def test_password_is_sent_over_stdin_not_arguments(self):
        backend = initializer.DockerBackend("fixture@example.invalid")
        with patch.object(initializer.subprocess, "run", return_value=Mock(returncode=0, stdout='{}')) as run:
            backend.call("finish", "fixture-password")
        self.assertNotIn("fixture-password", str(run.call_args.args))
        self.assertIn("fixture-password", run.call_args.kwargs["input"])
