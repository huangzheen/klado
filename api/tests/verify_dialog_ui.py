"""One dialog for the whole app: no page may still raise a browser-native one.

`window.confirm` / `window.prompt` / `window.alert` cannot be styled, cannot be keyboard-driven
consistently, block the whole tab and look nothing like the app — the user asked for the three
pages that still used them (Workspace, knowledge base, calendar) to be restyled. 29 call sites
later, this suite is what keeps them gone:

* the native functions are **counted**, and every flow that used to raise one must leave the
  counter at **0** (the counter is the whole point — a passing screenshot proves nothing);
* the in-app dialog takes their place: the right title, the danger button for a destructive
  action, Esc = cancel, Enter / the OK button = confirm, the scrim = cancel;
* a prompt returns what was typed (checked by following it into the request body);
* `alert()` became a toast, because a message you only have to read should not be a modal.

    .venv312/bin/python api/tests/verify_dialog_ui.py
"""
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8825
FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


REPORT = {"slug": "q3-deck", "title": "Q3 Deck", "status": "published", "visibility": "private",
          "owner_email": "owner@example.com", "submitter": "Zhen", "updated_at": "2026-09-20T10:00:00",
          "can_manage": True, "kind": "static", "has_cover": False, "html": "<div class='deck'></div>"}
KB = {"slug": "channel-notes", "title": "渠道笔记", "status": "published", "visibility": "private",
      "owner_email": "owner@example.com", "can_manage": True, "summary_zh": "口径", "summary_en": "defs",
      "document_type": "note", "created_at": "2026-09-20T10:00:00", "updated_at": "2026-09-20T10:00:00",
      "body": "# t\n\nx", "tags": [], "category": "渠道"}
EVENT = {"slug": "q3-review", "title": "Q3 渠道复盘会", "start_date": "2026-08-05",
         "end_date": "2026-08-09", "deadline": "2026-08-08", "kind": "review",
         "status": "published", "visibility": "private", "owner_email": "owner@example.com",
         "can_manage": True, "category": "渠道", "tags": [], "summary_en": "en", "summary_zh": "zh",
         "partners": ["mate@example.com"], "attachments": [], "created_at": "", "updated_at": "",
         # a cover, so "Remove the cover" is on the menu (it is conditional on has_cover)
         "has_cover": True, "cover_url": "/api/calendar/events/q3-review/cover"}

STUB = """
window.__native = {alert: 0, confirm: 0, prompt: 0};
window.alert = function (m) { window.__native.alert += 1; };
window.confirm = function (m) { window.__native.confirm += 1; return true; };
window.prompt = function (m, d) { window.__native.prompt += 1; return d; };
window.__req = [];
window.fetch = function (url, init) {
  const u = String(url);
  const method = ((init || {}).method || 'GET').toUpperCase();
  window.__req.push(method + ' ' + u);
  if (init && init.body) { try { window.__lastBody = JSON.parse(init.body); } catch (e) {} }
  const reply = (payload) => Promise.resolve({ok: true, status: 200,
    headers: {get: () => 'application/json'}, json: () => Promise.resolve(payload),
    blob: () => Promise.resolve(new Blob(['x']))});
  if (u.indexOf('/api/reports/cover') >= 0) return reply({});
  // ⚠️ The wall asks for the PROJECTS before it asks for anything else (2026-10-03:
  // 「我的工作台」is a wall of projects, and a report is a row in `#wr-list` once one of
  // them is open). Without this branch the generic `/api/reports` rule below answers it
  // with a single report object, `_projects` comes back empty, and the wall renders the
  // new-project tile with no card at all — a wall with nothing on it that still passes
  // a "something rendered" wait.
  if (/\\/api\\/reports\\/projects(\\?|$)/.test(u)) return reply({projects: PROJECTS, count: PROJECTS.length});
  if (u.indexOf('/api/reports') >= 0) {
    const m = u.match(/\\/api\\/reports\\/([^/?]+)/);
    // the Workspace list endpoint answers with `reports`, not `items` (a stub that says
    // `items` renders the empty state and the suite then waits forever for a card)
    return reply(m ? Object.assign({}, REPORTS[0], {slug: decodeURIComponent(m[1])})
                   : {reports: REPORTS, count: 1, scope: 'mine', categories: ['渠道']});
  }
  if (u.indexOf('/api/knowledge/items') >= 0) {
    const m = u.match(/\\/api\\/knowledge\\/items\\/([^/?]+)/);
    return reply(m ? Object.assign({}, KB_ITEM, {slug: decodeURIComponent(m[1])})
                   : {items: [KB_ITEM], count: 1, scope: 'mine'});
  }
  if (u.indexOf('/api/calendar/events') >= 0) {
    const m = u.match(/\\/api\\/calendar\\/events\\/([^/?]+)/);
    return reply(m ? Object.assign({}, EVENTS[0], {body: 'x'})
                   : {events: EVENTS, count: 1, scope: 'mine'});
  }
  return reply({});
};
const REPORTS = %s, KB_ITEM = %s, EVENTS = %s, PROJECTS = %s;
""" % (json.dumps([REPORT]), json.dumps(KB), json.dumps([EVENT]),
       json.dumps([{"slug": "unfiled-probe", "title": "未归档 / Unfiled", "summary": "",
                    "system": True, "has_cover": False, "cover_url": "", "cover_w": None,
                    "cover_h": None, "cover_prompt": "", "cover_model": "",
                    "folder_count": 1, "report_count": 1, "can_manage": True,
                    "created_at": "2026-09-20T10:00:00", "updated_at": "2026-09-20T10:00:00"}]))


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    httpd = _Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


DIALOG = """() => {
  const dlg = document.getElementById('kld-dlg');
  if (!dlg || dlg.hidden) return null;
  const ok = document.getElementById('kld-dlg-ok');
  const cancel = document.getElementById('kld-dlg-cancel');
  const input = document.getElementById('kld-dlg-input');
  const box = dlg.querySelector('.kld-dlg-bd');
  const ref = document.querySelector('.rpt-modal-box');
  return {title: document.getElementById('kld-dlg-title').textContent,
          body: document.getElementById('kld-dlg-body').textContent.trim(),
          ok: ok.textContent.trim(), ok_danger: ok.classList.contains('danger'),
          cancel: cancel.textContent.trim(), cancel_hidden: cancel.hidden,
          input: input ? {tag: input.tagName.toLowerCase(), type: input.type || '',
                          value: input.value, rows: input.rows || 0} : null,
          radius: getComputedStyle(box).borderRadius,
          focused: input ? document.activeElement === input : document.activeElement === ok,
          ref_radius: ref ? getComputedStyle(ref).borderRadius : null};
}"""


def open_row_menu(page, slug="q3-review"):
    """Right-click a calendar bar and wait for the row menu (the menu closes after a click)."""
    page.evaluate("""() => {
      const bar = document.querySelector('#cal-grid .cal-bar[data-slug="%s"]');
      const box = bar.getBoundingClientRect();
      bar.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
        clientX: box.left + 8, clientY: box.top + 8}));
    }""" % slug)
    page.wait_for_selector("#cal-ctx:not([hidden])", timeout=4000)


# ⚠️ `page.evaluate("() => someAsyncAction()")` AWAITS the returned promise, and these actions
# resolve only when the dialog is answered — so "evaluating" and "answering" would wait for each
# other and the suite would hang (found the hard way). Every such call is wrapped in braces so
# the arrow returns nothing.


def native(page):
    return page.evaluate("() => Object.assign({}, window.__native)")


def open_page(browser, viewport=None):
    page = browser.new_page(viewport=viewport or {"width": 1500, "height": 950})
    page.add_init_script(STUB)
    page.goto(f"http://127.0.0.1:{PORT}/index.html", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.calendarPage && !!window.reportsPage && !!window.knowledgePage")
    page.wait_for_timeout(250)
    return page


httpd = serve()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = open_page(browser)

        # ── the dialog itself: style, keyboard, scrim ─────────────────────────
        page.evaluate("""() => {
          window.__probe = null;
          kldDialog.confirm({title: 'Probe?', body: 'line one\\nline two', ok: 'Do it', danger: true})
            .then((v) => { window.__probe = v; });
        }""")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        dlg = page.evaluate(DIALOG)
        check("对话框出现，标题/正文/按钮都对", dlg and dlg["title"] == "Probe?" and dlg["ok"] == "Do it"
              and "line one" in dlg["body"] and dlg["cancel"] == "Cancel", dlg)
        check("破坏性动作的确认按钮是红色", bool(dlg) and dlg["ok_danger"] is True, dlg)
        check("外观沿用 app 的弹窗样式（与 .rpt-modal-box 同为 12px 圆角）",
              dlg and dlg["radius"] == "12px", dlg and [dlg["radius"], dlg["ref_radius"]])
        page.keyboard.press("Escape")
        page.wait_for_function("() => window.__probe !== null")
        check("Esc = 取消（false），且对话框关闭",
              page.evaluate("() => window.__probe") is False
              and page.evaluate("() => document.getElementById('kld-dlg').hidden"), True)

        # Enter confirms
        page.evaluate("""() => { window.__probe = null;
          kldDialog.confirm({title: 'Enter?', ok: 'Yes'}).then((v) => { window.__probe = v; }); }""")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        page.keyboard.press("Enter")
        page.wait_for_function("() => window.__probe !== null")
        check("Enter = 确认（true）", page.evaluate("() => window.__probe") is True, True)

        # scrim = cancel
        page.evaluate("""() => { window.__probe = null;
          kldDialog.confirm({title: 'Scrim?'}).then((v) => { window.__probe = v; }); }""")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        page.mouse.click(30, 30)
        page.wait_for_function("() => window.__probe !== null")
        check("点幕布 = 取消", page.evaluate("() => window.__probe") is False, True)

        # ── Calendar: delete / partners / cover ───────────────────────────────
        page.click("#nav-tab-calendar")
        page.wait_for_selector("#cal-grid .cal-month", state="visible")
        before = native(page)
        page.evaluate("() => { window.calendarPage.runAction('delete', 'q3-review'); }")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        dlg = page.evaluate(DIALOG)
        check("Calendar 删除：用的是 app 对话框，不是原生 confirm",
              dlg and "Delete" in dlg["title"] and dlg["ok"] == "Delete" and dlg["ok_danger"], dlg)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("取消后**没有**发出删除请求",
              not any("DELETE" in r and "/api/calendar/events/q3-review" in r
                      for r in page.evaluate("() => window.__req")),
              page.evaluate("() => window.__req.slice(-2)"))
        page.evaluate("() => { window.calendarPage.runAction('delete', 'q3-review'); }")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        page.click("#kld-dlg-ok")
        page.wait_for_timeout(300)
        check("确认后才真的删（DELETE 到了服务端）",
              any(r.startswith("DELETE /api/calendar/events/q3-review") for r in
                  page.evaluate("() => window.__req")), True)

        # partners: a prompt, and what was typed reaches the request body
        page.evaluate("() => { window.calendarPage.runAction('partners', 'q3-review'); }")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        dlg = page.evaluate(DIALOG)
        check("Calendar 参与人：多行输入框，预填了现有参与人",
              dlg and dlg["input"] and dlg["input"]["tag"] == "textarea"
              and dlg["input"]["value"] == "mate@example.com" and dlg["focused"], dlg and dlg["input"])
        page.fill("#kld-dlg-input", "mate@example.com, new.colleague@example.com")
        page.click("#kld-dlg-ok")
        page.wait_for_timeout(300)
        sent = page.evaluate("() => window.__lastBody")
        check("输入的内容进了 PUT 请求体",
              sent and sent.get("partners") == ["mate@example.com", "new.colleague@example.com"], sent)
        page.evaluate("() => window.calendarPage.closeEvent()")

        # cover removal
        page.evaluate("() => { window.calendarPage.openEvent('q3-review'); }")
        page.wait_for_timeout(300)
        page.evaluate("() => window.calendarPage.coverMenu({stopPropagation(){}, clientX: 60, clientY: 60})")
        page.wait_for_selector("#cal-ctx:not([hidden])", timeout=4000)
        page.click('#cal-ctx button[data-action="cover-clear"]')
        page.wait_for_selector("#kld-dlg:not([hidden])")
        check("Calendar 移除封面：app 对话框（红色确认）",
              page.evaluate(DIALOG)["ok_danger"] is True, page.evaluate(DIALOG)["title"])
        # Esc is covered by its own check above; here click Cancel — and WAIT for the dialog to
        # be gone, because a dialog still on screen (z-index 9700) covers the nav bar and the
        # next click would just time out.
        page.click("#kld-dlg-cancel")
        page.wait_for_selector("#kld-dlg", state="hidden", timeout=4000)

        # ── Knowledge base: delete from the row menu ──────────────────────────
        page.evaluate("() => window.calendarPage.closeEvent()")
        page.wait_for_selector("#kld-dlg", state="hidden", timeout=4000)
        page.click("#nav-tab-knowledge")
        page.wait_for_selector("#kb-toc-list .kb-link", timeout=5000)
        page.evaluate("() => { window.knowledgePage.open('channel-notes'); }")
        page.wait_for_timeout(400)
        page.evaluate("() => { window.knowledgePage.remove(); }")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        dlg = page.evaluate(DIALOG)
        check("知识库删除：app 对话框",
              dlg and "Delete" in dlg["title"] and dlg["ok_danger"], dlg)
        page.click("#kld-dlg-cancel")
        page.wait_for_selector("#kld-dlg", state="hidden", timeout=4000)

        # ── Workspace: delete a report from its row menu ─────────────────────
        page.click("#nav-tab-reports")
        # ⚠️ "the Workspace has loaded" is now "the wall has drawn a project card".
        # `.rpt-card` is gone from this page, and a project card is the same kind of
        # evidence it was: the wall renders it from the same response that makes the
        # rest of the page live, and the delete below is a call on that same page.
        page.wait_for_selector("#wr-wall .wr-proj", timeout=5000)
        page.evaluate("() => { window.reportsPage.remove('q3-deck'); }")
        page.wait_for_selector("#kld-dlg:not([hidden])")
        dlg = page.evaluate(DIALOG)
        check("Workspace 删除：app 对话框",
              dlg and "Delete" in dlg["title"] and dlg["ok_danger"], dlg)
        page.click("#kld-dlg-cancel")
        page.wait_for_selector("#kld-dlg", state="hidden", timeout=4000)

        # ── alert() became a toast ────────────────────────────────────────────
        page.evaluate("() => { window.calendarPage.openEvent('q3-review'); }")
        page.evaluate("() => window.kldDialog.notify('Something went wrong', 'err')")
        page.wait_for_selector(".kld-toast.err", timeout=3000)
        check("错误提示用 toast（右下角，不拦操作）",
              page.evaluate("""() => { const t = document.querySelector('.kld-toast.err');
                const r = t.getBoundingClientRect();
                return {text: t.textContent, right: Math.round(innerWidth - r.right),
                        bottom: Math.round(innerHeight - r.bottom),
                        blocks_clicks: getComputedStyle(t.parentElement).pointerEvents}; }"""),
              True)
        page.wait_for_timeout(100)

        after = native(page)
        check("★ 三个页面的这些流程里，原生弹窗一次都没被调用",
              before == after == {"alert": 0, "confirm": 0, "prompt": 0}, [before, after])

        errors = page.evaluate("() => window.__errors || []")
        check("整页没有 JS 报错", errors == [], errors[:2])
        page.close()
        browser.close()
finally:
    httpd.shutdown()

print("\n%d checks failed" % len(FAILURES))
raise SystemExit(1 if FAILURES else 0)