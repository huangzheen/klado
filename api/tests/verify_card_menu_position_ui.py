"""The card menu must open where you RIGHT-CLICKED, on that card, inside the window.

Needs the app up on :8000. Run from inside the app container.

The old bug: the handler is DELEGATED on `#dsh-grid`, so `ev.currentTarget` is
the grid, not the card. Measuring it put every card's menu at the grid's own
left/bottom — which looks like "the menu appears in the wrong place" for every
card at once, and is not a function of which card you right-clicked.

The trigger moved too: the ⋮ button is gone and right-click is the only way in,
so a `contextmenu` event on a card is what this drives. A `.click()` here would
open the VIEWER and every measurement would be of the wrong thing.

Each card is measured against the pointer that opened it, and the last card is
checked against the viewport edge, because a menu that is merely close to its
trigger is a menu that has already failed on a narrower window.
"""
#!/usr/bin/env python3
"""仪表盘卡片右键菜单定位的回归：右键每一张卡，菜单都要落在鼠标处。

旧 bug：菜单处理器是**委托在 #dsh-grid 上的**，而定位用的是
`ev.currentTarget.getBoundingClientRect()` —— 委托处理器里 currentTarget 是
grid 本身，不是那张卡，于是每张卡的菜单都落在 grid 的左下角。
"""
import os

from playwright.sync_api import sync_playwright

# ⚠️ Overridable, because this file is written for the app container (:8000) and the
# local dev box has `AUTH_ENABLED=true` with no signed-in browser — there the wall
# renders zero cards and every assertion below would measure nothing. Point it at a
# throwaway `AUTH_ENABLED=false` instance (identity falls back to AUTH_ADMIN_EMAILS)
# to exercise it outside the container. Default is unchanged.
BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
passed, failed = [], []


def _open_dashboard(page):
    """Get to the Dashboard page. The tab is in the LEFT RAIL, not the top bar."""
    page.evaluate("() => navTo(null, 'dashboard', 'Dashboard')")
    page.wait_for_timeout(2500)


def ok(m):  passed.append(m); print("  ✓", m)
def bad(m): failed.append(m); print("  ✗", m)


# Dispatched inside the page: a synthetic `contextmenu` with a real pointer position.
RIGHT_CLICK = """(card, fx, fy) => {
  const r = card.getBoundingClientRect();
  const x = r.left + r.width * fx;
  const y = r.top + r.height * fy;
  card.dispatchEvent(new MouseEvent('contextmenu',
    {bubbles: true, cancelable: true, clientX: x, clientY: y}));
  return {x, y};
}"""


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)
    _open_dashboard(page)
    page.wait_for_timeout(2500)

    n = page.evaluate("() => document.querySelectorAll('#dsh-grid .rpt-card').length")
    print(f"  卡片数: {n}")
    if not n:
        # A traceback on `cards[cards.length-1].getBoundingClientRect()` reads as a bug
        # in this file. It is not: an empty wall means the page never loaded cards
        # (no session, no data), and every check below would then be measuring a
        # default layout.
        bad("仪表盘墙是空的——没有卡片可测（未登录 / 没有数据？）")
        print("\n" + "=" * 60)
        print("菜单定位回归：通过 0  失败 1")
        print("  ✗ 仪表盘墙是空的——没有卡片可测（未登录 / 没有数据？）")
        b.close()
        raise SystemExit(1)
    for i in range(n):
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_timeout(2000)
        _open_dashboard(page)
        page.wait_for_timeout(2000)
        r = page.evaluate("""([idx, right]) => {
          const card = document.querySelectorAll('#dsh-grid .rpt-card')[idx];
          const at = eval(right)(card, 0.5, 0.5);
          const m = document.getElementById('dsh-menu');
          const mr = m.getBoundingClientRect();
          return {title: (card.querySelector('h3,.rpt-card-title')||{}).textContent?.trim().slice(0,22),
                  at, menu: {x: mr.left, y: mr.top, w: mr.width, h: mr.height},
                  hidden: m.hidden, count: m.querySelectorAll('button').length};
        }""", [i, RIGHT_CLICK])
        if r["hidden"]:
            bad(f"卡 {i+1}: 右键没打开菜单")
            continue
        if not r["count"]:
            bad(f"卡 {i+1}: 菜单打开了但一个选项都没有")
            continue
        # ⚠️ Two legal positions, not one. Near the right edge the menu FLIPS to the
        # left of the pointer instead of running off the window — that is the correct
        # behaviour and there is a separate check that it stays inside the viewport.
        # The old check allowed dx in [-20, 400], a window wide enough to swallow the
        # flip as well as the real position, so it could not tell the two apart.
        flipped = r["at"]["x"] + r["menu"]["w"] + 8 > 1600
        want_x = r["at"]["x"] - r["menu"]["w"] - 4 if flipped else r["at"]["x"]
        dx = r["menu"]["x"] - want_x
        dy = r["menu"]["y"] - r["at"]["y"]
        at_pointer = abs(dx) <= 3 and abs(dy) <= 3
        # 菜单整体不能超出视口
        inside = (0 <= r["menu"]["x"] and r["menu"]["x"] + r["menu"]["w"] <= 1600
                  and 0 <= r["menu"]["y"] and r["menu"]["y"] + r["menu"]["h"] <= 1000)
        where = "（已翻边）" if flipped else ""
        if at_pointer and inside:
            ok(f"卡 {i+1}「{r['title']}」右键({int(r['at']['x'])},{int(r['at']['y'])}) "
               f"→ 菜单({int(r['menu']['x'])},{int(r['menu']['y'])}){where}  Δ({int(dx)},{int(dy)})")
        else:
            bad(f"卡 {i+1}「{r['title']}」菜单在 {int(r['menu']['x'])},{int(r['menu']['y'])}，"
                f"应在 {int(want_x)},{int(r['at']['y'])}{where}（Δ {int(dx)},{int(dy)}）"
                f"{'，且超出视口' if not inside else ''}")

    # 靠近右边缘的卡片：菜单应翻到鼠标左侧，而不是被夹到视口外
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(2000)
    _open_dashboard(page)
    page.wait_for_timeout(2000)
    r = page.evaluate("""(right) => {
      const cards = [...document.querySelectorAll('#dsh-grid .rpt-card')];
      const last = cards[cards.length-1];
      const at = eval(right)(last, 0.95, 0.5);
      const mr = document.getElementById('dsh-menu').getBoundingClientRect();
      return {px: at.x, mx: mr.left, mw: mr.width, pw: last.getBoundingClientRect().width};
    }""", RIGHT_CLICK)
    if r["mx"] >= 0 and r["mx"] + r["mw"] <= 1600:
        ok(f"最后一张卡右键菜单右边缘 {int(r['mx']+r['mw'])} ≤ 视口 1600")
    else:
        bad(f"菜单超出右边界：{int(r['mx'])}..{int(r['mx']+r['mw'])}")
    b.close()

print("\n" + "=" * 60)
print(f"菜单定位回归：通过 {len(passed)}  失败 {len(failed)}")
for f in failed:
    print("  ✗", f)
