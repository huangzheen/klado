"""Export one business-knowledge page as Word, PDF or a picture.

**The page is rendered by the browser that is looking at it, and posted here.** That is
the whole design: the wiki's markdown renderer, its syntax highlighting, its Mermaid
pass and its image-width handling all live in the SPA, so re-implementing markdown in
Python would create a second renderer to keep in step — and this codebase has already
been bitten more than once by two implementations of one rule drifting apart (see
`routers/reports.py::detect_langs` vs `_LANG_RE`, `may_read` vs `READABLE_PREDICATE`).

What arrives is therefore trusted only as *content*: scripts are stripped, the document
is wrapped by us, and every resource it references is fetched by us.
"""
from __future__ import annotations

import base64
import io
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

# Same 16:9-ish reading column the app uses, so a PDF page break lands where the reader
# would expect one.
CONTENT_WIDTH_PX = 940
MAX_PICTURE_HEIGHT_PX = 20000
PNG_MEDIA_TYPE = "image/png"
PDF_MEDIA_TYPE = "application/pdf"

# ⚠️ The exported document MUST carry its own fonts. The container has no usable Chinese
# font: `api/fonts/*.otf` — copied to /usr/share/fonts for fontconfig — turned out to be
# **GitHub 404 pages saved as .otf** (2026-09-30), and fontconfig's fallback is a Latin-only
# face. So every Chinese glyph rendered as tofu ("乱码" / missing in the picture export)
# while the PDF's text layer looked perfect, which is why it read as a rendering mystery
# rather than a missing file. These two faces are already in the image for the report deck
# (`frontend/out/vendor/fonts`), so the export uses the same ones and stays deterministic.
# Paths are relative: the document has a `<base href>` pointing at the mount point.
FONT_CSS = """
  @font-face { font-family: "Roboto Condensed"; font-style: normal; font-weight: 100 900;
    font-display: block; src: url(vendor/fonts/roboto-condensed-latin.woff2) format("woff2"); }
  @font-face { font-family: "Roboto Condensed"; font-style: normal; font-weight: 100 900;
    font-display: block; src: url(vendor/fonts/roboto-condensed-latin-ext.woff2) format("woff2"); }
  @font-face { font-family: "CoolSans SC Narrow"; font-style: normal; font-weight: 400;
    font-display: block; src: url(vendor/fonts/coolsans-sc-narrow-400.woff2) format("woff2"); }
  @font-face { font-family: "CoolSans SC Narrow"; font-style: normal; font-weight: 700;
    font-display: block; src: url(vendor/fonts/coolsans-sc-narrow-700.woff2) format("woff2"); }
"""

FONT_STACK = '"Roboto Condensed", "CoolSans SC Narrow"'

# Everything the reader sees in the wiki body, narrowed to what a print sheet needs.
# Deliberately duplicated in spirit from `.kb-render` in index.html but not in substance:
# this is paper, that is a screen.
PRINT_CSS = f"""
  @page {{ size: A4; margin: 14mm 14mm; }}
  html, body {{ background: #fff; }}
  body {{ margin: 0; font-family: {FONT_STACK}, -apple-system, "PingFang SC",
         "Microsoft YaHei", "Noto Sans SC", sans-serif;
         font-size: 13px; line-height: 1.62; color: #0f172a; }}
  main {{ max-width: 940px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 12px; }}
  h2 {{ font-size: 17px; font-weight: 700; margin: 18px 0 7px; }}
  h3 {{ font-size: 15px; font-weight: 700; margin: 15px 0 6px; }}
  h4, h5, h6 {{ font-size: 13px; font-weight: 700; margin: 13px 0 5px; }}
  p {{ margin: 0 0 9px; }}
  ul, ol {{ margin: 0 0 9px; padding-left: 22px; }}
  li {{ margin: 2px 0; }}
  blockquote {{ margin: 0 0 9px; padding: 5px 12px; border-left: 3px solid #93b4f1;
               background: #f4f7fe; color: #334155; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "CoolSans SC Narrow",
         monospace; font-size: 0.86em;
         background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 4px; padding: 1px 5px; }}
  pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "CoolSans SC Narrow",
        monospace;
        background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px;
        overflow: hidden; margin: 0 0 10px; color: #0f172a; white-space: pre-wrap;
        word-break: break-word; }}
  pre code {{ background: none; border: 0; padding: 0; font-size: 12px; line-height: 1.5;
             color: inherit; }}
  img {{ max-width: 100%; height: auto; border-radius: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 10px; font-size: 12px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 5px 8px; text-align: left; }}
  th {{ background: #f8fafc; font-weight: 700; }}
  hr {{ border: 0; border-top: 1px solid #e2e8f0; margin: 16px 0; }}
  a {{ color: #1d4ed8; }}
  /* A diagram or a table must not be split across a page, and it must not be taller than
     one either: an unbreakable block that does not fit leaves a whole page nearly empty
     (that is how this export turned a one-screen note into three pages with a blank first). */
  .kb-mermaid {{ text-align: center; break-inside: avoid; }}
  .kb-mermaid svg {{ max-width: 100%; height: auto; max-height: 780px; }}
  /* ⚠️ A Mermaid SVG carries its OWN font stack ("trebuchet ms", verdana, arial…)
     and does not inherit the body's, so its Chinese labels were the one place still
     coming out as tofu while the rest of the page was already correct. */
  .kb-mermaid svg text, .kb-mermaid svg tspan, .kb-mermaid svg .nodeLabel {{
    font-family: {FONT_STACK}, sans-serif !important; }}
  table {{ break-inside: avoid; }}
  .doc-title {{ font-size: 24px; font-weight: 700; margin: 0 0 3px; }}
  .doc-meta {{ font-size: 11px; color: #64748b; margin: 0 0 16px; }}
  .doc-foot {{ margin-top: 20px; padding-top: 8px; border-top: 1px solid #e2e8f0;
              font-size: 10px; color: #94a3b8; }}
"""

# The syntax-highlight tokens the client renderer emits; kept readable on paper instead of
# reproducing the screen palette exactly.
TOKEN_CSS = """
  .kb-t-k { color: #1d4ed8; font-weight: 700; }
  .kb-t-s { color: #047857; }
  .kb-t-n { color: #b45309; }
  .kb-t-c { color: #94a3b8; font-style: italic; }
  .kb-t-v { color: #7c3aed; }
  .kb-t-f { color: #0e7490; }
"""

_SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed|link|meta)\b[^>]*>.*?<\s*/\s*\1\s*>"
                        r"|<\s*(script|iframe|object|embed)\b[^>]*/?>",
                        re.IGNORECASE | re.DOTALL)
_EVENT_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
# The note layer and its toggle: containers AND their contents. Removing only the
# opening tag would leave the bubble text behind as a paragraph in the PDF.
#
# ⚠️ This is the net, not the fix: the regex cannot count nested elements, so a layer
# with bubbles inside can leave the bubbles' closing tags behind. The precise strip is
# the DOM-aware `stripNoteChrome()` in the SPA, which every export path calls before
# submitting. Keep this anyway — the server boundary should not trust the client to
# have done it, and a few stray `</div>`s are cheaper than a note pasted into a PDF.
_NOTE_CHROME_RE = re.compile(
    r"<\s*(div|button|section|aside)\b[^>]*class\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)"
    r"[^>]*\bdoc-anno-(?:layer|toggle|menu|compose)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL)


def sanitize_fragment(html: str) -> str:
    """Keep the page's content, drop everything that could act.

    The submitted HTML comes from our own renderer, which already escapes raw HTML — but
    this is the server's boundary, and "the client always escapes it" is not a property
    the server should rely on. Scripts, frames and inline event handlers never survive.

    The reader's note chrome goes too, on purpose: it is app furniture (a floating
    layer of bubbles and a "Notes 3" pill), not document content, and a PDF that
    carries a stray pill over the chart is a bug the reader cannot explain. The
    client strips it as well — this is the second line, not the first.
    """
    text = _SCRIPT_RE.sub("", html or "")
    text = _NOTE_CHROME_RE.sub("", text)
    return _EVENT_ATTR_RE.sub("", text)


def build_document(html: str, title: str, *, meta: str = "", base_href: str = "/") -> str:
    """A standalone, printable document: our CSS + the reader's rendered body."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<base href='{base_href}'>"
        f"<title>{_esc(title)}</title>"
        f"<style>{FONT_CSS}{PRINT_CSS}{TOKEN_CSS}</style></head><body><main>"
        f"<div class='doc-title'>{_esc(title)}</div>"
        + (f"<div class='doc-meta'>{_esc(meta)}</div>" if meta else "")
        + f"<div class='doc-body'>{sanitize_fragment(html)}</div>"
        "<div class='doc-foot'>Exported from </div>"
        "</main></body></html>"
    )


def _esc(value: str) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ── PDF and picture (Chromium) ───────────────────────────────────────────────

def route_kind(url: str, *, origin: str, page_path: str) -> str:
    """What a request during an export actually is: `page`, `same-origin` or `external`.

    ⚠️ This exists because the first version matched the page by **prefix**, so every
    same-origin request — `/api/storage/serve?...` above all — was answered with the page's
    own HTML. The export still produced a PDF, the images still "loaded", and every one of
    them was the wrong bytes. A pure function so that cannot come back quietly.
    """
    parsed = urlparse(url or "")
    if f"{parsed.scheme}://{parsed.netloc}" != origin:
        return "external" if parsed.scheme in {"http", "https"} else "other"
    if not parsed.query and parsed.path.rstrip("/") in {"", page_path}:
        return "page"
    return "same-origin"


def _short(url: str) -> str:
    """A URL short enough to put in an error message, with nothing session-shaped in it."""
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    return (parsed.path or parsed.netloc or url or "?") + ("?…" if parsed.query else "")


class ExportError(RuntimeError):
    pass


async def render_visual(document: str, kind: str, *, base_href: str,
                        auth_headers: dict[str, str] | None = None) -> tuple[bytes, str, str]:
    """Return ``(content, media_type, extension)`` for ``pdf`` or ``picture``.

    Same-origin resources (the page's images, served by `/api/storage/serve`) are fetched
    with the caller's own credentials — an export must not be a way to read a file the
    requester could not open themselves.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    if kind not in {"pdf", "picture"}:
        raise ExportError(f"不支持的导出类型: {kind} / unsupported export kind: {kind}")
    origin = "http://127.0.0.1:8000"
    page_url = origin + base_href
    page_path = urlparse(page_url).path.rstrip("/")
    auth = {key: value for key, value in (auth_headers or {}).items() if value}
    failed_images: set[str] = set()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": CONTENT_WIDTH_PX + 120, "height": 1200})
            # An export with a missing figure is worse than a failed export: the reader
            # cannot tell "no chart here" from "the chart did not load". Same rule as
            # services/report_visual_export.py — and it covers **fonts** too, because a font
            # that quietly fails to load is exactly the tofu this export was just fixed for.
            wanted = {"image", "font"}
            page.on("requestfailed", lambda request: failed_images.add(_short(request.url))
                    if request.resource_type in wanted else None)
            page.on("response", lambda response: failed_images.add(_short(response.url))
                    if response.status >= 400
                    and response.request.resource_type in wanted else None)

            async def route_handler(route):
                kind = route_kind(route.request.url, origin=origin, page_path=page_path)
                if kind == "page":
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                        body=document)
                elif kind == "same-origin":
                    # The page's own images, fetched with the caller's credentials.
                    headers = dict(route.request.headers)
                    headers.update(auth)
                    await route.continue_(headers=headers)
                elif kind == "external":
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", route_handler)
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.evaluate("document.fonts ? document.fonts.ready : null")
                await page.evaluate(
                    "Promise.all([...document.images].map(i => i.decode().catch(() => {})))")
            except PlaywrightTimeoutError as exc:
                # A resource that hangs (rather than refuses) would otherwise surface as a
                # raw 500 from the route. Say what actually happened.
                raise ExportError(
                    "页面没有在导出前加载完成 —— 有资源超时未响应"
                    " / the page did not finish loading for export — a resource did not "
                    "respond in time") from exc
            if failed_images:
                raise ExportError("这些资源加载失败（缺图或缺字体会导出成空洞或方框）: "
                                  + ", ".join(sorted(failed_images)[:3])
                                  + " / these resources could not be loaded for the export (a "
                                    "missing image or font would ship as a hole or as boxes): "
                                  + ", ".join(sorted(failed_images)[:3]))

            if kind == "pdf":
                data = await page.pdf(format="A4", print_background=True,
                                      margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
                return data, PDF_MEDIA_TYPE, "pdf"

            height = await page.evaluate("document.documentElement.scrollHeight")
            if height > MAX_PICTURE_HEIGHT_PX:
                raise ExportError("页面太长，无法导出为一张图片 —— 请改用 PDF 导出 / this page is too tall for one picture — export it as PDF instead")
            await page.set_viewport_size({"width": CONTENT_WIDTH_PX + 120, "height": min(height, 1200)})
            data = await page.screenshot(full_page=True, type="png", animations="disabled")
            return data, PNG_MEDIA_TYPE, "png"
        finally:
            await browser.close()


# ── Word ─────────────────────────────────────────────────────────────────────
# ── Word ─────────────────────────────────────────────────────────────────────
#
# ⚠️ Deliberately **not** a real .docx. Building one means python-docx (plus lxml), and
# adding that dependency invalidated two slow Docker layers (pip + the `playwright install
# chromium` right after it), which turned a ~1.5 minute deploy into 16+ minutes. A `.doc`
# whose content is Word-flavoured HTML opens in Word with the images embedded, the sizes
# kept and the tables intact — and adds nothing to the image. The cost is that headings and
# tables are Word's HTML import rather than native Word objects, so it is a document to read
# rather than one to restyle. If that ever becomes the requirement, the fix is python-docx
# and accepting the slow first build.

WORD_MEDIA_TYPE = "application/msword"

# `@page WordSection1` + the `w:WordDocument` block are what make Word treat an .doc that
# contains HTML as a Word document instead of showing the "different format" warning.
WORD_CSS = """
  @page WordSection1 { size: 21cm 29.7cm; margin: 2cm 2cm 2cm 2cm; }
  div.WordSection1 { page: WordSection1; }
  body { font-family: "Microsoft YaHei", "PingFang SC", Calibri, sans-serif;
         font-size: 10.5pt; line-height: 1.62; color: #0f172a; }
  h1 { font-size: 20pt; font-weight: bold; margin: 0 0 10pt; }
  h2 { font-size: 15pt; font-weight: bold; margin: 16pt 0 6pt; }
  h3 { font-size: 12.5pt; font-weight: bold; margin: 13pt 0 5pt; }
  h4, h5, h6 { font-size: 11pt; font-weight: bold; margin: 11pt 0 4pt; }
  p { margin: 0 0 8pt; }
  ul, ol { margin: 0 0 8pt; padding-left: 22pt; }
  blockquote { margin: 0 0 8pt; padding: 4pt 10pt; border-left: 2pt solid #93b4f1;
               background: #f4f7fe; color: #334155; }
  code { font-family: Consolas, "Courier New", monospace; font-size: 9.5pt;
         background: #f1f5f9; }
  pre { font-family: Consolas, "Courier New", monospace; font-size: 9pt; line-height: 1.45;
        background: #f8fafc; border: 0.5pt solid #e2e8f0; padding: 8pt; white-space: pre-wrap;
        word-break: break-word; }
  img { max-width: 100%; }
  table { border-collapse: collapse; width: 100%; font-size: 10pt; }
  th, td { border: 0.5pt solid #cbd5e1; padding: 4pt 6pt; text-align: left; }
  th { background: #f1f5f9; font-weight: bold; }
  .doc-title { font-size: 22pt; font-weight: bold; margin: 0 0 3pt; }
  .doc-meta { font-size: 9pt; color: #64748b; margin: 0 0 16pt; }
  .doc-foot { margin-top: 18pt; padding-top: 6pt; border-top: 0.5pt solid #e2e8f0;
              font-size: 8pt; color: #94a3b8; }
  .kb-mermaid { text-align: center; }
"""

WORD_TOKEN_CSS = """
  .kb-t-k { color: #1d4ed8; font-weight: bold; }
  .kb-t-s { color: #047857; }
  .kb-t-n { color: #b45309; }
  .kb-t-c { color: #94a3b8; font-style: italic; }
  .kb-t-v { color: #7c3aed; }
  .kb-t-f { color: #0e7490; }
"""


def _style_of(node) -> dict[str, str]:
    """`style="width:560px;max-width:100%"` → `{width: "560px", max-width: "100%"}`."""
    out: dict[str, str] = {}
    for part in str(node.get("style") or "").split(";"):
        if ":" in part:
            key, _, value = part.partition(":")
            out[key.strip().lower()] = value.strip()
    return out


def _word_width(spec: str | None) -> str:
    """A rendered `width:560px` as a Word-safe length, or `''` when there is nothing to say.

    Word's HTML import maps px to points (1px = 0.75pt), but it is happier being told so; a
    percentage is left alone because Word resolves it against the container, exactly as the
    browser did.
    """
    text = str(spec or "").strip().lower()
    if not text:
        return ""
    if text.endswith("%"):
        try:
            share = float(text[:-1])
        except ValueError:
            return ""
        return f"{share:g}%" if 0 < share <= 100 else ""
    match = re.match(r"^([0-9.]+)\s*(px)?$", text)
    if not match:
        return ""
    try:
        return f"{float(match.group(1)) * 0.75:g}pt"
    except ValueError:
        return ""


def build_word_document(html: str, title: str, *, meta: str = "",
                        images: dict[str, bytes] | None = None) -> str:
    """A Word-openable document: the reader's body, with its images embedded.

    Images are inlined as data URIs rather than linked. ⚠️ A linked image would be fetched by
    Word at open time, and the page's images come from `/api/storage/serve`, which needs the
    *reader's* session — a link in a file that gets mailed around would simply be a hole. The
    bytes are already fetched here, with the requester's own credentials.
    """
    soup = BeautifulSoup(sanitize_fragment(html or ""), "html.parser")
    blob = images or {}
    for image in soup.find_all("img"):
        src = str(image.get("src") or "").strip()
        data = blob.get(src)
        width = _word_width(_style_of(image).get("width") or image.get("data-kb-w"))
        if not data:
            # Never drop a picture's space silently: a page that had one should say so.
            placeholder = soup.new_tag("span")
            placeholder.string = "[image: " + (str(image.get("alt") or "").strip() or src) + "]"
            image.replace_with(placeholder)
            continue
        image["src"] = "data:" + (_mime_for(data)) + ";base64," + base64.b64encode(data).decode()
        if width:
            image["style"] = f"width:{width};max-width:100%"
            image["width"] = width.replace("pt", "").replace("%", "%")
        else:
            image["style"] = "max-width:100%"
        image.attrs.pop("loading", None)
        image.attrs.pop("data-kb-w", None)
    return (
        "<html xmlns:o='urn:schemas-microsoft-com:office:office' "
        "xmlns:w='urn:schemas-microsoft-com:office:word' "
        "xmlns='http://www.w3.org/TR/REC-html40'><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title>"
        "<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View>"
        "<w:Zoom>100</w:Zoom><w:DoNotOptimizeForBrowser/></w:WordDocument></xml><![endif]-->"
        f"<style>{WORD_CSS}{WORD_TOKEN_CSS}</style></head>"
        "<body><div class='WordSection1'>"
        f"<div class='doc-title'>{_esc(title)}</div>"
        + (f"<div class='doc-meta'>{_esc(meta)}</div>" if meta else "")
        + "<div class='doc-body'>" + str(soup) + "</div>"
        "<div class='doc-foot'>Exported from </div>"
        "</div></body></html>"
    )


def _mime_for(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


async def fetch_bytes(url: str, *, base_href: str, auth_headers: dict[str, str] | None = None) -> bytes | None:
    """Fetch one referenced image, with the caller's credentials, for embedding in Word."""
    if not url:
        return None
    target = url if url.startswith(("http://", "https://")) else urljoin(
        "http://127.0.0.1:8000" + base_href, url.lstrip("/") if url.startswith("/") else url)
    headers = {key: value for key, value in (auth_headers or {}).items() if value}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(target, headers=headers)
        if response.status_code != 200 or len(response.content) < 32:
            return None
        return response.content
    except Exception:
        return None

# ── One export, two features ─────────────────────────────────────────────────
#
# The wiki and the Calendar export the same thing — the page the reader is looking at, run
# through the same document shell and the same headless renderer. Keeping the assembly in
# one place is the point: `routers/business_knowledge.py` and `routers/calendar.py` supply
# the already-rendered HTML and the permission check, and nothing else.

def auth_headers(request) -> dict[str, str]:
    """The caller's own credentials, so an export fetches only what they could read.

    The renderer is a headless browser with no session of its own: without this it would
    request a page's images anonymously and get 401s back into the document.
    """
    return {key: value for key, value in (
        ("cookie", request.headers.get("cookie")),
        ("authorization", request.headers.get("authorization")),
    ) if value}


async def export_document(kind: str, html: str, title: str, *, meta: str = "",
                          base_href: str = "/",
                          auth_headers: dict[str, str] | None = None) -> tuple[bytes, str, str]:
    """Build one export file. Returns `(bytes, media_type, extension)`.

    `kind` is `word` | `pdf` | `picture`. Anything that goes wrong raises `ExportError`,
    which the caller is expected to turn into a 422 — a caller that has not already
    validated `kind` should check it first so it can answer 400 instead.
    """
    if kind not in ("word", "pdf", "picture"):
        raise ExportError("kind 只能是 word、pdf 或 picture / kind must be word, pdf or picture")

    if kind == "word":
        fragment = sanitize_fragment(html or "")
        # The images are embedded, so they have to be here before the document is built.
        resolved: dict[str, bytes] = {}
        for src in set(re.findall(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', fragment, re.I)):
            data = await fetch_bytes(src, base_href=base_href, auth_headers=auth_headers)
            if data:
                resolved[src] = data
        try:
            built = build_word_document(fragment, title, meta=meta, images=resolved)
        except Exception as exc:  # noqa: BLE001 — reported as a 422, never as a 500
            raise ExportError(f"Word 文件生成失败: {exc} / could not build the Word file: {exc}") from exc
        return built.encode("utf-8"), WORD_MEDIA_TYPE, "doc"

    document = build_document(html or "", title, meta=meta, base_href=base_href)
    return await render_visual(document, kind, base_href=base_href, auth_headers=auth_headers)
