"""`deploy.sh` — the one-line install everyone actually runs.

⚠️ Why this file exists at all: `deploy.sh` is the only part of Klado whose
failure mode is *silence*. It copies `.env.example` and fills in five values.
If that fill silently does nothing, the install still starts, the health check
still returns 200, the script still prints "✓ Klado 起来了" — and what the user
actually gets is a database reachable with the literal password `change-me`,
an object store with empty credentials that `docker compose` refuses, and a
`SECRET_KEY` published in a public repository.

That is not hypothetical. The first version of `set_env` matched with
`grep -E "^KEY=(|change-me)"`. BSD grep (the one macOS ships) answers an empty
alternation with `grep: empty (sub)expression` and matches **nothing**, so not
one value was written — and the run reported success. Nothing but a check that
reads the `.env` back can catch that class of bug, which is why
`verify_secrets` lives in the script and why these tests exist.

Every test here runs the real script against a fake `docker` and a fake `curl`
on `PATH`, in a throwaway directory. Nothing touches a container, a port or the
network, so this is safe inside `ci_check.py`'s `--network none` container.

⚠️ The two "negative" tests are the load-bearing ones. A test that only checks
the happy path proves nothing about a guard: the guard would pass just as well
if `verify_secrets` were `:`. Each negative test therefore hands the script a
broken `.env` and requires it to REFUSE — and the last one requires it to
ACCEPT, so the check cannot be satisfied by blanket rejection either.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy.sh"

# Values `docker compose` refuses to start without (all three carry `:?` in
# docker-compose.yml), plus the session key. See api/tests/test_frontend_* for
# the other half of this — none of them are optional.
REQUIRED_SECRETS = ("SECRET_KEY", "POSTGRES_PASSWORD",
                    "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")


def _fake_docker() -> str:
    return "#!/usr/bin/env bash\necho \"docker $*\" >> \"$FAKE_LOG\"\nexit 0\n"


def _fake_curl() -> str:
    # `deploy.sh` probes two things: /api/health (exit status) and the landing
    # page (the %{http_code} write-out). Answer both the way a healthy install
    # does, so a test failure means deploy.sh is wrong rather than the stub.
    return ('#!/usr/bin/env bash\n'
            'for a in "$@"; do [[ "$a" == "%{http_code}" ]] && { echo 200; exit 0; }; done\n'
            'exit 0\n')


class DeployScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="klado-deploy-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("docker", _fake_docker()), ("curl", _fake_curl())):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "Dockerfile").write_text("# stub\n")
        for f in ("docker-compose.yml", ".env.example", "deploy.sh"):
            shutil.copy(ROOT / f, self.repo / f)
        (self.repo / "deploy.sh").chmod(0o755)

        self.log = self.tmp / "docker.log"
        self.log.write_text("")

    # ── helpers ────────────────────────────────────────────────────────────
    def run_deploy(self, *args: str, email: str | None = None,
                   stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LOG"] = str(self.log)
        env.pop("ADMIN_EMAIL", None)
        if email is not None:
            env["ADMIN_EMAIL"] = email
        return subprocess.run(["./deploy.sh", *args], cwd=self.repo, env=env,
                              stdin=stdin, capture_output=True, text=True,
                              timeout=180)

    def env_value(self, key: str) -> str | None:
        for line in (self.repo / ".env").read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.partition("=")[2]
        return None

    # ── the happy path, and the part that was silently broken ──────────────
    def test_fresh_install_writes_real_values_for_every_secret(self):
        r = self.run_deploy("me@example.com")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        for key in REQUIRED_SECRETS:
            val = self.env_value(key)
            self.assertIsNotNone(val, f"{key} 没有写进 .env")
            self.assertNotEqual(val, "", f"{key} 是空的——这正是那个 bug 的症状")
            self.assertFalse(val.startswith("change-me"),
                             f"{key} 仍然是占位值 {val!r}")

    def test_fresh_install_records_the_admin_email(self):
        """The email is not cosmetic: with auth off and this blank, EVERY /api
        request answers 401 — a healthy container and an unusable install."""
        r = self.run_deploy("me@example.com")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.env_value("AUTH_ADMIN_EMAILS"), "me@example.com")

    def test_fresh_install_is_not_reachable_from_the_network(self):
        """`AUTH_ENABLED=false` means anyone who can reach the port is the
        administrator, so the default bind has to be loopback."""
        self.run_deploy("me@example.com")
        self.assertEqual(self.env_value("KLADO_APP_BIND"), "127.0.0.1")
        self.assertEqual(self.env_value("KLADO_ADMIN_BIND"), "127.0.0.1")

    def test_generated_env_is_private(self):
        self.run_deploy("me@example.com")
        self.assertEqual((self.repo / ".env").stat().st_mode & 0o777, 0o600)

    def test_two_keys_do_not_share_a_value(self):
        """Not a security test so much as a copy-paste test: `set_env` takes
        key and value as two arguments, and a swapped pair is silent."""
        self.run_deploy("me@example.com")
        self.assertNotEqual(self.env_value("SECRET_KEY"),
                            self.env_value("POSTGRES_PASSWORD"))

    def test_it_really_invoked_compose(self):
        """Guards the guard: a stub that made the script exit early would
        otherwise 'pass' by never reaching the part under test."""
        self.run_deploy("me@example.com")
        calls = self.log.read_text()
        self.assertIn("docker compose build app", calls, calls)
        self.assertIn("docker compose up -d", calls, calls)

    # ── idempotence ────────────────────────────────────────────────────────
    def test_rerunning_does_not_rotate_the_secret_key(self):
        """Rotating SECRET_KEY invalidates every session and makes a stored
        SMTP password undecryptable. Re-running an installer must not do that."""
        self.run_deploy("me@example.com")
        first = self.env_value("SECRET_KEY")
        self.run_deploy("me@example.com")
        self.assertEqual(self.env_value("SECRET_KEY"), first)
        self.assertTrue(first)

    # ── negative: it must refuse, not limp on ──────────────────────────────
    def test_it_refuses_when_no_admin_email_can_be_asked_for(self):
        """Piped in from curl there is no TTY to prompt on. Guessing an
        identity would be the worst outcome available."""
        r = self.run_deploy()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ADMIN_EMAIL", r.stdout + r.stderr)
        self.assertFalse((self.repo / ".env").exists(),
                         "失败之后留下了半成品 .env")

    def test_it_refuses_an_env_whose_admin_email_is_blank(self):
        """The failure this script exists to prevent, fed in directly. If this
        ever passes, the guard is decorative."""
        self.run_deploy("me@example.com")
        text = (self.repo / ".env").read_text()
        (self.repo / ".env").write_text(
            "\n".join("AUTH_ADMIN_EMAILS=" if ln.startswith("AUTH_ADMIN_EMAILS=")
                      else ln for ln in text.splitlines()) + "\n")
        # email= is required here: without it deploy.sh stops at the missing-email
        # check, which never reaches verify_secrets — and a test that fails early
        # for the wrong reason is worse than no test.
        r = self.run_deploy(email="me@example.com")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("AUTH_ADMIN_EMAILS", r.stdout + r.stderr)

    def test_it_refuses_a_placeholder_secret_key(self):
        self.run_deploy("me@example.com")
        text = (self.repo / ".env").read_text()
        (self.repo / ".env").write_text(
            "\n".join("SECRET_KEY=change-me" if ln.startswith("SECRET_KEY=")
                      else ln for ln in text.splitlines()) + "\n")
        r = self.run_deploy(email="me@example.com")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SECRET_KEY", r.stdout + r.stderr)

    # ── negative the other way: it must not over-reject ────────────────────
    def test_a_blank_admin_email_is_fine_once_auth_is_enabled(self):
        """With AUTH_ENABLED=true the administrators live in the database and
        this line is legitimately blank. A check that demanded it anyway would
        refuse a correct install — which is why this sits next to the test
        above rather than being assumed."""
        self.run_deploy("me@example.com")
        text = (self.repo / ".env").read_text()
        (self.repo / ".env").write_text(
            "\n".join("AUTH_ADMIN_EMAILS=" if ln.startswith("AUTH_ADMIN_EMAILS=")
                      else ln for ln in text.splitlines()) + "\n")
        text = (self.repo / ".env").read_text()
        (self.repo / ".env").write_text(
            "\n".join("AUTH_ENABLED=true" if ln.startswith("AUTH_ENABLED=")
                      else ln for ln in text.splitlines()) + "\n")
        r = self.run_deploy(email="me@example.com")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class ComposeBindTests(unittest.TestCase):
    """The new-install default depends on this variable existing and on its
    empty default meaning exactly what it meant before it was introduced.

    ⚠️ A missing variable is the dangerous state, not a cosmetic one: with the
    line absent, compose binds every interface, and `deploy.sh` writes
    `KLADO_APP_BIND=127.0.0.1` which then does nothing.
    """

    def setUp(self) -> None:
        self.text = (ROOT / "docker-compose.yml").read_text()

    def test_both_services_have_a_bind_variable(self):
        self.assertIn('"${KLADO_APP_BIND:-}', self.text)
        self.assertIn('"${KLADO_ADMIN_BIND:-}', self.text)

    def test_the_default_is_empty_so_existing_deployments_are_unchanged(self):
        """`${VAR:-}` renders as `:8000:8000` — bind every interface, which is
        what the hard-coded mapping did. Any other default would silently
        re-bind the running deployment."""
        for name in ("KLADO_APP_BIND", "KLADO_ADMIN_BIND"):
            self.assertIn(f'"${{{name}:-}}', self.text,
                          f"{name} 的默认值不是空，会改变现有部署的行为")


if __name__ == "__main__":
    unittest.main()