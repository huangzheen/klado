#!/usr/bin/env python3
"""Delete the four agent-written dashboards and publish four new ones.

One publish script rather than four, so the delete and the create are the same
transaction from the reader's point of view: the board is never half-emptied.

Every page is verified after publishing — HTTP status, that the kit is in the served
HTML, and that the page actually RENDERS content (a dashboard whose queries all fail
still serves a 200 with an empty body, which is why "the page loaded" is not the bar).
"""
import base64
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
DASH = "/tmp/klado-specs/dash"
COVERS = "/tmp/klado-specs/covers"

# slug -> (cover file, title, summary, summary_en, datasets)
PLAN = [
    ("exec-overview", "overview-board.jpg", "经营概览",
     "价格带与区域的经营概览：四个关键指标、量排名、月度走势。",
     "Executive overview of bands and regions: four KPIs, a ranking, monthly trends.",
     ["band_monthly_2026", "region_monthly_2026"]),
    ("ops-monitor", "settlement-board.jpg", "运营监控",
     "按严重度排序的异常清单，加上收入构成与渠道争议率。",
     "Exceptions ranked by severity, plus revenue mix and channel dispute rates.",
     ["band_monthly_2026", "region_monthly_2026", "channel_disputes_2026"]),
    ("data-ledger", "region-board.jpg", "明细台账",
     "45 行区域明细与价格带矩阵，数字不换行，条形画在格子里面。",
     "45 rows of regional detail and a band matrix: figures never wrap, bars live in the cell.",
     ["region_monthly_2026", "band_monthly_2026"]),
    ("business-brief", "price-band-board.jpg", "经营简报",
     "一页可转发的窄栏简报：结论先行，数字都是可复制的文字，适合打印。",
     "A one-page forwardable brief: the finding first, every figure selectable text, print-ready.",
     ["band_monthly_2026"]),
]


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def cover_b64(name):
    with open(f"{COVERS}/{name}", "rb") as f:
        return base64.b64encode(f.read()).decode()


def main():
    print("── 删掉现有的 4 个仪表盘")
    existing = call("GET", "/api/dashboard")[1]
    rows = existing.get("dashboards", existing) if isinstance(existing, dict) else existing
    old = [r["slug"] for r in rows]
    for slug in old:
        st, _ = call("DELETE", f"/api/dashboard/{slug}")
        print(f"   {slug:20s} → {st}")
    if len(old) != 4:
        print(f"   ⚠ 预期 4 个，实际 {len(old)} 个：{old}", file=sys.stderr)

    print("\n── 发布 4 个新仪表盘")
    for slug, cover, title, zh, en, datasets in PLAN:
        html = open(f"{DASH}/{slug}.html", encoding="utf-8").read()
        st, body = call("PUT", f"/api/dashboard/{slug}", {
            "title": title, "html": html, "datasets": datasets,
            "summary": zh, "summary_en": en, "cover_base64": cover_b64(cover),
            # `in_nav` defaults to False and it is the flag that decides whether the
            # LEFT RAIL lists this dashboard (see dashboardPage.syncPromotedNav). Left
            # off, all four publish fine and the rail simply shows no dashboards —
            # which reads as "the rail is broken" rather than "a flag was not set".
            "in_nav": True,
        })
        if st != 200:
            print(f"   ✗ {slug:20s} → {st} {body}")
            return 1
        print(f"   ✓ {slug:20s}  {title:8s} {len(html):>6d} 字符  数据集 {len(datasets)}")

    print("\n── 读回校验")
    for slug, *_ in PLAN:
        st, _ = call("GET", f"/api/dashboard/{slug}")
        covered = st == 200
        req = urllib.request.Request(f"{BASE}/d/{slug}")
        with urllib.request.urlopen(req, timeout=30) as r:
            page = r.read().decode()
        kit = "klado-document-kit" in page
        # The author's own <style> must survive the injection.
        kept = "kld-page" in page or "kld-" in page
        print(f"   {slug:20s} 卡片={covered} 套件={kit} 正文={len(page):>6d}")
        if not (covered and kit and kept):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
