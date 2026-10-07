#!/usr/bin/env python3
"""Put four documents into a Workspace so the document cards can be looked at.

Asked for 2026-10-01: "请你往 workspace 里写入几个 pptx，docx，xlsx 和 pdf 文档，我看看效果" — one
of each supported type, with content that looks like a product-marketing centre's work rather
than lorem ipsum, because the point is to judge the CARD and the PREVIEW:

* **pptx** — a deck: the preview renders it to a PDF and shows it page by page;
* **docx** — a minutes document: python-docx turns it into HTML, so it reads as text;
* **xlsx** — a table: the server renders the grid itself, no JS;
* **pdf** — a one-pager: embedded as-is.

⚠️ Test only by default (same guard as `seed_calendar_demo.py`): these are uploads, and the
production workspace belongs to real people.

    python3 scripts/seed_workspace_documents.py --base-url http://localhost:8000
"""
import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request

AGENT_CODE_PATH = os.path.expanduser("~/.config/klado/agent_code")
API = "/api/reports/documents"

#: services/ is not importable when this file runs as a script from the repo root.
API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


def call(base, method, path, code, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + code)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ── the four files ───────────────────────────────────────────────────────────

def build_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)

    def slide(title, lines):
        layout = deck.slide_layouts[1]          # title + content
        page = deck.slides.add_slide(layout)
        page.shapes.title.text = title
        frame = page.placeholders[1].text_frame
        for i, line in enumerate(lines):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            para.text = line
            para.font.size = Pt(16)

    slide("Q4 渠道复盘与上市准备", [
        "评审人：产品营销中心 · 渠道计划 · 供应链",
        "分析窗口：2026-08 → 2026-10（sell-in / sell-out / inventory）",
        "本次要定的事：铺货节奏、价格带、上市物料",
    ])
    slide("渠道表现", [
        "Alpha Stores：销售额同比 +6.2%，ASP ¥7,410（-3.1%）",
        "Acme Retail：销售额同比 -4.8%，库存周转 68 天（+9 天）",
        "Beta Direct：销售额同比 +11.4%，Top3 SKU 占 46%",
        "全域：台量同比 +3.5%，均价下移 2.8%",
    ])
    slide("竞品动态", [
        "品牌 A：旗舰系列均价下探 ¥500，主推中端价格带",
        "品牌 B：以旧换新补贴加码，新兴渠道占比升至 21%",
        "品牌 C：保持高端不动价，用安装服务做差异化",
    ])
    slide("铺货与价格带", [
        "中端价格带：维持现价，改为「送装一体」主推",
        "入门价格带：Q4 收紧折扣至 -8% 以内",
        "新品 CD90：首批 12 家门店，11 月中前完成样机",
    ])
    slide("上市物料与内容", [
        "KV 两版（室内场景 / 户外场景），11-08 定稿",
        "TVC 15s + 30s，媒介投放从 11-15 起跑",
        "详情页：突出「续航 + 快充」两个卖点",
    ])
    slide("行动项", [
        "渠道口径对齐 —— 计划部 —— 11-10",
        "价格带方案定稿 —— 产品营销 —— 11-12",
        "样机铺设完成 —— 渠道 —— 11-14",
        "物料定稿 —— 品牌 —— 11-08",
    ])
    slide("下一步", [
        "11-13 复盘会：确认 Q4 最终铺货表",
        "11-20 上市首周数据回看",
    ])
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


def build_docx() -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading("产品营销月度例会 · 纪要", level=0)
    doc.add_paragraph("时间：2026-10-01 10:00–11:30　　地点：线上　　记录：产品营销中心")

    doc.add_heading("参会人", level=1)
    for who in ("Zhen Huang（主持）", "Li Ming（渠道计划）", "Florian Arnold（品牌媒介）",
                "Wang Fang（供应链）"):
        doc.add_paragraph(who, style="List Bullet")

    doc.add_heading("议题一 · Q4 铺货节奏", level=1)
    doc.add_paragraph(
        "供应链确认旗舰系列备货 8,600 台，可以覆盖 11–12 月的大促需求。渠道侧提出 Alpha Stores"
        "与 Acme Retail 的门店样机缺口约 120 台，需要在 11 月中之前补齐，否则会影响体验转化。")
    doc.add_paragraph("决议：样机由区域仓直发，11-14 前完成；缺口按周报跟踪。")

    doc.add_heading("议题二 · 价格带", level=1)
    doc.add_paragraph(
        "竞品在中端价格带下探明显，但我们的高端形象不能跟着降。会议同意用「送装一体 + "
        "延保」替代直接折扣，折扣上限收紧到 8%，超出部分需单独审批。")

    doc.add_heading("议题三 · 上市物料", level=1)
    doc.add_paragraph("KV 与 TVC 的排期已经确认，详情页需要一个明确的主卖点排序：续航优先，"
                      "快充其次，噪音指标放最后。")

    doc.add_heading("行动项", level=1)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Grid Accent 1"
    for i, head in enumerate(("事项", "负责人", "截止", "状态")):
        table.rows[0].cells[i].text = head
    for row in (("补齐门店样机", "Wang Fang", "2026-11-14", "进行中"),
                ("价格带方案定稿", "Li Ming", "2026-11-12", "未开始"),
                ("详情页卖点排序", "Florian Arnold", "2026-11-08", "进行中"),
                ("渠道口径对齐", "Li Ming", "2026-11-10", "已完成")):
        cells = table.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = value
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def build_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "渠道口径"
    rows = [
        ["渠道", "2026-08 销售额", "2026-09 销售额", "2026-10 销售额", "同比", "库存天数"],
        ["Alpha Stores", 18_420_000, 19_100_000, 20_350_000, "+6.2%", 41],
        ["Acme Retail", 12_980_000, 12_150_000, 11_760_000, "-4.8%", 68],
        ["Beta Direct", 9_640_000, 10_120_000, 11_050_000, "+11.4%", 33],
        ["Gamma Online", 7_210_000, 7_400_000, 7_890_000, "+4.1%", 36],
        ["Regional Chains", 5_880_000, 5_560_000, 5_410_000, "-2.6%", 52],
        ["全渠道合计", 54_130_000, 54_330_000, 56_460_000, "+3.5%", 44],
    ]
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.column_dimensions["A"].width = 14
    for col in "BCDEF":
        sheet.column_dimensions[col].width = 14
    for row in sheet.iter_rows(min_row=2, min_col=2, max_col=4):
        for cell in row:
            cell.number_format = "#,##0"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def build_pdf() -> bytes:
    from services.pdf_io import text_pdf

    # (size, text, baseline_y). Pillow positions text by the top of the line where PyMuPDF
    # used the baseline, so y is shifted up by the font size to keep the same layout.
    lines = [
        (22, "Q4 Product Marketing: One Pager", 70),
        (12, "Prepared for the channel review, 2026-10-19", 100),
        (14, "What we are doing", 140),
        (11, "Shipping the CD90 range to 12 flagship stores before mid-November.", 160),
        (11, "Replacing the discount with install-plus-warranty on the flagship range.", 178),
        (11, "Two KV versions and one TVC, media flight starts 15 November.", 196),
        (14, "What \"done\" looks like", 236),
        (11, "Sell-out above plan for four straight weeks, ASP down no more than 3%.", 256),
        (11, "Stock cover under 45 days in every channel by the end of December.", 274),
        (14, "Risks", 314),
        (11, "Sample units short by ~120 in two chains; competitor price cuts at 500L.", 334),
    ]
    page = [(60, y - size, size, text) for size, text, y in lines]
    return text_pdf([page])


def the_documents():
    return [
        {"slug": "demo-doc-pptx", "filename": "Q4-channel-review-and-launch.pptx",
         "builder": build_pptx, "title": "Q4 渠道复盘与上市准备（演示）",
         "summary": "产品营销中心 Q4 复盘用的演示文档，7 页。",
         "summary_en": "The Q4 channel review deck, seven pages.",
         "summary_zh": "Q4 渠道复盘与上市准备的演示文档，共 7 页。",
         "category": "Documents", "tags": "Q4,Channel,Launch,demo",
         "note": "deck：预览会转成 PDF 逐页看"},
        {"slug": "demo-doc-docx", "filename": "monthly-marketing-minutes.docx",
         "builder": build_docx, "title": "产品营销月度例会纪要（演示）",
         "summary": "含参会人、三个议题与一张行动项表格。",
         "summary_en": "Monthly marketing minutes with an action table.",
         "summary_zh": "月度营销例会纪要：参会人、议题与行动项表格。",
         "category": "Documents", "tags": "Minutes,Meeting,demo",
         "note": "docx：预览直接排成 HTML 文本，不下载也能读"},
        {"slug": "demo-doc-xlsx", "filename": "channel-sellin-inventory.xlsx",
         "builder": build_xlsx, "title": "渠道进销存口径表（演示）",
         "summary": "五个渠道近三个月的 GTO、同比与库存天数。",
         "summary_en": "Sell-in by channel for three months, with stock days.",
         "summary_zh": "五个渠道近三个月的 GTO、同比与库存天数。",
         "category": "Documents", "tags": "Channel,Inventory,demo",
         "note": "xlsx：服务端渲染成表格，不需要 JS"},
        {"slug": "demo-doc-pdf", "filename": "q4-one-pager.pdf",
         "builder": build_pdf, "title": "Q4 产品营销一页纸（演示）",
         "summary": "做三件事、怎么算完成、两个风险。",
         "summary_en": "One page: what we are doing, what done looks like, risks.",
         "summary_zh": "一页纸：在做什么、完成标准、两个风险。",
         "category": "Documents", "tags": "OnePager,demo",
         "note": "pdf：内嵌显示，另给下载"},
    ]


def main():
    parser = argparse.ArgumentParser(description="Upload one of each document type to a Workspace.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--agent-code", default="")
    parser.add_argument("--submitter", default="agent")
    args = parser.parse_args()

    base = args.base_url
    code = args.agent_code or (open(AGENT_CODE_PATH).read().strip()
                               if os.path.exists(AGENT_CODE_PATH) else "")
    if not code:
        sys.exit("no agent code: pass --agent-code or write it to %s" % AGENT_CODE_PATH)
    print("base: %s" % base)
    failed = 0
    for spec in the_documents():
        data = spec["builder"]()
        payload = {k: v for k, v in spec.items() if k not in ("builder", "note")}
        payload["content_base64"] = base64.b64encode(data).decode("ascii")
        payload["submitter"] = args.submitter
        status, body = call(base, "POST", API, code, payload)
        if status in (200, 201):
            item = body if isinstance(body, dict) else {}
            print("  OK   %-22s %-30s %6.1f KB  -> %s"
                  % (spec["slug"], spec["filename"], len(data) / 1024,
                     item.get("document_url") or item.get("slug") or ""))
        else:
            failed += 1
            print("  FAIL %-22s HTTP %s %s" % (spec["slug"], status, str(body)[:180]))
    print("failed: %d" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())