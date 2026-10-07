"""The product landing page (`/welcome`) and the first-visit redirect.

`/welcome` is the page a first-time visitor is sent to, so it is the first thing
anyone outside the team ever sees. Two halves, and they fail differently:

* **the page itself** — bilingual, themed from the same tokens as the app, and
  actually scrollable. That last one is not a given: `theme.css` sets
  `html, body { overflow: hidden }` because the app is a fixed shell with inner
  scroll containers, and a landing document inherits that unless it opts out. A
  page that renders beautifully and cannot be scrolled past its hero is the
  failure mode this suite exists to catch;
* **the redirect** — `/` sends an unflagged browser to `/welcome` exactly once,
  and does NOT send it there for a deep link, an embedded copy, or a browser
  whose storage is unavailable (which cannot record the visit, so redirecting
  would bounce the app against itself forever).

Runs against the REAL application (`main.app`, lifespan off so no database is
touched), because the interesting half is the routing: `/welcome` has to resolve
as an extensionless URL, and a missing route there sends every first-time
visitor to a 404.

    .venv312/bin/python api/tests/verify_welcome_ui.py
"""
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import urllib.request

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "out"
WELCOME = FRONTEND / "welcome.html"
# The banner that opens the landing page's block in app.css. Also the reader's
# own positive control: a section that cannot be found is EMPTY, and "0 colour
# literals in an empty string" is a check that silently proves nothing.
WELCOME_CSS_MARKER = "WELCOME — the product landing page"

FAILURES = []

# Any colour the page must not hard-code. `theme.css` is the only place a colour
# is defined; this is the rule that keeps the two themes in step.
LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(|\bcolor-mix\(")

# The landing page makes no API call at all, so this stub is only here to keep
# the APP (index.html, booted by the redirect checks) from logging fetch errors
# into the console assertions.
STUB = """
window.fetch = function () {
  return Promise.resolve({ok: true, status: 200, json: function () {
    return Promise.resolve({authenticated: false, enabled: false, modules: [],
                           items: [], reports: [], events: [], files: [],
                           folders: [], datasets: [], unread: 0});
  }});
};
"""


EITHER = "() => !!document.querySelector('.wl-hero') || !!document.querySelector('.nav')"
# Assertions below are about WHICH LANGUAGE is on screen, never about what a
# given line of copy says. The first version pinned the headline's exact wording,
# so rewording the page turned three checks red for a change that broke nothing —
# a guard that punishes editing is a guard people disable.
CJK = re.compile(r"[\u4e00-\u9fff]")
# The three places that state how many desks the room has. Scoped deliberately:
# see the note on the static half.
# ⚠️ Pinned by ID, not by `:not(.wl-plan-featured)`. There are now THREE plans,
# so that selector matched two of them, the "three claims" count became four, and
# the check would have failed for a reason that said nothing about seat count.
# ⚠️ And it is the INDIVIDUAL card, whose first line leads with the cap
# ("最多 12 个 agent，十二张工位"). Pointing it at the self-hosted card instead
# reads the same list but a different line, and that line leads with the price —
# the guard went red for exactly this reason once already.
SEAT_CLAIMS = (".wl-stats .wl-stat:first-child, #office h2, "
               "#plan-individual .wl-feats li:first-child")

#: Domains IANA reserves for documentation (RFC 2606) plus RFC 6761's `.invalid`.
#: A mailto pointing here is a placeholder nobody ever filled in — and because the
#: domain looks official, it survives review for years.
RESERVED_TLDS = ("@example.com", "@example.org", "@example.net", "@example", "@test",
                 "@localhost", "@invalid")

# Measures WCAG contrast between an element's own text colour and the colour
# behind it, which is what a losing cascade actually breaks. Written as a JS
# string because it runs per element in the page, on both themes.
CONTRAST_JS = """
els => els.map(e => {
  const cs = getComputedStyle(e);
  const visible = cs.display !== 'none' && cs.visibility !== 'hidden';
  const lum = c => {
    const m = c.match(/[\\d.]+/g).map(Number);
    const [r, g, b] = m.slice(0, 3).map(v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  // Walk up for the first opaque background: the badge's own fill is transparent
  // in some themes, and an alpha-zero colour would divide by nothing useful.
  let bg = cs.backgroundColor, node = e;
  while (/(rgba?\\([^)]*,\\s*0\\)|transparent)/.test(bg) && node.parentElement) {
    node = node.parentElement;
    bg = getComputedStyle(node).backgroundColor;
  }
  const l1 = lum(cs.color), l2 = lum(bg);
  const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  return {text: e.innerText.trim(), visible, ratio: Math.round(ratio * 100) / 100,
          color: cs.color, bg};
})"""


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILURES.append(name)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _restore_auth(previous) -> None:
    if previous is None:
        os.environ.pop("AUTH_ENABLED", None)
    else:
        os.environ["AUTH_ENABLED"] = previous


def serve():
    """The real app, no lifespan, so no database is required.

    ⚠️ `AUTH_ENABLED` is pinned here, and restoring it, because this guard
    silently depended on the developer's own `.env`.

    `root_document_for` serves `welcome.html` only when `AUTH_ENABLED` is true
    AND the caller is anonymous:

        if has_query or not auth_enabled:
            return "index.html"
        return "index.html" if authenticated else "welcome.html"

    A deployment with auth off is a single-user local mode — every caller is the
    first address in `AUTH_ADMIN_EMAILS` — so there is nobody to show the
    landing page TO, and `/` correctly returns the application.

    That is right for the product and wrong for this guard. It ran green in the
    working copy (whose `.env` says `AUTH_ENABLED=True`) and reported two
    failures in every temporary checkout, where `.env` is correctly absent and
    the default is false. Both readings were true; the guard was measuring the
    developer's `.env` instead of the behaviour. Set here so a fresh clone, a
    worktree and CI all get the same answer.
    """
    previous = os.environ.get("AUTH_ENABLED")
    os.environ["AUTH_ENABLED"] = "true"
    try:
        sys.path.insert(0, str(ROOT / "api"))
        import main  # noqa: E402  — importing is what registers the routes
        main.settings.AUTH_ENABLED = True
    except Exception:
        _restore_auth(previous)
        raise

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            _restore_auth(previous)
            return server, port
        time.sleep(0.1)
    raise RuntimeError("the app did not start")


def boot(browser, port, path="/welcome", lang=None, welcome=True):
    """A fresh page. `lang` pins the stored language. `welcome=False` boots the app
    instead, and `welcome="either"` accepts whichever document the gate chose."""
    page = browser.new_page(viewport={"width": 1500, "height": 900})
    errors, console = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
    page.add_init_script(STUB)
    if lang:
        page.add_init_script("try{localStorage.setItem('klado-lang','%s')}catch(e){}" % lang)
    page.goto("http://127.0.0.1:%d%s" % (port, path), wait_until="load")
    if welcome == "either":
        # For the checks whose SUBJECT is which page we landed on: waiting for one
        # specific page turns "wrong destination" into a 30s timeout instead of the
        # assertion that names the URL.
        page.wait_for_function(EITHER)
    elif welcome:
        page.wait_for_function("() => !!document.querySelector('.wl-hero')")
    else:
        page.wait_for_function("() => !!document.querySelector('.nav')")
    return page, errors, console


# ── static checks ────────────────────────────────────────────────────────────

def static_checks():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    html = WELCOME.read_text(encoding="utf-8")

    # The reader first: the welcome block must be FOUND and be big. Without this,
    # a marker edit silently turns the literal check into a check of "".
    marker_at = css.find(WELCOME_CSS_MARKER)
    section = css[css.rfind("/* ═", 0, marker_at):] if marker_at >= 0 else ""
    check("app.css 里找得到落地页样式分区（读取器自检）",
          marker_at >= 0 and len(section) > 5000 and ".wl-h1" in section,
          "marker@%d len=%d" % (marker_at, len(section)))
    # Positive control: the detector must find literals SOMEWHERE in the file, or
    # "0 in the welcome block" would just mean the regex stopped matching.
    check("颜色字面量探测器在本文件仍能命中（阳性对照）",
          len(LITERAL.findall(css)) > 20, len(LITERAL.findall(css)))
    check("落地页样式里没有硬编码颜色", LITERAL.findall(section) == [],
          LITERAL.findall(section)[:5])
    # The other direction: the block must actually USE tokens, or "no literals"
    # is also what an unstyled block looks like.
    check("落地页样式确实在用 theme.css 的 token",
          section.count("var(--") >= 40, section.count("var(--"))

    # Every icon class must have a glyph, or it renders as an empty box — with
    # no error anywhere. `ri-translate-2-line` was exactly that, silently.
    icons = {c: ("." + c + ":before") in (FRONTEND / "remixicon" / "remixicon.css").read_text(encoding="utf-8")
             for c in sorted(set(re.findall(r'class="[^"]*\b(ri-[a-z0-9-]+)', html)))}
    check("落地页用到的图标都有字形",
          all(icons.values()) and len(icons) > 0,
          [c for c, ok in icons.items() if not ok] or "%d icons" % len(icons))

    # ⚠️ The copy says "十二张工位 / Twelve desks". If the room ever grows, that
    # becomes a lie on the page — and nothing else would notice, because the
    # number is prose. Tie it to the one constant that decides it.
    office = (FRONTEND / "office.js").read_text(encoding="utf-8")
    m = re.search(r"SEATS_DEFAULT\s*=\s*(\d+)", office)
    check("工位数量能从 office.js 读到（读取器自检）", m is not None and m.group(1) == "12",
          m.group(1) if m else "not found")
    # The room's SIZE is stated in three places: the hero stat, the office heading
    # and the individual plan's first line. Those are the claims that have to track
    # `SEATS_DEFAULT`; the browser half below checks each of them. ⚠️ Do NOT scan
    # the whole file for `N张工位` instead — that also matches the distributive
    # "每个 agent 拿一张工位" (one desk EACH), which is not a capacity claim, and
    # conflating the two produced a red that said nothing.
    check("页面里有三处在声明房间容量（读取器自检）",
          html.count("张工位") >= 4, html.count("张工位"))

    # ⚠️ The plan rewrite removed the struck-through original prices (¥99 / ¥399)
    # on purpose: the tiers are "free for now" and nobody has decided what they
    # cost afterwards, so printing a number would be a promise nobody made. Read
    # the SOURCE rather than the rendered page — a hidden element, an aria-label or
    # a data- attribute would all still be on the page without being visible.
    leftovers = [p for p in ("¥99", "¥399", "99", "399") if p in html]
    check("页面上没有残留的原价（限时免费不该露出划线价）",
          "¥99" not in html and "¥399" not in html, leftovers)

    # The six module blurbs are quoted from `klado_shared/modules.py`. Pin that,
    # so the page cannot keep claiming things the product stopped doing. Twelve is
    # six modules × two languages: `core` and `settings` are deliberately absent,
    # since neither is one of the six modules the page is selling.
    mods = (ROOT / "klado_shared" / "modules.py").read_text(encoding="utf-8")
    all_blurbs = re.findall(r'blurb_(?:zh|en)="([^"]+)"', mods)
    quoted = [m for m in all_blurbs if m in html]
    check("模块文案直接引用 modules.py 的 blurb（防止文案漂移）",
          len(quoted) == 12, "%d/%d" % (len(quoted), len(all_blurbs)))

    # ⚠️ The team tier's only call to action is a mailto. It shipped pointing at
    # sales@example.com — IANA's reserved documentation domain, which is exactly why it
    # reads as "nobody ever changed this". A visitor who clicks it gets a dead end, and
    # nothing about that is visible until someone follows the link in anger.
    # Read the SOURCE again, not the DOM: the address could also hide in a hidden node.
    reserved = re.findall(r'mailto:([^"?]+)', html)
    check("联系我们指向真实地址，不是文档保留域名",
          bool(reserved) and all(not r.endswith(RESERVED_TLDS) for r in reserved), reserved)
    check("联系我们用的是 klado.team",
          any(r == "hello@klado.team" for r in reserved), reserved)


def route_checks(main_app):
    paths = {getattr(r, "path", "") for r in main_app.app.routes}
    check("/welcome 是一条真实的路由（少了它首访就是 404）", "/welcome" in paths, sorted(paths & {"/welcome"}))
    # The 401 trap: a path in the identity list gets a caller resolved before its
    # handler runs, and this page's whole audience is not signed in yet.
    check("/welcome 不在需身份校验的路径清单里",
          not any("/welcome" in p for p in main_app._SESSION_DOCUMENT_PREFIXES),
          main_app._SESSION_DOCUMENT_PREFIXES)


def http_checks(port):
    """The half of the gate that is genuinely about the wire: does `?welcome=1`
    reach the application, and does an anonymous caller really get the page.

    ⚠️ There is no cookie to assert on any more, and that is the point of the
    change rather than a gap in the test. The marker used to be written by
    `?welcome=1` and read by `/` for a YEAR, which made the landing page a
    once-per-browser page: a browser that had ever clicked "Sign in" could never
    be shown it again, signed out or not. The gate now asks the server the
    question it should have been asking all along — is there a session — so the
    negative assertion below ("`?welcome=1` writes no gate cookie") is a real
    invariant, not a leftover.

    The decision itself, including every combination this browser cannot reach,
    lives in `test_welcome_gate.py`: the guard has no session, so it only ever
    sees one column of that table.
    """
    def get(path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path)) as r:
            return r.read().decode("utf-8", "replace"), r.headers.get_all("Set-Cookie") or []

    body, cookies = get("/?welcome=1")
    check("?welcome=1 返回应用而不是落地页", "wl-hero" not in body)
    check("?welcome=1 不再种任何闸门 cookie（闸门改看登录态）",
          not any("klado-welcome" in c for c in cookies), cookies)

    body, _ = get("/")
    check("匿名访问 / 是落地页", "wl-hero" in body)

    # The two rows above are the whole contract and neither is a control for the
    # other alone: "the gate never fires" satisfies the first, "the gate always
    # fires" satisfies the second. This third row proves the SAME server hands out
    # both documents, so neither result can be an artefact of a static one.
    body, _ = get("/?welcome=1")
    check("（对照）同一个服务器上 ?welcome=1 拿到的是应用，不是落地页",
          "auth-gate" in body and "wl-hero" not in body)


def browser_checks(browser, port):
    # ⚠️ Language persistence needs TWO documents in ONE context, and the zh pin
    # must live on the first page only. `add_init_script` runs on EVERY navigation
    # of the page it is added to, so a reload re-pins the language and the reload
    # assertion tests the pin instead of the stored value. `browser.new_page()`
    # would not help either — it opens a new context, and localStorage does not
    # cross contexts.
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script(STUB)

    # ── 1. it renders, and in one language at a time ────────────────────────
    page = ctx.new_page()
    errors, console = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
    page.add_init_script("try{localStorage.setItem('klado-lang','zh')}catch(e){}")
    page.goto("http://127.0.0.1:%d/welcome" % port, wait_until="load")
    page.wait_for_function("() => !!document.querySelector('.wl-hero')")
    h1_zh = page.inner_text(".wl-h1")
    check("zh 下主标题是中文侧（含中文）", bool(CJK.search(h1_zh)), h1_zh[:60])

    # Both sides must exist and exactly one must be VISIBLE — measured, because
    # `textContent` returns hidden sides too and would report both languages as
    # present when only one is on screen.
    sides = page.eval_on_selector_all(
        ".wl-h1 [data-lang-zh], .wl-h1 [data-lang-en]",
        "els => els.map(e => [e.getAttribute('data-lang-zh') !== null,"
        " getComputedStyle(e).display])")
    visible = sorted("zh" if is_zh else "en" for is_zh, disp in sides if disp != "none")
    check("zh 下恰好一侧可见（量 display，不是读 textContent）",
          visible == ["zh"], sides)

    # Counts, not "at least one": a card that fails to render is the failure.
    check("痛点卡是 4 张", page.locator("#pain .wl-card").count() == 4,
          page.locator("#pain .wl-card").count())
    check("模块卡是 6 张", page.locator("#modules .wl-card").count() == 6,
          page.locator("#modules .wl-card").count())
    check("订阅是 3 档", page.locator("#plans .wl-plan").count() == 3,
          page.locator("#plans .wl-plan").count())
    # ⚠️ The three plans list the same number of features today, but anything
    # that lets the cards size themselves independently (`align-items: start`) puts
    # their buttons at different heights and the row stops reading as a
    # comparison. Measured, not read out of the stylesheet.
    plans = page.eval_on_selector_all(
        "#plans .wl-plan",
        "els => els.map(e => {const r = e.getBoundingClientRect();"
        "const b = e.querySelector('.wl-btn').getBoundingClientRect();"
        "return {h: Math.round(r.height), btnBottom: Math.round(b.bottom)};})")
    # ⚠️ Cards listing the SAME number of features measure equal whatever
    # `align-items` says — which is exactly how the height check below passed while
    # `align-items: start` was in the file. So pin the rule that makes it hold for
    # ANY copy, and keep the measurement as the user-visible consequence.
    align = page.eval_on_selector("#plans .wl-grid-plans", "e => getComputedStyle(e).alignItems")
    check("定价行是 stretch（条目数一多，各自算高度就错位）", align == "stretch", align)
    check("三档定价卡等高", len(plans) == 3 and len({p["h"] for p in plans}) == 1, plans)
    check("三档的按钮底边对齐",
          len(plans) == 3 and len({p["btnBottom"] for p in plans}) == 1, plans)

    # ── the plans themselves: three distinct tiers, none with a struck price ──
    # ⚠️ Positive control FIRST. Every assertion below is about three specific
    # cards; if `#plans .wl-plan` matched nothing, "all three are ¥0" would be
    # vacuously true over an empty list — the same way a `count() == 0` assertion
    # cannot tell "absent" from "selector broke".
    names = page.eval_on_selector_all(
        "#plans .wl-plan .wl-plan-name", "els => els.map(e => e.innerText.trim())")
    check("三档各有名字（阳性对照，不是空壳）",
          len(names) == 3 and all(names), names)
    prices = page.eval_on_selector_all(
        "#plans .wl-plan .wl-price b", "els => els.map(e => e.innerText.trim())")
    check("三档都是 ¥0", prices == ["¥0", "¥0", "¥0"], prices)

    # The badge says what the pricing is, and it says so about TODAY only. No
    # original price is printed anywhere, so nobody can infer the future figure
    # from this page — that was a deliberate product decision, not an omission.
    badges = page.eval_on_selector_all(
        "#plans .wl-plan",
        """els => els.map(card => {
             const vis = [...card.querySelectorAll('.wl-price-badge')]
               .filter(b => getComputedStyle(b).display !== 'none');
             if (vis.length !== 1) return {n: vis.length};
             const cs = getComputedStyle(vis[0]);
             return {n: 1, text: vis[0].innerText.trim(), bg: cs.backgroundColor,
                     radius: cs.borderTopLeftRadius,
                     cardBg: getComputedStyle(card).backgroundColor};
           })""")
    check("每档恰好一个可见的免费角标",
          len(badges) == 3 and all(b["n"] == 1 for b in badges), badges)
    check("自部署是永久免费，个人和团队是限时免费",
          [b.get("text") for b in badges] == ["永久免费", "限时免费", "限时免费"],
          [b.get("text") for b in badges])
    # ⚠️ Measured CONTRAST, not "is there a background". The first version of this
    # check asked for a background different from the card's and a non-zero radius,
    # and a mutation that dropped `.wl-price-badge` to a specificity it LOST to
    # `.wl-price span` stayed green — because the losing rule never set background
    # or radius, only `color`. The pill looked perfect and its text was grey.
    # Contrast is the property the eye judges and the one a losing cascade breaks,
    # so it is what gets asserted. 4.5 is WCAG AA for the badge's .72rem text.
    badge_contrast = page.eval_on_selector_all(
        "#plans .wl-plan .wl-price-badge",
        CONTRAST_JS)
    visible_badges = [b for b in badge_contrast if b["visible"]]
    check("角标在浅色主题下读得清（文字与底色对比度 ≥ 4.5）",
          len(visible_badges) == 3 and all(b["ratio"] >= 4.5 for b in visible_badges),
          visible_badges)
    check("办公室分区是 4 张卡", page.locator("#office .wl-card").count() == 4,
          page.locator("#office .wl-card").count())

    # ⚠️ Positive control first: if these three elements did not resolve, `all()`
    # over an empty list is vacuously true and the seat count stops being checked.
    seats_zh = page.eval_on_selector_all(SEAT_CLAIMS,
                                         "els => els.map(e => e.innerText.trim())")
    check("三处容量声明都定位到了（阳性对照）", len(seats_zh) == 3, seats_zh)
    check("中文下三处容量声明都写十二张工位",
          len(seats_zh) == 3 and all("十二张工位" in t for t in seats_zh), seats_zh)

    # ── 2. the switch: current value, persistence, and a fresh document ─────
    chip_before = page.inner_text("[data-lang-toggle] [data-lang-label]")
    check("语言芯片显示的是当前语言（不是点了会去的那个）",
          chip_before == "中文", chip_before)
    page.click("[data-lang-toggle]")
    page.wait_for_timeout(200)
    h1_en = page.inner_text(".wl-h1")
    check("点一下切到 en（同一处换成英文侧）",
          not CJK.search(h1_en) and h1_en != h1_zh, h1_en[:60])
    check("切到 en 后中文那一侧真的退场了", h1_zh not in page.inner_text(".wl-hero"))
    check("语言写进了 localStorage",
          page.evaluate("() => localStorage.getItem('klado-lang')") == "en")
    check("芯片跟着换成当前语言", page.inner_text("[data-lang-toggle] [data-lang-label]") == "English")
    check("整页切换过程中无 JS 报错", errors == [] and console == [], (errors + console)[:3])
    page.close()

    # A fresh document in the same context: the only thing it can read is what
    # the previous page stored, so this is persistence and nothing else.
    probe = ctx.new_page()
    probe.goto("http://127.0.0.1:%d/welcome" % port, wait_until="load")
    probe.wait_for_function("() => !!document.querySelector('.wl-hero')")
    check("换一个新文档打开，仍是上次选的语言",
          probe.inner_text(".wl-h1") == h1_en, probe.inner_text(".wl-h1")[:60])
    check("芯片也仍然是 English",
          probe.inner_text("[data-lang-toggle] [data-lang-label]") == "English")
    seats_en = probe.eval_on_selector_all(SEAT_CLAIMS,
                                          "els => els.map(e => e.innerText.trim())")
    # ⚠️ Case-insensitive on purpose. The claim may sit mid-sentence — the
    # individual card reads "Up to 12 agents, twelve desks" — and the thing under
    # test is that the number tracks SEATS_DEFAULT, not where the phrase starts.
    check("英文下三处容量声明都写 Twelve desks",
          len(seats_en) == 3 and all("twelve desks" in t.lower() for t in seats_en), seats_en)
    probe.close()
    ctx.close()

    # ── 3. it SCROLLS (theme.css clips html/body for the app shell) ────────
    page, errors, _ = boot(browser, port, lang="zh")
    page.evaluate("() => window.scrollTo(0, 20000)")
    page.wait_for_timeout(250)
    metrics = page.evaluate(
        "() => ({sh: document.scrollingElement.scrollHeight, ih: window.innerHeight,"
        " y: window.scrollY, ov: getComputedStyle(document.body).overflow})")
    check("落地页可以滚动（没有被 theme.css 的 overflow:hidden 截住）",
          metrics["y"] > 100 and metrics["sh"] > metrics["ih"] + 200, metrics)
    check("滚动容器的 overflow 确实被放开了", metrics["ov"] == "visible", metrics["ov"])
    # Positive control for the reader: the thing being overridden is really there.
    app, _, _ = boot(browser, port, path="/index.html?x=1", lang="zh", welcome=False)
    app_ov = app.evaluate("() => getComputedStyle(document.body).overflow")
    check("应用本身仍然是固定外壳（阳性对照）", app_ov == "hidden", app_ov)
    app.close()
    page.close()

    # ── 4. dark theme comes from tokens, not from a second palette ──────────
    page, errors, _ = boot(browser, port, lang="zh")
    # `logo.png` is a finished image with DARK lettering. On the dark theme it
    # measured 1.09:1 against the surface — which is why the app carries a second
    # `logo-dark.png` and hides one of the pair per theme. This page shares those
    # selector lists, and a page that forgets to would be unreadable, not broken.
    logos = page.eval_on_selector_all(
        ".wl-brand-ico img",
        "els => els.map(e => [e.className.replace('logo-', ''), getComputedStyle(e).display])")
    check("浅色主题只显示 logo.png",
          sorted(n for n, d in logos if d != "none") == ["light"], logos)
    light_bg = page.eval_on_selector("#plans .wl-plan", "e => getComputedStyle(e).backgroundColor")
    page.click("[data-theme-toggle]")
    page.wait_for_timeout(250)
    logos_dark = page.eval_on_selector_all(
        ".wl-brand-ico img",
        "els => els.map(e => [e.className.replace('logo-', ''), getComputedStyle(e).display])")
    check("深色主题只显示 logo-dark.png",
          sorted(n for n, d in logos_dark if d != "none") == ["dark"], logos_dark)
    dark_bg = page.eval_on_selector("#plans .wl-plan", "e => getComputedStyle(e).backgroundColor")
    theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    check("主题切换把 data-theme 改成了 dark", theme == "dark", theme)
    check("卡片底色确实随主题变了（不是同一份写死的颜色）",
          light_bg != dark_bg, "%s -> %s" % (light_bg, dark_bg))
    # ⚠️ Dark theme is where a badge goes wrong, so it is checked there and not
    # only in light: `--accent` LIGHTENS in dark mode, and white on `--accent`
    # measures about 2.5:1. The light-theme number alone would have passed with
    # `--accent-fill` replaced by `--accent`, since light mode is where that
    # substitution still looks fine.
    dark_badges = [b for b in page.eval_on_selector_all(
        "#plans .wl-plan .wl-price-badge", CONTRAST_JS) if b["visible"]]
    check("角标在深色主题下读得清（对比度 ≥ 4.5，没有退回 --accent）",
          len(dark_badges) == 3 and all(b["ratio"] >= 4.5 for b in dark_badges),
          dark_badges)
    page.reload(wait_until="load")
    page.wait_for_function("() => !!document.querySelector('.wl-hero')")
    check("深色主题被记住",
          page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "dark")
    page.close()

    # ── 5. the gate: the landing page belongs to people who are NOT signed in ─
    # ⚠️ One context, several pages. `browser.new_page()` opens a NEW context each
    # time and cookies do not cross contexts — so "the second visit" has to be a
    # second page in the SAME context, or it is really just a first visit again.
    #
    # ⚠️ This section used to assert the OPPOSITE of what it asserts now, and the
    # old expectation was the bug: "a browser that has been here goes straight
    # to the app", tracked with a one-year `klado-welcome-seen` cookie. A visitor
    # who clicked Sign in once could never see the landing page again — and the
    # browser's Back button could not rescue them either, because the page is
    # served AT `/` by a rewrite, so the previous history entry is `/` itself.
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script(STUB)
    ctx.add_init_script("try{localStorage.setItem('klado-lang','zh')}catch(e){}")

    fresh = ctx.new_page()
    fresh.goto("http://127.0.0.1:%d/" % port, wait_until="load")
    fresh.wait_for_function(EITHER)
    check("没登录的浏览器打开 / 看到的是落地页",
          fresh.locator(".wl-hero").count() == 1, fresh.url)
    # No marker of any kind: the gate asks the server who you are, so there is
    # nothing for the browser to carry between visits.
    cookies = [c["name"] for c in ctx.cookies()]
    check("整个上下文里没有任何闸门 cookie", not any(
        "klado-welcome" in c for c in cookies), cookies)
    fresh.click(".wl-cta .wl-btn-primary")
    fresh.wait_for_load_state("load")
    # EITHER again: if the call to action forgot to carry `?welcome=1`, the server
    # hands back the landing page. Waiting for `.nav` would report that as a
    # timeout instead of as "it looped us back".
    fresh.wait_for_function(EITHER)
    check("点「开始使用」后进了应用", fresh.locator(".nav").count() == 1, fresh.url)
    fresh.close()

    again = ctx.new_page()
    again.goto("http://127.0.0.1:%d/" % port, wait_until="load")
    again.wait_for_function(EITHER)
    # ⚠️ THE row this section exists for. Still signed out — the STUB above answers
    # 401 for /auth/me — so `/` must serve the landing page again. Under the old
    # cookie gate this page came back as the application, which is the "I clicked
    # Sign in and now I can never get back" report.
    check("没登录时刷新 / 仍然回到落地页（不是应用）",
          again.locator(".wl-hero").count() == 1, again.url)
    # The control for the row above, in the same context and at the same moment:
    # `?welcome=1` is still the escape hatch the call to action uses, so
    # "every visit gets the landing page" would be caught here.
    ctl, _, _ = boot(browser, port, path="/?welcome=1", lang="zh",
                     welcome="either")
    check("（对照）同一时刻 ?welcome=1 仍然直达应用",
          ctl.locator(".nav").count() == 1, ctl.url)
    ctl.close()
    again.close()

    # ⚠️ THE property that keeps the other 22 browser guards alive. They serve
    # `frontend/out` from a plain static file server, where `/` is `index.html`
    # and no gate exists at all. This is why the gate is a server route and not a
    # `<head>` script: a client-side redirect fired inside every one of them and
    # each then waited for `navTo` on a page that had become a 404.
    raw, _, _ = boot(browser, port, path="/index.html", lang="zh", welcome="either")
    check("直接请求 /index.html 永远是应用（静态服务器上的 22 个守卫靠这条）",
          raw.locator(".nav").count() == 1, raw.url)
    raw.close()

    # ── 6. what must NOT be hijacked ──────────────────────────────────────
    deep, _, _ = boot(browser, port, path="/?report=quarterly", lang="zh", welcome="either")
    check("深链 ?report= 不被落地页截走", deep.locator(".nav").count() == 1, deep.url)
    deep.close()

    # The raw file has to work on its own too: it is what a static server, an
    # offline copy or a reverse-proxy mount serves.
    rawfile, _, _ = boot(browser, port, path="/welcome.html", lang="zh")
    check("/welcome.html 直接访问也是落地页（不依赖路由）",
          rawfile.locator(".wl-hero").count() == 1, rawfile.url)
    rawfile.close()
    ctx.close()

    # ── 7. a phone must not scroll sideways ────────────────────────────────
    mob = browser.new_page(viewport={"width": 390, "height": 844})
    mob_errors = []
    mob.on("pageerror", lambda exc: mob_errors.append(str(exc)))
    mob.add_init_script("try{localStorage.setItem('klado-lang','zh')}catch(e){}")
    mob.add_init_script(STUB)
    mob.goto("http://127.0.0.1:%d/welcome" % port, wait_until="load")
    mob.wait_for_function("() => !!document.querySelector('.wl-hero')")
    w = mob.evaluate("() => ({sw: document.scrollingElement.scrollWidth,"
                     " cw: document.documentElement.clientWidth})")
    check("390px 宽下没有横向溢出", w["sw"] <= w["cw"] + 1, w)
    check("窄屏也没有 JS 报错", mob_errors == [], mob_errors[:3])
    mob.close()

    # ── 8. desktop is ONE row, and a button label is ONE line ───────────────
    # ⚠️ Neither of these was ever broken in Chrome — both were broken in Safari,
    # which is exactly why "it looked fine in Chrome" is not evidence here. The
    # nav's "Sign in" had `clientWidth - scrollWidth == 0`, i.e. Chrome kept it
    # on one line only because the width came out to the pixel; Safari's metrics
    # run a hair wider and the label broke into "Sign / in". The footer had
    # `flex-wrap: wrap`, which lets the logo orphan onto a centred line of its
    # own. So the load-bearing assertions pin the two PROPERTIES that make the
    # layout engine-independent, and the geometry check is what a person sees.
    page = browser.new_page(viewport={"width": 1500, "height": 900})
    page.add_init_script(STUB)
    page.goto("http://127.0.0.1:%d/welcome" % port, wait_until="load")
    page.wait_for_function("() => !!document.querySelector('.wl-foot-in')")

    # Positive control FIRST. The footer was already a single row in Chrome, so
    # "one row" on its own would pass for the same reason a blank page does.
    # Force a wrap, prove the counter can see more than one line, remove the
    # forced style (the probe restores what it changed), and only then believe
    # the real measurement.
    forced = page.evaluate("""() => {
      const st = document.createElement('style');
      st.textContent = '.wl-foot-in{flex-wrap:wrap !important;' +
                       'width:220px !important}';
      document.head.appendChild(st);
      const fin = document.querySelector('.wl-foot-in');
      const centres = [...fin.children].map(k => {
        const r = k.getBoundingClientRect();
        return Math.round(r.top + r.height / 2);
      });
      const out = {lines: [...new Set(centres)].length,
                   children: fin.children.length};
      st.remove();
      return out;
    }""")
    check("正对照：强制折行时行数计数器数得到多行（否则下一条是恒绿断言）",
          forced["lines"] > 1 and forced["children"] >= 4, forced)

    # ⚠️ Vertical CENTRES, never `offsetTop`. `align-items: center` gives items
    # of different heights different `offsetTop` on the SAME line, so counting
    # offsetTop values reports three lines for a perfectly straight row — which
    # is how the first version of this probe "confirmed" a bug that wasn't
    # there.
    geom = page.evaluate("""() => {
      const fin = document.querySelector('.wl-foot-in');
      const centres = [...fin.children].map(k => {
        const r = k.getBoundingClientRect();
        return Math.round(r.top + r.height / 2);
      });
      return {lines: [...new Set(centres)].length,
              children: fin.children.length,
              wrap: getComputedStyle(fin).flexWrap};
    }""")
    check("桌面宽度下 footer 的 logo / 说明 / Privacy 在同一行",
          geom["lines"] == 1 and geom["children"] >= 4, geom)

    props = page.evaluate("""() => {
      const btn = document.querySelector('.wl-head-acts .wl-btn');
      const chip = document.querySelector('.wl-toggle');
      const label = btn && (btn.querySelector('[data-lang-en]') ||
                            btn.querySelector('span'));
      return {
        btnWhiteSpace: btn ? getComputedStyle(btn).whiteSpace : null,
        chipWhiteSpace: chip ? getComputedStyle(chip).whiteSpace : null,
        btnLines: label ? label.getClientRects().length : -1,
        btnW: btn ? Math.round(btn.getBoundingClientRect().width) : -1,
      };
    }""")
    check("顶栏 Sign in 按钮标签是 nowrap（Safari 上它曾折成 Sign / in）",
          props["btnWhiteSpace"] == "nowrap", props)
    check("语言切换 chip 同样是 nowrap",
          props["chipWhiteSpace"] == "nowrap", props)
    check("Sign in 标签只占一个行盒",
          props["btnLines"] == 1, props)
    check("footer 在桌面宽度是 nowrap（Safari 上 logo 曾被甩到单独一行）",
          geom["wrap"] == "nowrap", geom["wrap"])

    # The phone layout must still be allowed to wrap — nowrap is a desktop
    # guarantee, not a blanket one.
    mid = browser.new_page(viewport={"width": 700, "height": 900})
    mid.add_init_script(STUB)
    mid.goto("http://127.0.0.1:%d/welcome" % port, wait_until="load")
    mid.wait_for_function("() => !!document.querySelector('.wl-foot-in')")
    mwrap = mid.evaluate(
        "() => getComputedStyle(document.querySelector('.wl-foot-in')).flexWrap")
    check("≤900px 恢复 wrap（手机布局不受桌面 nowrap 影响）",
          mwrap == "wrap", mwrap)
    mid.close()
    page.close()


def main() -> int:
    sys.path.insert(0, str(ROOT / "api"))
    static_checks()
    server, port = serve()
    try:
        import main  # noqa: E402

        route_checks(main)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            http_checks(port)
            browser_checks(browser, port)
            browser.close()
    finally:
        server.should_exit = True
        time.sleep(0.3)

    print("\n%d checks failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())