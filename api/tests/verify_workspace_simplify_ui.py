"""Workspace, inside a folder: what was removed, and what must survive it.

    .venv312/bin/python api/tests/verify_workspace_simplify_ui.py

This is a browser test because the removals are almost all about **what is on the
screen**, and every one of them has a way of passing a source read:

* `#wr-filters { display: flex }` — a dropdown hidden with the `hidden` attribute is
  only hidden while no author rule sets `display`. `.rpt-select` does not, but that is
  a fact about app.css that this file pins by measuring, not by reading.
* the mode chip "My workspace" is gone for owned documents — `textContent` still
  contains the string right up to the moment it is assigned, so only the rendered
  box proves it left the screen.
* the row badge is gone for documents — `badge` is computed and then thrown away by a
  ternary arm, which is invisible in the DOM but obvious in the row's width.

And the removals have an opposite risk that a source read cannot see either: what was
deleted was sometimes the ONLY place a fact was shown. So the second half of this file
asserts the facts still arrive somewhere else — the filename moved into the viewer
toolbar, the status badge stays on rows, Back stays on the toolbar.

The last group is the one that is genuinely easy to get wrong. Hiding a filter is only
safe if the filter cannot trap the reader in the state it created, so:
  * the visibility decision is measured from the UNFILTERED load and remembered,
  * using a filter does not make the filter disappear,
  * a folder of one status has no status dropdown at all.
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
PORT = 8802

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


def doc(name, doc_type="docx", status="published", slug=None, author="agent",
        visibility="private", shared_with_me=False):
    return {
        "id": abs(hash(name)) % 100000,
        "slug": slug or name.rsplit(".", 1)[0].replace("_", "-"),
        "title": "季度定价备忘 " + name,
        "summary": "",
        "category": "Documents",
        "tags": [],
        "status": status,
        "doc_type": doc_type,
        "doc_name": name,
        "owner_email": "me@example.com",
        "author": author,
        "visibility": visibility,
        "kind": "document",
        "size_bytes": 37888,
        "can_manage": True,
        "shared_with_me": shared_with_me,
        "shared": shared_with_me,
        "created_at": "2026-09-29T10:00:00",
        "updated_at": "2026-10-01T10:00:00",
    }


# The payload is a global so a single page can be driven through several datasets
# without reloading, which is also what makes "the filter survived being used" testable.
STUB = """
window.__docs = [];
window.__cats = ['Documents'];
window.__calls = [];
window.fetch = function (url, init) {
  const u = String(url);
  window.__calls.push(u);
  const json_ = (body) => Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
  if (u.indexOf('/api/reports/projects/') > -1 && u.indexOf('/folders') > -1) {
    return json_({folders: [{id: 1, name: 'Documents', system: true, count: window.__docs.length}]});
  }
  if (u.indexOf('/api/reports/projects') > -1) {
    return json_({projects: [{slug: 'pricing', name: 'Pricing', doc_count: window.__docs.length}]});
  }
  if (u.indexOf('/api/reports?') === 0 || u.indexOf('/api/reports') === 0) {
    let list = window.__docs.slice();
    const st = (u.match(/[?&]status=([a-z]+)/) || [])[1];
    const cat = (u.match(/[?&]category=([^&]+)/) || [])[1];
    if (st) list = list.filter((d) => (d.status || 'published') === st);
    if (cat) list = list.filter((d) => d.category === cat);
    return json_({reports: list, count: list.length, categories: window.__cats});
  }
  return json_({});
};
"""


def set_docs(page, docs, cats):
    page.evaluate(
        "([d, c]) => { window.__docs = %s; window.__cats = %s; }"
        % (json.dumps(docs), json.dumps(cats)), [docs, cats])


def visible(page, sel):
    """Truthy only if the element has a box on screen — `hidden` elements never do."""
    return page.evaluate(
        """(s) => { const el = document.querySelector(s);
             if (!el) return false;
             const r = el.getBoundingClientRect();
             return r.width > 0 && r.height > 0
                 && getComputedStyle(el).display !== 'none'; }""", sel)


def _srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(rgb):
    r, g, b = (_srgb_to_linear(v / 255.0) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def parse_css_rgb(value):
    nums = [float(p) for p in value[value.index("(") + 1:value.index(")")].split(",")[:3]]
    return tuple(nums)


def delta_e(a, b):
    """CIE76 colour difference. Same function as the one the palette was chosen with."""
    la, lb = _rgb_to_lab(parse_css_rgb(a)), _rgb_to_lab(parse_css_rgb(b))
    return sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5


def min_pair_delta_e(colours):
    """The closest pair, and how close. Returns (delta, key_a, key_b).

    ⚠️ A palette with fewer than two entries has NO closest pair, and `min([])` raises —
    so this reports 0.0 instead. That is a real case, not a hypothetical: dropping
    `data-doctype` leaves the list empty, and a test that raises there reports a
    traceback rather than the assertion that actually failed, which is how a red test
    gets mistaken for a broken test.
    """
    keys = sorted(colours)
    if len(keys) < 2:
        return (0.0, "", "")
    pairs = [(delta_e(colours[a], colours[b]), a, b)
             for i, a in enumerate(keys) for b in keys[i + 1:]]
    return min(pairs)


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
            page.wait_for_selector("#rpt-tab-mine", state="visible")

            fourteen = [doc("q%d_pricing_memo.docx" % i) for i in range(14)]

            # ── the case that prompted this: a folder of published documents ──
            set_docs(page, fourteen, ["Documents"])
            page.evaluate("() => reportsPage.openProject('pricing')")
            page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 14")
            check("14 行都在", page.evaluate("() => document.querySelectorAll('#wr-list .wr-row').length") == 14)
            check("只有一种状态时，状态下拉框不出现",
                  not visible(page, "#rpt-status"), "只有一个 published")
            # ⚠️ The category dropdown is REMOVED, not hidden. So the assertion is that the
            # element is not in the document at all — `hidden` would have passed a
            # "is it visible" test and left the control in the tree to be re-found later.
            check("分类下拉框已从 DOM 里移除（不是隐藏）",
                  page.evaluate("() => document.querySelectorAll('#rpt-category').length") == 0)
            check("整行 filters 在无筛选项时收起（不留家具）",
                  not visible(page, "#wr-filters"))

            # ── …and the case where it does narrow something ──────────────────
            set_docs(page, fourteen[:3] + [doc("old_memo.docx", status="archived", slug="old-memo")],
                     ["Documents"])
            page.evaluate("() => reportsPage.load()")
            page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 4")
            check("有两种状态时，状态下拉框出现（它真的能筛）", visible(page, "#rpt-status"))
            check("filters 行随之出现", visible(page, "#wr-filters"))

            # ⚠️ The trap. The filters are applied server-side, so `_items` is already
            # the narrowed list. Re-deriving visibility from it would hide the very
            # control the reader is holding.
            page.select_option("#rpt-status", "archived")
            page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 1")
            check("用了筛选之后，筛选框仍然在（不能把自己藏起来把人困住）",
                  visible(page, "#rpt-status"))
            page.select_option("#rpt-status", "")
            page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 4")

            # ── rows: one line, and the extension is not printed three times ──
            row = page.evaluate("""() => {
              const el = document.querySelector('#wr-list .wr-row');
              const t = el.querySelector('.wr-row-t'), s = el.querySelector('.wr-row-size');
              const tr = t.getBoundingClientRect(), sr = s ? s.getBoundingClientRect() : null;
              return {text: el.innerText,
                      badges: Array.from(el.querySelectorAll('.wr-row-b'))
                        .map((b) => b.innerText.trim()),
                      hasFileLine: !!el.querySelector('.wr-row-file'),
                      sameLine: sr ? Math.abs(tr.top - sr.top) < 3 : null,
                      lines: Math.round(el.getBoundingClientRect().height)};
            }""")
            check("文档行末尾不再有类型徽章（图标已经用颜色说了）",
                  row["badges"] == [], row["badges"])
            check("行内没有第二行的文件名了", not row["hasFileLine"], row["hasFileLine"])
            check("标题与大小在同一行", row["sameLine"] is True, row["sameLine"])
            # ⚠️ Height, not "there is no second element": a `.wr-row-file` that still had
            # content but was laid out inline would pass the selector check above while
            # still being the two-line row this replaced. One line of text at 6px padding
            # is ~32px; the old two-line row was ~44.
            check("行高是单行的尺寸（≈32px，不是双行的 ≈44px）",
                  row["lines"] <= 36, row["lines"])
            archived_badge = page.evaluate("""() => {
              const rows = Array.from(document.querySelectorAll('#wr-list .wr-row'));
              const r = rows.find((x) => x.innerText.includes('old_memo'));
              return r ? r.innerText : '';
            }""")
            # ⚠️ Compared lowercased, because `.wr-row-b` is `text-transform: uppercase`
            # and `innerText` returns the RENDERED text — so the DOM says "Archived" and
            # the screen says "ARCHIVED", and a case-sensitive read of the screen fails on
            # a badge that is plainly there.
            check("已归档的行仍然带状态徽章（文件名里读不出来）",
                  "已归档" in archived_badge or "archived" in archived_badge.lower(),
                  archived_badge.replace("\n", " | ")[:80])

            # ── the icon is coloured, and the four are told apart BY COLOUR ────
            #
            # ⚠️ Why the second half is a NUMBER and not "they look different": the four
            # are close in lightness on purpose (a light and a dark icon in one column
            # reads as "this one matters"), so they are told apart by hue — and hue
            # separation is only meaningful in a perceptual space. PPTX vs PDF is the
            # pair a naive brand-colour choice collapses: Office PowerPoint is a
            # red-orange and Adobe's is a red, and measured there the two are the same
            # icon. So the palette is asserted to keep that pair apart, on the actual
            # rendered colours, in BOTH themes — a token edit that quietly re-converges
            # them fails here.
            four = [doc("a_deck.pptx", doc_type="pptx", slug="a-deck"),
                    doc("b_sheet.xlsx", doc_type="xlsx", slug="b-sheet"),
                    doc("c_memo.docx", doc_type="docx", slug="c-memo"),
                    doc("d_spec.pdf", doc_type="pdf", slug="d-spec")]
            for theme in ("light", "dark"):
                page.evaluate("(t) => localStorage.setItem('klado-theme', t)", theme)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("() => !!window.reportsPage")
                set_docs(page, four, ["Documents"])
                page.evaluate("() => reportsPage.openProject('pricing')")
                page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 4")
                colours = page.evaluate("""() => {
                  const out = {};
                  for (const r of document.querySelectorAll('#wr-list .wr-row')) {
                    const i = r.querySelector('.wr-row-icon');
                    if (i && i.dataset.doctype) out[i.dataset.doctype] = getComputedStyle(i).color;
                  }
                  return out;
                }""")
                check("%s 主题下四种文档类型的图标都有颜色" % theme,
                      sorted(colours) == ["docx", "pdf", "pptx", "xlsx"], colours)
                # A grey icon identifies nothing — that is the state this replaced.
                check("%s 主题下图标不是继承来的灰色" % theme,
                      all(v not in ("rgb(24, 36, 51)", "rgb(99, 113, 133)", "rgb(0, 0, 0)")
                          for v in colours.values()), colours)
                worst = min_pair_delta_e(colours)
                check("%s 主题下最接近的一对图标仍可分辨（CIE76 ΔE ≥ 25）" % theme,
                      worst[0] >= 25, "%s vs %s  ΔE=%.1f" % (worst[1], worst[2], worst[0]))
            page.evaluate("(t) => localStorage.setItem('klado-theme', t)", "light")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.reportsPage")
            set_docs(page, fourteen[:3] + [doc("old_memo.docx", status="archived", slug="old-memo")],
                     ["Documents"])
            page.evaluate("() => navTo(null, 'reports', 'Workspace')")
            page.evaluate("() => reportsPage.openProject('pricing')")
            page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 4")

            # ── opening a document: the toolbar is now the only header ────────
            page.evaluate("() => reportsPage.open('q0-pricing-memo')")
            page.wait_for_selector("#rpt-viewer.open", state="visible")
            bar = page.evaluate("""() => {
              const v = document.getElementById('rpt-viewer');
              return {
                title: document.getElementById('rpt-viewer-title').innerText.trim(),
                meta: document.getElementById('rpt-viewer-meta').innerText.trim(),
                mode: document.getElementById('rpt-viewer-mode').innerText.trim(),
                modeVisible: (() => { const e = document.getElementById('rpt-viewer-mode');
                  const r = e.getBoundingClientRect();
                  return r.width > 0 && r.height > 0; })(),
                frameSrc: v.querySelector('iframe') ? v.querySelector('iframe').src : '',
                buttons: Array.from(v.querySelectorAll('.rpt-viewer-bar button'))
                  .map((b) => (b.innerText || b.title || '').trim()),
              };
            }""")
            check("文件名搬进了工具栏 meta（否则删掉页面头部就丢了）",
                  "q0_pricing_memo.docx" in bar["meta"], bar["meta"])
            check("meta 不再打印默认作者 agent（常量不是信息）",
                  "agent" not in bar["meta"], bar["meta"])
            check("自己的文档不再挂 'My workspace' 徽章", not bar["modeVisible"], bar["mode"])
            check("iframe 带 embed=1（页面据此不画自己的头）",
                  "embed=1" in bar["frameSrc"], bar["frameSrc"][:110])
            check("工具栏没有 × 关闭按钮（Back 已经在同一位置干同一件事）",
                  not any(b in ("Close", "关闭") for b in bar["buttons"]), bar["buttons"])
            check("Back 还在", any("Back" in b for b in bar["buttons"]), bar["buttons"])
            check("下载还在", any("Download" in b for b in bar["buttons"]), bar["buttons"])

            page.evaluate("() => reportsPage.closeViewer()")

            # ── a borrowed document still says so ─────────────────────────────
            set_docs(page, [doc("borrowed.docx", slug="borrowed", visibility="public")],
                     ["Documents"])
            page.evaluate("() => reportsPage.load()")
            page.wait_for_function("() => document.querySelectorAll('#wr-list .wr-row').length === 1")
            page.evaluate("() => reportsPage.open('borrowed')")
            page.wait_for_selector("#rpt-viewer.open", state="visible")
            borrowed = page.evaluate("""() => {
              const e = document.getElementById('rpt-viewer-mode');
              const r = e.getBoundingClientRect();
              return {text: e.innerText.trim(),
                      visible: r.width > 0 && r.height > 0};
            }""")
            check("公开文档仍然显示归属徽章（这正是它存在的理由）", borrowed["visible"], borrowed["text"])
            check("归属徽章说清了是只读", "只读" in borrowed["text"] or "read only" in borrowed["text"],
                  borrowed["text"])

            check("整页没有 JS 报错", errors == [], errors[:3])
            browser.close()
    finally:
        httpd.shutdown()

    print("\n失败 %d 项" % len(FAILURES))
    if FAILURES:
        print("失败：")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
