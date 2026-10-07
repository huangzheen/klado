"""The left rail's two filing actions, end to end: card menu → folder → right-click →
remove. Needs the app on :8000. Run from the app container.

The user's two asks are a MATCHED PAIR — "add it from the document menu" and "remove
it by right-clicking the row" — so they are verified in one run. Testing only the
first would pass on a rail that cannot be emptied, which is the state a user reaches
after filing one thing too many.

⚠️ **This script is self-seeding and safe to run twice.** An earlier version filed a
row, removed it, and ended with an empty rail — so the *second* run found no row to
right-click and every later step failed on a null. A verification you only get to
believe once is not a verification.
"""
#!/usr/bin/env python3
"""左侧栏的「加入 / 移出」两条路径：卡片菜单 → 选目录 → 条目出现 → 右键移出。

Needs the app on :8000. Run from the app container.

The two actions the user asked for are a MATCHED PAIR — file a document from its card
menu, then remove it from the rail by right-clicking the row — so they are verified
together, end to end. Testing only the first would pass with a rail that cannot be
emptied, which is the state a user reaches after filing one thing too many.
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# ⚠️ Overridable, same reason as verify_card_menu_position_ui: this file is written
# for the app container (:8000), and the local dev box has `AUTH_ENABLED=true` with
# no signed-in browser, so the wall is empty and every step below measures nothing.
# Point it at a throwaway `AUTH_ENABLED=false` instance to exercise it locally.
BASE = os.environ.get("KLADO_BASE", "http://127.0.0.1:8000")
SHOT = str(Path("/tmp/klado-specs/rail_ctx.png"))
Path(SHOT).parent.mkdir(parents=True, exist_ok=True)
passed, failed = [], []


def ok(m):
    passed.append(m); print("  ✓", m)


def bad(m):
    failed.append(m); print("  ✗", m)


def open_rail(page):
    """Hover the left edge and WAIT for the slide to finish.

    ⚠️ Two things this gets right that a naive version does not:

    * it waits on the measured RECT, not a sleep — the `rail-open` class applies
      immediately and the 180ms transition runs after, so a timed read sees the panel
      at -248px with the class set, which is indistinguishable from "the CSS is dead";
    * every retry LEAVES the edge first. Chrome does not re-fire `mouseenter` for a
      pointer that was already on the strip, so retrying from x=4 is a no-op and the
      function reports "the rail will not open" on a build that opens it every time.
    """
    for _ in range(5):
        page.mouse.move(1200, 500)
        page.wait_for_timeout(150)
        page.mouse.move(300, 500)
        page.wait_for_timeout(150)
        page.mouse.move(4, 500)
        try:
            page.wait_for_function(
                "() => document.getElementById('rail').getBoundingClientRect().left >= -1",
                timeout=3000)
            return True
        except Exception:
            page.mouse.move(1200, 500)
            page.wait_for_timeout(150)
    return False


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    # ── a folder must exist, or the picker has nothing to offer ──
    page.evaluate("""async () => {
      const res = await fetch('/api/rail/folders', {credentials: 'same-origin'});
      const data = await res.json();
      if (!(data.folders || []).length) {
        await fetch('/api/rail/folders', {method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: '加入测试'})});
      }
    }""")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)

    # ── 1. the row menu offers it ──
    # ⚠️ The Workspace is **项目 → 报告**, not a flat list: `showWall()` empties
    # `#wr-list` and draws the project cards instead, so a scope-only visit has zero
    # rows and every step below would measure nothing. The report rows live one
    # `openProject()` deeper, which lands on 未归档.
    #
    # ⚠️ `#rpt-grid` of `.rpt-card` with a ⋮ button has not existed since the wall
    # became `#wr-list` of `.wr-row` rows opened by right-click; this file sat dead on
    # it. The trigger is a REAL `page.mouse.click(..., button="right")` at measured
    # coordinates: `el.click()` dispatches straight at an element without hit-testing,
    # so it would pass even on a row something else is covering.
    page.evaluate("() => navTo(null, 'reports', 'Workspace')")
    page.wait_for_timeout(2500)
    page.wait_for_selector("#wr-wall .wr-proj[data-proj]", state="visible", timeout=20000)
    proj = page.evaluate("""() => {
      const c = document.querySelector('#wr-wall .wr-proj[data-proj]');
      const r = c.getBoundingClientRect();
      return {slug: c.dataset.proj, x: r.left + 40, y: r.top + r.height / 2};
    }""")
    page.mouse.click(proj["x"], proj["y"])
    page.wait_for_selector("#wr-list .wr-row[data-slug]", state="visible", timeout=20000)
    ok(f"打开项目 {proj['slug']} 后出现报告行")

    rows = page.evaluate("() => document.querySelectorAll('#wr-list .wr-row[data-slug]').length")
    if rows == 0:
        bad("项目里一行都没有")
    else:
        ok(f"项目里有 {rows} 行")
        at = page.evaluate("""() => {
          const row = document.querySelector('#wr-list .wr-row[data-slug]');
          const r = row.getBoundingClientRect();
          return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }""")
        page.mouse.click(at["x"], at["y"], button="right")
        page.wait_for_timeout(400)
        has_action = page.evaluate("""() => {
          const menu = document.getElementById('rpt-menu');
          if (!menu || menu.hidden) return {found: false, why: 'menu not open'};
          const found = Array.from(menu.querySelectorAll('[data-action]'))
            .some((b) => b.dataset.action === 'rail-add');
          if (found) { window.__slug = menu.dataset.slug; }
          return {found, slug: menu.dataset.slug,
                  actions: Array.from(menu.querySelectorAll('[data-action]'))
                             .map((b) => b.dataset.action)};
        }""")
        if has_action.get("found"):
            ok(f"文档行菜单里有「加入左侧栏」 {has_action['actions']}")
        else:
            bad(f"文档行菜单里没有「加入左侧栏」: {has_action}")

    # ── 2. it opens the folder picker ──
    picker_open = page.evaluate("""() => {
      const menu = document.getElementById('rpt-menu');
      menu.querySelector('[data-action="rail-add"]').click();
      const modal = document.getElementById('rail-picker');
      return modal.classList.contains('open')
        && document.querySelectorAll('#rail-picker-list [data-pick-folder]').length;
    }""")
    if picker_open:
        ok(f"「加入左侧栏」打开目录选择器，里面有 {picker_open} 个目录")
    else:
        bad(f"目录选择器没打开（{picker_open}）")

    # ── 3. the picker is VISIBLE, not an invisible box ──
    # ⚠️ `.rpt-modal` is `display:none` / `.open{display:flex}`, so a modal whose
    # class was set wrongly renders nothing while still swallowing clicks. The check
    # is a measured box, not the class.
    box = page.evaluate("""() => {
      const m = document.getElementById('rail-picker');
      const r = m.getBoundingClientRect();
      return {w: r.width, h: r.height, display: getComputedStyle(m).display};
    }""")
    if box["display"] == "flex" and box["w"] > 100 and box["h"] > 50:
        ok(f"选择器真的画出来了 {box['w']:.0f}×{box['h']:.0f} display={box['display']}")
    else:
        bad(f"选择器是个看不见的盒子：{box}")

    # ── 4. picking a folder files it ──
    filed = page.evaluate("""async () => {
      const row = document.querySelector('#rail-picker-list [data-pick-folder]');
      if (!row) return {ok: false, why: 'no folder row'};
      row.click();
      await new Promise((r) => setTimeout(r, 1500));
      const res = await fetch('/api/rail/items', {credentials: 'same-origin'});
      const data = await res.json();
      return {ok: true, count: (data.items || []).length,
              first: (data.items || [])[0] || null,
              modalClosed: !document.getElementById('rail-picker').classList.contains('open')};
    }""")
    if filed.get("ok"):
        first = filed.get("first")
        # ⚠️ "There is at least one item" and not "there is one MORE": re-running the
        # script files a card that is already filed, and the store treats that as a
        # no-op (a drag that ends where it started must not look like a failure).
        # Asserting a count increase would make the script fail on a correct build
        # the second time it runs.
        if filed.get("count", 0) > 0:
            ok(f"归档成功：{first['item_type']}/{first['item_slug']}"
               f" → 「{first['title'] or first['item_slug']}」（栏里共 {filed['count']} 条）")
        else:
            bad(f"点了目录却什么都没归档: {filed}")
        if filed.get("modalClosed"):
            ok("归档后选择器自动关闭")
        else:
            bad("归档后选择器还开着")
    else:
        bad(f"归档失败: {filed}")

    # ── 5. the row shows in the rail, expanded ──
    # ⚠️ Close the picker FIRST. `.rpt-modal` is a full-screen scrim at z-index 8100,
    # so while it is open the left edge belongs to the dialog and the rail's hot zone
    # cannot be reached. That is correct behaviour, not a bug — and it is exactly why
    # a test that hovers the edge without dismissing the dialog reports "the rail
    # will not open" and sends somebody hunting for a z-index problem that is not
    # there.
    page.evaluate("() => window.railPageClosePicker()")
    page.wait_for_timeout(400)
    if open_rail(page):
        ok("左侧栏打开了")
        # ⚠️ EXPAND, never toggle. The picker already expanded the folder it filed
        # into, so clicking it again COLLAPSES it — and the row then looks missing on a
        # build that is working exactly as designed. The bug this hides is "the row is
        # not there", which sends somebody to the store instead of to their own
        # assertion.
        page.evaluate("""() => {
          document.querySelectorAll('[data-drop-folder]').forEach((f) => {
            if (!f.classList.contains('open')) f.click();
          });
        }""")
        page.wait_for_timeout(700)
        rows = page.evaluate("() => document.querySelectorAll('.rail-folder-item[data-rail-item]').length")
        if rows > 0:
            ok(f"左侧栏里有 {rows} 条已归档条目")
        else:
            bad("左侧栏打开了却看不到已归档的条目")
    else:
        bad("左侧栏打不开")

    # ── 6. right-click opens the menu ──
    # ⚠️ Self-seeding. A previous run of this script ends with the rail EMPTY (step 8
    # removes the row it filed), so a re-run finds no row to right-click and every
    # later step fails on a null. A verification that cannot run twice is a
    # verification you only get to believe once.
    page.evaluate("""async () => {
      const res = await fetch('/api/rail/items', {credentials: 'same-origin'});
      const data = await res.json();
      if ((data.items || []).length) return;
      const folders = await (await fetch('/api/rail/folders',
        {credentials: 'same-origin'})).json();
      if (!(folders.folders || []).length) return;
      await fetch('/api/rail/folders/' + folders.folders[0].id + '/items', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item_type: 'dashboard', item_slug: 'overview-board'})});
      await window.railPage.load();
    }""")
    open_rail(page)
    page.evaluate("""() => {
      document.querySelectorAll('[data-drop-folder]').forEach((f) => {
        if (!f.classList.contains('open')) f.click();
      });
    }""")
    page.wait_for_timeout(700)

    menu_shown = page.evaluate("""() => {
      const row = document.querySelector('.rail-folder-item[data-rail-item]');
      if (!row) return {ok: false, why: 'no row'};
      const r = row.getBoundingClientRect();
      row.dispatchEvent(new MouseEvent('contextmenu', {
        clientX: r.left + 20, clientY: r.top + 8, bubbles: true, cancelable: true}));
      const menu = document.getElementById('rail-ctx');
      return {ok: true, hidden: menu.hidden,
              actions: Array.from(menu.querySelectorAll('[data-ctx]')).map((x) => x.dataset.ctx),
              itemId: menu.dataset.itemId};
    }""")
    if menu_shown.get("ok") and not menu_shown.get("hidden"):
        ok(f"右键弹出菜单，动作：{menu_shown['actions']}")
        if "unfile" in menu_shown["actions"]:
            ok("菜单里有「移出目录」")
        else:
            bad("菜单里没有「移出目录」")
    else:
        bad(f"右键没弹出菜单: {menu_shown}")

    # ── 7. the menu is a REAL visible box ──
    ctx_box = page.evaluate("""() => {
      const m = document.getElementById('rail-ctx');
      const r = m.getBoundingClientRect();
      return {w: r.width, h: r.height, display: getComputedStyle(m).display,
              left: r.left, top: r.top};
    }""")
    if ctx_box["display"] != "none" and ctx_box["w"] > 80 and ctx_box["h"] > 40:
        ok(f"菜单画出来了 {ctx_box['w']:.0f}×{ctx_box['h']:.0f} @ ({ctx_box['left']:.0f},{ctx_box['top']:.0f})")
    else:
        bad(f"菜单是看不见的盒子：{ctx_box}")

    # ── 8. 「移出目录」真的移出 ──
    # ⚠️ "gone from the rail" is the assertion, not "the rail is empty". The seeding
    # step may have added a row this run did not file, and demanding 0 would make the
    # script fail on a correct build for the wrong reason.
    removed = page.evaluate("""async () => {
      const menu = document.getElementById('rail-ctx');
      const before = document.querySelectorAll('.rail-folder-item[data-rail-item]').length;
      menu.querySelector('[data-ctx="unfile"]').click();
      await new Promise((r) => setTimeout(r, 1500));
      const res = await fetch('/api/rail/items', {credentials: 'same-origin'});
      const data = await res.json();
      return {before, after: (data.items || []).length,
              rows: document.querySelectorAll('.rail-folder-item[data-rail-item]').length};
    }""")
    if removed["after"] < removed["before"] and removed["rows"] < removed["before"]:
        ok(f"「移出目录」生效：{removed['before']} 条 → {removed['rows']} 条，条目从栏里消失")
    else:
        bad(f"移出没生效：{removed}")

    # ── 9. the file itself is untouched ──
    # ⚠️ The whole reason a folder is a virtual tag: removing it from the rail must
    # not delete the document. If this fails, the design is wrong, not the test.
    # ⚠️ `/api/reports` returns an ENVELOPE here, not a bare array — `len()` on the
    # envelope is the number of KEYS, which is 2 for an empty list and therefore
    # "documents survived" for a report that deleted everything.
    survivors = page.evaluate("""async () => {
      const res = await fetch('/api/reports?scope=mine', {credentials: 'same-origin'});
      const data = await res.json();
      const rows = Array.isArray(data) ? data
                 : (Array.isArray(data.reports) ? data.reports
                 : (Array.isArray(data.items) ? data.items : []));
      return {count: rows.length, shape: Array.isArray(data) ? 'array' : Object.keys(data)};
    }""")
    if survivors["count"] > 0:
        ok(f"移出后文档本身还在（{survivors['count']} 份，响应形状 {survivors['shape']}）"
           f" —— 目录是虚拟标签，不动文件")
    else:
        bad(f"移出把文档也弄没了（响应形状 {survivors['shape']}，0 份）"
            f" —— 目录不应该是虚拟标签？")

    # ── 10. folder header right-click ──
    folder_menu = page.evaluate("""() => {
      const f = document.querySelector('.rail-folder');
      if (!f) return {ok: false, why: 'no folder'};
      const r = f.getBoundingClientRect();
      f.dispatchEvent(new MouseEvent('contextmenu', {
        clientX: r.left + 20, clientY: r.top + 8, bubbles: true, cancelable: true}));
      const menu = document.getElementById('rail-ctx');
      return {ok: true, hidden: menu.hidden,
              actions: Array.from(menu.querySelectorAll('[data-ctx]')).map((x) => x.dataset.ctx)};
    }""")
    if folder_menu.get("ok") and not folder_menu.get("hidden"):
        ok(f"目录标题右键也有菜单：{folder_menu['actions']}")
    else:
        bad(f"目录标题右键没反应: {folder_menu}")

    # ── 11. Esc closes the popover without yanking the rail ──
    esc = page.evaluate("""async () => {
      const f = document.querySelector('.rail-folder');
      const r = f.getBoundingClientRect();
      f.dispatchEvent(new MouseEvent('contextmenu', {
        clientX: r.left + 20, clientY: r.top + 8, bubbles: true, cancelable: true}));
      const wasOpen = !document.getElementById('rail-ctx').hidden;
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      await new Promise((r) => setTimeout(r, 200));
      return {wasOpen, menuClosed: document.getElementById('rail-ctx').hidden,
              railStillOpen: document.body.classList.contains('rail-open')};
    }""")
    if esc["wasOpen"] and esc["menuClosed"] and esc["railStillOpen"]:
        ok("Esc 只关菜单，左侧栏留在原地")
    else:
        bad(f"Esc 的行为不对：{esc}")

    # ── 12. no page errors along the way ──
    if errors:
        bad(f"页面报了 {len(errors)} 个 JS 错误：{errors[:2]}")
    else:
        ok("全程没有 JS 错误")

    page.screenshot(path=SHOT)
    b.close()

print(f"\n{'=' * 52}")
print(f"通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败项：")
    for f in failed:
        print("  -", f)
    sys.exit(1)
print("✅ 加入 / 移出 全部通过")
