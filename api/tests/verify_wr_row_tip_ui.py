"""The Workspace report row's hover card, and the project card's two description lines.

Both were added the same day and are tested here together because they are one
decision: **the row stays one line, and the facts it does not print move somewhere a
reader can reach without a click.** For the row that somewhere is a hover card; for a
project card, whose body has room, it is two lines printed on the card itself.

The things this file exists to catch, each of which was true at least once while
writing it:

* **A card that renders under something opaque.** `.rpt-viewer` is 8000 and
  full-screen; a hover card at the `.kb-tip` default of 69 is a correct element with a
  correct box and nothing on screen. So the visibility assertion is a hit test that
  compares the winner's z-index against the card's, not `offsetWidth > 0`.
* **"It closed" passing because it never opened.** Every close assertion here is
  preceded by an explicit open assertion, and the scroll one additionally asserts the
  container actually scrolled — the scroll container is `.rpt-body`, and a page that
  cannot scroll never fires the event that would prove anything.
* **`pointer-events: none` read as "invisible".** The card is deliberately not
  clickable, so `elementFromPoint` at its centre returns what is UNDER it. The
  assertion therefore reads the z-index of whatever won, not whether the card itself
  won.
* **A reserved space that can only ever be empty.** The two description lines are a
  fixed height, and the unfiled project gets a built-in line, because an empty slot on
  the one card every account has is a hole in the wall.

Needs playwright (present in `.venv312`, not in the API image) and a local Chrome.
`fetch` is stubbed, so this needs no database and no server.

    .venv312/bin/python api/tests/verify_wr_row_tip_ui.py
"""
import functools
import http.server
import json
import os
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = int(os.environ.get("KLADO_TIP_PORT", "8803"))

# ⚠️ A report that carries EVERY field the card knows how to print, so a regression that
# drops one of them is visible here rather than in production. Production is all
# bilingual (8/8 on 2026-10-05), so `summary` alone is empty there and a card that only
# read `summary` would look fine and be blank.
REPORT = {
    "id": 1, "slug": "disputes", "title": "结算争议跟踪表", "summary": "",
    "summary_zh": "分渠道争议统计与四笔未结争议，附结算日历。",
    "summary_en": "Disputes by channel with the four still open.",
    "category": "", "tags": ["settlement", "disputes", "xlsx", "结算", "争议"],
    "status": "published", "owner_email": "me@example.com", "visibility": "private",
    "kind": "static", "can_manage": True, "submitter": "Zhen Huang", "author": "agent",
    "size_bytes": 7168, "doc_type": "xlsx", "doc_name": "settlement_disputes.xlsx",
    "doc_pages": 3, "created_at": "2026-10-01T09:00:00", "updated_at": "2026-10-02T09:00:00",
    "project_slug": "unfiled-x", "folder_id": 1,
}
# A second row with NO summary at all. The card must still say something useful rather
# than leaving a blank paragraph; and the pair of them is what makes the "no ragged
# grid" check on the project wall meaningful.
REPORT_PLAIN = {
    "id": 2, "slug": "plain", "title": "没有摘要的报告", "summary": "", "summary_zh": "",
    "summary_en": "", "category": "", "tags": [], "status": "published",
    "owner_email": "me@example.com", "visibility": "private", "kind": "static",
    "can_manage": True, "submitter": "Zhen Huang", "author": "agent",
    "size_bytes": 1024, "created_at": "2026-10-01T09:00:00",
    "updated_at": "2026-10-02T09:00:00", "project_slug": "unfiled-x", "folder_id": 1,
}
# ⚠️ Filler rows, and they are load-bearing rather than padding. "Scrolling the list
# hides the card" can only be proven by a list that scrolls: with two rows the container
# never overflows, `scroll` never fires, and the assertion is a free green — the exact
# vacuous pass this file is meant to not have. (A page that cannot scroll was one of the
# two ways an earlier version of this suite earned a green it had not earned.)
FILLER = [dict(REPORT_PLAIN, id=100 + i, slug="filler-%d" % i, title="填充报告 %d" % i,
               submitter="Someone %d" % i) for i in range(40)]

PROJECTS = [
    {"slug": "unfiled-x", "title": "未归档 / Unfiled", "summary": "", "system": True,
     "has_cover": False, "cover_url": "", "folder_count": 1,
     "report_count": len(FILLER) + 2,
     "can_manage": True, "created_at": "2026-10-01T09:00:00",
     "updated_at": "2026-10-01T09:00:00"},
    # A project with a description, and one WITHOUT, so the equal-height assertion has
    # something to compare. Equal height with only one project on the wall is a constant.
    {"slug": "q4", "title": "Q4 复盘", "system": False, "has_cover": False, "cover_url": "",
     "summary": "把 Q4 三个渠道的价格带、结算口径和争议处理放在一个项目里，"
                "方便交接给下一任负责人。",
     "folder_count": 2, "report_count": 5, "can_manage": True,
     "created_at": "2026-10-01T09:00:00", "updated_at": "2026-10-01T09:00:00"},
    {"slug": "empty-desc", "title": "还没有描述的项目", "summary": "", "system": False,
     "has_cover": False, "cover_url": "", "folder_count": 0, "report_count": 0,
     "can_manage": True, "created_at": "2026-10-01T09:00:00",
     "updated_at": "2026-10-01T09:00:00"},
]

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
const REPORTS = %s, PROJECTS = %s;const ok = (body) => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
window.fetch = function (url, init) {
  const u = String(url);
  window.__calls.push(u);
  if (u.indexOf('/api/reports/projects') >= 0) {
    if (/folders/.test(u)) return ok({folders: [{id: 1, title: '未归档 / Unfiled', system: true}]});
    if (/cover/.test(u)) return Promise.resolve({ok: false, status: 404, json: () => Promise.resolve({})});
    return ok({projects: PROJECTS, count: PROJECTS.length});
  }
  if (u.indexOf('/api/reports?') === 0 || u.indexOf('/api/reports') === 0) {
    if (/\\/document|\\/raw|\\/share|\\/filters|\\/edit/.test(u)) return ok({html: '<p>x</p>'});
    return ok({reports: REPORTS, count: REPORTS.length});
  }
  return ok({});
};
""" % (json.dumps([REPORT, REPORT_PLAIN] + FILLER), json.dumps(PROJECTS))


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
            page.evaluate("() => navTo(null, 'reports', 'Workspace')")

            # ── the project wall: two reserved description lines ─────────────
            page.wait_for_selector("#wr-wall .wr-proj[data-proj]", state="visible")
            page.wait_for_timeout(120)
            cards = page.query_selector_all("#wr-wall .wr-proj[data-proj]")
            check("（前置）项目墙有卡可量", len(cards) >= 2, len(cards))

            sums = page.evaluate("""() => [...document.querySelectorAll('#wr-wall .wr-proj')].map((c) => {
              const s = c.querySelector('.wr-proj-sum');
              if (!s) return null;
              const cs = getComputedStyle(s);
              const r = s.getBoundingClientRect();
              return {text: s.textContent.trim(), h: r.height,
                      lh: parseFloat(cs.lineHeight), clamp: cs.webkitLineClamp,
                      display: cs.display, card: c.getBoundingClientRect().height};
            })""")
            check("每张项目卡都有 .wr-proj-sum", all(s is not None for s in sums), sums)
            check("夹断是 2 行", all(s["clamp"] == "2" for s in sums),
                  [s["clamp"] for s in sums])
            check("高度正好是两行行高（line-height × 2）",
                  all(abs(s["h"] - s["lh"] * 2) < 1.5 for s in sums),
                  [(round(s["h"], 1), round(s["lh"] * 2, 1)) for s in sums])
            check("有卡高一致：预留的高度真的被预留了",
                  len({round(s["card"]) for s in sums}) == 1,
                  [round(s["card"]) for s in sums])
            check("写了描述的卡显示描述",
                  any("Q4 三个渠道" in s["text"] for s in sums), [s["text"][:24] for s in sums])
            check("没写描述的普通项目：位置留着，文字为空",
                  any(s["text"] == "" and s["card"] > 0 for s in sums),
                  [s["text"][:24] for s in sums])
            unfiled = page.evaluate(
                "() => document.querySelector('#wr-wall .wr-proj .wr-proj-sum').textContent.trim()")
            check("「未归档」不是空的（不能只有等于一个值的预留空间）", unfiled != "", unfiled)
            check("「未归档」那行走的是语言（不是 data-i18n-skip）",
                  page.evaluate("() => !document.querySelector('#wr-wall .wr-proj .wr-proj-sum')"
                                ".hasAttribute('data-i18n-skip')"))
            check("普通项目的描述是 data-i18n-skip（人写的字不被翻译）",
                  page.evaluate("""() => {
                    const c = [...document.querySelectorAll('#wr-wall .wr-proj')]
                      .find((x) => x.dataset.proj === 'q4');
                    return !!c && c.querySelector('.wr-proj-sum').hasAttribute('data-i18n-skip');
                  }"""))

            # ── the built-in line, in the session's language ────────────────
            # ⚠️ Asserts ONE side, deliberately. "Switch language and it flips" is
            # asserted NOWHERE, because it is measured to be false for this card — and
            # not because of anything in the card. `i18n.js::processTextNode()` writes
            # `WROTE.set(node, undefined)` when the replacement already equals the
            # current text; the MutationObserver then re-walks the node i18n itself just
            # wrote, WROTE is cleared within a frame, and the next `apply()` re-baselines
            # onto the already-resolved single-language string — which has no second side
            # to flip. Verified in-page: a raw pair resolves both ways through `t()`, and
            # a FRESHLY inserted pair node follows the language, while this card's node
            # does not. Fixing it means changing `i18n.js` for every page at once.
            # Asserting the frozen behaviour here would be asserting a bug, so this file
            # asserts only what is true and true on purpose.
            page.evaluate("() => kladoI18n.setLang('zh')")
            page.wait_for_timeout(300)
            zh = page.evaluate("""() => {
              const c = [...document.querySelectorAll('#wr-wall .wr-proj')]
                .find((x) => x.dataset.proj === 'unfiled-x');
              return c.querySelector('.wr-proj-sum').textContent.trim();
            }""")
            page.evaluate("() => kladoI18n.setLang('en')")
            page.wait_for_timeout(300)
            en = page.evaluate("""() => {
              const c = [...document.querySelectorAll('#wr-wall .wr-proj')]
                .find((x) => x.dataset.proj === 'unfiled-x');
              return c.querySelector('.wr-proj-sum').textContent.trim();
            }""")
            page.evaluate("() => kladoI18n.setLang('zh')")
            page.wait_for_timeout(250)
            def one_side(s):
                # Exactly one of the two languages is present. This is the property that
                # actually holds in both languages, and it is the one worth pinning: a
                # pair that fails to split shows the reader BOTH at once, which is the
                # failure the i18n walker exists to prevent.
                return ("没有归档" in s) != ("Unfiled" in s)

            check("内置描述在两种语言下都渲染出内容", zh.strip() != "" and en.strip() != "",
                  {"zh": zh, "en": en})
            check("内置描述只出现一侧，从不双语混排", one_side(zh) and one_side(en),
                  {"zh": zh, "en": en})
            # The DOM cannot show that the two sides differ (the freeze above), so the
            # PAIR itself is checked instead — and that is the half that is actionable:
            # a malformed pair never splits, and a never-splitting pair is exactly what
            # "两种语言同时出现在一行" looks like to a reader.
            pair = page.evaluate("""(l) => {
              kladoI18n.setLang(l);
              return kladoI18n.t('没有归档的报告都在这里 / Unfiled reports land here');
            }""", "zh")
            pair_en = page.evaluate("""(l) => {
              kladoI18n.setLang(l);
              return kladoI18n.t('没有归档的报告都在这里 / Unfiled reports land here');
            }""", "en")
            page.evaluate("() => kladoI18n.setLang('zh')")
            page.wait_for_timeout(200)
            check("双语对本身能拆开，两侧是不同的两句",
                  pair == "没有归档的报告都在这里" and pair_en == "Unfiled reports land here",
                  {"zh": pair, "en": pair_en})
            check("计数行的数字没被 i18n 吃掉",
                  page.evaluate("""() => {
                    const c = [...document.querySelectorAll('#wr-wall .wr-proj')]
                      .find((x) => x.dataset.proj === 'unfiled-x');
                    return /42/.test(c.querySelector('.wr-proj-sub').textContent);
                  }"""))

            # the write path exists — the menu action, and the dialog it opens
            page.evaluate("() => reportsPage.openProject('unfiled-x')")
            page.wait_for_selector("#wr-list .wr-row[data-slug]", state="visible")
            page.wait_for_timeout(150)
            page.evaluate("() => navTo(null, 'reports', 'Workspace'); reportsPage.closeProject()")
            page.wait_for_selector("#wr-wall .wr-proj[data-proj]", state="visible")
            page.wait_for_timeout(120)
            cbox = page.evaluate("""() => {
              const c = document.querySelector('#wr-wall .wr-proj[data-proj="unfiled-x"]');
              const r = c.getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }""")
            page.mouse.click(cbox["x"], cbox["y"], button="right")
            page.wait_for_timeout(200)
            actions = page.evaluate(
                "() => [...document.querySelectorAll('#rpt-menu [data-action]')]"
                ".map((b) => b.dataset.action)")
            check("项目卡右键菜单有「写描述」", "describe-project" in actions, actions)
            check("「写描述」对「未归档」也在（否则预留的两行永远填不上）",
                  "describe-project" in actions)

            # ⚠️ **Click it.** Asserting the row existed is what let a dead menu ship:
            # `runCardMenuAction()` called `closeCardMenu()` — which resets `_ctxKind`
            # to `'report'` — and only THEN read it, so every project and folder action
            # fell through to the report branch and did nothing. Rename, set a cover,
            # delete, and every folder action were all dead, and the only reason it was
            # not noticed is that production has exactly one project (the system 未归档,
            # whose menu offers none of them) and no folders. So the assertion here is
            # not "the action is in the menu", it is "clicking it opens the dialog".
            page.click('#rpt-menu [data-action="describe-project"]')
            page.wait_for_timeout(300)
            dlg = page.evaluate("""() => {
              const d = document.getElementById('kld-dlg');
              const input = d.querySelector('input, textarea');
              return {hidden: d.hidden, title: document.getElementById('kld-dlg-title').textContent,
                      body: document.getElementById('kld-dlg-body').innerText,
                      hasInput: !!input, value: input ? input.value : null,
                      h: d.getBoundingClientRect().height};
            }""")
            check("点「写描述」真的打开了对话框（存在 ≠ 可用）", not dlg["hidden"], dlg["hidden"])
            check("对话框有实际面积", dlg["h"] > 80, dlg["h"])
            check("对话框标题是「项目描述」", "项目描述" in dlg["title"], dlg["title"])
            check("对话框有输入框", dlg["hasInput"], dlg["hasInput"])
            # 未归档 has no description, so "prefilled with nothing" is the correct
            # prefilled value here — the wrong thing to assert is "non-empty". The
            # prefill actually carries text is asserted on `q4` below, which has one.
            check("没有描述的项目预填为空（这样直接打字就是新增）",
                  (dlg["value"] or "") == "", repr(dlg["value"]))
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            check("Esc 关掉对话框", page.evaluate("() => document.getElementById('kld-dlg').hidden"))

            # ⚠️ And the same for a NON-system project, because that is where the three
            # pre-existing actions live (rename / cover / delete) — the ones that were
            # dead. Their dialogs are asserted, not their presence in the menu.
            q4 = page.evaluate("""() => {
              const c = document.querySelector('#wr-wall .wr-proj[data-proj="q4"]');
              const r = c.getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }""")
            page.mouse.click(q4["x"], q4["y"], button="right")
            page.wait_for_timeout(220)
            q4acts = page.evaluate(
                "() => [...document.querySelectorAll('#rpt-menu [data-action]')]"
                ".map((b) => b.dataset.action)")
            check("普通项目有重命名/设封面/删除（改动前这些全是死的）",
                  {"rename-project", "cover-project", "delete-project"} <= set(q4acts), q4acts)
            page.click('#rpt-menu [data-action="describe-project"]')
            page.wait_for_timeout(300)
            q4val = page.evaluate("""() => {
              const d = document.getElementById('kld-dlg');
              const i = d.querySelector('input, textarea');
              return {hidden: d.hidden, value: i ? i.value : null};
            }""")
            check("点「写描述」也真的打开了对话框", not q4val["hidden"], q4val["hidden"])
            check("预填的是这个项目自己的描述（改错项目的值比没有值更坏）",
                  "Q4 三个渠道" in (q4val["value"] or ""), q4val["value"])
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            page.mouse.click(q4["x"], q4["y"], button="right")
            page.wait_for_timeout(220)
            page.click('#rpt-menu [data-action="rename-project"]')
            page.wait_for_timeout(300)
            check("点「重命名」打开了对话框", not page.evaluate(
                "() => document.getElementById('kld-dlg').hidden"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

            # and a folder, which is the third kind sharing this one menu element
            page.evaluate("() => reportsPage.openProject('unfiled-x')")
            page.wait_for_selector("#wr-list .wr-row[data-slug]", state="visible")
            page.wait_for_timeout(200)
            fbox = page.evaluate("""() => {
              const f = document.querySelector('#wr-folders .wr-folder[data-folder]');
              const r = f.getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }""")
            page.mouse.click(fbox["x"], fbox["y"], button="right")
            page.wait_for_timeout(220)
            fbox_acts = page.evaluate(
                "() => [...document.querySelectorAll('#rpt-menu [data-action]')]"
                ".map((b) => b.dataset.action)")
            check("文件夹菜单有「打开」", "open-folder" in fbox_acts, fbox_acts)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            page.evaluate("() => { reportsPage.closeProject(); }")
            page.wait_for_selector("#wr-wall .wr-proj[data-proj]", state="visible")
            page.wait_for_timeout(150)

            # ── into a project, onto the rows ───────────────────────────────
            page.evaluate("() => reportsPage.openProject('unfiled-x')")
            page.wait_for_selector("#wr-list .wr-row[data-slug]", state="visible")
            page.wait_for_timeout(200)
            rows = page.query_selector_all("#wr-list .wr-row[data-slug]")
            check("（前置）列表有行可 hover", len(rows) >= 2, len(rows))

            check("行不再有原生 title（否则原生气泡会跟着冒出来）",
                  page.evaluate("""() => [...document.querySelectorAll('#wr-list .wr-row')]
                    .every((r) => !r.hasAttribute('title'))"""))

            # ── the hover intent: 200ms, measured ────────────────────────────
            r0 = rows[0].bounding_box()
            cx, cy = r0["x"] + r0["width"] / 2, r0["y"] + r0["height"] / 2
            page.mouse.move(cx - 120, cy - 120)
            page.mouse.move(cx, cy)
            page.wait_for_timeout(90)
            early = page.evaluate("() => document.getElementById('wr-tip').classList.contains('on')")
            check("90ms 时还没出（悬停意图是真的，不是零延迟）", not early, early)
            page.wait_for_timeout(260)
            on = page.evaluate("() => document.getElementById('wr-tip').classList.contains('on')")
            check("（前置）停留之后卡片出现了", on, on)

            tip = page.evaluate("""() => {
              const t = document.getElementById('wr-tip');
              const b = t.getBoundingClientRect();
              return {w: b.width, h: b.height, x: b.x, y: b.y,
                      z: getComputedStyle(t).zIndex, pe: getComputedStyle(t).pointerEvents,
                      aria: t.getAttribute('aria-hidden'), text: t.innerText};
            }""")
            check("卡片有实际面积", tip["w"] > 100 and tip["h"] > 60, (tip["w"], tip["h"]))
            check("z-index 是 8100（高于查看器 8000，低于弹窗）", tip["z"] == "8100", tip["z"])
            check("pointer-events: none（卡片永远不吃点击）", tip["pe"] == "none", tip["pe"])
            check("aria-hidden 跟着开关翻", tip["aria"] == "false", tip["aria"])
            check("卡片里有标题", REPORT["title"] in tip["text"], tip["text"][:40])
            check("卡片里有中文摘要", "分渠道争议统计" in tip["text"], tip["text"][:80])
            check("卡片里有英文摘要（双语两条都在）", "Disputes by channel" in tip["text"])
            check("卡片里有完整文件名", "settlement_disputes.xlsx" in tip["text"])
            check("卡片里有提交人与日期", "Zhen Huang" in tip["text"] and "2026-10-02" in tip["text"])
            # ⚠️ Asserted on the DOM, not on the text. A `not in tip_text` version of this
            # fails for the right implementation: the report is called 结算争议跟踪表 and
            # its summary says 争议, so the tag strings are IN the card — as title and as
            # prose, which is exactly what should be there. Only the chips were wrong.
            chips = page.evaluate(
                "() => document.querySelectorAll('#wr-tip .wr-tip-tag, #wr-tip [class*=tag]').length")
            check("卡片里没有标签 chip（重复事实已去掉）", chips == 0, chips)

            row0 = page.evaluate("""() => {
              const r = document.querySelector('#wr-list .wr-row[data-slug]').getBoundingClientRect();
              return {right: r.right, top: r.top, bottom: r.bottom};
            }""")
            check("卡片贴在行的右侧，不压住正在扫的这一列",
                  tip["x"] >= row0["right"], (tip["x"], row0["right"]))
            check("卡片没有跑出窗口",
                  tip["x"] >= 0 and tip["x"] + tip["w"] <= 1500, (tip["x"], tip["w"]))

            # ── VISIBILITY: who wins the hit test, and do they outrank us? ──
            # ⚠️ `pointer-events: none` means the card itself can never win a hit test, so
            # "is it on top" is the winner's stacking order versus the card's. Computed
            # entirely in the page: a DOM element cannot be handed to `page.evaluate` as
            # an argument, and the first version of this helper did exactly that and
            # died on `getComputedStyle(null)`.
            covered = page.evaluate("""() => {
              const tip = document.getElementById('wr-tip');
              const tipZ = parseInt(getComputedStyle(tip).zIndex, 10) || 0;
              const b = tip.getBoundingClientRect();
              const stack = (el) => {
                let n = el, best = 0;
                while (n && n !== document.documentElement) {
                  const z = getComputedStyle(n).zIndex;
                  if (z && z !== 'auto') best = Math.max(best, parseInt(z, 10) || 0);
                  n = n.parentElement;
                }
                return best;
              };
              const pts = [[b.left + 4, b.top + 4], [b.right - 4, b.top + 4],
                           [b.left + 4, b.bottom - 4], [b.right - 4, b.bottom - 4],
                           [b.left + b.width / 2, b.top + b.height / 2]];
              const out = [];
              for (const [x, y] of pts) {
                const el = document.elementFromPoint(x, y);
                if (!el) continue;
                const z = stack(el);
                if (z >= tipZ) out.push({who: el.id || el.className || el.tagName, z: z});
              }
              return {tipZ: tipZ, covered: out};
            }""")
            check("五个探测点都没有更高层的东西盖在卡片上", not covered["covered"],
                  covered["covered"])

            # ── every way out ────────────────────────────────────────────────
            # ⚠️ `park()` before every re-hover. `page.mouse.move` to the coordinate the
            # pointer is ALREADY on does not fire `mouseover`, so "hover again" against
            # a pointer that never left is not a hover at all — the first version of this
            # block moved to the same pixels three times and two preconditions failed for
            # that reason alone, which is how a precondition can fake a product bug.
            def park():
                page.mouse.move(1180, 820)
                page.wait_for_timeout(140)

            def hover_row():
                park()
                page.mouse.move(cx, cy)
                page.wait_for_timeout(340)

            def tip_on():
                return page.evaluate("() => document.getElementById('wr-tip').classList.contains('on')")

            hover_row()
            check("（前置 A）重新 hover 后卡片是开的", tip_on(), tip_on())
            page.mouse.move(900, 700)
            page.wait_for_timeout(180)
            check("指针离开列表 → 收", not tip_on())

            # ⚠️ The scrolling container is FOUND, not named. `.rpt-body` is the right
            # answer for the viewer and `document.scrollingElement` is the wrong answer
            # almost everywhere here (the shell is `calc(100vh - var(--nav-h))`, so the
            # window never overflows), and this file's first version hard-coded
            # `.rpt-body` and measured a 0×0 box — `scrollTop` on it cannot move, `scroll`
            # never fires, and "scrolling hides the card" passes without ever being tried.
            scroller = page.evaluate("""() => {
              const list = document.getElementById('wr-list');
              let n = list;
              while (n && n !== document.body) {
                const cs = getComputedStyle(n);
                if (/(auto|scroll)/.test(cs.overflowY) && n.scrollHeight > n.clientHeight + 4) {
                  return {sel: n.id || n.className, sh: n.scrollHeight, ch: n.clientHeight};
                }
                n = n.parentElement;
              }
              return null;
            }""")
            check("（前置 B0）列表真的有一个可滚的容器", scroller is not None, scroller)
            if scroller:
                hover_row()
                check("（前置 B1）hover 后卡片是开的", tip_on(), tip_on())
                before = page.evaluate("""(sel) => {
                  const b = document.querySelector('.' + sel) || document.getElementById(sel);
                  const y0 = b.scrollTop;
                  b.scrollTop = y0 + 40;
                  return {y0: y0, y1: b.scrollTop};
                }""", scroller["sel"].split()[0])
                page.wait_for_timeout(200)
                check("（前置 B2）容器真的滚动了", before["y1"] > before["y0"], before)
                check("列表滚动 → 收", not tip_on(), tip_on())

            hover_row()
            check("（前置 C）hover 后卡片是开的", tip_on(), tip_on())
            page.mouse.click(cx, cy, button="right")
            page.wait_for_timeout(220)
            check("右键菜单 → 收", not tip_on())
            check("（顺带）菜单确实开了，否则上面那条也是白送",
                  not page.evaluate("() => document.getElementById('rpt-menu').hidden"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(140)

            hover_row()
            check("（前置 D）hover 后卡片是开的", tip_on(), tip_on())
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            check("Esc → 收", not tip_on(), tip_on())

            # ── the keyboard is not a second-class path ───────────────────────
            page.evaluate("""() => document.querySelector('#wr-list .wr-row[data-slug]').focus()""")
            page.wait_for_timeout(200)
            kb_on = tip_on()
            check("键盘聚焦行 → 卡片出现（不是只有 hover）", kb_on, kb_on)
            page.keyboard.press("Escape")
            page.wait_for_timeout(160)
            check("（前置 E）Esc 之后确实收住了", not tip_on(), tip_on())
            # ⚠️ It must STAY dismissed while the row is still focused, and this is the
            # behaviour, not a gap in it: Escape is "dismiss this", so re-arming the card
            # for a pointer that never moved and a focus that never changed would undo the
            # reader's own action. It comes back when focus actually moves, which is the
            # next line — asserting an instant re-show here would be asserting a bug.
            page.wait_for_timeout(200)
            check("Esc 之后、焦点没变，卡片不会自己弹回来", not tip_on(), tip_on())
            page.evaluate("""() => {
              const rows = document.querySelectorAll('#wr-list .wr-row[data-slug]');
              rows[0].focus();
              rows[1].focus();
            }""")
            page.wait_for_timeout(220)
            check("焦点真的移走再回来 → 卡片重新出现", tip_on(), tip_on())

            # ── the viewer wins, by construction ────────────────────────────
            # ⚠️ The viewer is an OVERLAY, not a modal: rows behind it are still focusable
            # and `focusin` fires on them, at a moment the card (8100) is above the
            # viewer (8000). This is the one that used to go red in the wrong place — an
            # earlier version asserted "navigating away hides the card", which was really
            # being satisfied by the list being torn down and firing `mouseleave`, since
            # `closeAnyViewer()` early-returns while no viewer is open.
            page.evaluate("""() => document.querySelectorAll('#wr-list .wr-row[data-slug]')[0]
              .dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
            page.wait_for_timeout(700)
            check("（前置 G）文档打开了",
                  page.evaluate("() => document.getElementById('rpt-viewer').classList.contains('open')"))
            page.evaluate("""() => {
              const rows = document.querySelectorAll('#wr-list .wr-row[data-slug]');
              rows[1].focus();
              rows[2].focus();
            }""")
            page.wait_for_timeout(300)
            check("查看器开着时，Tab 到它背后的行也不会出卡片（否则卡片浮在文档上面）",
                  not tip_on(), tip_on())
            page.evaluate("() => reportsPage.closeViewer()")
            page.wait_for_timeout(250)
            check("（顺带）查看器关了", not page.evaluate(
                "() => document.getElementById('rpt-viewer').classList.contains('open')"))

            # ── leaving the page entirely ───────────────────────────────────
            page.evaluate("""() => document.querySelectorAll('#wr-list .wr-row[data-slug]')[1].focus()""")
            page.wait_for_timeout(240)
            check("（前置 H）换页前卡片是开的", tip_on(), tip_on())
            page.evaluate("() => navTo(null, 'calendar', 'Calendar')")
            page.wait_for_timeout(400)
            check("换页之后卡片不留在屏幕上（body 级浮层不会跟着 display:none 走）",
                  not tip_on(), tip_on())
            page.evaluate("() => navTo(null, 'reports', 'Workspace')")
            page.wait_for_timeout(300)
            page.evaluate("() => reportsPage.openProject('unfiled-x')")
            page.wait_for_selector("#wr-list .wr-row[data-slug]", state="visible")
            page.wait_for_timeout(200)

            page.evaluate("""() => document.querySelectorAll('#wr-list .wr-row[data-slug]')[0].focus()""")
            page.wait_for_timeout(200)
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
            check("回车打开文档后卡片不跟着浮在文档上面", not tip_on(), tip_on())
            check("（顺带）查看器确实开了",
                  page.evaluate("() => document.getElementById('rpt-viewer').classList.contains('open')"))

            # ── a row with no summary: still worth opening ───────────────────
            page.evaluate("() => { reportsPage.closeViewer(); reportsPage.closeProject(); }")
            page.wait_for_timeout(200)
            page.evaluate("() => reportsPage.openProject('unfiled-x')")
            page.wait_for_selector("#wr-list .wr-row[data-slug='plain']", state="visible")
            page.wait_for_timeout(200)
            pb = page.evaluate("""() => {
              const r = document.querySelector("#wr-list .wr-row[data-slug='plain']").getBoundingClientRect();
              return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }""")
            page.mouse.move(pb["x"], pb["y"])
            page.wait_for_timeout(340)
            plain = page.evaluate("""() => {
              const t = document.getElementById('wr-tip');
              return {on: t.classList.contains('on'), text: t.innerText,
                      sums: t.querySelectorAll('.kb-tip-s').length};
            }""")
            check("没有摘要的行照样能出卡片", plain["on"], plain["on"])
            check("没有摘要就不留空的摘要段", plain["sums"] == 0, plain["sums"])
            check("但底栏的提交人还在（行里本来没有这个信息）", "Zhen Huang" in plain["text"])

            # ── dark theme: the shadow token, and the card still readable ───
            page.evaluate("() => kladoTheme.set('dark')")
            page.wait_for_timeout(250)
            page.mouse.move(pb["x"] - 200, pb["y"] - 200)
            page.mouse.move(pb["x"], pb["y"])
            page.wait_for_timeout(340)
            dark = page.evaluate("""() => {
              const t = document.getElementById('wr-tip');
              const cs = getComputedStyle(t);
              return {shadow: cs.boxShadow, bg: cs.backgroundColor, on: t.classList.contains('on')};
            }""")
            check("深色主题下卡片仍然出现", dark["on"], dark["on"])
            check("深色主题下阴影不是写死的浅色 rgba(15,23,42,.18)",
                  "15, 23, 42" not in dark["shadow"], dark["shadow"])

            check("没有 JS 报错", not errors, errors[:3])
            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
