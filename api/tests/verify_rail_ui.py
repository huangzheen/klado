"""The left rail, driven by a real browser against a real server.

Needs the app up on :8000 with a signed-in session (`AUTH_ENABLED=false` is
fine — the implicit local owner is a real identity). Run from inside the app
container, which is where Playwright and the browsers live:

    docker compose exec -T app sh -c 'cd /app/api/tests && python verify_rail_ui.py'

Every assertion here measures the SCREEN — `getBoundingClientRect()`,
`getComputedStyle`, DOM counts. None of it can be checked statically, and the
reasons are specific:

* a drawer that is translated off-screen has no hover region, so a rail opened
  by `:hover` on itself can never be reached (that is why there is a hot zone);
* `body.rail-open` can be set while the 180ms transition is still running, and a
  `getBoundingClientRect()` read mid-transition reports the panel at -248px with
  the class applied — which looks exactly like "the CSS does not work";
* the rail renders its module rows from the registry and its dashboards from a
  SEPARATE fetch, so it can be complete-looking and still be missing one of them.
"""
#!/usr/bin/env python3
"""左侧栏的端到端回归：hover 唤出、模块短标题、目录树、拖拽归档。

判据全部是「屏幕上真的发生了什么」——量 `getBoundingClientRect()`、读
`getComputedStyle`、数 DOM 里的行。静态断言看不出「面板停在屏幕外」这类问题，
而这正是它最容易出的问题（元素 translateX(-100%) 后没有 hover 区域）。
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
passed, failed = [], []


def ok(m):
    passed.append(m); print("  ✓", m)


def bad(m):
    failed.append(m); print("  ✗", m)


def rail_box(page):
    return page.evaluate("""() => {
      const r = document.getElementById('rail');
      if (!r) return null;
      const b = r.getBoundingClientRect();
      return {left: b.left, right: b.right, width: b.width, top: b.top, bottom: b.bottom,
              open: document.body.classList.contains('rail-open'),
              display: getComputedStyle(r).display};
    }""")


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)

    # ── 1. hidden by default ──
    box = rail_box(page)
    if box is None:
        bad("左侧栏根本不在 DOM 里")
    elif box["display"] == "none":
        bad("左侧栏被 display:none 藏起来了（应当是滑出视口，不是隐藏元素）")
    elif box["right"] <= 0:
        ok(f"默认收起：右边缘 {box['right']:.0f} ≤ 0，完全在视口外")
    else:
        bad(f"默认就是展开的：右边缘 {box['right']:.0f} > 0")

    # ── 2. hover the hot zone opens it ──
    # ⚠️ Move through a real point on the strip, not `.hover()` on the rail: the rail
    # is off-screen so it has no hover region — that is the bug the hot zone exists
    # to work around, and asserting on it would test the workaround away.
    # ⚠️ TWO moves, from two different places. A single `mouse.move` teleports the
    # pointer, and Chrome does not synthesise the `mouseenter` a teleport would have
    # caused — so one move tests nothing. The path across the window is what a user's
    # pointer actually does.
    # ⚠️ Retried a few times. The very first pointer move after a fresh page load can
    # land before the rail's listeners are bound, and Chrome does not re-fire
    # `mouseenter` for a pointer that never left the strip — so one attempt measures
    # the boot race, not the behaviour. The rail has no such problem in use (a real
    # pointer keeps moving), and a flaky assertion here would train everyone to
    # ignore this line.
    box = None
    for _ in range(6):
        page.mouse.move(800, 500)
        page.wait_for_timeout(150)
        page.mouse.move(200, 500)
        page.wait_for_timeout(120)
        page.mouse.move(4, 500)
        # ⚠️ Wait for the TRANSITION, not a fixed delay. The slide is 180ms and the
        # measurement is a `getBoundingClientRect()` — reading it mid-transition
        # reports the panel still at `translateX(-100%)` with `rail-open` already set,
        # which reads as "the class applies but the CSS does not". It is the one
        # failure shape in this file that is purely a measurement artefact.
        try:
            page.wait_for_function(
                "() => { const r = document.getElementById('rail');"
                " return r && r.getBoundingClientRect().left >= -1; }",
                timeout=3000)
        except Exception:
            pass                      # try the next approach to the edge
        box = rail_box(page)
        if box and box["open"]:
            break
    if box and box["open"] and box["left"] >= -1:
        ok(f"鼠标移到左边缘后滑出：left={box['left']:.0f} body.rail-open={box['open']}")
    else:
        bad(f"移到左边缘没滑出：{box}")

    # ── 3. it carries the modules ──
    mods = page.evaluate("""() => Array.from(
        document.querySelectorAll('#rail-modules .rail-mod'))
        .map((e) => e.dataset.railMod)""")
    print(f"  rail 模块: {mods}")
    for want in ("datacenter", "workspace", "dashboard", "knowledge", "calendar"):
        if want in mods:
            ok(f"rail 里有 {want}")
        else:
            bad(f"rail 里没有 {want}")

    # ── 4. the rail is a SECOND way in, never a replacement ──
    # ⚠️ This used to assert the opposite ("Dashboard 还在顶部导航栏里" = fail). It was
    # correct when a `rail_only` flag kept the module out of the top bar, and the user
    # then read the missing tab as the module having been deleted. The rail is additive:
    # a module in the rail is expected to be in the top bar too.
    nav_keys = page.evaluate("""() => Array.from(
        document.querySelectorAll('#nav-center .nav-tab'))
        .filter((e) => !e.hidden)
        .map((e) => e.getAttribute('data-nav-key'))""")
    print(f"  顶栏: {nav_keys}")
    if "dashboard" in nav_keys:
        ok("Dashboard 同时在顶部导航栏里（两个入口，不是搬家）")
    else:
        bad("Dashboard 从顶栏里消失了 —— 顶栏是模块地图，少一个标签就等于模块没了")
    promoted = page.evaluate(
        "() => document.querySelectorAll('#nav-center [data-dash-slug]').length")
    if promoted == 0:
        ok("顶栏没有任何被提升的仪表盘标签")
    else:
        bad(f"顶栏还有 {promoted} 个仪表盘标签")

    # ── 5. the dashboards themselves are listed in the rail ──
    dashes = page.evaluate("""() => Array.from(
        document.querySelectorAll('#rail-modules [data-rail-dash]'))
        .map((e) => e.dataset.railDash)""")
    print(f"  rail 里的仪表盘: {dashes}")
    if len(dashes) >= 3:
        ok(f"rail 里列出了 {len(dashes)} 个仪表盘")
    else:
        bad(f"rail 里只列出 {len(dashes)} 个仪表盘（发布时标了 in_nav 的应该有 3 个）")

    # ── 6. a folder appears, and holds a filed item ──
    # ⚠️ The id comes back and EVERY later step addresses THIS folder by id. Two
    # reasons, and the second one is the reason that matters: `[data-drop-folder]`
    # alone resolves to the FIRST folder row, which in a real deployment is the
    # user's own — so a regression run used to file a dashboard into somebody's
    # 「季度复盘」. A test that writes into the operator's data is not a test.
    folder_id = page.evaluate("""async () => {
      const res = await fetch('/api/rail/folders', {method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: '回归测试目录'})});
      const data = await res.json();
      return data.id;
    }""")
    # Not an early return: the steps below address the folder by id and each one
    # reports its own failure ('no folder row'), which says more than a bail-out here.
    if not folder_id:
        bad("建不出测试目录，后面几步都测不到")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)
    page.mouse.move(800, 500); page.wait_for_timeout(150)
    page.mouse.move(200, 500); page.wait_for_timeout(150)
    page.mouse.move(4, 500)
    page.wait_for_timeout(600)
    folders = page.evaluate("""() => Array.from(
        document.querySelectorAll('#rail-modules, #rail-folders'))
        .map((e) => e.textContent).join(' ')""")
    if "回归测试目录" in folders:
        ok("目录出现在 rail 里")
    else:
        bad(f"目录没出现在 rail 里（看到的是 {folders[:120]!r}）")

    # ── 7. drag a card onto a folder ──
    # ⚠️ HTML5 drag-and-drop is NOT reproducible with synthetic mouse events in
    # Playwright — the browser only starts a drag from a real input sequence. So the
    # drop is driven through the app's own handler with a synthetic DataTransfer,
    # which is the same object the browser would hand it.
    page.query_selector('[data-nav-key="dashboard"]')
    page.evaluate("() => document.querySelector('[data-nav-key=\"dashboard\"]')")
    # open the dashboard page through the rail
    page.evaluate("() => window.railPage && document.querySelector('[data-rail-mod=\"dashboard\"]').click()")
    page.wait_for_timeout(2500)
    cards = page.evaluate("() => document.querySelectorAll('#dsh-grid .rpt-card[draggable=\"true\"]').length")
    print(f"  可拖拽的仪表盘卡片: {cards}")
    if cards > 0:
        ok(f"{cards} 张仪表盘卡片可拖拽")
    else:
        bad("仪表盘卡片不可拖拽（缺 draggable）")

    dropped = page.evaluate("""async (fid) => {
      const card = document.querySelector('#dsh-grid .rpt-card[data-rail-type]');
      if (!card) return {ok: false, why: 'no card'};
      const dt = new DataTransfer();
      dt.setData('text/plain', JSON.stringify({
        item_type: card.dataset.railType, item_slug: card.dataset.railSlug, id: 0}));
      const target = document.querySelector('[data-drop-folder="' + fid + '"]');
      if (!target) return {ok: false, why: 'no folder row'};
      target.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true,
                                                      cancelable: true}));
      target.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true,
                                                  cancelable: true}));
      await new Promise((r) => setTimeout(r, 1500));
      const res = await fetch('/api/rail/items', {credentials: 'same-origin'});
      const data = await res.json();
      // ⚠️ Count only THIS folder's items. Counting every item on the rail passes on
      // a drop that did nothing at all as long as the account owns anything else.
      const mine = (data.items || []).filter((it) => it.folder_id === fid);
      return {ok: true, count: mine.length, first: mine[0] || null};
    }""", folder_id)
    if dropped.get("ok") and dropped.get("count", 0) > 0:
        first = dropped["first"]
        ok(f"拖拽归档成功：{first['item_type']}/{first['item_slug']} → "
           f"「{first['title'] or first['item_slug']}」missing={first['missing']}")
        if not first["missing"]:
            ok("归档后标题解析出来了（真的连到了那张卡）")
        else:
            bad("归档成功但标题解析不出来（missing=true）")
    else:
        bad(f"拖拽归档失败: {dropped}")

    # ── 8. the filed item shows under the folder, expanded ──
    page.mouse.move(800, 500); page.wait_for_timeout(150)
    page.mouse.move(200, 500); page.wait_for_timeout(150)
    page.mouse.move(4, 500)
    page.wait_for_timeout(600)
    page.evaluate("""(fid) => {
      const f = document.querySelector('[data-drop-folder="' + fid + '"]');
      if (f) f.click();
    }""", folder_id)
    page.wait_for_timeout(800)
    filed = page.evaluate("() => document.querySelectorAll('.rail-folder-item').length")
    if filed > 0:
        ok(f"展开目录后能看到 {filed} 条已归档条目")
    else:
        bad("展开目录后看不到已归档条目")

    # ── 9. the module short title can be renamed ──
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)
    page.mouse.move(800, 500); page.wait_for_timeout(150)
    page.mouse.move(200, 500); page.wait_for_timeout(150)
    page.mouse.move(4, 500)
    page.wait_for_timeout(600)
    before = page.evaluate("""() => {
      const e = document.querySelector('[data-rail-mod="calendar"] .rail-mod-name');
      return e ? e.textContent.trim() : null;
    }""")
    renamed = page.evaluate("""async () => {
      await fetch('/api/rail/module-labels', {method: 'PUT', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({labels: {calendar: {label_zh: '日程', label_en: 'Events'}}})});
      await window.railPage.load();
      const e = document.querySelector('[data-rail-mod="calendar"] .rail-mod-name');
      return e ? e.textContent.trim() : null;
    }""")
    # ⚠️ The expected value follows the APP's language, not the test author's. The
    # page is in English here, so `label_zh: "日程"` correctly does nothing and
    # `label_en: "Events"` is what must appear — asserting the Chinese side would fail
    # on a working rename and pass on a broken one.
    want = "Events" if page.evaluate("() => kladoI18n.lang()") == "en" else "日程"
    if renamed == want:
        ok(f"模块短标题 rename 生效：{before} → {renamed}")
    else:
        bad(f"模块短标题没生效：{before} → {renamed}（期望 {want}）")
    page.evaluate("""async () => {
      await fetch('/api/rail/module-labels', {method: 'PUT', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({labels: {calendar: {label_zh: '', label_en: ''}}})});
      await window.railPage.load();
    }""")

    # ── 10. the rail follows the page ──
    page.evaluate("() => navTo(null, 'calendar', 'Calendar')")
    page.wait_for_timeout(900)
    lit = page.evaluate("""() => {
      const on = document.querySelector('.rail-mod.on');
      return on ? on.dataset.railMod : null;
    }""")
    if lit == "calendar":
        ok("切到日历后 rail 高亮跟着走")
    else:
        bad(f"切到日历后 rail 高亮的是 {lit}")

    # ── 11. moving the pointer away closes it ──
    # ⚠️ Wait for the CLOSE, not a fixed sleep. The rail's close is deliberately
    # DELAYED (120ms) so a pointer that dips out and comes straight back does not
    # flicker it — see the `.rail` rules in app.css. A 500ms read happens to clear
    # that today, but a fixed sleep is a race with a timer whose value is a product
    # decision, and the day somebody lowers it this fails on correct code.
    page.mouse.move(800, 500)
    try:
        page.wait_for_function(
            "() => !document.body.classList.contains('rail-open')", timeout=4000)
        ok("鼠标移开后自动收回")
    except Exception:
        box = rail_box(page)
        bad(f"鼠标移开后没收回：open={box and box['open']}")

    page.screenshot(path="/tmp/klado-specs/rail.png", full_page=False)

    # ── 12. clean up after itself ──
    # ⚠️ This script creates a folder and files a card into it, so without this it
    # leaks one 「回归测试目录」 per run. Measured: four of them had piled up in the
    # deployment, all identical — the only trace of a test suite is garbage a person
    # has to recognise and delete by hand.
    page.evaluate("""async (fid) => {
      const items = await (await fetch('/api/rail/items',
        {credentials: 'same-origin'})).json();
      for (const it of (items.items || []).filter((x) => x.folder_id === fid)) {
        await fetch('/api/rail/items/' + it.id, {method: 'DELETE',
          credentials: 'same-origin'});
      }
      await fetch('/api/rail/folders/' + fid, {method: 'DELETE',
        credentials: 'same-origin'});
    }""", folder_id)
    left = page.evaluate("""async () => {
      const d = await (await fetch('/api/rail/folders',
        {credentials: 'same-origin'})).json();
      return (d.folders || []).filter((f) => f.name === '回归测试目录').length;
    }""")
    if left == 0:
        ok("跑完把自己建的目录清掉了，不留残留")
    else:
        bad(f"跑完还剩 {left} 个测试目录没清掉")
    b.close()

print(f"\n{'=' * 52}")
print(f"通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败项：")
    for f in failed:
        print("  -", f)
    sys.exit(1)
print("✅ 左侧栏全部通过")
