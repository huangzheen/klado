"""Workspace areas — the three scopes and the page chrome around them.

Two things this pins down, both of which were wrong before:

* **"Shared with me" is its own area.** A colleague's report used to be folded into
  "My workspace", so the wall mixed documents the viewer owns with documents other
  people own. The tabs, the request each one sends, and the actions a shared card
  offers are all asserted here.
* **The Knowledge base page has no second title row.** It used to render the shell's
  breadcrumb bar ("home > Knowledge base") directly under a nav bar that already
  highlights where you are — one wasted row that the Workspace page does not have.
  (The knowledge-page half of that check lives in ``verify_knowledge_wiki_ui.py``.)

Needs playwright (present in `.venv312`, not in the API image) and a local Chrome.

    .venv312/bin/python api/tests/verify_workspace_scopes_ui.py
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8801

OWNED = {
    "id": 1, "slug": "my-report", "title": "我自己的报告", "summary": "", "category": "渠道",
    "tags": ["渠道"], "status": "published", "owner_email": "me@example.com", "visibility": "private",
    "kind": "static", "can_manage": True, "shared_with_me": False, "shared": False,
    "created_at": "2026-09-29T10:00:00", "updated_at": "2026-09-29T10:00:00",
}
# ⚠️ A second, DRAFT card in 'mine'. Since 2026-10-04 the status dropdown is shown only
# when it can actually narrow something, and this stub used to return one published card
# and then assert the dropdown was visible — which asserted a control that could not change
# a single row. The rule this file is testing (the filter applies in 'mine', and nowhere
# else) is still worth pinning, so the fixture now contains a status it can narrow TO.
OWNED_DRAFT = dict(OWNED, id=3, slug="my-draft", title="我的草稿", status="draft")
SHARED = {
    "id": 2, "slug": "mate-report", "title": "同事分享的报告", "summary": "", "category": "市场",
    "tags": ["市场"], "status": "published", "owner_email": "mate@example.com", "visibility": "private",
    "kind": "static", "can_manage": False, "shared_with_me": True, "shared": True,
    "created_at": "2026-09-29T10:00:00", "updated_at": "2026-09-29T10:00:00",
}
# ⚠️ A **draft** in 'shared' as well. This fixture used to hold one shared report, all
# published, so `_statusKinds` was 1 outside 'mine' and the status dropdown stayed
# hidden for a reason that had nothing to do with the scope: there was nothing to
# narrow TO. The `_scope === 'mine'` half of `renderFilterVisibility()` therefore had
# no way to be reached, and dropping it (`varied = _statusKinds > 1`) was a mutation
# that SURVIVED — not because the guard is decorative but because the fixture could not
# produce the case it defends. With a draft here, `_statusKinds` is 2 in 'shared', the
# dropdown would appear, and `listQuery()` still only sends the status in 'mine' —
# which is precisely the dead control the guard exists to prevent.
SHARED_DRAFT = dict(SHARED, id=4, slug="mate-draft", title="同事的草稿", status="draft")

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


STUB = """
window.__calls = [];
const OWNED = %s, OWNED_DRAFT = %s, SHARED = %s, SHARED_DRAFT = %s;
window.fetch = function (url, init) {
  const u = String(url);
  const scope = (u.match(/[?&]scope=([a-z]+)/) || [])[1] || 'mine';
  window.__calls.push(u);
  let body = {reports: [OWNED, OWNED_DRAFT], count: 2, categories: ['渠道'], scope: scope};
  if (scope === 'shared') {
    body = {reports: [SHARED, SHARED_DRAFT], count: 2, categories: ['市场'], scope: scope};
  }
  if (scope === 'public') body = {reports: [], count: 0, categories: [], scope: scope};
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
};
""" % (json.dumps(OWNED), json.dumps(OWNED_DRAFT), json.dumps(SHARED), json.dumps(SHARED_DRAFT))


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
            page.wait_for_function("() => !!window.reportsPage")

            # ── the three areas ──────────────────────────────────────────────
            page.evaluate("() => navTo(null, 'reports', 'Workspace')")
            page.wait_for_selector("#rpt-tab-shared", state="visible")
            tabs = page.evaluate("""() => Array.from(
                document.querySelectorAll('#page-reports .rpt-tabs .rpt-tab'))
              .map((t) => ({id: t.id, label: t.textContent.trim(), on: t.classList.contains('on')}))""")
            check("Workspace 标签条有三个 area", len(tabs) == 3, tabs)
            check("顺序是 My workspace / Public area / Shared with me",
                  [t["id"] for t in tabs] == ["rpt-tab-mine", "rpt-tab-public", "rpt-tab-shared"], tabs)
            check("默认停在 My workspace", tabs[0]["on"] and not tabs[1]["on"] and not tabs[2]["on"], tabs)

            # ── each tab requests its own scope ──────────────────────────────
            page.click("#rpt-tab-shared")
            page.wait_for_function("() => document.getElementById('rpt-tab-shared').classList.contains('on')")
            calls = page.evaluate("() => window.__calls")
            check("点 Shared with me 请求 scope=shared",
                  any("scope=shared" in c for c in calls), calls[-3:])
            check("切换后 My workspace 不再高亮",
                  not page.evaluate("() => document.getElementById('rpt-tab-mine').classList.contains('on')"))
            check("我的报告不出现在 Shared with me 里",
                  "我自己的报告" not in page.inner_text("#wr-list"),
                  page.inner_text("#wr-list")[:60])
            check("同事分享的报告出现在 Shared with me 里",
                  "同事分享的报告" in page.inner_text("#wr-list"))

            # ── a shared row offers Pull, never manage actions ────────────────
            # ⚠️ Right-click, with a real mouse at real coordinates. The wall is
            # `#wr-list` of `.wr-row` buttons and `#rpt-grid` of cards has not existed
            # since the list refactor — this file sat dead on `wait_for_selector`
            # until then. `el.click()` would prove nothing about a hit-tested
            # pointer either, so the trigger here is `page.mouse.click(..., "right")`.
            at = page.evaluate("""() => {
              const row = document.querySelector('#wr-list .wr-row[data-slug]');
              const r = row.getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }""")
            page.mouse.click(at["x"], at["y"], button="right")
            page.wait_for_selector("#rpt-menu", state="visible")
            menu = page.inner_text("#rpt-menu")
            check("共享行给的是 Pull to my workspace", "Pull to my workspace" in menu, menu)
            for absent in ("Publish", "Share with colleagues", "Delete", "Rename"):
                check("共享行不给 %s" % absent, absent not in menu, menu)
            check("共享行仍然给「加入左侧栏」（rail 是个人的，不碰作者的东西）",
                  "Add to the left rail" in menu, menu)
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            check("Escape 关掉菜单", page.evaluate(
                "() => document.getElementById('rpt-menu').hidden"))

            # ── the status filter only applies where it can ──────────────────
            # ⚠️ Each click waits for the **tab to become active**, never for the
            # dropdown state that is about to be asserted. A `wait_for_function` on the
            # asserted condition makes the assertion unreachable: it can only ever pass,
            # and a real regression shows up as a 30s timeout with no named failure
            # instead of a line that says which invariant broke. Measured: dropping the
            # `_scope === 'mine'` half of `renderFilterVisibility` hung this test here.
            def switch_to(scope):
                page.click("#rpt-tab-" + scope)
                page.wait_for_function(
                    "(s) => document.getElementById('rpt-tab-' + s).classList.contains('on')",
                    arg=scope)
                # ⚠️ Wait for the page to have finished switching — **not** for rows.
                # Measured, not assumed: flat 'mine' lands on the PROJECT wall, and it
                # is the only scope where `#wr-list` is legitimately empty (0 children)
                # while `#wr-wall` shows the project cards. 'public' shows the empty
                # state, 'shared' shows rows. Waiting on `.wr-row` therefore times out on
                # the very scope this file is named after.
                page.wait_for_function("""() => {
                  const W = document.getElementById('wr-wall');
                  const L = document.getElementById('wr-list');
                  return (!W || !W.hidden) || (L && L.children.length > 0);
                }""", timeout=20000)

            def status_hidden():
                return page.evaluate("() => document.getElementById('rpt-status').hidden")

            switch_to("mine")
            # The fact that trips people up, pinned so the next reader does not have to
            # rediscover it: 'mine' is the only scope that shows PROJECTS, so the
            # report rows are one click deeper. Every other scope lists rows directly.
            check("My workspace 落在项目墙上（报告行要打开项目才有）",
                  page.evaluate("""() => {
                    const W = document.getElementById('wr-wall');
                    return !!W && !W.hidden && W.children.length > 0
                        && document.getElementById('wr-list').children.length === 0;
                  }"""),
                  page.evaluate("""() => ({
                    wall: document.getElementById('wr-wall').children.length,
                    list: document.getElementById('wr-list').children.length})"""))
            check("My workspace 显示状态筛选（这里有 published + draft 可筛）", not status_hidden())
            switch_to("public")
            check("Public area 隐藏状态筛选（只有 published，筛了等于没筛）", status_hidden())
            switch_to("shared")
            check("Shared with me 也隐藏状态筛选", status_hidden())
            # ⚠️ The load-bearing half of that check. 'shared' now holds a published
            # report AND a draft, so `_statusKinds` is 2 — the dropdown would have
            # something to narrow TO, and `listQuery()` still only sends the status in
            # 'mine'. A filter that appears where its parameter is never sent is a
            # control that cannot change anything, so the scope guard must hold.
            check("（前提）Shared with me 里确实有草稿，_statusKinds > 1 成立", page.evaluate(
                "() => document.querySelectorAll('#wr-list .wr-row[data-slug]').length") >= 2)
            check("Shared with me 有草稿也仍然不给状态筛选（非 mine 不发 status）", status_hidden())

            # ── the empty state explains how a report gets here ───────────────
            # ⚠️ These were one mushy assertion that passed if EITHER the empty copy
            # was right OR a title was visible, so it could not tell "the empty state
            # explains itself" from "there happened to be a row". They are two
            # different claims about two different scopes; now they are two checks.
            page.click("#rpt-tab-public")
            page.wait_for_function("() => document.querySelector('#wr-list .rpt-empty') !== null")
            public_empty = page.inner_text("#wr-list .rpt-empty")
            check("Public area 空状态说明报告怎么到这儿来",
                  "Nothing has been shared yet" in public_empty
                  or "Publish a report" in public_empty, public_empty[:90])
            page.click("#rpt-tab-shared")
            page.wait_for_function("() => document.querySelector('#wr-list .wr-row[data-slug]') !== null")
            check("Shared with me 有内容时不再显示空状态",
                  page.evaluate("() => !document.querySelector('#wr-list .rpt-empty')"))
            check("Shared with me 的行是那一篇同事的",
                  "同事分享的报告" in page.inner_text("#wr-list"),
                  page.inner_text("#wr-list")[:60])

            check("整页没有 JS 报错", errors == [], errors[:3])

            # ── the two pages must look like the same control ────────────────
            # They are the same kind of area switcher, so a difference in styling
            # reads as two different widgets. It has drifted once already: the
            # Workspace tabs sat inside a grey pill container while the Knowledge
            # tabs were flat, which is exactly what the user spotted.
            def tab_styles(page_obj, bar_selector):
                # Measure the ACTIVE tab and the container of the bar, whatever the
                # currently selected area is — measuring a fixed tab id compares an
                # active tab on one page with an inactive one on the other.
                return page_obj.evaluate("""(sel) => {
                  const bar = document.querySelector(sel);
                  const active = bar.querySelector('.rpt-tab.on');
                  const idle = bar.querySelector('.rpt-tab:not(.on)');
                  const cs = getComputedStyle(active);
                  const ics = getComputedStyle(idle);
                  const ccs = getComputedStyle(bar);
                  return {
                    container_bg: ccs.backgroundColor,
                    container_border: ccs.borderTopWidth + ' ' + ccs.borderTopColor,
                    container_padding: ccs.padding,
                    container_radius: ccs.borderRadius,
                    font_size: cs.fontSize, font_weight: cs.fontWeight,
                    padding: cs.padding, radius: cs.borderRadius,
                    active_color: cs.color, active_bg: cs.backgroundColor,
                    active_shadow: cs.boxShadow,
                    idle_color: ics.color,
                  };
                }""", bar_selector)

            page.evaluate("() => { navTo(null, 'reports', 'Workspace'); reportsPage.switchScope('mine'); }")
            page.wait_for_selector("#rpt-tab-mine.on", state="attached")
            page.wait_for_timeout(80)
            report_tab = tab_styles(page, "#page-reports .rpt-tabs")
            page.evaluate("() => { navTo(null, 'knowledge', 'Knowledge base'); knowledgePage.switchScope('mine'); }")
            page.wait_for_selector("#kb-tab-mine.on", state="attached")
            page.wait_for_timeout(80)
            knowledge_tab = tab_styles(page, "#page-knowledge .rpt-tabs")

            differing = {k: (report_tab[k], knowledge_tab[k])
                         for k in report_tab if report_tab[k] != knowledge_tab[k]}
            check("两个页面的 area 标签样式完全一致（容器 + 选中态 + 未选中态）",
                  differing == {}, differing)
            check("标签条不是分段控件（容器没有底色/边框/内边距）",
                  report_tab["container_bg"] in ("rgba(0, 0, 0, 0)", "transparent")
                  and report_tab["container_border"].startswith("0")
                  and report_tab["container_padding"] == "0px", report_tab)
            check("选中态仍然看得出来（选中与未选中颜色不同）",
                  report_tab["active_color"] != report_tab["idle_color"],
                  {"active": report_tab["active_color"], "idle": report_tab["idle_color"]})

            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())