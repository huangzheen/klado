#!/usr/bin/env python3
"""
Klado — Playwright PDF Export Service
=========================================
Runs on the HOST machine (not in Docker), listens on port 19010.
Accepts POST /pdf with JSON body:
  { "html": "...", "filename": "export.pdf", "content_width": 900 }
Returns a PDF binary (application/pdf).

Usage:
  python3 scripts/pdf_service.py

Install dependencies (if not already installed):
  pip install playwright && playwright install chromium
"""

import asyncio
import base64
import json
import re
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


def _inline_remixicon(html: str) -> str:
    """Replace <link href="...remixicon.css"> with an inlined @font-face using
    a base64 data URI so Playwright's sandboxed page can render the glyphs
    without any network access."""
    link_pat = re.compile(
        r'<link[^>]+href=["\']([^"\']*remixicon\.css[^"\']*)["\'][^>]*>', re.I
    )
    m = link_pat.search(html)
    if not m:
        return html  # nothing to inline

    css_url = m.group(1)
    # Ensure absolute URL
    if css_url.startswith('/'):
        css_url = 'http://localhost' + css_url
    elif not css_url.startswith('http'):
        css_url = 'http://localhost/' + css_url

    try:
        css_text = urllib.request.urlopen(css_url, timeout=10).read().decode('utf-8')
    except Exception as e:
        print(f"[pdf-service] WARNING: could not fetch remixicon CSS: {e}", flush=True)
        return html

    # Find the woff2 src URL inside the CSS (most modern file, best support)
    woff2_pat = re.compile(r'url\(["\']?([^"\')\s]*remixicon\.woff2[^"\')\s]*)["\']?\)')
    wm = woff2_pat.search(css_text)
    if not wm:
        print("[pdf-service] WARNING: remixicon.woff2 not found in CSS", flush=True)
        return html

    font_url = wm.group(1)
    # Resolve relative font URL against CSS base URL
    css_base = css_url.rsplit('/', 1)[0] + '/'
    if not font_url.startswith('http'):
        font_url = css_base + font_url

    try:
        font_bytes = urllib.request.urlopen(font_url, timeout=10).read()
    except Exception as e:
        print(f"[pdf-service] WARNING: could not fetch remixicon.woff2: {e}", flush=True)
        return html

    b64 = base64.b64encode(font_bytes).decode('ascii')
    # Build a minimal @font-face with the icon class rules preserved from original CSS
    # (keep all rules except @font-face itself; replace the src with data URI)
    clean_css = re.sub(r'@font-face\s*\{[^}]*\}', '', css_text, flags=re.S)
    font_face = (
        f'@font-face {{\n'
        f'  font-family: "remixicon";\n'
        f'  src: url("data:font/woff2;base64,{b64}") format("woff2");\n'
        f'  font-display: block;\n'
        f'}}\n'
    )
    inline_style = f'<style>{font_face}{clean_css}</style>'
    return html[:m.start()] + inline_style + html[m.end():]

PORT = 19010
# A4 at 96 dpi = 794×1123px
A4_W_PX   = 794
A4_H_PX   = 1123
# Margins: top 8mm, bottom 8mm, left/right 6mm  (1mm = 3.7795px at 96dpi)
A4_MARGIN_TOP    = 30   # 8mm
A4_MARGIN_BOTTOM = 30   # 8mm
A4_MARGIN_LR     = 23   # 6mm
# Usable height: page minus margins, minus ~6mm for native page-number header
A4_USABLE_W = A4_W_PX - A4_MARGIN_LR * 2          # 748px
A4_USABLE_H = A4_H_PX - A4_MARGIN_TOP - A4_MARGIN_BOTTOM - 23  # ~1040px


async def _render_pdf(html: str, content_width: int) -> bytes:
    from playwright.async_api import async_playwright

    # Embed remixicon font as base64 data URI so headless Chromium
    # (sandboxed, no localhost access) can render icon glyphs.
    html = _inline_remixicon(html)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── Pass 1: measure height at A4 width ─────────────────────────────
        # Viewport width = A4 usable width so the table lays out exactly as
        # it will appear on paper.  Only height can overflow → scale by height.
        page = await browser.new_page()
        await page.set_viewport_size({"width": A4_USABLE_W, "height": 5000})
        await page.set_content(html, wait_until="load", timeout=60000)
        dims: dict = await page.evaluate("""() => {
            const pages = Array.from(document.querySelectorAll('.ap-page'));
            const maxH = Math.max(...pages.map(el => el.getBoundingClientRect().height), 0);
            return { maxH, count: pages.length };
        }""")
        await page.close()

        nat_h = dims.get("maxH") or A4_USABLE_H

        # ── Scale: only shrink for height; width already fits A4 ─────────────
        scale = max(0.2, round(min(A4_USABLE_H / nat_h, 1.0), 4))
        # Pass-2 viewport = A4_USABLE_W / scale  →  content * scale = A4 width
        p2_vp_w = max(A4_USABLE_W, int(A4_USABLE_W / scale))
        print(f"[pdf-service] combos={dims.get('count',0)} "
              f"nat_h={nat_h:.0f}px scale={scale} vp2_w={p2_vp_w}", flush=True)

        # ── Pass 2: render PDF with computed scale ────────────────────────────
        page = await browser.new_page()
        await page.set_viewport_size({"width": p2_vp_w, "height": 5000})
        await page.set_content(html, wait_until="load", timeout=60000)
        # Native header: page numbers only (brand/channel ID is in the per-page HTML header)
        header_tmpl = (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
            'font-size:8px;color:#999;width:100%;padding:0 6mm;'
            'display:flex;justify-content:flex-end;align-items:center">'
            'Page <span class="pageNumber" style="margin:0 2px"></span>'
            '&nbsp;/&nbsp;<span class="totalPages" style="margin-left:2px"></span>'
            '</div>'
        )
        footer_tmpl = '<span></span>'  # required non-empty placeholder
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            scale=scale,
            display_header_footer=True,
            header_template=header_tmpl,
            footer_template=footer_tmpl,
            margin={"top": "8mm", "right": "6mm", "bottom": "8mm", "left": "6mm"},
        )
        await page.close()
        await browser.close()
    return pdf_bytes


async def _render_screenshot(url: str, selector: str, wait_ms: int) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1600, "height": 900})
        await page.goto(url, wait_until="networkidle", timeout=60000)
        # Wait for snapshot-ready signal or fixed delay
        try:
            await page.wait_for_function(
                "document.body.dataset.snapshotReady === '1'",
                timeout=wait_ms,
            )
        except Exception:
            pass  # Fall back to fixed delay already elapsed
        await page.wait_for_timeout(500)  # Extra settle time for chart animations
        el = await page.query_selector(selector)
        if not el:
            await browser.close()
            raise ValueError(f"Selector not found: {selector}")
        png_bytes = await el.screenshot(type="png")
        await browser.close()
    return png_bytes


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path == "/screenshot":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                url = body["url"]
                selector = body.get("selector", "body")
                wait_ms = int(body.get("wait_ms", 15000))
                png_bytes = asyncio.run(_render_screenshot(url, selector, wait_ms))
                b64 = base64.b64encode(png_bytes).decode("ascii")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"image_base64": b64}).encode())
            except Exception as e:
                print(f"[pdf-service] /screenshot ERROR: {e}", file=sys.stderr)
                try:
                    self.send_response(500)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
                except Exception:
                    pass
            return

        if self.path != "/pdf":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            html = body["html"]
            filename = body.get("filename", "export.pdf")
            content_width = int(body.get("content_width", 900))

            pdf_bytes = asyncio.run(_render_pdf(html, content_width))

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.end_headers()
            self.wfile.write(pdf_bytes)
        except Exception as e:
            print(f"[pdf-service] ERROR: {e}", file=sys.stderr)
            try:
                self.send_response(500)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        print(f"[pdf-service] {fmt % args}")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[pdf-service] Listening on http://0.0.0.0:{PORT}/pdf")
    print("[pdf-service] Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[pdf-service] Stopped.")
