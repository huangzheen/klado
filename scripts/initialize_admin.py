#!/usr/bin/env python3
"""Initialize an authorized admin through a temporary, loopback-only password form.

Run on the OrbStack host. Passwords go through stdin to Docker, never command
arguments, files or logs. An existing usable account is never overwritten.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import http.cookiejar
import json
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request

PENDING_HASH = "!klado-admin-initialization"
DOCKER_CODE = r'''
import json, sys
from klado_shared import accounts
data = json.load(sys.stdin)
email, pending = data["email"], data["pending"]
if not accounts.is_admin_email(email):
    raise SystemExit("The email must already be configured in AUTH_ADMIN_EMAILS")
if data["mode"] == "prepare":
    accounts.create_user_with_hash(email, pending, display_name=email.split("@")[0], role="admin")
    with accounts._db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role, password_hash = %s, disabled, deleted_at IS NOT NULL FROM app_users WHERE email = %s", (pending, email))
            role, awaiting, disabled, deleted = cur.fetchone()
    if role != "admin" or not awaiting or disabled or deleted:
        raise SystemExit("An existing account cannot be initialized by this utility")
    print(json.dumps({"email": email, "role": role, "password_pending": awaiting}))
else:
    password = data["password"]
    if len(password) < 8 or len(password) > 1024:
        raise SystemExit("Invalid password length")
    with accounts._db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE app_users SET password_hash = %s WHERE email = %s AND role = 'admin' AND password_hash = %s AND NOT disabled AND deleted_at IS NULL RETURNING id", (accounts._hash(password), email, pending))
            if cur.fetchone() is None:
                raise SystemExit("Account initialization is no longer available")
    print(json.dumps({"email": email, "role": "admin", "password_pending": False}))
'''


class DockerBackend:
    def __init__(self, email):
        self.email = email

    def call(self, mode, password=None):
        payload = {"email": self.email, "pending": PENDING_HASH, "mode": mode}
        if password is not None:
            payload["password"] = password
        result = subprocess.run(
            ["docker", "exec", "-i", "--env", "PYTHONPATH=/app", "klado-admin-1", "python", "-c", DOCKER_CODE],
            input=json.dumps(payload), text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Account initialization refused; an existing password was not overwritten")
        return json.loads(result.stdout)

    def prepare(self):
        return self.call("prepare")

    def finish(self, password):
        self.call("finish", password)
        # Verify the real console login and session, without exposing its cookie.
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request = urllib.request.Request(
            "http://127.0.0.1:8787/api/admin-console/login",
            data=json.dumps({"email": self.email, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=10) as response:
                login = json.load(response)
            with opener.open("http://127.0.0.1:8787/api/admin-console/me", timeout=10) as response:
                me = json.load(response)
            return login.get("ok") is True and me.get("user", {}).get("role") == "admin"
        except Exception:
            return False  # The password was stored; don't encourage another reset.


def make_handler(backend, port, lifetime=900):
    nonce = secrets.token_urlsafe(32)
    deadline = time.monotonic() + lifetime
    state = {"done": False, "login_verified": False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log request bodies, credentials or form tokens.

        def reply(self, code, body, mime="text/html; charset=utf-8"):
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.allowed_host():
                return self.reply(403, "Forbidden")
            if self.path == "/status":
                return self.reply(200, json.dumps({"email": backend.email, **state}), "application/json")
            if self.path != "/":
                return self.reply(404, "Not found")
            if time.monotonic() >= deadline:
                return self.reply(410, "设置页面已过期，请重新启动初始化工具。")
            if state["done"]:
                return self.reply(200, "密码已设置。请打开 <a href='http://localhost:8787/'>管理后台</a> 登录。")
            return self.reply(200, f'''<!doctype html><html lang="zh"><meta charset="utf-8">
<title>Klado · 设置管理员密码</title>
<style>body{{font-family:system-ui;max-width:440px;margin:12vh auto;padding:24px}}label,input,button{{display:block}}input{{box-sizing:border-box;width:100%;padding:12px;margin:8px 0 20px}}button{{padding:12px 20px}}p{{line-height:1.6}}</style>
<h1>设置超级管理员密码</h1><p>账号：<strong>{html.escape(backend.email)}</strong></p>
<p>超级管理员身份已创建。设置密码后即可登录管理后台。密码仅用于本机账号初始化，不会写入聊天或配置文件。</p>
<form method="post" action="/" autocomplete="off"><input type="hidden" name="nonce" value="{nonce}">
<label for="password">登录密码（至少 8 个字符）</label><input id="password" name="password" type="password" minlength="8" maxlength="1024" autocomplete="new-password" required>
<label for="confirm">再次输入密码</label><input id="confirm" name="confirm" type="password" minlength="8" maxlength="1024" autocomplete="new-password" required>
<button type="submit">设置密码并验证登录</button></form></html>''')

        def do_POST(self):
            if self.path != "/" or not self.allowed_host() or self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
                return self.reply(403, "Forbidden")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError()
                fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
            except (ValueError, UnicodeError):
                return self.reply(400, "Invalid form")
            if not hmac.compare_digest(fields.get("nonce", [""])[0], nonce):
                return self.reply(403, "Forbidden")
            password = fields.get("password", [""])[0]
            if not 8 <= len(password) <= 1024 or password != fields.get("confirm", [""])[0]:
                return self.reply(400, "密码须至少 8 个字符，且两次输入一致。请返回重试。")
            with lock:
                if state["done"] or time.monotonic() >= deadline:
                    return self.reply(410, "设置已完成或页面已过期。")
                try:
                    verified = backend.finish(password)
                except Exception:
                    return self.reply(500, "初始化未完成，未覆盖任何已有密码。请查看工具状态。")
                state.update(done=True, login_verified=verified)
            message = "密码已设置，管理后台登录验证通过。" if verified else "密码已设置，但自动登录验证未通过。请在管理后台手动登录。"
            print(json.dumps({"email": backend.email, **state}), flush=True)
            return self.reply(200, message + " <a href='http://localhost:8787/'>打开管理后台</a>")

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--port", type=int, default=8852)
    args = parser.parse_args()
    backend = DockerBackend(args.email.strip().lower())
    print(json.dumps(backend.prepare()), flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(backend, args.port))
    expiry = threading.Timer(900, server.shutdown)
    expiry.daemon = True
    expiry.start()
    print(f"Password setup: http://127.0.0.1:{args.port}/ (expires in 15 minutes)", flush=True)
    try:
        server.serve_forever()
    finally:
        expiry.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
