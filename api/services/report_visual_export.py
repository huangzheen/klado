"""Pixel-faithful PDF and picture exports of the rendered Workspace report.

Unlike editable PPTX, these formats preserve the browser's painted result. A
deck becomes one 1920×1080 PNG per slide; the PDF embeds those same PNG bytes
one per page. Multi-slide picture downloads are ZIP archives of numbered PNGs.
"""
from __future__ import annotations

import io
import json
import zipfile
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from services.pdf_io import PdfError, pngs_to_pdf

CANVAS_W = 1920
CANVAS_H = 1080
MAX_SLIDES = 80
STATIC_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
                   ".woff", ".woff2", ".ttf", ".css", ".js")


class VisualExportError(Exception):
    pass


def _pdf_from_pngs(pages: list[bytes]) -> bytes:
    try:
        return pngs_to_pdf(pages, max_page_pt=14400)
    except PdfError as exc:
        raise VisualExportError("渲染后的页面太大，无法导出 PDF；请改为图片导出 / a rendered page is too large for PDF; export it as a picture") from exc


def _picture_from_pngs(pages: list[bytes]) -> tuple[bytes, str, str]:
    if len(pages) == 1:
        return pages[0], "image/png", "png"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for number, png in enumerate(pages, 1):
            archive.writestr(f"page-{number:02d}.png", png)
    return output.getvalue(), "application/zip", "zip"


async def render_report_visual(report_url: str, html_body: str, state_json: str,
                               kind: str, data_snapshots: dict[str, str] | None = None,
                               asset_auth_headers: dict[str, str] | None = None,
                               ) -> tuple[bytes, str, str]:
    """Return (content, MIME type, extension) for `pdf` or `picture`.

    Report-authored JavaScript runs in an isolated browser. Only the selected
    HTML, its saved state, approved read-only data snapshots, and local static
    assets are reachable. Missing dynamic data fails loudly instead of silently
    producing an incomplete export.
    """
    parsed = urlparse(report_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 8000:
        raise VisualExportError("导出器只接受本地报告路由 / the exporter accepts only the local report route")
    report_path = parsed.path.rstrip("/")
    if "/r/" not in report_path:
        raise VisualExportError("导出器只接受本地报告路由 / the exporter accepts only the local report route")
    base_path, slug = report_path.rsplit("/r/", 1)
    state_path = f"{base_path}/api/reports/{slug}/state"
    cover_path = f"{base_path}/api/reports/{slug}/cover"
    storage_path = f"{base_path}/api/storage/serve"
    snapshots = data_snapshots or {}
    missing_data: set[str] = set()
    failed_assets: set[str] = set()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": CANVAS_W, "height": CANVAS_H},
                                          device_scale_factor=1)
            page.on("requestfailed", lambda request: failed_assets.add(
                urlparse(request.url).path) if request.resource_type in
                {"image", "font", "stylesheet", "script"} else None)
            page.on("response", lambda response: failed_assets.add(
                urlparse(response.url).path) if response.status >= 400 and
                response.request.resource_type in {"image", "font", "stylesheet", "script"} else None)

            async def restrict(route):
                url = urlparse(route.request.url)
                same = url.hostname in {"127.0.0.1", "localhost"} and url.port == 8000
                path = url.path
                key = path + ("?" + url.query if url.query else "")
                method = route.request.method
                if same and method == "GET" and path == report_path:
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                        body=html_body)
                elif same and method == "GET" and path.rstrip("/") == state_path:
                    await route.fulfill(status=200, content_type="application/json",
                                        headers={"Cache-Control": "no-store"},
                                        body=state_json or "{}")
                elif same and method == "GET" and key in snapshots:
                    await route.fulfill(status=200, content_type="application/json",
                                        body=snapshots[key])
                elif same and method == "GET" and (path.lower().endswith(STATIC_SUFFIXES)
                                                      or path in {cover_path, storage_path}):
                    headers = dict(route.request.headers)
                    headers.update(asset_auth_headers or {})
                    await route.continue_(headers=headers)
                else:
                    if url.scheme in {"http", "https"}:
                        missing_data.add(path or url.hostname or "external resource")
                    await route.abort()

            await page.route("**/*", restrict)
            response = await page.goto(report_url, wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status != 200:
                raise VisualExportError("报告页面加载失败，无法导出 / report page could not be loaded for export")
            if await page.locator(".slide").count():
                await page.wait_for_function(
                    "document.documentElement.classList.contains('deck-ready')", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.evaluate("document.fonts.ready")
            await page.evaluate("Promise.all([...document.images].map(i => i.decode().catch(() => {})))")
            if missing_data:
                names = ", ".join(sorted(missing_data)[:3])
                raise VisualExportError("报告资源不可用，无法导出: " + names + " / report resources were not available for export: " + names)
            if failed_assets:
                names = ", ".join(sorted(failed_assets)[:3])
                raise VisualExportError("报告的图片/字体/样式未加载: " + names + " / report images, fonts or styles did not load: " + names)
            # Viewer controls and the read-only notice are outside the authored
            # page content. Hide them before capturing, without changing layout.
            await page.add_style_tag(content=(
                '[data-pptx-export="omit"], .deck-bar, .deck-progress {'
                'display:none!important}'
            ))
            pages: list[bytes] = []
            count = await page.evaluate("window.reportDeck && window.reportDeck.count || 0")
            if count:
                if count > MAX_SLIDES:
                    raise VisualExportError(f"报告超过 {MAX_SLIDES} 页 / report has more than {MAX_SLIDES} pages")
                for index in range(count):
                    await page.evaluate("""index => {
                      window.reportDeck.goTo(index);
                      document.querySelector('.deck').style.setProperty('--deck-scale', '1');
                    }""", index)
                    pages.append(await page.locator(".deck .slide.is-current").screenshot(
                        type="png", animations="disabled", caret="hide", timeout=30000))
            else:
                pages.append(await page.screenshot(type="png", full_page=True,
                                                    animations="disabled", caret="hide"))
            # Lazy-loaded slide assets may be requested only after goTo(). A
            # successful screenshot alone must not certify an incomplete page.
            if missing_data:
                names = ", ".join(sorted(missing_data)[:3])
                raise VisualExportError("报告资源不可用，无法导出: " + names + " / report resources were not available for export: " + names)
            if failed_assets:
                names = ", ".join(sorted(failed_assets)[:3])
                raise VisualExportError("报告的图片/字体/样式未加载: " + names + " / report images, fonts or styles did not load: " + names)
        finally:
            await browser.close()

    if kind == "pdf":
        return _pdf_from_pngs(pages), "application/pdf", "pdf"
    if kind == "picture":
        return _picture_from_pngs(pages)
    raise VisualExportError("unsupported visual export format")
