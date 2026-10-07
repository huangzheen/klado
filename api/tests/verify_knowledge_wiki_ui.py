"""Business knowledge base UI — the wiki.

What it pins down (each of these has been got wrong somewhere before):

* the entry sits on the **right** of the Workspace toolbar and actually opens the page;
* **reading never shows markdown syntax** — `##`, `**`, `- ` and fences must be
  rendered, not printed (that is the whole point of the requested WYSIWYG view);
* **editing never shows markdown syntax either** — the editor is a block list, so a
  heading is an `<h2>`-shaped block and bold is bold;
* the source is still regenerated as markdown on save (the page is markdown-first,
  the syntax is only hidden);
* **a page cannot script the reader**: raw HTML in a page is displayed, not executed,
  and `onerror=` on an image does not survive;
* the actions follow the permission tier — the owner gets Edit/Publish, someone
  reading a public page gets "Pull a copy" instead;
* **the page title is printed once** — the header renders it and the body's opening
  `# …` is dropped, so a page cannot say its own name twice;
* the page list is **one line per page**: the enable star (remix `ri-star-fill` gold /
  `ri-star-line` grey — lit = retrieval, dim = out but readable), `YYMMDD`, the
  ellipsised title, with the full title / summary / submitter in the tooltip;
* **right-clicking a row copies a link** that carries the mount point (`?kb=<slug>`),
  and that link opens the page it names;
* a **bilingual page shows one language at a time** with a ZH | EN switch, and the
  `<!-- lang:… -->` markers survive a trip through the block editor.

Needs playwright (present in `.venv312`, not in the API image) and a local Chrome.

    .venv312/bin/python api/tests/verify_knowledge_wiki_ui.py
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
PORT = 8795

BODY = """# 渠道笔记

这是**粗体**和*斜体*，还有 `代码`。

## 适用场景

- 甲公司 KA1
- 乙公司 B2B

> 注意：口径以字典为准

```js
const x = 1;
```

| 渠道 | 口径 |
| --- | --- |
| 甲公司 | sell-out |
| 乙公司 | sell-in |

![渠道图](/api/storage/serve?path=knowledge-assets%2Fdemo.png){width=420}

![半宽图](/api/storage/serve?path=knowledge-assets%2Fdemo2.png){width=50%}

想要内联 HTML 也只能当文字看：<script>window.__xss = 1</script>
<img src=x onerror="window.__xss = 2">

```mermaid
graph LR
  A[起点] --> B[终点]
```

```sql
SELECT channel, SUM(gto_rmb) FROM sales_invoice WHERE pa = 'CL' GROUP BY channel;
```

```brainfuck
+++[->+++<]>.
```

参见 [市场笔记](?kb=market-notes)。
"""

# Contrast audit over the rendered document. A surface whose background was
# overridden without its text colour produces readable-looking HTML and an invisible
# block — Tabler styles `pre` as a dark surface with `color: var(--tblr-light)`, and
# overriding only the background left all five code blocks at ratio 1.00 (white on
# light grey) while every structural check still passed.
AUDIT_JS = """() => {
  const lum = (c) => { const f = c.map((v) => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
    return 0.2126*f[0] + 0.7152*f[1] + 0.0722*f[2]; };
  const parse = (s) => { const m = (s || '').match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(parseFloat); return {rgb: p.slice(0,3), a: p.length > 3 ? p[3] : 1}; };
  const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b), hi = Math.max(l1,l2), lo = Math.min(l1,l2);
    return (hi + 0.05) / (lo + 0.05); };
  const bgOf = (el) => { let n = el; while (n) { const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.5) return c.rgb; n = n.parentElement; } return [255,255,255]; };
  const out = [];
  document.querySelectorAll('#kb-render *').forEach((el) => {
    const text = Array.from(el.childNodes).filter((n) => n.nodeType === 3)
      .map((n) => n.nodeValue.trim()).join('');
    if (!text) return;
    const fg = parse(getComputedStyle(el).color); if (!fg) return;
    out.push({tag: el.tagName.toLowerCase(), cls: String(el.className || ''), sample: text.slice(0, 24),
              fg: 'rgb(' + fg.rgb.join(',') + ')', bg: 'rgb(' + bgOf(el).join(',') + ')',
              ratio: Math.round(ratio(fg.rgb, bgOf(el)) * 100) / 100});
  });
  return out.sort((a, b) => a.ratio - b.ratio);
}"""

OWNED = {
    "slug": "channel-notes", "document_id": "kb:channel-notes", "title": "渠道笔记",
    "summary": "口径笔记", "summary_en": "Definitions in one line", "summary_zh": "口径笔记",
    "category": "渠道", "tags": ["渠道", "甲公司"], "document_type": "note",
    "status": "published", "version": "1.0", "author": "agent", "submitter": "Zhen Huang",
    "owner_email": "owner@example.com", "visibility": "private", "source_item_id": None,
    "size_bytes": len(BODY), "created_at": "2026-09-29T10:00:00", "updated_at": "2026-09-29T10:00:00",
    "shared_emails": [], "can_manage": True, "enabled": True,
}
PUBLIC_BODY = """<!-- lang:zh -->
# 市场笔记

别人的页。

<!-- lang:en -->
# Market Notes

Someone else's page.
"""
PUBLIC = dict(OWNED, slug="market-notes", document_id="kb:market-notes", title="市场笔记",
              category="市场", owner_email="mate@example.com", visibility="public", can_manage=False,
              enabled=True,
              body=PUBLIC_BODY,
              # Older than OWNED on purpose: the list is ordered by date, and an assertion
              # that cannot tell the two apart would pass on any order at all.
              created_at="2026-09-28T10:00:00", updated_at="2026-09-28T10:00:00")

FAILURES = []
SENT = []


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
window.__sent = [];
window.__copied = [];
try {
  // No clipboard permission in a headless run: record what the page tried to copy.
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: (t) => { window.__copied.push(String(t)); return Promise.resolve(); } },
  });
} catch (e) { window.__clipboard_error = String(e); }
const OWNED = %s, PUBLIC = %s, BODY = %s;
window.__body = BODY;      // the test swaps this to open a bilingual version
window.fetch = function (url, init) {
  const u = String(url);
  const method = ((init || {}).method || 'GET').toUpperCase();
  let payload = null;
  try { payload = init && init.body ? JSON.parse(init.body) : null; } catch (e) {}
  window.__sent.push({method: method, url: u, body: payload});
  let body = {ok: true};
  if (method === 'GET' && u.indexOf('/items/') >= 0) {
    const slug = decodeURIComponent(u.split('/items/')[1].split('?')[0]);
    body = slug === 'market-notes' ? PUBLIC : Object.assign({}, OWNED, {body: window.__body});
  } else if (method === 'GET') {
    body = {items: [OWNED, PUBLIC], count: 2, categories: ['市场', '渠道'], scope: 'mine'};
  } else if (u.indexOf('/publish') >= 0) {
    body = Object.assign({}, OWNED, {slug: 'channel-notes-public', visibility: 'public'});
  } else if (u.indexOf('/pull') >= 0) {
    body = Object.assign({}, OWNED, {slug: 'market-notes-copy'});
  } else if (method === 'PUT' || method === 'POST') {
    body = Object.assign({}, OWNED, payload || {}, {slug: 'channel-notes'});
  }
  return Promise.resolve({ok: true, status: 200,
    headers: {get: () => 'application/json'},
    json: () => Promise.resolve(body)})
};
""" % (json.dumps(OWNED), json.dumps(PUBLIC), json.dumps(BODY))


def main():
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            # confirm()/prompt() are auto-dismissed by Playwright; accept them so a test
            # can drive a flow that asks (discarding edits, confirming a duplicate title).
            page.on("dialog", lambda dialog: dialog.accept())
            page.add_init_script(STUB)
            page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.knowledgePage")
            browser_errors = list(errors)

            # ── the entry point: main nav bar, right of the Workspace tab ────
            entry = page.evaluate("""() => {
              const tab = document.getElementById('nav-tab-knowledge');
              const ws = document.getElementById('nav-tab-reports');
              if (!tab) return null;
              const rect = tab.getBoundingClientRect();
              const img = tab.querySelector('img');
              return {
                left: tab.offsetLeft, ws_left: ws ? ws.offsetLeft : -1,
                width: Math.round(rect.width), height: Math.round(rect.height),
                label: (tab.textContent || '').trim(),
                icon: img ? img.getAttribute('src') : '',
                icon_w: img ? Math.round(img.getBoundingClientRect().width) : 0,
                in_nav: !!tab.closest('.nav-center'),
              };
            }""")
            check("主导航栏里有 Knowledge base 入口", entry is not None)
            check("入口在主导航栏（.nav-center）内", bool(entry) and entry["in_nav"], entry)
            check("入口排在 Workspace 标签的右边", bool(entry) and entry["left"] > entry["ws_left"], entry)
            check("入口有可见尺寸（不是隐形按钮）",
                  bool(entry) and entry["width"] > 80 and entry["height"] >= 20, entry)
            check("入口文案是 Knowledge base", bool(entry) and entry["label"] == "Knowledge base", entry)
            check("入口带自己的图标 knowledge-icon.png",
                  bool(entry) and entry["icon"] == "knowledge-icon.png", entry)
            check("图标按导航栏尺寸渲染（22px，与 Workspace 图标一致）",
                  bool(entry) and entry["icon_w"] == 22, entry)
            check("Workspace 工具栏里没有重复放这个入口（已按要求移走）",
                  page.locator("#page-reports .rpt-toolbar button:has-text('Knowledge base')").count() == 0)

            # ── opening the wiki ─────────────────────────────────────────────
            page.click("#nav-tab-knowledge")
            page.wait_for_function(
                "() => getComputedStyle(document.getElementById('page-knowledge')).display === 'flex'")
            check("点击入口后知识库页显示", True)

            # The shell's breadcrumb bar must be collapsed here, exactly as it is on
            # the Workspace page: the nav bar already highlights where you are, so
            # "🏠 > Knowledge base" is a second title row that wastes a line.
            chrome = page.evaluate("""() => {
              const shell = document.querySelector('.workspace');
              const bar = shell.querySelector('.workspace-bar');
              return {
                compact: shell.classList.contains('workspace-page-compact'),
                bar_display: bar ? getComputedStyle(bar).display : null,
                breadcrumb: (document.getElementById('breadcrumb-text') || {}).textContent || '',
              };
            }""")
            check("知识库页收起面包屑标题栏（与 Workspace 页同样处理）",
                  chrome["compact"] and chrome["bar_display"] == "none", chrome)
            check("那行原本写的就是 Knowledge base（证明是收起、不是不存在）",
                  "Knowledge base" in chrome["breadcrumb"], chrome["breadcrumb"])

            # …and switching away must bring the bar back for the pages that want it.
            page.evaluate("() => navTo(null, 'home', 'Overview')")
            page.wait_for_timeout(120)
            restored = page.evaluate("""() => getComputedStyle(document.querySelector('.workspace-bar')).display""")
            check("离开知识库页后面包屑恢复（不是全局隐藏）", restored != "none", restored)
            page.evaluate("() => navTo(null, 'knowledge', 'Knowledge base')")
            page.wait_for_timeout(120)
            page.wait_for_selector("#kb-toc-list .kb-link", state="visible")

            toc = page.evaluate("""() => ({
              groups: document.querySelectorAll('#kb-toc-list .kb-group-t').length,
              slugs: Array.from(document.querySelectorAll('#kb-toc-list .kb-link')).map((e) => e.dataset.slug),
              links: Array.from(document.querySelectorAll('#kb-toc-list .kb-link')).map((e) => e.textContent.trim()),
            })""")
            check("目录不再按分类分组（产品/测试/渠道这类标题没有了）", toc["groups"] == 0, toc)
            check("目录按日期倒序：最新的一页排在最前",
                  toc["slugs"] == ["channel-notes", "market-notes"], toc["slugs"])
            check("目录里两页都在", len(toc["links"]) == 2, toc["links"])

            # ── the ⭐ curator switch: lit = retrieval, dim = out but readable ──
            # 点亮=启用（ri-star-fill 金色，参与 agent 检索），不亮=ri-star-line 灰描边
            # （不启用：仍可读、仍在列表）。星在日期前面；只有 can_manage 的行有星；
            # 点击只发 PUT /enabled，绝不顺带开页。
            star0 = page.evaluate("""() => {
              const own = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"] .kb-star');
              const other = document.querySelector('#kb-toc-list .kb-link[data-slug="market-notes"] .kb-star');
              if (!own) return null;
              const r = own.getBoundingClientRect();
              const icon = own.querySelector('i');
              return { exists: true, lit: !own.classList.contains('off'),
                       icon: icon ? icon.className : '',
                       color: getComputedStyle(own).color,
                       tabbable: own.getAttribute('tabindex') === '0',
                       visible: r.width > 8 && r.height > 8,
                       otherAbsent: !other,
                       first: own.parentElement.firstElementChild === own };
            }""")
            check("自己的页目录行有星标（can_manage 才有）", bool(star0) and star0["exists"], star0)
            check("别人的页没有星标（不摆点了会 404 的按钮）", bool(star0) and star0["otherAbsent"], star0)
            check("星标默认点亮（enabled=true → 无 .off）", bool(star0) and star0["lit"], star0)
            check("点亮态是 remix 实心星 ri-star-fill", bool(star0) and star0["icon"] == "ri-star-fill", star0)
            check("点亮态是金色", bool(star0) and star0["color"] == "rgb(245, 158, 11)", star0)
            check("星标可见且有尺寸（不是零尺寸假元素）", bool(star0) and star0["visible"], star0)
            check("星标可键盘聚焦（tabindex=0）", bool(star0) and star0["tabbable"], star0)
            check("星标在日期前面（行内第一个元素）", bool(star0) and star0["first"], star0)

            sent_before = page.evaluate("() => window.__sent.length")
            page.click('#kb-toc-list .kb-link[data-slug="channel-notes"] .kb-star')
            page.wait_for_timeout(150)
            toggled = page.evaluate("""() => {
              const own = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"] .kb-star');
              const req = window.__sent.filter((r) => r.url.indexOf('/enabled') >= 0).pop() || null;
              const icon = own ? own.querySelector('i') : null;
              return { off: !!own && own.classList.contains('off'),
                       icon: icon ? icon.className : '',
                       req: req ? { method: req.method, body: req.body } : null };
            }""")
            new_reqs = page.evaluate("(n) => window.__sent.slice(n)", sent_before)
            check("点星标只发一个请求（PUT /items/<slug>/enabled），不顺带打开页面",
                  len(new_reqs) == 1 and new_reqs[0]["method"] == "PUT"
                  and "/enabled" in new_reqs[0]["url"], new_reqs)
            check("第一次点击的 body 是 {enabled: false}",
                  bool(toggled["req"]) and toggled["req"]["body"] == {"enabled": False}, toggled["req"])
            check("点击后星标熄灭（.off 且换 ri-star-line）",
                  toggled["off"] and toggled["icon"] == "ri-star-line", toggled)

            page.click('#kb-toc-list .kb-link[data-slug="channel-notes"] .kb-star')
            page.wait_for_timeout(150)
            relit = page.evaluate("""() => {
              const own = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"] .kb-star');
              const reqs = window.__sent.filter((r) => r.url.indexOf('/enabled') >= 0);
              const icon = own ? own.querySelector('i') : null;
              return { lit: !!own && !own.classList.contains('off'),
                       icon: icon ? icon.className : '',
                       last: reqs.length ? reqs[reqs.length - 1].body : null };
            }""")
            check("再点一次：body {enabled: true}，星标重新点亮（ri-star-fill）",
                  relit["lit"] and relit["icon"] == "ri-star-fill"
                  and relit["last"] == {"enabled": True}, relit)

            # ── the page list: one line per page, date first ─────────────────
            row = page.evaluate("""() => {
              const el = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"]');
              const when = el.querySelector('.kb-when');
              const name = el.querySelector('.kb-link-t');
              const a = when ? when.getBoundingClientRect() : null;
              const b = name ? name.getBoundingClientRect() : null;
              return {
                when: when ? when.textContent.trim() : '',
                name: name ? name.textContent.trim() : '',
                nativeTitle: el.hasAttribute('title'),
                hasSmall: !!el.querySelector('small'),
                // Two boxes on the same line overlap vertically; on separate lines they do not.
                sameLine: !!a && !!b && Math.max(a.top, b.top) < Math.min(a.bottom, b.bottom) - 1,
                gap: a && b ? Math.round(b.left - a.right) : -1,
                overflow: name ? getComputedStyle(name).textOverflow : '',
                whiteSpace: name ? getComputedStyle(name).whiteSpace : '',
              };
            }""")
            check("目录行是单行：YYMMDD 与标题在同一行（不再是两行）",
                  row["sameLine"] and not row["hasSmall"], row)
            check("日期是 6 位 YYMMDD（2026-09-29 → 260929）", row["when"] == "260929", row["when"])
            check("日期在前、空格、标题在后", row["name"] == "渠道笔记" and row["gap"] >= 4, row)
            check("标题过长用省略号截断（不换行）",
                  row["overflow"] == "ellipsis" and row["whiteSpace"] == "nowrap", row)
            check("行上不再挂原生 title（否则会和自绘卡片叠成两个提示）", not row["nativeTitle"])
            check("超长标题确实被裁掉（ellipsis 规则真的生效）", page.evaluate("""() => {
              const name = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"] .kb-link-t');
              const before = name.textContent;
              name.textContent = '这是一个非常非常长的页面标题用来验证超出宽度时会被省略号截断而不是换行显示完整内容';
              const clipped = name.scrollWidth > name.clientWidth;
              name.textContent = before;
              return clipped;
            }"""))

            # ── the hover card (a designed panel, not the browser's own tooltip) ──────
            page.hover('#kb-toc-list .kb-link[data-slug="channel-notes"]')
            # The card fades in (opacity transition); wait for the state rather than for a
            # fixed number of milliseconds, or this asserts on the animation's first frame.
            page.wait_for_function(
                "() => getComputedStyle(document.getElementById('kb-tip')).opacity === '1'",
                timeout=4000)
            card = page.evaluate("""() => {
              const tip = document.getElementById('kb-tip');
              const row = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"]');
              const style = getComputedStyle(tip);
              const a = tip.getBoundingClientRect(), b = row.getBoundingClientRect();
              return {
                on: tip.classList.contains('on'), opacity: style.opacity,
                border: style.borderTopWidth + ' ' + style.borderTopStyle,
                radius: Math.round(parseFloat(style.borderTopLeftRadius)),
                shadow: style.boxShadow !== 'none', pointerEvents: style.pointerEvents,
                title: (tip.querySelector('.kb-tip-t') || {}).textContent || '',
                lines: Array.from(tip.querySelectorAll('.kb-tip-s')).map((e) => e.textContent.trim()),
                who: (tip.querySelector('.kb-tip-f') || {}).innerText || '',
                clearOfRow: a.right <= b.left + 1 || a.left >= b.right - 1,
                inside: a.left >= 0 && a.right <= window.innerWidth + 1 && a.top >= 0,
              };
            }""")
            check("悬停弹出的是自绘卡片（不再依赖浏览器原生 tooltip）",
                  card["on"] and card["opacity"] == "1", card)
            check("卡片是设计过的面板：圆角 / 细边框 / 阴影 / 不吃鼠标事件",
                  card["radius"] >= 8 and card["border"].startswith("1px solid")
                  and card["shadow"] and card["pointerEvents"] == "none", card)
            check("卡片给出完整标题 / 两行摘要 / 提交人",
                  card["title"] == "渠道笔记"
                  and card["lines"] == ["Definitions in one line", "口径笔记"]
                  and "Zhen Huang" in card["who"], card)
            check("卡片贴着行旁边显示，不盖住行本身且在窗口内",
                  card["clearOfRow"] and card["inside"], card)
            check("移开鼠标卡片就收起", page.evaluate("""() => {
              document.getElementById('kb-toc-list')
                .dispatchEvent(new MouseEvent('mouseleave', {bubbles: true}));
              return !document.getElementById('kb-tip').classList.contains('on');
            }"""))

            # ── right-click a page row: copy its link ────────────────────────
            page.evaluate("""() => {
              const el = document.querySelector('#kb-toc-list .kb-link[data-slug="channel-notes"]');
              const box = el.getBoundingClientRect();
              el.dispatchEvent(new MouseEvent('contextmenu', {
                bubbles: true, cancelable: true, clientX: box.left + 12, clientY: box.top + 12}));
            }""")
            menu = page.evaluate("""() => {
              const m = document.getElementById('kb-ctx');
              return {
                shown: !m.hidden && getComputedStyle(m).display !== 'none',
                items: Array.from(m.querySelectorAll('button')).map((b) => b.textContent.trim()),
                danger: Array.from(m.querySelectorAll('button.danger')).map((b) => b.textContent.trim()),
                hrMargins: Array.from(m.querySelectorAll('hr')).map((h) => Math.round(
                  parseFloat(getComputedStyle(h).marginTop))),
                height: Math.round(m.getBoundingClientRect().height),
              };
            }""")
            check("右键目录行弹出菜单（且真的可见）", menu["shown"], menu)
            # The same menu a Workspace card offers, with one deliberate difference: a page
            # is prose, so its document export is Word rather than a slide deck.
            check("菜单项与 Workspace 卡片菜单对齐（PPT 换成 Word）",
                  menu["items"] == ["Open", "Open in new tab", "Copy link",
                                    "Share with colleagues",
                                    "Export to Word", "Export to PDF", "Export to Picture",
                                    "Download markdown", "Rename", "Publish to public", "Delete"],
                  menu["items"])
            check("危险项带 danger 标（Delete 不能和普通项长得一样）",
                  menu["danger"] == ["Delete"], menu["danger"])
            # ⚠️ Tabler gives every <hr> a 2rem top/bottom margin. Left alone, the separators
            # added 64px of blank space each and the rows read as "spaced far apart" — the
            # Workspace menu pins this down (`.rpt-menu hr`), and this one has to as well.
            check("菜单分隔线没有撑出大片空白（Tabler 的 hr 外边距已被压掉）",
                  bool(menu["hrMargins"]) and all(m <= 8 for m in menu["hrMargins"]), menu["hrMargins"])
            check("整个菜单高度紧凑（11 项 < 520px）", menu["height"] < 520, menu["height"])
            page.click('#kb-ctx button[data-action="copy-link"]')
            page.wait_for_timeout(80)
            copied = page.evaluate("() => window.__copied")
            check("点 Copy link 复制出一条链接", bool(copied), copied)
            check("复制的是这一页的 ?kb=<slug> 链接",
                  bool(copied) and copied[-1].endswith("?kb=channel-notes"), copied)
            check("复制完菜单收起", page.evaluate("() => document.getElementById('kb-ctx').hidden"))
            link_src = page.evaluate("() => String(window.knowledgePage.copyLink)")
            check("链接走 appAbsUrl（解析 document.baseURI），没有自己拼 location.origin",
                  "appAbsUrl(" in link_src and "location.origin" not in link_src, link_src[:80])

            # ── reading: no markdown syntax ──────────────────────────────────
            page.click("#kb-toc-list .kb-link:has-text('渠道笔记')")
            page.wait_for_selector("#kb-render h2", state="visible")
            read = page.evaluate("""() => {
              const box = document.getElementById('kb-render');
              return {
                text: box.innerText,
                html: box.innerHTML,
                strong: Array.from(box.querySelectorAll('strong')).map((e) => e.textContent),
                h2: Array.from(box.querySelectorAll('h2')).map((e) => e.textContent),
                items: Array.from(box.querySelectorAll('li')).map((e) => e.textContent.trim()),
                tableRows: box.querySelectorAll('table tr').length,
                codeBlock: (box.querySelector('pre code') || {}).textContent || '',
                quote: Array.from(box.querySelectorAll('blockquote')).map((e) => e.textContent.trim()),
                scripts: box.querySelectorAll('script').length,
                onerror: box.querySelectorAll('[onerror]').length,
              };
            }""")
            check("阅读态渲染出真正的标题（不是 ## 文本）", read["h2"] == ["适用场景"], read["h2"])
            check("阅读态渲染出真正的粗体", read["strong"] == ["粗体"], read["strong"])
            check("阅读态列表是列表（没有 - 前缀）", read["items"] == ["甲公司 KA1", "乙公司 B2B"], read["items"])
            check("阅读态表格渲染成表格", read["tableRows"] == 3, read["tableRows"])
            check("阅读态代码块渲染成 pre>code", "const x = 1" in read["codeBlock"], read["codeBlock"])
            check("阅读态引用块渲染成 blockquote", read["quote"] and read["quote"][0].startswith("注意"), read["quote"])
            for symbol in ("##", "**", "```", "- 甲公司", "> 注意", "| --- |"):
                check("阅读态不出现 markdown 符号 %r" % symbol, symbol not in read["text"])
            check("页面里的 <script> 没有被当作脚本执行（只是文字）",
                  read["scripts"] == 0 and "<script>" in read["text"])
            check("图片上的 onerror 没有存活下来", read["onerror"] == 0)
            check("内联 HTML 没有执行", page.evaluate("() => window.__xss") is None)

            # ── images: the size rides in markdown and is folded into the render ────
            # `![alt](url){width=420}` — markdown has no size syntax and an inline <img> is
            # not an option (raw HTML is escaped on purpose), so the spec is written after
            # the image. The reader must see the picture, never the spec.
            images = page.evaluate("""() => Array.from(document.querySelectorAll('#kb-render img'))
              .map((img) => ({src: img.getAttribute('src'), alt: img.getAttribute('alt'),
                              width: img.style.width, marker: img.getAttribute('data-kb-w')}))""")
            check("正文里的图片渲染成图片", len(images) == 2, images)
            # `{width=420}` (a bare number means pixels) becomes a real CSS length, and the
            # author's own spec is kept alongside so the editor can write back what was typed.
            check("像素宽度写进了 img（420 → 420px）",
                  bool(images) and images[0]["width"] == "420px"
                  and images[0]["marker"] == "420", images[:1])
            check("百分比宽度原样保留（50%）",
                  len(images) > 1 and images[1]["width"] == "50%", images[1:2])
            check("读者看不到 {width=…} 这个标记",
                  "{width=" not in page.inner_text("#kb-render"),
                  page.inner_text("#kb-render")[:80])

            # ⚠️ The page title was rendered twice: once by the header, once by the body's
            # opening `# 渠道笔记`. The header is the page's identity; the body must not
            # repeat it (in any language of a bilingual page).
            title_once = page.evaluate("""() => ({
              header: (document.getElementById('kb-doc-title') || {}).textContent || '',
              bodyH1: Array.from(document.querySelectorAll('#kb-render h1')).map((e) => e.textContent.trim()),
              visibleCount: (document.getElementById('kb-doc').innerText.match(/渠道笔记/g) || []).length,
            })""")
            check("页面大标题在头部渲染", title_once["header"] == "渠道笔记", title_once)
            check("正文里不再重复一遍 H1（标题只出现一次）",
                  title_once["bodyH1"] == [] and title_once["visibleCount"] == 1, title_once)

            # ── code fences: diagram, highlighting, fallbacks ─────────────────
            # The decoration pass runs after the markdown lands; a ```mermaid block is
            # replaced by an SVG, the rest get token spans. Give the lazily-loaded
            # (3 MB) diagram library a moment.
            page.wait_for_selector("#kb-render .kb-mermaid svg", timeout=15000)
            check("```mermaid 被渲染成 SVG 图形", True)
            check("渲染成功后不再显示图的源码",
                  "graph LR" not in page.inner_text("#kb-render"))

            spans = page.evaluate("""() => {
              const pre = Array.from(document.querySelectorAll('#kb-render pre'))
                .find((p) => (p.textContent || '').indexOf('SELECT') >= 0);
              return pre ? pre.querySelectorAll('span[class^="kb-t-"]').length : 0;
            }""")
            check("SQL 代码块有语法高亮 token", spans >= 3, spans)

            unknown = page.evaluate("""() => {
              const pre = Array.from(document.querySelectorAll('#kb-render pre'))
                .find((p) => (p.textContent || '').indexOf('+++') >= 0);
              return pre ? {spans: pre.querySelectorAll('span').length, text: pre.textContent.trim()} : null;
            }""")
            check("未知语言不做猜测：保持纯文本且仍可读",
                  bool(unknown) and unknown["spans"] == 0 and unknown["text"].startswith("+++"), unknown)

            # ⚠️ The regression that started this: five code blocks were white on light
            # grey (ratio 1.00) and read as empty boxes while every structural check
            # above still passed.
            contrast = page.evaluate(AUDIT_JS)
            invisible = [row for row in contrast if row["ratio"] < 3]
            check("渲染区没有看不见的文字（对比度 < 3 的元素必须为 0）",
                  invisible == [], invisible[:4])
            check("审计确实覆盖了代码块（否则这条形同虚设）",
                  any("kb-t-" in row["cls"] or row["tag"] == "code" for row in contrast),
                  len(contrast))

            # ── editing: no markdown syntax either ───────────────────────────
            page.click("button:has-text('Edit')")
            page.wait_for_selector("#kb-editor .kb-block[data-kind='h2']", state="visible")
            edit = page.evaluate("""() => {
              const box = document.getElementById('kb-editor');
              return {
                text: box.innerText,
                kinds: Array.from(box.querySelectorAll('.kb-block')).map((e) => e.dataset.kind),
                h2: Array.from(box.querySelectorAll('.kb-block[data-kind="h2"]')).map((e) => e.textContent.trim()),
                strong: Array.from(box.querySelectorAll('strong')).map((e) => e.textContent),
                items: Array.from(box.querySelectorAll('.kb-list li')).map((e) => e.textContent.trim()),
                cells: box.querySelectorAll('table td').length,
                props: !!document.getElementById('kb-f-title'),
                bar: document.querySelectorAll('#kb-bar button').length,
                placeholders: Array.from(box.querySelectorAll('.kb-block')).slice(0, 1)
                  .map((e) => e.textContent).join(''),
              };
            }""")
            check("编辑态是块列表（段落/标题/列表/引用…）",
                  edit["kinds"][:4] == ["p", "h2", "ul", "quote"], edit["kinds"])
            check("编辑态没有 h1 块（正文不重复页面大标题）", "h1" not in edit["kinds"], edit["kinds"])
            check("编辑态的标题显示为标题", edit["h2"] == ["适用场景"], edit["h2"])
            check("编辑态粗体显示为粗体", edit["strong"] == ["粗体"], edit["strong"])
            check("编辑态列表是列表项", edit["items"] == ["甲公司 KA1", "乙公司 B2B"], edit["items"])
            check("编辑态表格可编辑单元格齐全", edit["cells"] == 4, edit["cells"])
            check("编辑态有属性面板（标题/分类/标签/类型/状态）", edit["props"])
            check("编辑态有格式工具栏", edit["bar"] >= 10, edit["bar"])
            for symbol in ("##", "**", "```", "- 甲公司", "> 注意"):
                check("编辑态不出现 markdown 符号 %r" % symbol, symbol not in edit["text"])
            editor_images = page.evaluate("""() => Array.from(
              document.querySelectorAll('#kb-editor img')).map((img) => img.getAttribute('data-kb-w'))""")
            check("编辑态里图片带着宽度标记（不然编辑一次尺寸就丢了）",
                  editor_images == ["420", "50%"], editor_images)
            check("编辑态也不显示 {width=…} 这个标记", "{width=" not in edit["text"], edit["text"][:80])

            # ── saving still produces markdown ───────────────────────────────
            page.evaluate("() => { document.getElementById('kb-f-title').value = '渠道笔记'; }")
            page.evaluate("() => window.knowledgePage.save()")
            page.wait_for_function("() => window.__sent.some((c) => c.method === 'PUT')")
            sent = page.evaluate("() => window.__sent.filter((c) => c.method === 'PUT')")
            body = (sent[-1] or {}).get("body") or {}
            markdown = body.get("body") or ""
            check("保存走 PUT /api/knowledge/items/{slug}", "/api/knowledge/items/channel-notes" in sent[-1]["url"],
                  sent[-1]["url"])
            check("保存回去的是 markdown（正文开头那行 H1 已去掉，不再重复标题）",
                  not markdown.lstrip().startswith("#"), markdown[:40])
            check("正文其余内容都在（不是把标题删掉就完事）",
                  "## 适用场景" in markdown and "**粗体**" in markdown, markdown[:60])
            check("保存回去的是 markdown（二级标题）", "## 适用场景" in markdown)
            check("保存回去的是 markdown（粗体）", "**粗体**" in markdown)
            check("保存回去的是 markdown（列表）", "- 甲公司 KA1" in markdown)
            check("保存回去的是 markdown（表格）", "| 渠道 | 口径 |" in markdown and "| --- |" in markdown)
            check("保存回去的是 markdown（代码块围栏）", "```" in markdown)
            check("保存回去的 markdown 保住图片宽度（{width=…}）",
                  "{width=420}" in markdown and "{width=50%}" in markdown, markdown[:200])
            check("保存带上了属性字段", body.get("document_type") == "note" and "category" in body)

            # ── the permission tier drives the actions ────────────────────────
            page.evaluate("() => window.knowledgePage.open('market-notes')")
            page.wait_for_selector("button:has-text('Pull a copy')", state="visible")
            actions = page.inner_text("#kb-doc-meta")
            check("别人的页给的是「Pull a copy」", "Pull a copy" in actions)
            check("别人的页不给 Edit / Publish", "Edit" not in actions and "Publish" not in actions, actions)
            check("公共快照有公共标记", "Public snapshot" in actions, actions)

            # ── a bilingual page: one language on screen, switch beside the title ─
            bi = page.evaluate("""() => {
              const btns = Array.from(document.querySelectorAll('#kb-doc-meta .kb-langswitch button'));
              const secs = Array.from(document.querySelectorAll('#kb-render .kb-lang'));
              return {
                labels: btns.map((b) => b.textContent.trim()),
                on: btns.filter((b) => b.classList.contains('on')).map((b) => b.textContent.trim()),
                langs: secs.map((s) => s.dataset.lang),
                visible: secs.filter((s) => !s.hidden).map((s) => s.dataset.lang),
                text: document.getElementById('kb-render').innerText,
              };
            }""")
            check("双语页面标题旁出现 ZH | EN 切换", bi["labels"] == ["ZH", "EN"], bi["labels"])
            check("默认显示中文，其余语言收起来",
                  bi["on"] == ["ZH"] and bi["visible"] == ["zh"] and bi["langs"] == ["zh", "en"], bi)
            check("另一语言的内容真的不在页面上（不是两版堆在一起）",
                  "Market Notes" not in bi["text"] and "Someone else's page." not in bi["text"],
                  bi["text"][:60])
            page.click(".kb-langswitch button:has-text('EN')")
            page.wait_for_timeout(80)
            en = page.evaluate("""() => {
              const secs = Array.from(document.querySelectorAll('#kb-render .kb-lang'));
              return {
                visible: secs.filter((s) => !s.hidden).map((s) => s.dataset.lang),
                on: Array.from(document.querySelectorAll('.kb-langswitch button.on'))
                  .map((b) => b.textContent.trim()),
                text: document.getElementById('kb-render').innerText,
              };
            }""")
            check("点 EN 切到英文、中文段收起",
                  en["visible"] == ["en"] and "Someone else's page." in en["text"], en["text"][:60])
            check("切换按钮的高亮跟着走", en["on"] == ["EN"], en["on"])
            check("英文段的标题也不重复（页面大标题在正文之外，语言无关）",
                  "Market Notes" not in en["text"], en["text"][:60])

            # ── the export posts the rendered page, in the language on screen ────
            page.evaluate("""() => {
              const el = document.querySelector('#kb-toc-list .kb-link[data-slug="market-notes"]');
              const box = el.getBoundingClientRect();
              el.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
                clientX: box.left + 12, clientY: box.top + 12}));
            }""")
            page.click('#kb-ctx button[data-action="export-word"]')
            page.wait_for_function(
                "() => window.__sent.some((c) => c.method === 'POST' && c.url.indexOf('/export/word') >= 0)",
                timeout=8000)
            exported = page.evaluate("""() => {
              const call = window.__sent.filter((c) => c.method === 'POST'
                && c.url.indexOf('/export/word') >= 0).pop();
              return {url: call.url, body: call.body || {}};
            }""")
            check("导出走 POST /api/knowledge/items/{slug}/export/word",
                  "/api/knowledge/items/market-notes/export/word" in exported["url"], exported["url"])
            exported_html = exported["body"].get("html") or ""
            check("导出提交的是渲染后的正文（服务端不重跑 markdown）",
                  "<h2" in exported_html or "<p" in exported_html, exported_html[:70])
            check("导出只带屏幕上那一版语言（隐藏段被摘掉）",
                  "Someone else's page." in exported_html and "别人的页" not in exported_html,
                  exported_html[:90])
            check("导出带上标题", exported["body"].get("title") == "市场笔记",
                  exported["body"].get("title"))

            page.evaluate("() => window.knowledgePage.open('channel-notes')")
            page.wait_for_selector("button:has-text('Publish to public')", state="visible")
            own = page.inner_text("#kb-doc-meta")
            check("自己的页给的是 Edit / Share / Publish / Delete",
                  all(word in own for word in ("Edit", "Share", "Publish to public", "Delete")), own)

            # ── the language markers survive a trip through the block editor ──
            # The marker is structure, not prose: it shows as a chip and is written back
            # unchanged, so a bilingual page can be edited in the browser without losing
            # its languages (an escaped `&lt;!-- lang:zh --&gt;` paragraph would).
            page.evaluate("""() => {
              window.__body = '<!-- lang:zh -->\\n# 渠道笔记\\n\\n中文正文。\\n\\n'
                            + '<!-- lang:en -->\\n# Channel Notes\\n\\nEnglish body.\\n';
            }""")
            page.evaluate("() => window.knowledgePage.open('channel-notes')")
            page.wait_for_selector("#kb-doc-meta .kb-langswitch button", state="visible")
            page.click("button:has-text('Edit')")
            page.wait_for_selector("#kb-editor .kb-block[data-kind='lang']", state="visible")
            chips = page.evaluate("""() => {
              const box = document.getElementById('kb-editor');
              return {
                chips: Array.from(box.querySelectorAll('.kb-block[data-kind="lang"]')).map((e) => ({
                  label: e.textContent.trim(), lang: e.dataset.lang,
                  editable: e.getAttribute('contenteditable')})),
                text: box.innerText,
              };
            }""")
            check("双语页面在编辑器里把语言标记显示成 ZH / EN 徽标",
                  [c["label"] for c in chips["chips"]] == ["ZH", "EN"], chips["chips"])
            check("徽标不可编辑（它是结构，不是正文）",
                  all(c["editable"] == "false" for c in chips["chips"]), chips["chips"])
            check("编辑器里不出现 <!-- lang: --> 这些符号", "<!--" not in chips["text"], chips["text"][:80])
            # A bilingual page needs the summary pair, and the editor is where a human
            # writes it: the inputs must be there, pre-filled from the page, and carried by
            # the save. (Checked while still in edit mode — a save leaves it.)
            fields = page.evaluate("""() => {
              const en = document.getElementById('kb-f-summary-en');
              const zh = document.getElementById('kb-f-summary-zh');
              return {en: en ? en.value : null, zh: zh ? zh.value : null};
            }""")
            check("编辑器里有 Summary — EN / Summary — 中文，且按页面填好",
                  fields["en"] == "Definitions in one line" and fields["zh"] == "口径笔记", fields)
            page.evaluate("() => window.knowledgePage.save()")
            page.wait_for_timeout(200)
            sent2 = page.evaluate("() => window.__sent.filter((c) => c.method === 'PUT')")
            saved2 = ((sent2[-1] if sent2 else {}) or {}).get("body", {})
            markdown2 = saved2.get("body", "")
            check("保存时语言标记原样写回（双语页面可以在网页里放心编辑）",
                  "<!-- lang:zh -->" in markdown2 and "<!-- lang:en -->" in markdown2, markdown2[:140])
            check("保存带上了 summary_en / summary_zh",
                  saved2.get("summary_en") == "Definitions in one line"
                  and saved2.get("summary_zh") == "口径笔记", saved2)

            # ── a shared link lands on the page it names ────────────────────────────────

            deep = browser.new_page(viewport={"width": 1400, "height": 900})
            deep_errors = []
            deep.on("pageerror", lambda exc: deep_errors.append(str(exc)))
            deep.on("dialog", lambda dialog: dialog.accept())
            deep.add_init_script(STUB)
            deep.goto("http://127.0.0.1:%d/index.html?kb=market-notes" % PORT, wait_until="domcontentloaded")
            deep.wait_for_function("() => !!window.knowledgePage")
            deep.wait_for_function(
                "() => getComputedStyle(document.getElementById('page-knowledge')).display === 'flex'")
            deep.wait_for_function(
                "() => (document.getElementById('kb-doc-title') || {}).textContent === '市场笔记'",
                timeout=8000)
            opened = deep.evaluate("""() => ({
              title: (document.getElementById('kb-doc-title') || {}).textContent,
              shown: !document.getElementById('kb-doc').hidden,
              got: window.__sent.map((c) => c.method + ' ' + c.url),
            })""")
            check("?kb=<slug> 直接打开那一页（不是停在默认页）",
                  opened["title"] == "市场笔记" and opened["shown"], opened["title"])
            check("深链是按 slug 去取那一页",
                  any("/api/knowledge/items/market-notes" in u for u in opened["got"]), opened["got"])
            check("深链页面没有 JS 报错", deep_errors == [], deep_errors[:3])
            deep.close()

            # ── a cross-reference written inside a page is followed ────────────────────
            #
            # The failure this pins: `?kb=` was read exactly once, by openDeepLink(), on
            # the first load — correct while every way to arrive at one was a fresh load
            # (a pasted link, Open in new tab, Copy link). A link written *inside* a page
            # is a different animal: clicking `?kb=other` from `/?kb=this` is a
            # same-document navigation, so the browser rewrites the URL and does NOT
            # reload. Nothing re-ran init(), the reader never moved, and the address bar
            # ended up naming a page that was not on screen — a link that silently goes
            # somewhere else, which this codebase already calls "worse than no link".
            #
            # Both halves are asserted, because either alone passes on the broken build:
            # the title alone also passes if the reader moved but the URL did not, and the
            # URL alone is precisely what the browser had already done to itself.
            xref = browser.new_page(viewport={"width": 1400, "height": 900})
            xref_errors = []
            xref.on("pageerror", lambda exc: xref_errors.append(str(exc)))
            xref.on("dialog", lambda dialog: dialog.accept())
            xref.add_init_script(STUB)
            xref.goto("http://127.0.0.1:%d/index.html?kb=channel-notes" % PORT, wait_until="domcontentloaded")
            xref.wait_for_function("() => !!window.knowledgePage")
            # Enter the page the way a reader does — navTo is what shows the container —
            # and then neutralise ONE unrelated pre-existing defect: opening a page leaves
            # `#kb-body` at display:none with the projects wall on top (reproduces
            # identically at HEAD, no in-flight edits involved). Left alone, the
            # cross-reference has a 0x0 box and cannot be clicked at all, which would make
            # this test measure the wall bug instead of the thing it is about. Only
            # `display` is overridden; the interception and history logic are untouched.
            xref.evaluate("() => navTo(null, 'knowledge', 'Knowledge base')")
            xref.wait_for_timeout(1000)
            xref.add_style_tag(content="#kb-body{display:flex !important}#kb-wall{display:none !important}")
            xref.evaluate("() => window.knowledgePage.open('channel-notes')")
            xref.wait_for_function(
                "() => (document.getElementById('kb-doc-title') || {}).textContent === '渠道笔记'",
                timeout=8000)
            link = xref.query_selector("#kb-render a[href*='kb=market-notes']")
            check("正文里的 ?kb= 跨页链接被渲染成真链接", link is not None, link)
            if link is not None:
                # A real click at coordinates, not el.click(): the report was a reader
                # clicking a visible link, and only a hit-tested click can say it was
                # actually reachable where it was drawn.
                link.scroll_into_view_if_needed()
                xref.wait_for_timeout(150)
                rect = link.bounding_box()
                check("跨页链接在视口内可点（elementFromPoint 命中的是它自己）",
                      xref.evaluate(
                          """([x, y]) => {
                            const el = document.elementFromPoint(x, y);
                            return !!(el && el.closest("#kb-render a[href*='kb=market-notes']"));
                          }""",
                          [rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2]), rect)
                xref.mouse.click(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                xref.wait_for_function(
                    "() => (document.getElementById('kb-doc-title') || {}).textContent === '市场笔记'",
                    timeout=8000)
                landed = xref.evaluate("""() => ({
                  title: (document.getElementById('kb-doc-title') || {}).textContent,
                  shown: !document.getElementById('kb-doc').hidden,
                  search: location.search,
                })""")
                check("点正文的跨页链接会换到那一页（不是只动地址栏）",
                      landed["title"] == "市场笔记" and landed["shown"], landed["title"])
                check("换页之后地址栏也指着那一页（URL 与页面不再各说各话）",
                      "kb=market-notes" in landed["search"], landed["search"])
            check("跨页链接没有 JS 报错", xref_errors == [], xref_errors[:3])
            xref.close()

            # ⚠️ A copied link that resolves against the wrong base lands on the host
            # root, where a gateway answers **200 with an error body** — the "downloaded
            # 49 bytes" class of bug. The link builder must resolve against
            # document.baseURI, so shadow it for one call and read the URL back. The
            # shadowed value has to be a *valid* base: the app serves from "/" by default
            # but still supports a sub-path mount, and that is the case worth pinning.
            # This is the last check on this page: it changes baseURI for good.
            mounted = page.evaluate("""() => {
              try {
                Object.defineProperty(document, 'baseURI', {
                  configurable: true, get: () => 'https://klado.example/klado/',
                });
              } catch (e) { return {error: String(e)}; }
              return {url: appAbsUrl('?kb=channel-notes')};
            }""")
            check("部署在子路径下时链接照样带挂载点（不是 host root）",
                  mounted.get("url") == "https://klado.example/klado/?kb=channel-notes", mounted)

            check("整页没有 JS 报错", errors == [], errors[:3])
            browser.close()
    finally:
        httpd.shutdown()

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())