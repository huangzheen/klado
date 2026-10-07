"""The user menu: it must open as a real menu, and it must never open as an empty box.

    docker compose exec -T app sh -c 'cd /app/api/tests && python verify_user_menu_ui.py'
    (or, with the repo venv: .venv312/bin/python api/tests/verify_user_menu_ui.py)

Three defects, and every assertion below is written so that the version of it that
passes while the product is broken is impossible:

1. **The menu opened as a small empty white box.** `renderMenu()` used to rewrite the
   whole container's `innerHTML`, and it is only reached from `applyUser()` — which
   `bootstrap()` skips entirely when `AUTH_ENABLED=false`, i.e. exactly the local
   single-machine deployment. So the menu kept its empty markup and the avatar opened
   a bordered 244x14 box with nothing in it. The two assertions that matter are
   therefore run under BOTH auth states, and they count *visible rows with text* — not
   "the container is display:flex", which was true while it showed nothing at all.

2. **The menu is hit-tested, not computed.** `getComputedStyle().display` answers for
   the element, not for the screen: a menu behind `.rpt-viewer` (z-index 8000) still
   reports itself visible. `elementFromPoint` at the menu's own centre is the only
   thing that reports what the reader would actually see. The menu also has to live
   OUTSIDE `.nav` — that element is z-index 200 and establishes a stacking context, so
   anything nested in it can never rise above the report viewer. That is asserted
   structurally, because it is invisible until someone opens a report and looks.

3. **No unstyled control in the top bar.** The two toggles that used to live there
   carried `class="nav-icon-btn"`, and that class had **no rule in any stylesheet** —
   they rendered as raw native `<button>`s (grey fill, beveled border) inside an
   otherwise flat bar. A name-list check would have passed the moment somebody typed a
   new one badly, so this asserts the RULE instead: every class on every `<button>`
   inside `<nav class="nav">` must actually match a selector in app.css/theme.css.
"""
import functools
import http.server
import re
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2] / "frontend" / "out"
PORT = 8848

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


STUB = """
window.__AUTH_ENABLED__ = __AUTH_ENABLED__;
window.fetch = function (url) {
  const u = String(url);
  let body;
  if (u.indexOf('/auth/me') >= 0) {
    body = window.__AUTH_ENABLED__
      ? {enabled: true, authenticated: true,
         user: {email: 'admin@local', role: 'admin', display_name: 'Admin'}}
      : {enabled: false, authenticated: false, user: null};
  }
  else if (u.indexOf('/knowledge/items?') >= 0) body = {items: []};
  else if (u.indexOf('/annotations') >= 0) body = {items: [], notes: []};
  else if (u.indexOf('/inbox') >= 0) body = {items: [], unread: 0};
  else if (u.indexOf('/calendar/events') >= 0) body = {items: [], events: []};
  else if (u.indexOf('/files/browse') >= 0) body = {folders: [], files: []};
  else if (u.indexOf('/files/tree') >= 0) body = [];
  else if (u.indexOf('/datasets') >= 0) body = {datasets: [], items: []};
  else if (u.indexOf('/files') >= 0) body = {files: []};
  else if (u.indexOf('/reports?') >= 0) body = {reports: [], count: 0, categories: [], items: []};
  else if (u.indexOf('/org/me') >= 0) body = null;
  else body = {items: [], events: [], reports: [], count: 0, categories: [], folders: [], files: []};
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
};
"""


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    httpd = _Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def boot(browser, auth_enabled=True, lang=None):
    page = browser.new_page(viewport={"width": 1500, "height": 900}, locale="en-US")
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    if lang:
        page.add_init_script("try{localStorage.setItem('klado-lang','%s')}catch(e){}" % lang)
    page.add_init_script(STUB.replace("__AUTH_ENABLED__", "true" if auth_enabled else "false"))
    page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
    page.wait_for_function("() => typeof navTo === 'function' && window.kladoI18n")
    return page, errors


# ── what is actually drawn inside the open menu ───────────────────────────────
# `innerText`, never `textContent`: i18n hides the other language with `display:none`
# in some containers, and `textContent` would read the half nobody can see.
ROWS = """() => {
  const m = document.getElementById('auth-menu');
  if (!m) return null;
  return Array.from(m.querySelectorAll('.au-item, .au-name')).map(el => ({
    text: el.innerText.replace(/\\s+/g, ' ').trim(),
    chip: (el.querySelector('.au-chip') || {}).textContent || '',
    h: Math.round(el.getBoundingClientRect().height),
  }));
}"""

HIT = """() => {
  const m = document.getElementById('auth-menu');
  const r = m.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {inMenu: !!(hit && m.contains(hit)), tag: hit ? hit.tagName + '.' + hit.className : null};
}"""


def open_menu(page):
    page.click("#nav-avatar")
    page.wait_for_selector("#auth-menu", state="visible")


def nav_buttons_have_rules():
    """Every class on every <button> inside the top bar must match a real selector."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<nav class="nav".*?</nav>', html, re.S)
    if not m:
        return (False, "could not find <nav class=\"nav\"> in index.html")
    # Comments first: the nav carries `<!-- ... -->` notes that quote markup verbatim
    # (including `<button>`), and scanning those reports a control that does not exist.
    block = re.sub(r"<!--.*?-->", "", m.group(0), flags=re.S)
    css = (ROOT / "app.css").read_text(encoding="utf-8") + \
          (ROOT / "theme.css").read_text(encoding="utf-8")
    missing = []
    for tag in re.findall(r"<button\b[^>]*>", block, re.I):
        classes = re.findall(r'class="([^"]+)"', tag)
        if not classes:
            missing.append("<button> with no class at all")
            continue
        for c in " ".join(classes).split():
            if not re.search(r"\.%s(?![\w-])" % re.escape(c), css):
                missing.append(c)
    return (not missing, sorted(set(missing)))


def menu_icons_exist():
    """Every `ri-*` class on a menu icon must exist in the INLINED Remix Icon block.

    ⚠️ The icon set is a `<style>` block pasted into index.html, not `remixicon/*.css`
    in the repo — those two copies are not in step, so an icon can exist in the
    directory file and still render as nothing. `ri-translate-2-line` did exactly that:
    the Language row shipped with no icon at all, and the screenshot is the only thing
    that ever notices."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<div class="nav-user-menu" id="auth-menu".*?</div>\s*<!--', html, re.S)
    block = m.group(0) if m else ""
    used = sorted(set(re.findall(r'class="(ri-[a-z0-9-]+)"', block)))
    missing = [c for c in used if (".%s:before" % c) not in html]
    return used, missing


def main() -> int:
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)

            # ── 0. static: the top bar has no unstyled control ────────────────
            ok, detail = nav_buttons_have_rules()
            check("顶栏里每个 <button> 的 class 都有真实样式规则", ok, detail)
            used, missing = menu_icons_exist()
            check("菜单图标的 remixicon class 都在内联图标表里", not missing,
                  "missing=%r used=%r" % (missing, used))

            # ── 1. AUTH DISABLED — the reported bug, the local deployment ──────
            page, errors = boot(browser, auth_enabled=False)
            check("auth 关闭时页面无 JS 报错", not errors, errors)
            open_menu(page)
            rows = page.evaluate(ROWS)
            visible = [r for r in (rows or []) if r["text"] and r["h"] > 0]
            check("auth 关闭时菜单点开不是空框（至少 2 条可见行）",
                  len(visible) >= 2, "%d rows: %r" % (len(visible), [r["text"] for r in visible]))
            check("auth 关闭时语言/主题两行都在",
                  any("语言" in r["text"] or "Language" in r["text"] for r in visible)
                  and any("主题" in r["text"] or "Theme" in r["text"] for r in visible),
                  [r["text"] for r in visible])
            hit = page.evaluate(HIT)
            check("菜单在自己中心能被命中（没被盖住）", hit["inMenu"], hit)

            # the toggles live here now, not in the bar
            check("语言开关已移入菜单",
                  page.evaluate("() => !!document.querySelector('#auth-menu [data-lang-toggle]')"))
            check("主题开关已移入菜单",
                  page.evaluate("() => !!document.querySelector('#auth-menu [data-theme-toggle]')"))
            check("顶栏里没有遗留的 nav-icon-btn",
                  page.evaluate("() => document.querySelectorAll('.nav .nav-icon-btn').length") == 0)
            check("菜单不在 .nav 内部（否则永远被 z-index 200 的顶栏压住）",
                  page.evaluate("() => !document.getElementById('auth-menu').closest('.nav')"))
            check("菜单在视口内且贴在头像下方",
                  page.evaluate("""() => {
                    const m = document.getElementById('auth-menu').getBoundingClientRect();
                    const a = document.getElementById('nav-avatar').getBoundingClientRect();
                    return m.left >= 0 && m.right <= window.innerWidth + 1
                        && m.top >= a.bottom - 1 && m.left <= a.right + 1;
                  }"""))

            # chips report the CURRENT value, not the one a click would switch to
            check("en 下语言 chip 显示当前语言 English",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-lang-label]')||{}).textContent") == "English",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-lang-label]')||{}).textContent"))
            check("en 下主题 chip 显示当前主题 Light",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-theme-label]')||{}).textContent") == "Light",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-theme-label]')||{}).textContent"))

            # theme toggle actually repaints, from inside the menu
            page.click("#auth-menu [data-theme-toggle]")
            page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'dark'")
            check("点主题后 <html data-theme> 变 dark", True)
            check("切到深色后主题 chip 变 Dark",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-theme-label]')||{}).textContent") == "Dark",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-theme-label]')||{}).textContent"))
            check("切到深色后主题图标变成太阳",
                  page.evaluate("() => !!document.querySelector('#auth-menu [data-theme-toggle] i.ri-sun-line')"))

            # language toggle switches, and the theme row follows it
            page.click("#auth-menu [data-lang-toggle]")
            page.wait_for_function("() => window.kladoI18n.lang() === 'zh'")
            check("点语言后运行时语言变 zh", True)
            check("zh 下语言 chip 显示中文",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-lang-label]')||{}).textContent") == "中文",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-lang-label]')||{}).textContent"))
            check("zh 下主题 chip 变「深色」（跟着语言走，不是停在英文）",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-theme-label]')||{}).textContent") == "深色",
                  page.evaluate("() => (document.querySelector('#auth-menu [data-theme-label]')||{}).textContent"))
            check("zh 下主题 tooltip 是中文",
                  page.evaluate("() => document.querySelector('#auth-menu [data-theme-toggle]').getAttribute('title')") == "切换到浅色",
                  page.evaluate("() => document.querySelector('#auth-menu [data-theme-toggle]').getAttribute('title')"))
            zrows = page.evaluate(ROWS)
            zvisible = [r for r in (zrows or []) if r["text"] and r["h"] > 0]
            check("zh 下菜单行都是中文侧（没有中英并排）",
                  all((" " not in r["text"] or " / " not in r["text"]) for r in zvisible),
                  [r["text"] for r in zvisible])
            # Escape closes it, and clicking away closes it
            page.keyboard.press("Escape")
            check("Esc 关闭菜单",
                  page.evaluate("() => getComputedStyle(document.getElementById('auth-menu')).display") == "none")
            open_menu(page)
            page.mouse.click(200, 400)
            check("点击别处关闭菜单",
                  page.evaluate("() => getComputedStyle(document.getElementById('auth-menu')).display") == "none")
            check("整个 auth 关闭流程无 JS 报错", not errors, errors)
            page.close()

            # ── 2. AUTH ENABLED — the account half, in BOTH orders ────────────
            # Booting straight into zh only proves the pair resolved once at parse
            # time. The failure this guards is the other order: the account rows were
            # written with `kladoI18n.t()` from `renderMenu()`, which runs ONCE at
            # `applyUser()`, so they froze in the boot language — switch to zh with the
            # menu open and "Agent access" sat next to Chinese rows, in English.
            page, errors = boot(browser, auth_enabled=True, lang="en")
            open_menu(page)
            labels = [r["text"] for r in (page.evaluate(ROWS) or []) if r["text"]]
            check("en 下账号三项都是英文",
                  any("Agent access" in t for t in labels) and any("Password" in t for t in labels)
                  and any("Sign out" in t for t in labels), labels)
            page.click("#auth-menu [data-lang-toggle]")           # → zh, menu stays open
            page.wait_for_function("() => window.kladoI18n.lang() === 'zh'")
            labels = [r["text"] for r in (page.evaluate(ROWS) or []) if r["text"]]
            check("菜单开着切语言后，账号三项跟着变中文",
                  any("代理访问" in t for t in labels) and any("修改密码" in t for t in labels)
                  and any("退出登录" in t for t in labels), labels)
            check("切语言后账号三项没有残留英文",
                  not any("Agent access" in t or "Sign out" in t for t in labels), labels)
            check("auth 开启时菜单显示账号名", any("Admin" in t for t in labels), labels)
            check("账号名不被机器翻译（Admin 不是 管理员）",
                  not any("管理员" in t for t in labels), labels)
            check("zh 下语言/主题行也在", any("语言" in t for t in labels) and any("主题" in t for t in labels), labels)
            check("auth 开启流程无 JS 报错", not errors, errors)
            page.close()

            browser.close()
    finally:
        httpd.shutdown()

    print()
    if FAILURES:
        print("%d checks failed" % len(FAILURES))
        return 1
    print("0 checks failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
