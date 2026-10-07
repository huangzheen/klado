"""Which Chinese strings does an English reader still see?

Loads the real SPA with the API stubbed, switches to English, then walks every
rendered element and reports any user-visible text that still contains Chinese:
text nodes, plus the attributes a reader actually sees (`placeholder`, `title`,
`aria-label`).

Run in the browser rather than re-implementing the dictionary, so the answer is
what the reader gets — a Chinese string that survives here survives because it has
neither a `中文 / English` pair nor a `DICT` entry, and that is a content gap the
convention alone will not close.
"""
import sys
from collections import Counter

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/tmp")
from shot_bilingual import STUB  # noqa: E402  (same stub, so the pages render)

CJK = r"\u4e00-\u9fff"

PROBE = r"""
() => {
  const CJK = /[一-鿿]/;
  const out = [];
  const label = (el) => {
    const bits = [el.id, el.className && typeof el.className === 'string' ? el.className : ''];
    return bits.filter(Boolean).join('#') || el.tagName.toLowerCase();
  };
  // Only what is actually on screen: every page but the active one sits in a
  // display:none container, and counting those turns a handful of real gaps into
  // hundreds of phantom ones.
  const shown = (el) => !!(el.getClientRects && el.getClientRects().length);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const t = (n.textContent || '').trim();
    if (t && CJK.test(t)) {
      const el = n.parentElement;
      if (el && el.closest('[data-i18n-skip]')) continue;
      if (el && (el.tagName === 'SCRIPT' || el.tagName === 'STYLE')) continue;
      if (!shown(el)) continue;
      out.push({where: 'text', el: label(el), value: t});
    }
  }
  for (const attr of ['placeholder', 'title', 'aria-label']) {
    for (const el of document.querySelectorAll('[' + attr + ']')) {
      const v = (el.getAttribute(attr) || '').trim();
      if (v && CJK.test(v) && !el.closest('[data-i18n-skip]')) {
        out.push({where: attr, el: label(el), value: v, visible: shown(el)});
      }
    }
  }
  return out;
}
"""

PAGES = [
    ("data-center", "navTo(null, 'data-center', 'Data Center')"),
    ("inbox", "navTo(null, 'inbox', 'Inbox')"),
    ("workspace", "navTo(null, 'reports', 'Workspace')"),
    ("knowledge", "navTo(null, 'knowledge', 'Knowledge')"),
    ("calendar", "navTo(null, 'calendar', 'Calendar')"),
    ("settings", "navTo(null, 'system-settings', 'Settings')"),
]

# Strings that are Chinese *on purpose*. The toggle shows the name of the language
# it switches to, in that language's own name — `中` in English mode, `EN` in
# Chinese mode — so "an English reader sees a Chinese character" is the feature,
# not a gap. Anything added here needs a reason, because this list is how a real
# regression would be waved through.
EXPECTED = {
    "中": "the language toggle names its target language ('中' in en, 'EN' in zh)",
}


def main() -> int:
    found = Counter()
    detail = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(STUB)
        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded")
        page.evaluate("() => localStorage.setItem('klado-lang', 'en')")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(700)
        for name, js in PAGES:
            try:
                page.evaluate(f"() => {js}")
                page.wait_for_timeout(500)
            except Exception as exc:
                print(f"  ! {name}: {exc}")
                continue
            for hit in page.evaluate(PROBE):
                key = (name, hit["where"], hit["value"])
                found[key] += 1
                detail.setdefault(key, hit["el"])
        browser.close()

    # A node is a gap when *part* of it is Chinese: "搜索… / Search" is translated,
    # "搜索… / " is not. So the allowlist matches whole values, and the report
    # still shows anything merely containing an expected string.
    gaps = {k: n for k, n in found.items() if k[2].strip() not in EXPECTED}
    print(f"\n英文模式下仍含中文的可见文案：{len(gaps)} 种"
          f"（另有 {len(found) - len(gaps)} 种已豁免）\n")
    for (page_name, where, value), n in sorted(gaps.items(),
                                               key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  [{page_name}/{where}] ×{n}  {detail[(page_name, where, value)]}")
        print(f"      {value[:100]}")
    for value, why in sorted(EXPECTED.items()):
        print(f"  [豁免] {value}  — {why}")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
