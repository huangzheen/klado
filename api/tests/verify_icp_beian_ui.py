"""ICP 备案号 —— 未登录时主页底部必须显示，且链接工信部备案系统。

这不是「页面上有个字符串」的检查，是**合规检查**：备案号是要给管局和公众看的，
所以判据是「打开 klado.team、还没登录、屏幕上真的看得见、且在底部居中、点得动」。

依据：
  · 《非经营性互联网信息服务备案管理办法》第十三条：网站开通时应当在**主页底部的
    中央位置**标明备案编号，并在备案编号下方链接工信部备案管理系统。第二十五条：
    未链接的责令限期改正，逾期处 5000~10000 元罚款。
  · 《互联网信息服务管理办法》第十二条：主页**显著位置**未标明备案编号的，
    责令改正处 5000~50000 元罚款。

匿名访问 klado.team 看到的就是这张登录闸门，所以「主页底部中央」落在
`.auth-gate-card` 内的 `.auth-beian`。它必须**独立于 `.auth-foot`** ——
后者被 `foot.textContent = bits.join(' ')` 整体覆盖（放运行期提示）。

⚠️ 这个套件自己踩过的坑：只断言「文案存在」会漏掉两种真实故障 ——
① 元素在 DOM 里但被 CSS 隐藏（那不叫「显示」），
② 整个 auth-gate 根本没渲染（那样「找不到备案号」和「页面是空��」输出完全一样）。
所以每条否定断言都配一条**正对照**先证明闸门真的出来了。

静态 server + API stub，不需要后端，不写任何东西：

    .venv312/bin/python api/tests/verify_icp_beian_ui.py
"""
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = str(Path(__file__).resolve().parents[2] / "frontend" / "out")
PORT = 8849

# 与阿里云备案系统里的「网站备案号」字段逐字一致。改了这里就要同步改
# index.html 里 `.auth-beian` 的文案 —— 错位会让下面第 3 条打红。
EXPECTED = "苏ICP备2026056508号-2"
BEIAN_HOME = "beian.miit.gov.cn"

FAILURES = []

# 未登录态：`/api/auth/me` 必须真的返 401，闸门才会出现。返回 200 会让整套件
# 测成「已登录的应用界面」，下面所有断言都会失去意义。
STUB = """
window.fetch = function (url) {
  const u = String(url);
  let status = 200, body = {items: [], events: [], reports: [], count: 0,
    categories: [], folders: [], files: [], datasets: []};
  if (u.indexOf('/auth/me') >= 0) {
    status = 401; body = {detail: 'Not authenticated'};
  } else if (u.indexOf('/auth/health') >= 0) {
    body = {enabled: true, mode: 'self', allowed_domain: 'klado.team',
            mail: 'unconfigured'};
  }
  return Promise.resolve({ok: status < 400, status: status,
                          json: () => Promise.resolve(body)});
};
"""


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


def pe(page, expr):
    """Eval in the page, returning None on any error.

    页面里 `null.getBoundingClientRect()` 这类 TypeError 会把整个 run 打死，
    后面一条断言都不报。缺失/异常是断言失败，不是脚本崩溃。
    """
    try:
        return page.evaluate(expr)
    except Exception:
        return None


def q(page, selector, expr="el => el.innerText.trim()"):
    """Eval on a selector that may not exist.

    元素缺失时返回 None 而不是抛异常。抛异常会让整个 run 死在这一行，
    后面所有断言**一条都不报告** —— 一处坏掉就看不到其余结论了，
    读起来像「测得更多」，实际是它自己先崩了。缺失是断言失败，不是脚本崩溃。
    """
    try:
        return page.eval_on_selector(selector, expr)
    except Exception:
        return None


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    httpd = _Server(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def boot(browser, lang=None):
    page = browser.new_page(viewport={"width": 1500, "height": 900})
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    if lang:
        page.add_init_script(
            "try{localStorage.setItem('klado-lang','%s')}catch(e){}" % lang)
    page.add_init_script(STUB)
    page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="domcontentloaded")
    # 不在这里等 #auth-beian：元素缺失要作为断言失败报出来，不是让 boot 抛超时。
    page.wait_for_function("() => !!window.kladoI18n", timeout=15000)
    page.wait_for_function("() => window.kladoI18n")
    return page, errors


def main() -> int:
    httpd = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page, errors = boot(browser)

            # ── 0. 正对照：闸门确实渲染出来了 ────────────────────────────
            # 少了这条，下面「找不到备案号」就和「页面整个没出来」同形。
            gate_visible = pe(page, 
                "() => { const g = document.querySelector('.auth-gate');"
                "  if (!g) return null;"
                "  const cs = getComputedStyle(g);"
                "  const r = g.getBoundingClientRect();"
                "  return {display: cs.display, h: r.height}; }")
            check("正对照：auth-gate 存在且有实际高度",
                  gate_visible and gate_visible["h"] > 100, gate_visible)
            check("正对照：登录表单在屏（auth-field 至少 1 个）",
                  page.locator(".auth-field").count() >= 1,
                  page.locator(".auth-field").count())

            # ── 1. 文案与备案系统逐字一致 ─────────────────────────────────
            txt = q(page, "#auth-beian")
            check("备案号文案与备案系统逐字一致",
                  txt == EXPECTED, repr(txt))

            # ── 2. 真的「显示」了，不只是在 DOM 里 ────────────────────────
            vis = pe(page, 
                "() => { const el = document.querySelector('#auth-beian');"
                "  if (!el) return null;"
                "  const cs = getComputedStyle(el);"
                "  const r = el.getBoundingClientRect();"
                "  return {display: cs.display, vis: cs.visibility, h: r.height,"
                "          w: r.width, top: r.top, left: r.left,"
                "          mid: r.left + r.width / 2, cx: window.innerWidth / 2}; }")
            check("元素 display 不是 none", vis and vis["display"] != "none", vis)
            check("元素 visibility 不是 hidden", vis and vis["vis"] != "hidden", vis)
            check("渲染高度 > 0（真在屏幕上，不是零高）", vis and vis["h"] > 0, vis)
            check("宽度 > 0", vis and vis["w"] > 0, vis)

            # ── 3. 法规要的是「底部中央」，位置要真的对 ────────────────────
            form_bottom = pe(page, 
                "() => { const f = document.querySelector('.auth-field');"
                "  if (!f) return null;"
                "  return f.getBoundingClientRect().bottom; }")
            check("排在登录表单下方（bottom > 表单底边）",
                  vis and form_bottom and vis["top"] >= form_bottom,
                  "beian.top=%s form.bottom=%s" % (vis and vis["top"], form_bottom))
            check("水平居中（|中心 - 视口中心| < 4px）",
                  vis and abs(vis["mid"] - vis["cx"]) < 4,
                  vis and "delta=%.1f" % abs(vis["mid"] - vis["cx"]))
            card = pe(page, 
                "() => { const c = document.querySelector('.auth-gate-card');"
                "  if (!c) return null; const r = c.getBoundingClientRect();"
                "  return {top: r.top, bottom: r.bottom}; }")
            check("在卡片底部（贴近卡片底边，不是顶部）",
                  card and vis and abs((card["bottom"] - vis["h"]) - vis["top"]) < 40,
                  "card.bottom=%s" % (card and card["bottom"]))

            # ── 4. 链接指向工信部备案系统 ────────────────────────────────
            href = q(page, "#auth-beian a", "el => el.getAttribute('href') || ''")
            check("链接 href 指向工信部备案系统",
                  bool(href) and BEIAN_HOME in href, href)
            rel = q(page, "#auth-beian a", "el => el.getAttribute('rel') || ''")
            check("新窗口带 noopener（别给备案页开一个能反 opener 的口子）",
                  bool(rel) and "noopener" in rel, rel)

            # ── 5. 命中测试：坐标打在它上面，命中的是它自己 ────────────────
            # `el.click()` 证明不了可点 —— 它直接派发事件，根本不做命中测试。
            hit = pe(page,
                "() => { const el = document.querySelector('#auth-beian a');"
                "  if (!el) return null;"
                "  const r = el.getBoundingClientRect();"
                "  const hitEl = document.elementFromPoint(r.left + r.width/2,"
                "                                          r.top + r.height/2);"
                "  return {tag: hitEl && hitEl.tagName,"
                "          inside: !!(hitEl && el.contains(hitEl))}; }")
            check("坐标命中测试打到备案链接自身", hit and hit["inside"], hit)

            # ── 6. 切语言不能把它改写（data-i18n-skip 的意义所在）────────
            for lang in ("zh", "en"):
                pg, errs = boot(browser, lang=lang)
                t2 = q(pg, "#auth-beian")
                check("切到 %s 后备案号未被 i18n 改写" % lang, t2 == EXPECTED, repr(t2))
                check("切到 %s 期间无 pageerror" % lang, not errs, errs[:1])
                pg.close()

            # ── 7. 守门元素本身不能被 JS 覆盖掉 ──────────────────────────
            # #auth-foot 会被 `foot.textContent = ...` 整个换掉；备案号如果写
            # 在它里面，下一次 health 轮询就没了。所以钉住两者是独立元素。
            sibling = pe(page, 
                "() => { const f = document.querySelector('#auth-foot');"
                "  const b = document.querySelector('#auth-beian');"
                "  return {hasFoot: !!f, sameParent: !!(f && b && f.parentNode === b.parentNode),"
                "          notChild: !!(f && b && !f.contains(b))}; }")
            check("备案号与 #auth-foot 是同级独立元素（不被 textContent 覆盖）",
                  sibling and sibling["notChild"] and sibling["sameParent"], sibling)
            page.close()

            # ══════════════════════════════════════════════════════════════
            # 8. 落地页 welcome.html —— 这才是「首访看到的主页」
            # ══════════════════════════════════════════════════════════════
            # 踩过的坑：只测登录闸门会漏掉真正的首页。本项目首访看到的是
            # `welcome.html`（AGENTS.md：首访看到 /welcome，之后直接进应用），
            # 它自带一套 `<footer class="wl-foot">`，与登录闸门是**两个独立页**。
            # 只在闸门上挂备案号，等于管局打开域名第一眼看到的那个页面没有 ——
            # 而那恰恰是被抽查的那一眼。
            wp = browser.new_page(viewport={"width": 1500, "height": 900})
            werrs = []
            wp.on("pageerror", lambda exc: werrs.append(str(exc)))
            wp.goto("http://127.0.0.1:%d/welcome.html" % PORT,
                    wait_until="domcontentloaded")
            wp.wait_for_selector(".wl-foot", state="attached", timeout=15000)

            # 正对照：落地页真的渲染了（否则「没有备案号」和「页面没出来」同形）
            wfoot = pe(wp, "() => { const f = document.querySelector('.wl-foot');"
                            "  if (!f) return null;"
                            "  const r = f.getBoundingClientRect();"
                            "  return {h: r.height, w: r.width}; }")
            check("正对照：落地页 .wl-foot 存在且有实际高度",
                  wfoot and wfoot["h"] > 20 and wfoot["w"] > 200, wfoot)
            check("正对照：隐私政策链接在（说明是完整 footer 而非残缺）",
                  wp.locator(".wl-foot a[href='privacy.html']").count() >= 1)

            wtxt = q(wp, ".wl-beian")
            check("落地页备案号文案逐字一致", wtxt == EXPECTED, repr(wtxt))

            wvis = pe(wp,
                "() => { const el = document.querySelector('.wl-beian');"
                "  if (!el) return null;"
                "  const cs = getComputedStyle(el);"
                "  const r = el.getBoundingClientRect();"
                "  return {display: cs.display, vis: cs.visibility, h: r.height,"
                "          w: r.width, top: r.top, mid: r.left + r.width/2,"
                "          cx: window.innerWidth / 2}; }")
            check("落地页元素 display 不是 none",
                  wvis and wvis["display"] != "none", wvis)
            check("落地页渲染高度 > 0", wvis and wvis["h"] > 0, wvis)
            check("落地页水平居中（|中心 - 视口中心| < 4px）",
                  wvis and abs(wvis["mid"] - wvis["cx"]) < 4,
                  wvis and "delta=%.1f" % abs(wvis["mid"] - wvis["cx"]))
            wrects = pe(wp,
                "() => { const b = document.querySelector('.wl-beian');"
                "  const row = document.querySelector('.wl-foot-in');"
                "  const foot = document.querySelector('.wl-foot');"
                "  if (!b || !row || !foot) return null;"
                "  const br = b.getBoundingClientRect(),"
                "        rr = row.getBoundingClientRect(),"
                "        fr = foot.getBoundingClientRect();"
                "  return {bTop: br.top, bBottom: br.bottom,"
                "          rowBottom: rr.bottom, footTop: fr.top, footBottom: fr.bottom}; }")
            # ⚠️ 位置比较必须拿位置比位置。曾写成 `beian.top >= foot.height` ——
            # 拿**绝对坐标**去比**高度**，量纲就不对：只要页面够长，备案号挪到
            # 任何位置它都绿，只有 top 小于 118 才红。这条当时是「假绿」。
            check("落地页备案号落在 footer 内",
                  wrects and wrects["footTop"] <= wrects["bTop"]
                  and wrects["bBottom"] <= wrects["footBottom"], wrects)
            check("落地页备案号在 footer 原有那一行（logo/隐私政策）**下方**",
                  wrects and wrects["bTop"] >= wrects["rowBottom"], wrects)
            check("落地页备案号紧贴 footer 底边（底部，不是中间）",
                  wrects and (wrects["footBottom"] - wrects["bBottom"]) < 40, wrects)

            whref = q(wp, ".wl-beian", "el => el.getAttribute('href') || ''")
            check("落地页链接 href 指向工信部备案系统",
                  bool(whref) and BEIAN_HOME in whref, whref)

            for lang in ("zh", "en"):
                pg2 = browser.new_page(viewport={"width": 1500, "height": 900})
                pg2.add_init_script(
                    "try{localStorage.setItem('klado-lang','%s')}catch(e){}" % lang)
                pg2.goto("http://127.0.0.1:%d/welcome.html" % PORT,
                         wait_until="domcontentloaded")
                pg2.wait_for_selector(".wl-beian", state="attached", timeout=15000)
                t3 = q(pg2, ".wl-beian")
                check("落地页切到 %s 后备案号未被 i18n 改写" % lang,
                      t3 == EXPECTED, repr(t3))
                pg2.close()

            check("落地页全程无 pageerror", not werrs, werrs[:1])
            wp.close()
            check("全程无 pageerror", not errors, errors[:1])
            browser.close()
    finally:
        httpd.shutdown()

    print()
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
