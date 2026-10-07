"""The sign-in gate: the two ways OUT of it, and the application frame it hides.

Three separate defects, one file, because they are one story — "somebody who is
not logged in cannot get to the public page":

* **there was no way back.** No link to `/welcome` existed anywhere in
  `index.html`, the browser's Back button cannot help (the landing page is served
  AT `/` by a rewrite, so the previous history entry is `/` itself), and a
  `position:fixed; inset:0` gate covers the page rather than trapping history.
  Asserted as a real anchor with a real href, in both places it now lives.
* **and the shell was on screen first.** `.nav` / `.shell` are static markup, so
  they paint immediately; the gate cannot appear until `bootstrap()` has asked
  `/api/auth/me`. Measured over the public domain that gap was ~2.8 s — not a
  loading animation, but the internal application's skeleton shown to somebody
  who is not signed in. The fix hides the shell while the answer is unknown and
  lets the gate in as soon as it is a definite 401.
* **which is only safe if "unknown" gets to clear it.** A 5xx, `AUTH_ENABLED=
  false`, or a plain static file server with no endpoint must all leave the app
  exactly as it was. Turn the hiding on without that and the bug becomes a
  permanently blank page in the other 20-odd browser guards.

Static server + route interception; no backend, no database, nothing written.

    .venv312/bin/python api/tests/verify_auth_gate_ui.py
"""
import functools
import http.server
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")

FAILURES = []

# `/api/auth/me` → 401 is the anonymous state, which is the one the gate is for.
STUB = """
window.fetch = function (url) {
  const u = String(url);
  let status = 200, body = {items: [], events: [], reports: [], count: 0,
    categories: [], folders: [], files: [], datasets: []};
  if (u.indexOf('/auth/me') >= 0) {
    status = 401; body = {detail: 'Not authenticated'};
  } else if (u.indexOf('/auth/health') >= 0) {
    body = {enabled: true, mode: 'self', allowed_domain: 'klado.team',
            mail: 'unconfigured'};
  }
  return Promise.resolve({ok: status < 400, status: status,
                          json: () => Promise.resolve(body)});
};
"""


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


def q(page, selector, expr="el => el.innerText.trim()"):
    """Eval on a selector that may not exist. Missing is an assertion failure, not
    a crashed run — a throw here would silence every later check, which reads like
    "it tested more" while actually having tested nothing."""
    try:
        return page.eval_on_selector(selector, expr)
    except Exception:
        return None


def _free_port() -> int:
    """Ask the OS for a port instead of naming one.

    Fixed ports can remain occupied by another test server after a crashed run.
    Requesting an ephemeral port avoids EADDRINUSE before assertions can run.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server(socketserver.TCPServer):
    # ⚠️ On the CLASS, not on an instance: the socket is already bound by the time
    # `TCPServer.__init__` returns, so setting the attribute afterwards is a line
    # that does nothing.
    allow_reuse_address = True


def serve(port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    httpd = _Server(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _shell_state(page):
    """Everything the flash assertions read, in one round trip.

    ⚠️ `.shell` being absent from the DOM and `.shell` being `display:none` are
    different worlds: the first means "the document has not got there yet" and the
    second is the thing under test. They are reported separately on purpose.
    """
    return page.evaluate("""() => {
      const sh = document.querySelector('.shell');
      const nv = document.querySelector('.nav');
      const gate = document.querySelector('.auth-gate');
      const cs = e => e ? getComputedStyle(e).display : null;
      return {
        rootCls: document.documentElement.className,
        bodyCls: document.body ? document.body.className : null,
        shellPresent: !!sh,
        navPresent: !!nv,
        shell: cs(sh),
        nav: cs(nv),
        gate: cs(gate),
      };
    }""")


def main() -> int:
    port = _free_port()
    httpd = serve(port)
    base = "http://127.0.0.1:%d/index.html" % port
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)

            # ── 1. the way out, on the gate ─────────────────────────────────
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(STUB)
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_function("() => !!document.querySelector('#auth-gate')",
                                   timeout=20000)
            page.wait_for_function(
                "() => getComputedStyle(document.querySelector('#auth-gate')).display !== 'none'",
                timeout=20000)

            back = q(page, "#auth-back-home", """el => {
              const r = el.getBoundingClientRect();
              return {href: el.getAttribute('href'),
                      abs: el.href, text: el.innerText.trim(),
                      w: Math.round(r.width), h: Math.round(r.height),
                      display: getComputedStyle(el).display,
                      inCard: !!el.closest('.auth-gate-card')};
            }""")
            # ⚠️ The positive control first: "not found" and "found but invisible"
            # produce the same null here, so prove the card is really on screen
            # before believing anything about the link inside it.
            check("正对照：登录卡片本身在屏（否则下面「找不到链接」同形）",
                  q(page, ".auth-gate-card",
                    "el => Math.round(el.getBoundingClientRect().height)") > 100,
                  q(page, ".auth-gate-card",
                    "el => Math.round(el.getBoundingClientRect().height)"))
            check("闸门上有「回到首页」的链接", back is not None, back)
            if back:
                check("返回链接可见（有实际高度，不只是存在于 DOM）",
                      back["display"] != "none" and back["w"] > 20 and back["h"] > 8,
                      back)
                check("返回链接指向落地页", back["abs"].endswith("/welcome"), back["abs"])
                check("返回链接在卡片内部（不是浮在页面上的孤岛）",
                      back["inCard"] is True, back)
                check("返回链接有可读文案", "home" in back["text"].lower(), back["text"])
                pos = page.evaluate("""() => {
                  const link = document.querySelector('#auth-back-home');
                  const beian = document.querySelector('.auth-beian');
                  const l = link.getBoundingClientRect(), b = beian.getBoundingClientRect();
                  return {linkBottom: Math.round(l.bottom),
                          beianTop: Math.round(b.top)};
                }""")
                check("返回链接不压在备案号那一行上（合规位置不能被挤）",
                      pos["linkBottom"] < pos["beianTop"], pos)

            # ── 2. the way out, from inside the app ────────────────────────
            home = q(page, "#au-acct a.au-item[href]", """el => {
              const items = [...document.querySelectorAll('#au-acct .au-item')];
              const danger = document.querySelector('#au-acct .au-danger');
              return {abs: el.href, text: el.innerText.trim(),
                      before: !!(danger &&
                        items.indexOf(el) < items.indexOf(danger))};
            }""")
            check("用户菜单里有「Klado 首页」入口", home is not None, home)
            if home:
                check("菜单入口指向落地页", home["abs"].endswith("/welcome"),
                      home["abs"])
                check("菜单入口排在「退出登录」之前（破坏性操作永远最后）",
                      home["before"] is True, home)
            check("判定为 401 之后闸门出现、外壳仍然不在屏",
                  q(page, ".auth-gate", "el => getComputedStyle(el).display") != "none"
                  and q(page, ".shell", "el => getComputedStyle(el).display") == "none",
                  q(page, ".shell", "el => getComputedStyle(el).display"))
            check("判定完成之后 auth-pending 已被清掉（不留永久空白）",
                  "auth-pending" not in (_shell_state(page)["rootCls"] or ""),
                  _shell_state(page)["rootCls"])
            page.close()

            # ── 3. the flash: shell hidden while the answer is unknown ──────
            # ⚠️ The request is left hanging rather than delayed by a timer:
            # `route.fulfill()` from another thread never reaches the browser, and
            # `time.sleep` inside the handler blocks the very thread that services
            # `wait_for_selector` — so the probe only resumes AFTER the answer has
            # landed and the shell reads as hidden because the GATE locked it.
            # That version passed for entirely the wrong reason. Never responding
            # is the honest way to be "still asking".
            slow = browser.new_page(viewport={"width": 1500, "height": 900})
            slow.route("**/api/auth/me", lambda route: None)
            slow.goto(base, wait_until="commit")
            slow.wait_for_selector(".shell", state="attached", timeout=30000)
            pending = _shell_state(slow)
            check("等待登录态时 <html> 挂着 auth-pending",
                  "auth-pending" in pending["rootCls"], pending["rootCls"])
            # ⚠️ The second half of the same claim, and the half that makes the row
            # above mean something: the app must NOT already be locked at this
            # moment. With `auth-gate-open` on <body> the shell would be hidden by
            # the gate, and this whole section would be asserting a property that
            # was already true before the fix.
            check("取样时闸门尚未上锁（外壳是被 pending 挡住的，不是被闸门）",
                  "auth-gate-open" not in (pending["bodyCls"] or "")
                  and pending["gate"] == "none",
                  {"bodyCls": pending["bodyCls"], "gate": pending["gate"]})
            check("等待登录态时应用外壳不在屏（这条就是要修的那 2.8 秒）",
                  pending["shell"] == "none" and pending["nav"] == "none", pending)
            slow.close()

            # ── 3b. what `auth-gate-early` is FOR, tested as behaviour ───────
            # ⚠️ ⚠️ What is NOT covered here, stated rather than papered over: the
            # window this class exists for. It is the stretch between the <head>
            # probe learning "401" and `bootstrap()` running at the end of a
            # 2.2 MB document. Playwright cannot observe it — the parse is one
            # uninterrupted main-thread task, so every `wait_for_selector` and
            # `evaluate` lands after `bootstrap()` has already unlocked the
            # shell. (An earlier version of this section sampled at `.shell`
            # attach and read an empty className, which is that fact, not a bug.)
            #
            # So the two things this class DEPENDS on are asserted instead: the
            # probe adds it on a 401 (source), and the stylesheet does what the
            # probe assumes when it does (behaviour, below). Deleting either half
            # goes red; losing the timing optimisation does not.
            src = (Path(ROOT) / "index.html").read_text(encoding="utf-8")
            check("<head> 探针在 401 时挂上 auth-gate-early",
                  "classList.add('auth-gate-early')" in src
                  and "res.status === 401" in src)

            # ── 4. the control: "unknown" must NOT mean "hidden forever" ────
            # ⚠️ This is the row that keeps the other browser guards alive. They
            # serve `frontend/out` from a static server where `/api/auth/me` does
            # not exist, so the probe gets a 404 and the app has to be left alone.
            # Without it, assertion 3 passes just as well on a page that is
            # permanently blank — which is the exact failure the fix could cause.
            ctl = browser.new_page(viewport={"width": 1500, "height": 900})
            ctl.route("**/api/auth/me", lambda r: r.fulfill(
                status=404, content_type="application/json", body='{"detail":"nope"}'))
            ctl.goto(base, wait_until="commit")
            ctl.wait_for_selector(".shell", state="attached", timeout=30000)
            ctl.wait_for_function(
                "() => !document.documentElement.classList.contains('auth-pending')",
                timeout=30000)
            after = _shell_state(ctl)
            check("（对照）端点不存在时外壳照常显示 —— 证明上一节的判据看得见「可见」",
                  after["shell"] not in ("none", None), after)
            check("（对照）端点不存在时 auth-pending 已清除",
                  "auth-pending" not in after["rootCls"], after["rootCls"])

            # `auth-gate-early` does the same job once the answer IS known — and
            # this is the only page where that can be told apart from the gate
            # merely being up: the shell is VISIBLE here, so `flex → none → flex`
            # is a measurement with a visible failure state at both ends.
            # ⚠️ The probe adds a class and removes it again, so it has to hand the
            # page back the way it found it — a guard that leaves state behind turns
            # every later assertion into noise about its own leftovers.
            forced = ctl.evaluate("""() => {
              const r = document.documentElement;
              const sh = document.querySelector('.shell');
              const gate = document.querySelector('.auth-gate');
              const before = getComputedStyle(sh).display;
              r.classList.add('auth-gate-early');
              const out = {before: before,
                           gateShown: getComputedStyle(gate).display !== 'none',
                           shellHidden: getComputedStyle(sh).display === 'none'};
              r.classList.remove('auth-gate-early');
              out.after = getComputedStyle(sh).display;
              return out;
            }""")
            check("auth-gate-early 会把闸门显示出来", forced["gateShown"], forced)
            check("auth-gate-early 会把可见的应用外壳藏起来",
                  forced["shellHidden"] and forced["before"] == "flex", forced)
            check("（复原）撤销这个类之后外壳回到 flex",
                  forced["after"] == "flex", forced)
            ctl.close()

            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
