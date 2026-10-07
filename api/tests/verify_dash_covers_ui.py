"""A dashboard card must show a real cover image, not a placeholder.

Needs the app up on :8000. Run from inside the app container.

The assertion is `naturalWidth`, not "is there an `<img>`": a tag in the DOM only
proves the branch was taken. `naturalWidth` is 0 for an image the browser could
not fetch, which is the failure that matters (a cover that silently 404s is a
grey rectangle saying nothing, exactly like no cover at all).

It also checks the placeholder layer is GONE. `.rpt-cover-ph` is absolutely
positioned and comes after the `<img>`, so rendering it unconditionally paints
"Live page" on top of a perfectly good cover — the same trap the report card
carries, and the reason this file exists.
"""
#!/usr/bin/env python3
"""仪表盘卡片封面的回归：每张卡都必须画出一张真的图，而不是占位符。

判据不看「有没有 <img>」——那只能证明标签在。要证明**图真的画出来了**，
量的是 naturalWidth（没加载成功就是 0）和占位层是否被替换掉。
"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
passed, failed = [], []

def _open_dashboard(page):
    """Get to the Dashboard page. The tab is in the LEFT RAIL, not the top bar."""
    page.evaluate("() => navTo(null, 'dashboard', 'Dashboard')")
    page.wait_for_timeout(2500)



def ok(m):
    passed.append(m)
    print("  ✓", m)


def bad(m):
    failed.append(m)
    print("  ✗", m)


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)
    # ⚠️ The Dashboard tab left the TOP BAR (it lives in the left rail now), so
    # clicking `[data-nav-key="dashboard"]` finds nothing. The rail row is the way in.
    _open_dashboard(page)
    page.wait_for_timeout(3500)

    n = page.evaluate("() => document.querySelectorAll('#dsh-grid .rpt-card').length")
    print(f"  卡片数: {n}")
    if n == 0:
        bad("仪表盘页面上一张卡都没有")
    else:
        for i in range(n):
            info = page.evaluate("""async (idx) => {
              const card = document.querySelectorAll('#dsh-grid .rpt-card')[idx];
              const img = card.querySelector('.rpt-cover img');
              const ph  = card.querySelector('.rpt-cover .rpt-cover-ph');
              if (img && !img.complete) {
                await new Promise((r) => { img.onload = r; img.onerror = r; setTimeout(r, 8000); });
              }
              const cs = img ? getComputedStyle(img) : null;
              const r  = img ? img.getBoundingClientRect() : null;
              return {
                title: (card.querySelector('.rpt-card-title')||{}).textContent?.trim() || '?',
                hasImg: !!img,
                natural: img ? img.naturalWidth : 0,
                src: img ? img.getAttribute('src') : '',
                display: cs ? cs.display : null,
                w: r ? Math.round(r.width) : 0,
                h: r ? Math.round(r.height) : 0,
                placeholderVisible: ph ? getComputedStyle(ph).display !== 'none' : false,
              };
            }""", i)
            t = info["title"][:24]
            if not info["hasImg"]:
                bad(f"{t}: 卡片里没有 <img>（走了占位符分支）")
            elif info["natural"] == 0:
                bad(f"{t}: <img> 的 naturalWidth=0，图没加载出来  src={info['src'][:70]}")
            elif info["display"] == "none" or info["w"] == 0 or info["h"] == 0:
                bad(f"{t}: <img> 存在但没被画出来  display={info['display']} {info['w']}x{info['h']}")
            elif info["placeholderVisible"]:
                # 这正是报告卡片注释里记的那个陷阱：占位层在 <img> 之后且绝对定位，
                # 无条件渲染就会盖在一张好图上面。
                bad(f"{t}: 占位层仍然盖在图上（NO COVER 盖住真封面）")
            else:
                ok(f"{t}: {info['natural']}px 宽，画布 {info['w']}x{info['h']}")

    # 破缓存参数：封面换了以后 URL 必须变，否则浏览器一直给旧的
    has_v = page.evaluate(
        "() => Array.from(document.querySelectorAll('#dsh-grid .rpt-cover img'))"
        ".every((i) => i.getAttribute('src').includes('?v='))")
    ok("每张封面都带 ?v= 破缓存参数") if has_v else bad("有封面没带 ?v=，换封面后会读到缓存")

    # 封面图片本身的请求要真的 200
    resp = page.evaluate("""async () => {
      const img = document.querySelector('#dsh-grid .rpt-cover img');
      if (!img) return null;
      const r = await fetch(img.getAttribute('src'), {credentials: 'same-origin'});
      return {status: r.status, type: r.headers.get('content-type')};
    }""")
    if resp and resp["status"] == 200 and (resp["type"] or "").startswith("image/"):
        ok(f"封面 URL 直接取回 {resp['status']} {resp['type']}")
    else:
        bad(f"封面 URL 取回异常: {resp}")

    page.screenshot(path="/tmp/klado-specs/dash_covers.png", full_page=False)
    b.close()

print(f"\n{'=' * 52}")
print(f"通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败项：")
    for f in failed:
        print("  -", f)
    raise SystemExit(1)
print("✅ 仪表盘封面全部通过")
