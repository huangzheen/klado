"""Dark-mode coverage: walk every page and report surfaces that stay light.

Why this exists: `theme.css` gives every colour a token, but a rule can still pin a
literal (`background: #fff`) or inherit a background from a vendor sheet, and that
only shows up as "this one page is still light in dark mode". This boots the REAL
app with a real login and real content, forces dark, visits every page, and reports
any visible element whose computed background is much lighter than its own text.

⚠️ It must log in and seed content first. Running it anonymously is worse than
useless: the pages render empty, the scan finds nothing, and it reports a clean
sweep over content that was never on screen.

Needs playwright, a local Chrome and PostgreSQL.

    ../.venv312/bin/python tests/verify_theme_coverage_ui.py [--keep]
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

import requests
import uvicorn
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import auth_store  # noqa: E402

PORT = int(os.environ.get("THEME_PORT", "18893"))
BASE = f"http://127.0.0.1:{PORT}"
WEB = BASE + "/"
PASSWORD = "theme-cov-pass-1"
ADMIN = ("theme", "theme-admin@example.com")

KB_PAGE = "theme-cov-page"
KB_TITLE = "Theme coverage page"

FAILURES = []
NOTES = []

# Each entry: the label, the JS that navigates there, and whether the page needs a
# seeded fixture before it has anything on screen worth scanning.
PAGES = [
    ("home", "goHome()", False),
    ("data-center", "switchToDataCenter()", False),
    ("inbox", "navTo(null, 'inbox', 'Inbox')", True),
    ("reports", "navTo(null, 'reports', 'Reports')", True),
    ("dashboard", "navTo(null, 'dashboard', 'Dashboard')", False),
    ("knowledge", "navTo(null, 'knowledge', 'Knowledge')", True),
    ("calendar", "navTo(null, 'calendar', 'Calendar')", False),
    ("system-settings", "navTo(null, 'system-settings', 'Settings')", True),
]


def check(ok, label, detail=""):
    (NOTES if ok else FAILURES).append(("PASS" if ok else "FAIL", label, detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail else ""))


# Walks every painted element and returns the ones that read as light-on-dark.
# Runs in the page, so geometry and inheritance match the real layout.
SCAN = r"""
() => {
  const lum = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(s => parseFloat(s));
    const a = p.length > 3 ? p[3] : 1;
    if (a < 0.05) return null;                 // transparent: nothing painted
    return (0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]) * a;
  };
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;         // ignore dots, icons
    if (r.bottom < 0 || r.top > innerHeight) continue;  // only what is on screen
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (parseFloat(cs.opacity) === 0) continue;
    const bg = lum(cs.backgroundColor);
    if (bg === null) continue;
    const fg = lum(cs.color) ?? 0;
    if (bg > 150 && bg - fg > 90) {
      const key = el.tagName + '|' + (el.id || '') + '|' + el.className;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        cls: (typeof el.className === 'string' ? el.className : '').slice(0, 90),
        bg: cs.backgroundColor,
        size: Math.round(r.width) + 'x' + Math.round(r.height),
        sample: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
      });
    }
  }
  return out;
}
"""


def _prepare():
    from services.ai import business_knowledge as store
    from services import inbox
    auth_store._ensure_schema()
    store.ensure_tables()
    inbox.ensure_tables()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (ADMIN[1],))
        cur.execute(
            "INSERT INTO app_users (email, password_hash, display_name, role) "
            "VALUES (%s, %s, %s, %s)",
            (ADMIN[1], auth_store._hash(PASSWORD), ADMIN[0], "admin"))
        conn.commit()
    _seed_knowledge()
    _seed_inbox()


def _seed_knowledge():
    """A real wiki page: the knowledge page is one of the panes that renders its own
    surface, and an empty list proves nothing about how the document pane looks."""
    from services.ai import business_knowledge as store
    store.upsert_item({
        "title": KB_TITLE,
        "title_zh": "主题覆盖页",
        "summary": "A page seeded by the dark-mode coverage check.",
        "summary_zh": "深色模式覆盖检查用的页面。",
        "body": ("# Theme coverage\n\nA paragraph so the document pane has real "
                 "content to theme.\n\n- one\n- two\n"),
        "visibility": "private",
    }, ADMIN[1], slug=KB_PAGE)


def _seed_inbox():
    """An inbox row pointing at the wiki page, so the preview pane has a document to
    load — that pane is an iframe, and an iframe boots the app a second time."""
    from services import inbox
    with inbox._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM inbox_events WHERE target_slug = %s", (KB_PAGE,))
        conn.commit()
    inbox.emit(
        kind="share", actor_email=ADMIN[1],
        recipient_email=ADMIN[1], target_type="knowledge", target_slug=KB_PAGE,
        target_title=KB_TITLE, target_url=f"{WEB}?kb={KB_PAGE}",
        summary="把知识库页面《%s》分享给了你" % KB_TITLE)


def _cleanup():
    from services import inbox
    with inbox._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM inbox_events WHERE target_slug = %s", (KB_PAGE,))
        conn.commit()
    with auth_store._db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE email = %s", (ADMIN[1],))
        conn.commit()


def _serve():
    from main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        try:
            if requests.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise SystemExit("server did not come up")



def _frame_theme(frame):
    """The frame's own theme attribute, or 'unreadable' if it is cross-origin."""
    try:
        return frame.evaluate("() => document.documentElement.getAttribute('data-theme')")
    except Exception:  # noqa: BLE001
        return "unreadable"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    _prepare()
    _serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.goto(WEB, wait_until="domcontentloaded")

            # Log in through the real form, so the pages have their own content.
            page.evaluate(
                """async (creds) => {
                    const r = await fetch('api/auth/login', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(creds),
                    });
                    if (!r.ok) throw new Error('login failed: ' + r.status);
                }""",
                {"email": ADMIN[1], "password": PASSWORD},
            )
            page.evaluate("() => { try { localStorage.setItem('klado-theme', 'dark'); } catch (e) {} }")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1800)

            theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
            check(theme == "dark", "页面处于深色模式", f"data-theme={theme}")
            rows = page.evaluate(
                """async () => {
                    const r = await fetch('api/inbox?limit=5');
                    const j = await r.json();
                    return (j.items || j || []).length;
                }""")
            check(rows > 0, "收件箱有数据（否则扫描的是空页面）", f"rows={rows}")

            for name, nav, _needs in PAGES:
                print(f"\n── {name} ──")
                res = page.evaluate(
                    "(expr) => { try { eval(expr); return 'ok'; } catch (e) { return String(e); } }", nav)
                if res != "ok":
                    check(False, f"{name}: 切页失败", res)
                    continue
                page.wait_for_timeout(1400)
                # The Inbox preview is an iframe that boots the app a second time, and
                # that copy reads the SAME localStorage. Selecting a message is what
                # loads it, so the scan has to select one or it never sees a document.
                if name == "inbox":
                    picked = page.evaluate(
                        """() => {
                            const it = document.querySelector('#inbox-list .ibx-item');
                            if (!it) return 'no row';
                            it.click();
                            return 'clicked';
                        }""")
                    if picked != "clicked":
                        check(False, "inbox: 选不中消息", picked)
                    page.wait_for_timeout(2500)
                hits = page.evaluate(SCAN)
                # An iframe paints its own document, which the parent's scan cannot see.
                for fr in page.frames:
                    if fr == page.main_frame:
                        continue
                    try:
                        for h in fr.evaluate(SCAN):
                            hits.append({**h, tag: "iframe:" + h["tag"]})
                    except Exception as exc:  # noqa: BLE001 - a cross-origin frame is fine
                        NOTES.append(("INFO", f"{name}: iframe 未扫描 ({exc})", ""))
                if not hits:
                    check(True, f"{name}: 没有浅色残留")
                for h in hits[:10]:
                    label = f"{name}: <{h['tag']} id={h['id']!r} class={h['cls']!r}> {h['size']} bg={h['bg']}"
                    check(False, label, h["sample"])
                if len(hits) > 10:
                    NOTES.append(("INFO", f"{name}: 另有 {len(hits) - 10} 处未列出", ""))

            # ── 顺序探针：先浅色加载 iframe，再切深色 ──────────────────────
            # 覆盖检查是在加载前就设成深色的，因此完全看不到这条路径。
            # 真实顺序是：读者在浅色下打开消息（frame 加载并涂成浅色），
            # 之后才点顶栏的主题按钮 —— frame 是另一个 document，
            # theme.js 没有把变更通知过去。
            print("\n── 顺序：浅色加载 → 切深色 ──")
            # 必须整页重载：预览 iframe 已经在上面的扫描里加载过了，
            # `set('light')` 不会让它重新读取 localStorage，那测的是"陈旧"，
            # 不是"没有跟随"。真实顺序是读者从头就在浅色里。
            page.evaluate("() => { try { localStorage.setItem('klado-theme', 'light'); } catch (e) {} }")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1600)
            page.evaluate("() => navTo(null, 'inbox', 'Inbox')")
            page.wait_for_timeout(1000)
            page.evaluate(
                """() => { const it = document.querySelector('#inbox-list .ibx-item');
                           if (it) it.click(); }""")
            page.wait_for_timeout(2500)

            def live_frames():
                # about:blank 的空 frame 没有 document.documentElement，读出来是 None
                return [f for f in page.frames
                        if f != page.main_frame and not f.url.startswith("about:")]

            before = [(f.url.rsplit("/", 1)[-1][:30], _frame_theme(f)) for f in live_frames()]
            top_before = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
            page.evaluate("() => window.kladoTheme.toggle()")
            page.wait_for_timeout(1500)
            after = [(f.url.rsplit("/", 1)[-1][:30], _frame_theme(f)) for f in live_frames()]
            top_after = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
            print(f"  顶层 data-theme: {top_before} -> {top_after}")
            for (u, b), (_, a) in zip(before, after):
                print(f"  iframe {u}: 切换前 theme={b} 切换后 theme={a}")
            if not after:
                NOTES.append(("INFO", "顺序探针：没有可读的 iframe", ""))
            elif any(b == "light" and a == "light" for _, (b, a) in zip(after, after)) or \
                    any(b == "light" and a == "light" for (u, b), (u2, a) in zip(before, after)):
                check(False, "主题切换后 iframe 仍是浅色",
                      f"before={before} after={after} top={top_after}")
            else:
                check(True, "主题切换后 iframe 跟随", f"{before} -> {after}")

            if args.keep:
                page.wait_for_timeout(600000)
            browser.close()
    finally:
        _cleanup()

    fails = [f for f in FAILURES if f[0] == "FAIL"]
    print(f"\n{'=' * 60}\n{len(fails)} failure(s), {len(NOTES)} note(s)")
    for _, label, detail in fails:
        print(f"  FAIL {label} {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
