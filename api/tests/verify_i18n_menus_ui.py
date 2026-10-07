"""Every row names its file, and no menu mixes two languages.

    docker compose exec -T app sh -c 'cd /app/api/tests && python verify_i18n_menus_ui.py'

Two invariants, both measured on the SCREEN in BOTH languages, and both
deliberately written as rules rather than as a list of expected strings:

* **zh mode: every menu label contains a CJK character.** The bug was not that a
  label was wrong, it was that `Export to PPT` and `Publish to public` had no
  dictionary entry while their neighbours did — so one menu read 打开 / 导出为 PPT /
  Publish to public / 删除. A list of correct labels would pass the moment somebody
  adds a new one badly; this rule fails on the FIRST single-language label.
* **en mode: no menu label contains a CJK character.** The mirror, because the fix
  for the first half is trivially "hardcode Chinese".

A pair is `中文 / English` and `i18n.js::pickPair` splits on `' / '` with the CJK on
the LEFT. Both halves are asserted: a label that splits into an empty side, or one
that still shows both, is worse than an untranslated one — the reader cannot tell
which part is for them.

Why not a static check: the same label is built in five different places and
rendered through two different mechanisms (`i18n.js`'s MutationObserver for the
workspace/knowledge/calendar menus, a manual `data-lang-*` split for the dashboard
menu). Only opening the menus says whether the label that reaches the reader is the
label in the source.
"""
import os
import sys
from playwright.sync_api import sync_playwright

# ⚠️ `KLADO_BASE`, like `verify_knowledge_projects_ui.py` — a hard-coded 8000 made
# this guard untestable against any other instance, which is exactly the instance
# you want when the change under test is a backend one: the frontend is read from
# disk on every request, but the ROUTES are whatever that process booted with.
BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
passed, failed, skipped = [], [], []


def ok(m):
    passed.append(m); print("  ✓", m)


def bad(m):
    failed.append(m); print("  ✗", m)


def gap(where, why):
    skipped.append(where); print(f"  ! {where} 没测到：{why}")


# ⚠️ `innerText`, NEVER `textContent`. The two menu families localize by DIFFERENT
# mechanisms and only one of them leaves a single language in the DOM:
#   · the dashboard menu emits BOTH sides and hides one with
#     `html[data-lang] [data-lang-*] { display: none }` — `textContent` reads
#     "分享给同事Share with colleagues" and every assertion in this file would call that
#     a mixed-language bug, when in fact one of the two is `display: none`;
#   · the workspace / knowledge / calendar menus let the i18n walker REWRITE the text
#     node, so only one side is ever in the DOM.
# `innerText` is what approximates the rendered result for both. The first version of
# this file used `textContent` and reported a defect that did not exist.
LABELS = r"""() => {
  const out = [];
  document.querySelectorAll('#rpt-menu button, #dsh-menu button, #kb-ctx button,'
    + ' #cal-ctx button, .cal-ctx button, #rail-ctx button')
    .forEach((b) => { if (b.getClientRects().length) out.push(b.innerText.replace(/\s+/g, ' ').trim()); });
  return out;
}"""

# ⚠️ Collapse the whitespace: the Inbox tab's `innerText` is "Inbox\n36" (the badge is a
# sibling span), so joining the raw values puts a NEWLINE in the middle of the summary
# line — and any grep/filter on the output then drops everything after it, which reads
# exactly like "only two tabs exist". That happened to me while reading this file's own
# first green run. A guard whose output a newline can eat is a guard you will misread.
TABS = """() => [...document.querySelectorAll('#nav-center [data-nav-key]')]
  .filter((e) => !e.hidden).map((e) => e.innerText.replace(/\\s+/g, ' ').trim())"""

# ⚠️ The Workspace list is a column of `.wr-row` inside `#wr-list` since 2026-10-03,
# and the card grid is gone from this page. The four fields are the same four the card
# printed, so the rules below (a filename on every row, a single-language badge) are
# unchanged — only the elements that carry them moved. `docType` is read from the type
# ICON, which is where a row records it; the card used a `rpt-doc-*` class instead.
CARDS = """() => [...document.querySelectorAll('#wr-list .wr-row[data-slug]')].map((r) => ({
  title: (r.querySelector('.wr-row-t') || {}).innerText || '',
  chip: (r.querySelector('.wr-row-b') || {}).innerText || '',
  file: (r.querySelector('.wr-row-file span') || {}).innerText || '',
  size: (r.querySelector('.wr-row-file em') || {}).innerText || '',
  docType: ((((r.querySelector('.wr-row-icon') || {}).className || '')
    .match(/ri-file-(ppt|excel|pdf|word)-2/) || [])[1]) || '',
}))"""

# The project cards on the wall, in the order the server sends them (the unfiled one
# first). Each one is opened in turn — there is no "show me everything" view, which is
# the point of the two-level shape.
PROJECTS = """() => [...document.querySelectorAll('#wr-wall .wr-proj[data-proj]')]
  .map((c) => c.dataset.proj)"""

# A document row is the one whose badge names a file format; that is exactly the test
# the old `.rpt-chip` was given, and the badge is still the only place that says so.
MATCH_SLUG = """(wantDoc) => {
  const rows = [...document.querySelectorAll('#wr-list .wr-row[data-slug]')];
  const isDoc = (r) => /XLSX|PDF|DOCX|PPTX/.test(
    ((r.querySelector('.wr-row-b') || {}).textContent) || '');
  const hit = rows.find((r) => isDoc(r) === wantDoc);
  return hit ? hit.dataset.slug : '';
}"""


CJK = lambda s: any("一" <= c <= "鿿" for c in s)
# A label that is still BOTH languages at once: the failure mode a one-sided fix
# produces when only half the pair is written.
BOTH = lambda s: CJK(s) and any(w in s for w in
                                ("Open", "Export", "Download", "Delete", "Rename",
                                 "Publish", "Copy", "Share", "Filters", "Dynamic",
                                 "Interactive", "Static", "Archived", "Pull"))


def check_labels(page, where, want_cjk):
    labels = page.evaluate(LABELS)
    if not labels:
        gap(where, "菜单一条都没打开（该入口没渲染，或选择器变了）")
        return
    # A label that reads back EMPTY must fail. `CJK('') is False`, so an empty label
    # satisfies "contains no CJK" in English mode and gets filtered out of the wrong
    # list in Chinese mode — i.e. an entry that stopped rendering entirely would turn
    # this whole check green.
    empty = [i for i, l in enumerate(labels) if not l]
    if empty:
        bad(f"{where}: 第 {empty} 条标签读出来是空串（空串会被上面两条判据一起放过）")
    wrong = [l for l in labels if (CJK(l) is not want_cjk)]
    boths = [l for l in labels if BOTH(l)]
    if boths:
        bad(f"{where}: 中英同时出现（读者分不出哪半是自己的）：{boths}")
    elif wrong:
        side = "中文" if want_cjk else "英文"
        bad(f"{where}: 这些标签没有{side}侧 → {wrong}")
    else:
        ok(f"{where}: {len(labels)} 条标签全部单语（{'中文' if want_cjk else '英文'}）")


def open_row_menu(page, slug):
    """Right-click one row. ⚠️ The 「⋯」button (`#rpt-menu` opener of the card era) is
    gone; the same `#rpt-menu` now opens from the row's own `contextmenu`, which is
    what the listener on `#wr-list` is bound to."""
    page.evaluate("""(slug) => {
      const row = document.querySelector('#wr-list .wr-row[data-slug="' + slug + '"]');
      row.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 500, clientY: 500}));
    }""", slug)
    page.wait_for_timeout(700)


def walk_workspace(page, on_listing):
    """Visit every listing the Workspace can show, in the order it shows them.

    ⚠️ 「我的工作台」opens on the WALL OF PROJECTS and `#wr-list` is empty until one is
    opened, so "look at the list" is a walk, not a read. Public / Shared are a FLAT
    list — a borrowed report is filed in its OWNER's project, a wall of which this
    account cannot open — so there the rows are read where they already are, and the
    wall is not on screen to walk. `on_listing(slug)` runs right after each one renders.
    """
    for scope in [None] + ["public", "shared"]:
        if scope is None:
            for slug in page.evaluate(PROJECTS):
                page.evaluate("(s) => window.reportsPage.openProject(s)", slug)
                page.wait_for_timeout(1500)
                on_listing(slug)
            continue
        page.evaluate("(s) => window.reportsPage.switchScope(s)", scope)
        page.wait_for_timeout(1500)
        on_listing(scope)


def open_report_menu(page, doc):
    page.evaluate("navTo(null,'reports','Workspace')")
    page.wait_for_timeout(2500)
    found = []

    def visit(_where):
        slug = page.evaluate(MATCH_SLUG, doc)
        if slug and not found:
            found.append(slug)

    walk_workspace(page, visit)
    if not found:
        return False
    # ⚠️ The 「⋯」button that opened this menu on the card is gone with the card wall; the
    # same `#rpt-menu` opens from the row's own `contextmenu`, which is what the
    # delegated listener on `#wr-list` is bound to. A synthetic right-click, the same
    # one this file already sends to the knowledge and calendar lists.
    open_row_menu(page, found[0])
    return True


def collect_rows(page):
    """Every row the Workspace can list, in one flat list.

    ⚠️ Reading `#wr-list` while the wall is up finds NOTHING, and an empty result would
    make "every row names its file" pass vacuously — hence the walk.
    """
    out = []
    walk_workspace(page, lambda _where: out.extend(page.evaluate(CARDS)))
    return out


with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-sandbox"])
    page = br.new_context(viewport={"width": 1600, "height": 1000}).new_page()

    for lang, want_cjk in (("zh", True), ("en", False)):
        side = "中文" if want_cjk else "英文"
        print(f"\n══════ {side}模式 ══════")
        page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
        page.evaluate(f"() => localStorage.setItem('klado-lang','{lang}')")
        page.reload(wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        # ── the nav bar's module tabs ──
        tabs = page.evaluate(TABS)
        bad_tabs = [t for t in tabs if CJK(t) is not want_cjk or BOTH(t)]
        if not tabs:
            bad("顶栏一个标签都没有")
        elif bad_tabs:
            bad(f"顶栏标签没有{side}侧 / 中英混排：{bad_tabs}（全部：{tabs}）")
        else:
            ok(f"顶栏 {len(tabs)} 个标签全部{side}：" + " · ".join(tabs))

        # ── the four context menus that carry labels ──
        if open_report_menu(page, doc=True):
            check_labels(page, "工作台 · 文档行右键菜单", want_cjk)
            page.keyboard.press("Escape"); page.wait_for_timeout(400)
        else:
            gap("工作台 · 文档行右键菜单", "没找到文档行（我的/公开/共享三处都没有）")

        if open_report_menu(page, doc=False):
            check_labels(page, "工作台 · 报告行右键菜单", want_cjk)
            page.keyboard.press("Escape"); page.wait_for_timeout(400)
        else:
            gap("工作台 · 报告行右键菜单", "没找到报告行（我的/公开/共享三处都没有）")

        page.evaluate("document.getElementById('nav-tab-dashboard').click()")
        page.wait_for_timeout(2500)
        if page.evaluate("document.querySelectorAll('#page-dashboard .rpt-card').length"):
            # ⚠️ A `contextmenu` event on the card. Right-click is now the only way this
            # menu opens (the ⋮ button is gone), so dispatching a right-click is not
            # just an alternative to a click — a click would open the VIEWER instead and
            # the surface would report "no labels", a gap that looks like a pass.
            page.evaluate("""() => {
              const card = document.querySelector('#page-dashboard .rpt-card');
              const r = card.getBoundingClientRect();
              card.dispatchEvent(new MouseEvent('contextmenu',
                {bubbles: true, cancelable: true, clientX: r.left + 40, clientY: r.top + 40}));
            }""")
            page.wait_for_timeout(800)
            check_labels(page, "仪表盘卡片菜单", want_cjk)
            page.keyboard.press("Escape"); page.wait_for_timeout(400)
        else:
            gap("仪表盘卡片菜单", "仪表盘没有卡片")

        page.evaluate("navTo(null,'knowledge','Knowledge base')")
        page.wait_for_timeout(2500)
        if page.evaluate("""() => {
          const b = document.querySelector('#kb-toc-list .kb-link');
          if (!b) return false;
          b.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 400, clientY: 400}));
          return true;
        }"""):
            page.wait_for_timeout(700)
            check_labels(page, "知识库目录右键菜单", want_cjk)
            page.keyboard.press("Escape"); page.wait_for_timeout(400)
        else:
            gap("知识库目录右键菜单", "目录里没有条目")

        page.evaluate("navTo(null,'calendar','Calendar')")
        page.wait_for_timeout(3000)
        if page.evaluate("""() => {
          const b = document.querySelector('.cal-bar');
          if (!b) return false;
          b.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 400, clientY: 400}));
          return true;
        }"""):
            page.wait_for_timeout(700)
            check_labels(page, "日历事件右键菜单", want_cjk)
            page.keyboard.press("Escape"); page.wait_for_timeout(400)
        else:
            gap("日历事件右键菜单", "日历上没有事件条")

    # ── every row names its file (language-independent) ──
    print("\n══════ 行文件名 ══════")
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)
    # ⚠️ `collect_rows`, not one read: 「我的工作台」is a wall of projects and a single
    # read of `#wr-list` on it returns nothing at all (an empty list would turn every
    # rule below into a no-op that reports success).
    cards = collect_rows(page)
    if not cards:
        bad("工作台一行都没有，这条判据没有意义")
    for c in cards:
        if not c["file"]:
            bad(f"「{c['title']}」这一行没有文件名（chip={c['chip']!r}）")
    named = [c for c in cards if c["file"]]
    if named and all(c["file"] for c in cards):
        ok(f"{len(cards)} 行全部带文件名")
        for c in cards:
            print(f"      {c['file']:46s} {c['size']:>9s}   chip={c['chip']}")

    # A report row's name must be the name its download actually produces.
    # `reports.py:1826` sends `<slug>.html`; the check is that the extension is there
    # and the base matches the row's own route, not that it pretty.
    api = page.evaluate("""async () => {
      const r = await fetch('api/reports?scope=mine', {credentials: 'same-origin'});
      const d = await r.json();
      return (d.reports || d).map((x) => ({slug: x.slug, doc_name: x.doc_name, doc_type: x.doc_type}));
    }""")
    byslug = {c["file"]: c for c in cards}
    for row in api:
        want = row["doc_name"] or (row["slug"] + ".html")
        if want not in byslug:
            bad(f"{row['slug']}：行上显示的是 {byslug.get(want) or '（找不到）'!r}，应是 {want!r}")
    ok(f"{len(api)} 份内容（{sum(1 for r in api if r['doc_type'])} 份文档 + "
       f"{sum(1 for r in api if not r['doc_type'])} 份报告）的文件名与后端一致")

    # ── the kind chips ──
    bad_chip = [c for c in cards if c["chip"] and BOTH(c["chip"])]
    if bad_chip:
        bad(f"类型徽章中英混排：{[c['chip'] for c in bad_chip]}")
    else:
        ok("类型徽章没有中英混排：" + " · ".join(sorted({c["chip"] for c in cards if c["chip"]})))

    br.close()

print()
print(f"通过 {len(passed)} / 失败 {len(failed)} / 未测到 {len(skipped)}")
if skipped:
    print("未测到的入口（不算通过）：")
    for s in skipped:
        print("  -", s)
for f in failed:
    print("  FAIL:", f)
sys.exit(1 if failed else 0)
