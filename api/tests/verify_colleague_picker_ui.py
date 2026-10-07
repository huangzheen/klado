"""The colleague picker — live search of registered users, in both share dialogs.

Two callers, one component:

* **Workspace** → the report dialog's "Share with colleagues" box. It used to be a
  plain text field where you had to know the address by heart.
* **Knowledge base** → the page dialog. It used to be the browser's `window.prompt`, which
  could not show who is registered, could not be driven from the keyboard, and looked
  nothing like the rest of the app.

The assertions that matter:

* typing runs a real query (`/api/auth/colleagues?q=…`) and renders the matches;
* **Tab and Enter insert the highlighted colleague** and keep the caret in the field, so
  the *second* address works exactly like the first — that is the multi-email requirement;
* when the dropdown is closed, Tab still moves focus out (otherwise the field would become
  a keyboard trap);
* Esc closes the dropdown and *not* the dialog behind it;
* the knowledge-base dialog is the *same design*, so this compares the two dialogs'
  computed styles element by element rather than trusting two copies of markup to stay in
  step (the way the Workspace/Knowledge area tabs drifted once);
* `window.prompt` is never called from the knowledge base again.

Needs playwright (present in `.venv312`, not in the API image) and a local Chrome.

    .venv312/bin/python api/tests/verify_colleague_picker_ui.py
"""
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8808

FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    httpd = _Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# A fake directory + the two share endpoints. `co` matches all three addresses (every
# address ends in `.com`), which is what gives the arrow-key test more than one row.
STUB = """
window.__realFetch = window.fetch.bind(window);
window.__calls = [];
window.__directory = [
  {email: 'anne.zhao@example.com',  display_name: 'Anne Zhao',  role: 'user'},
  {email: 'boris.li@example.com',  display_name: 'Boris Li',   role: 'admin'},
  {email: 'carla.wu@example.com',  display_name: 'Carla Wu',   role: 'user'},
];
window.__granted = [];
window.__shared = ['carla.wu@example.com'];
window.__fail401 = false;
window.__prompts = [];
window.prompt = function () { window.__prompts.push([].slice.call(arguments)); return null; };

window.fetch = function (url, init) {
  const u = String(url);
  const method = ((init && init.method) || 'GET').toUpperCase();
  if (u.indexOf('/api/') < 0) return window.__realFetch.apply(this, arguments);
  window.__calls.push({url: u, method: method, body: init && init.body});
  const reply = (body, status) => Promise.resolve({
    ok: (status || 200) < 400, status: status || 200,
    json: () => Promise.resolve(body),
  });
  if (u.indexOf('/api/auth/colleagues') >= 0) {
    if (window.__fail401) return reply({detail: 'not authenticated'}, 401);
    const m = u.match(/[?&]q=([^&]*)/);
    const q = decodeURIComponent(m ? m[1] : '').toLowerCase();
    return reply({domain: 'example.com', colleagues: window.__directory.filter((c) =>
      c.email.toLowerCase().indexOf(q) >= 0 || c.display_name.toLowerCase().indexOf(q) >= 0)});
  }
  if (u.indexOf('/shares/colleagues') >= 0) return reply({emails: [], url: 'http://x/r/demo'});
  if (u.indexOf('/api/reports/demo-slug/shares') >= 0) {
    return reply({colleagues: window.__granted, colleague_url: 'http://x/r/demo-slug'});
  }
  if (u.indexOf('/api/knowledge/items') >= 0) {
    if (u.indexOf('/shares') >= 0) {
      if (method === 'DELETE') {
        const e = decodeURIComponent((u.match(/[?&]email=([^&]*)/) || [, ''])[1]);
        window.__shared = window.__shared.filter((x) => x !== e);
      } else {
        const add = JSON.parse((init && init.body) || '{}').emails || [];
        window.__shared = window.__shared.concat(add.filter((e) => window.__shared.indexOf(e) < 0));
      }
      return reply({slug: 'demo', shared_emails: window.__shared});
    }
    if (/\\/api\\/knowledge\\/items\\/demo$/.test(u)) {
      return reply({slug: 'demo', title: 'Channel notes', body: '# Channel notes',
                    document_type: 'note', status: 'published', visibility: 'private',
                    owner_email: 'me@example.com', can_manage: true, version: '1.0',
                    shared_emails: window.__shared, category: '', tags: []});
    }
    return reply({items: []});
  }
  return reply({reports: [], count: 0, categories: [], scope: 'mine', items: []});
};
"""


def calls(page, needle, method=None):
    return page.evaluate("""([needle, method]) => window.__calls.filter(
      (c) => c.url.indexOf(needle) >= 0 && (!method || c.method === method))""",
                         [needle, method])


def pop(page, input_sel):
    """The picker's state for one input, including where its dropdown actually ended up."""
    return page.evaluate("""(sel) => {
      const input = document.querySelector(sel);
      const box = document.querySelector('.colleague-pop[data-for="' + input.id + '"]');
      const rows = box ? [...box.querySelectorAll('.cp-row')] : [];
      const rect = (el) => el ? el.getBoundingClientRect() : null;
      const dialog = input.closest('.rpt-share-bd');
      const foot = box ? box.querySelector('.cp-foot') : null;
      const br = rect(box), dr = rect(dialog), fr = rect(foot);
      return {
        pops: document.querySelectorAll('.colleague-pop[data-for="' + input.id + '"]').length,
        open: !!box && !box.hidden,
        role: box ? box.getAttribute('role') : '',
        rows: rows.map((r) => ({text: r.textContent.replace(/\\s+/g, ' ').trim(),
                                on: r.classList.contains('on'),
                                granted: !!r.querySelector('.cp-granted')})),
        msg: box && box.querySelector('.cp-msg') ? box.querySelector('.cp-msg').textContent : '',
        foot: foot ? foot.textContent : '',
        value: input.value,
        expanded: input.getAttribute('aria-expanded'),
        focused: document.activeElement === input,
        // The dropdown is a fixed layer on <body>: it must escape the dialog's clipping
        // AND still fit on screen.
        position: box ? getComputedStyle(box).position : '',
        parentIsBody: !!box && box.parentElement === document.body,
        overflowsDialog: !!(br && dr) && br.bottom > dr.bottom + 1,
        fullyVisible: !!br && br.bottom <= window.innerHeight + 1 && br.top >= -1,
        footVisible: !!fr && fr.bottom <= window.innerHeight + 1 && fr.height > 4,
        sameWidthAsInput: !!br && Math.abs(br.width - input.getBoundingClientRect().width) <= 1,
        // A fixed layer on <body> also has to win the stacking order against .rpt-modal
        // (z-index 8100) — otherwise the rows render but nothing can be clicked.
        rowHitTest: rows.length ? (() => {
          const r = rows[0].getBoundingClientRect();
          const el = document.elementFromPoint(r.left + 10, r.top + r.height / 2);
          return !!(el && el.closest('.colleague-pop'));
        })() : null,
      };
    }""", input_sel)


def styles(page, pairs):
    """Computed styles for one element from each dialog, keyed by the caller."""
    return page.evaluate("""(pairs) => {
      const out = {};
      for (const [key, sel, props] of pairs) {
        const el = document.querySelector(sel);
        const cs = el ? getComputedStyle(el) : null;
        out[key] = el ? props.reduce((acc, p) => (acc[p] = cs[p], acc), {}) : null;
      }
      return out;
    }""", pairs)


def main():
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(STUB)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => typeof reportsPage !== 'undefined' && typeof knowledgePage !== 'undefined'"
                " && typeof kldColleaguePicker === 'object'")

            # ── Workspace dialog ─────────────────────────────────────────────
            page.evaluate("() => reportsPage.openColleagueShare('demo-slug')")
            page.wait_for_selector("#rpt-share-colleagues.open")
            page.wait_for_timeout(250)
            st = pop(page, "#rpt-share-emails")
            check("分享框已挂上选择器，下拉初始为空",
                  st["pops"] == 1 and not st["open"], st)
            check("下拉是可访问的 listbox（不是随便一个 div）", st["role"] == "listbox", st["role"])
            check("字段仍是普通文本框，值可以手工输入", st["value"] == "", st["value"])

            page.click("#rpt-share-emails")
            page.keyboard.type("co")
            page.wait_for_timeout(400)
            st = pop(page, "#rpt-share-emails")
            check("输入两个字就实时查注册同事", bool(calls(page, '/api/auth/colleagues?limit=8&q=co')),
                  [c["url"] for c in calls(page, '/api/auth/colleagues')])
            check("匹配到 3 位同事并显示邮箱 + 姓名 + 角色",
                  st["open"] and len(st["rows"]) == 3
                  and "anne.zhao@example.com" in st["rows"][0]["text"]
                  and "Anne Zhao" in st["rows"][0]["text"]
                  and "ADMIN" in st["rows"][1]["text"],
                  st["rows"])
            check("第一条默认高亮（打字后直接按 Tab/Enter 就能选中）",
                  st["rows"][0]["on"] and st["expanded"] == "true", st["rows"])
            check("下拉里有键盘说明", "Tab" in st["foot"] and "Esc" in st["foot"], st["foot"])

            check("下拉是挂在 body 上的 fixed 浮层：能越过弹窗的滚动容器，底部不被裁掉",
                  st["position"] == "fixed" and st["parentIsBody"] and st["overflowsDialog"]
                  and st["fullyVisible"] and st["footVisible"] and st["sameWidthAsInput"]
                  and st["rowHitTest"] is True,
                  {"position": st["position"], "parentIsBody": st["parentIsBody"],
                   "overflowsDialog": st["overflowsDialog"], "fullyVisible": st["fullyVisible"],
                   "footVisible": st["footVisible"], "widthMatches": st["sameWidthAsInput"],
                   "rowClickable": st["rowHitTest"]})

            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(120)
            st = pop(page, "#rpt-share-emails")
            check("↓ 把高亮移到第二条", st["rows"][1]["on"] and not st["rows"][0]["on"], st["rows"])
            page.keyboard.press("ArrowUp")
            page.wait_for_timeout(120)
            check("↑ 移回第一条", pop(page, "#rpt-share-emails")["rows"][0]["on"])

            # Enter inserts the highlighted one.
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
            st = pop(page, "#rpt-share-emails")
            check("Enter 插入高亮的同事，并自动补好逗号分隔",
                  st["value"] == "anne.zhao@example.com, ", st["value"])
            check("插入后光标仍在输入框里（可以接着打第二个）", st["focused"] and not st["open"], st)

            # The second address, via Tab this time.
            page.keyboard.type("bo")
            page.wait_for_timeout(400)
            check("第二个邮箱同样会触发搜索",
                  len([c for c in calls(page, '/api/auth/colleagues') if 'q=bo' in c['url']]) == 1,
                  [c["url"] for c in calls(page, '/api/auth/colleagues')])
            page.keyboard.press("Tab")
            page.wait_for_timeout(200)
            st = pop(page, "#rpt-share-emails")
            check("Tab 插入第二个同事，多个邮箱的写法一致",
                  st["value"] == "anne.zhao@example.com, boris.li@example.com, ", st["value"])
            check("Tab 插入后没有把焦点移走（否则第二个就白打了）", st["focused"], st)

            # Esc: dropdown only, dialog stays.
            page.keyboard.type("co")
            page.wait_for_timeout(400)
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            st = pop(page, "#rpt-share-emails")
            check("Esc 只关下拉，不关背后的弹窗",
                  not st["open"]
                  and page.evaluate("() => !!document.querySelector('#rpt-share-colleagues.open')"),
                  st)
            page.keyboard.press("Tab")
            page.wait_for_timeout(150)
            check("下拉关着的时候 Tab 仍然是普通 Tab（不失焦也不吞键）",
                  not pop(page, "#rpt-share-emails")["focused"])

            # Grant: the parsed list goes out as several addresses.
            page.click("#rpt-share-emails")
            page.evaluate("""() => {
              const f = document.getElementById('rpt-share-emails');
              f.value = 'anne.zhao@example.com, boris.li@example.com';
            }""")
            page.click("#rpt-share-colleagues .btn-primary")
            page.wait_for_timeout(350)
            post = calls(page, '/shares/colleagues', 'POST')
            check("Grant access 把多个邮箱一起提交",
                  len(post) == 1
                  and post[0]["body"] == '{"emails":["anne.zhao@example.com","boris.li@example.com"]}',
                  post)
            check("提交后输入框被清空、弹窗仍开着",
                  pop(page, "#rpt-share-emails")["value"] == ""
                  and page.evaluate("() => !!document.querySelector('#rpt-share-colleagues.open')"))

            # GRANTED tag for whoever already has access.
            page.evaluate("() => { window.__granted = ['carla.wu@example.com']; }")
            page.evaluate("() => reportsPage.closeColleagueShare()")
            page.evaluate("() => reportsPage.openColleagueShare('demo-slug')")
            page.wait_for_timeout(250)
            page.click("#rpt-share-emails")
            page.keyboard.type("co")
            page.wait_for_timeout(400)
            st = pop(page, "#rpt-share-emails")
            granted = [r for r in st["rows"] if r["granted"]]
            check("已经有权限的同事被标成 GRANTED（而不是让你重复添加）",
                  len(granted) == 1 and "carla.wu@example.com" in granted[0]["text"], st["rows"])

            # 401 → the dropdown says so instead of pretending the directory is empty.
            page.evaluate("() => { window.__fail401 = true; }")
            page.keyboard.press("Escape")
            page.keyboard.type("co")
            page.wait_for_timeout(400)
            st = pop(page, "#rpt-share-emails")
            check("未登录时下拉说明原因，而不是静默无结果",
                  "Sign in" in st["msg"], st["msg"])
            page.evaluate("() => { window.__fail401 = false; }")
            page.evaluate("() => reportsPage.closeColleagueShare()")

            # ── Knowledge base dialog: same dialog, same picker ──────────────
            page.evaluate("() => navTo(null, 'knowledge', 'Knowledge base')")
            page.wait_for_timeout(200)
            page.evaluate("() => knowledgePage.open('demo')")
            page.wait_for_timeout(400)
            check("知识库打开了某一页（准备验证分享）",
                  page.evaluate("() => document.getElementById('kb-doc-title').textContent")
                  == "Channel notes",
                  page.evaluate("() => document.getElementById('kb-doc-title').textContent"))

            page.click("#kb-doc-meta button[onclick='knowledgePage.share()']")
            page.wait_for_timeout(250)
            check("知识库的 Share 打开的是应用自己的弹窗（不再是浏览器原生 prompt）",
                  page.evaluate("() => window.__prompts.length") == 0
                  and page.evaluate("() => getComputedStyle(document.getElementById('kb-share-colleagues')).display")
                  == "flex")
            st = pop(page, "#kb-share-emails")
            check("知识库弹窗里是同一个同事选择器",
                  st["pops"] == 1 and st["role"] == "listbox"
                  and st["position"] == "fixed" and st["parentIsBody"], st)
            check("原有授权对象以同一套样式列出来（含移除按钮）",
                  page.evaluate("""() => [...document.querySelectorAll('#kb-share-list .rpt-share-person')]
                    .map((r) => r.textContent.trim())""")
                  and "carla.wu@example.com"
                  in page.evaluate("() => document.getElementById('kb-share-list').textContent"),
                  page.evaluate("() => document.getElementById('kb-share-list').textContent"))

            page.click("#kb-share-emails")
            page.keyboard.type("anne")
            page.wait_for_timeout(400)
            st = pop(page, "#kb-share-emails")
            check("知识库里的下拉同样不被裁（最后一个弹窗也一样）",
                  st["fullyVisible"] and st["footVisible"] and st["overflowsDialog"], st)
            check("知识库里也能实时搜到同事", st["open"] and len(st["rows"]) == 1
                  and "anne.zhao@example.com" in st["rows"][0]["text"], st["rows"])
            page.keyboard.press("Tab")
            page.wait_for_timeout(200)
            check("知识库里 Tab 也能插入",
                  pop(page, "#kb-share-emails")["value"] == "anne.zhao@example.com, ",
                  pop(page, "#kb-share-emails")["value"])

            page.click("#kb-share-colleagues .btn-primary")
            page.wait_for_timeout(350)
            post = calls(page, '/api/knowledge/items/demo/shares', 'POST')
            check("Save access 提交到知识库的分享接口",
                  len(post) == 1 and post[0]["body"] == '{"emails":["anne.zhao@example.com"]}', post)
            check("保存后列表里出现新同事、输入框清空",
                  "anne.zhao@example.com" in page.evaluate("() => document.getElementById('kb-share-list').textContent")
                  and pop(page, "#kb-share-emails")["value"] == "",
                  page.evaluate("() => document.getElementById('kb-share-list').textContent"))

            page.click("#kb-share-list button[data-email='carla.wu@example.com']")
            page.wait_for_timeout(350)
            dels = calls(page, '/api/knowledge/items/demo/shares', 'DELETE')
            check("点 ✕ 走删除接口移除那一个人",
                  len(dels) == 1 and "email=carla.wu%40example.com" in dels[0]["url"], dels)
            check("移除后列表里不再有他",
                  "carla.wu" not in page.evaluate("() => document.getElementById('kb-share-list').textContent"),
                  page.evaluate("() => document.getElementById('kb-share-list').textContent"))

            # ── "same design" as a computed-style comparison, not a hope ─────
            page.click("#kb-share-colleagues .btn-ghost")      # Cancel
            page.wait_for_timeout(150)
            page.evaluate("() => reportsPage.openColleagueShare('demo-slug')")
            page.wait_for_timeout(250)
            props = ["width", "borderRadius", "backgroundColor", "boxShadow"]
            box_a = styles(page, [["a", "#rpt-share-colleagues .rpt-modal-box", props]])
            box_b = styles(page, [["a", "#kb-share-colleagues .rpt-modal-box", props]])
            check("两个弹窗的容器几何与外观完全一致", box_a == box_b, {"workspace": box_a, "knowledge": box_b})
            iprops = ["fontSize", "padding", "borderRadius", "backgroundColor", "color", "borderWidth"]
            in_a = styles(page, [["a", "#rpt-share-emails", iprops]])
            in_b = styles(page, [["a", "#kb-share-emails", iprops]])
            check("两个输入框的样式完全一致", in_a == in_b, {"workspace": in_a, "knowledge": in_b})
            bprops = ["fontSize", "padding", "borderRadius", "backgroundColor", "color"]
            btn_a = styles(page, [["a", "#rpt-share-colleagues .btn-primary", bprops]])
            btn_b = styles(page, [["a", "#kb-share-colleagues .btn-primary", bprops]])
            check("两边的按钮样式完全一致", btn_a == btn_b, {"workspace": btn_a, "knowledge": btn_b})
            hd_a = page.inner_text("#rpt-share-colleagues .rpt-modal-hd strong")
            hd_b = page.inner_text("#kb-share-colleagues .rpt-modal-hd strong")
            check("标题文案一致（都是 Share with colleagues）",
                  hd_a == hd_b == "Share with colleagues", [hd_a, hd_b])

            # ── reopening must not stack a second dropdown ──────────────────
            page.evaluate("() => reportsPage.closeColleagueShare()")
            page.evaluate("() => reportsPage.openColleagueShare('demo-slug')")
            page.wait_for_timeout(200)
            check("反复打开不会叠出第二个下拉",
                  pop(page, "#rpt-share-emails")["pops"] == 1)

            check("整页没有 JS 报错", errors == [], errors[:3])
            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())