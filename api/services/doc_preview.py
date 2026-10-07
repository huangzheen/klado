"""Read-only Office document preview rendering (pptx / docx / xlsx -> HTML,
HTML -> PDF).

This is the rendering engine behind the Workspace document cards: an agent can
write a .pptx / .xlsx / .docx / .pdf into a workspace, and opening the card has
to *show* the document in the browser instead of only offering a download.
PDFs are displayed by the browser itself; spreadsheets become HTML tables; decks
and documents are converted to self-contained HTML and rasterised to a PDF
preview through the Chromium that already ships in the image.

Safety contract -- the generated markup is served to end users:

* the HTML never contains a ``<script>`` element (nor any ``on*=`` handler);
* every author-supplied string passes through :func:`html.escape`; nothing taken
  from the document is ever concatenated into the markup as raw HTML;
* images are only read from the container's own binary parts and only when the
  detected content type is in :data:`IMAGE_MIME_OK`; a single image larger than
  :data:`MAX_IMAGE_BYTES` is skipped instead of shipped;
* unknown / malformed content is skipped rather than raised -- only an
  unreadable container or an unsupported ``doc_type`` raises
  :class:`PreviewError`.

Only the standard library plus python-pptx / python-docx / openpyxl / playwright
are imported: no database, no environment, no network, no sibling project
module.

Geometry notes (they bite when touched):

* EMU -> px is ``emu / 9525`` (914400 EMU per inch, 96 CSS px per inch);
  pt -> px is ``pt * 4 / 3``.
* A slide is always laid out on a fixed 1280x720 canvas, so pptx geometry is
  scaled per axis by ``canvas / slide_size``; a deck whose slide size is not
  16:9 still fills the canvas (it is a preview, not a pixel-exact print).
* Chromium swaps the paper width/height when ``landscape=True``, so
  :func:`html_to_pdf` hands it the *rotated* box -- otherwise the printed page
  is narrower than the slide and the right-hand side gets clipped.
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import io
import os
import re

import logging

_LOG = logging.getLogger(__name__)
from contextlib import closing
from datetime import date, datetime, time
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# public surface
# --------------------------------------------------------------------------

DOC_MIME: dict[str, str] = {
    "pptx": (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    ),
    "docx": (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    ),
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}

#: doc types :func:`preview_html` knows how to turn into markup. All four: the reader
#: page embeds one HTML preview for every card, so a workbook and a PDF are rendered
#: here too (see `_pdf_html` for why a PDF is not simply handed to the browser).
HTML_DOC_TYPES = ("pptx", "docx", "xlsx", "pdf")

#: The per-page elements a note layer anchors to, in document order. `services.annotations`
#: hands this to the runtime as its `pages` selector, which is what makes "this note belongs
#: to slide 4" true rather than "42% down a long scroll".
PAGE_SELECTOR = ".pv-page, .pv-slide, .pv-sheet"

#: content types accepted for embedded images (everything else is skipped).
IMAGE_MIME_OK = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/bmp",
        "image/tiff",
        "image/webp",
    }
)

#: a single image larger than this never enters the HTML.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: soft ceiling for one generated document; the caller decides what to do when
#: it is exceeded, the renderer just reports it through :func:`estimate_size`.
MAX_HTML_BYTES = 12 * 1024 * 1024

EMU_PER_PX = 9525.0  # 1 CSS px = 9525 EMU, so 914400 EMU = 96px

SLIDE_W = 1280
SLIDE_H = 720
DOC_PAGE_W = 900

_DEFAULT_TEXT_PX = 24  # PowerPoint's 18pt default body size


class PreviewError(Exception):
    """Raised when a document cannot be turned into a preview.

    The message is user-facing: it says *why* (unsupported type, unreadable
    container, renderer failure), never a bare traceback string.
    """


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _esc(value: Any) -> str:
    """Escape any author-supplied value for use in text or attribute position."""
    return _html.escape("" if value is None else str(value), quote=True)


def _css(**pairs: Any) -> str:
    """Build a ``property: value;`` string, dropping empty values."""
    out = []
    for key, value in pairs.items():
        if value is None or value == "":
            continue
        out.append(f"{key.replace('_', '-')}:{value}")
    return ";".join(out)


def _num(value: float, digits: int = 2) -> str:
    """Format a length so the markup stays small (``120px``, not ``120.0px``)."""
    if value is None:
        return "0"
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _clamp_box(left: Any, top: Any, width: Any, height: Any) -> Optional[tuple]:
    """Return ``(left, top, width, height)`` in px, or None when unusable."""
    try:
        left = float(left)
        top = float(top)
        width = float(width)
        height = float(height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return left, top, width, height


def _emu_to_px(value: Any) -> Optional[float]:
    """CSS px for an EMU length (914400 EMU = 1in, 96 CSS px = 1in).

    Everything python-pptx / python-docx hands back as a ``Length`` goes through
    here, **including font sizes and paragraph spacing**: ``float(Pt(16))`` is
    203200 (EMU), not 16.  Reading that number as points is how a 16pt run turns
    into a 270933px one.
    """
    try:
        return float(value) / EMU_PER_PX
    except (TypeError, ValueError):
        return None


def _color_hex(color_format: Any) -> Optional[str]:
    """``#RRGGBB`` for an explicitly set colour, else None.

    Theme / inherited colours are deliberately *not* guessed: an unresolved
    colour is better than a wrong one.
    """
    if color_format is None:
        return None
    try:
        if color_format.type is None:
            return None
        rgb = color_format.rgb
    except Exception:
        return None
    if rgb is None:
        return None
    return f"#{rgb}"


def _solid_fill_bg(fill: Any) -> Optional[str]:
    """Background colour for a solid fill, else None."""
    try:
        from pptx.enum.dml import MSO_FILL

        if fill is None or fill.type != MSO_FILL.SOLID:
            return None
        return _color_hex(fill.fore_color)
    except Exception:
        return None


def _solid_line(line: Any) -> tuple[Optional[str], Optional[float]]:
    """``(colour, width_px)`` for a visible outline, else ``(None, None)``."""
    try:
        from pptx.enum.dml import MSO_FILL

        if line is None or line.fill.type != MSO_FILL.SOLID:
            return None, None
        colour = _color_hex(line.color)
        if not colour:
            return None, None
        width_px = _emu_to_px(line.width) if line.width else None
        return colour, max(1.0, width_px or 1.0)
    except Exception:
        return None, None


class _ImageBudget:
    """Total embedded-image allowance for one document.

    The per-image cap alone is not enough: a deck of a hundred 7MB pictures
    still produces a 900MB page.  The budget counts the *encoded* size (base64
    grows the payload by a third) and simply stops embedding once the document
    would pass :data:`MAX_HTML_BYTES`.
    """

    def __init__(self, limit: int = MAX_HTML_BYTES):
        self.limit = max(0, int(limit))
        self.used = 0
        self.skipped = 0

    def take(self, encoded_size: int) -> bool:
        if encoded_size > self.limit or self.used + encoded_size > self.limit:
            self.skipped += 1
            return False
        self.used += encoded_size
        return True


def _data_uri(
    blob: bytes, content_type: str, budget: Optional[_ImageBudget] = None
) -> Optional[str]:
    """``data:`` URI for a whitelisted image, else None (skipped)."""
    if not blob:
        return None
    mime = (content_type or "").strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in IMAGE_MIME_OK:
        return None
    if len(blob) > MAX_IMAGE_BYTES:
        return None
    encoded = (len(blob) + 2) // 3 * 4
    if budget is not None and not budget.take(encoded):
        return None
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def estimate_size(markup: str) -> int:
    """Byte size of a generated document (UTF-8), for budget checks."""
    return len(markup.encode("utf-8", "replace"))


# The CJK face inlined into every preview. See `_cjk_font_face`.
_PREVIEW_FONT_FAMILY = "PreviewCJK"
_cjk_face_cache: Optional[str] = None


def _cjk_font_face() -> str:
    """An ``@font-face`` for Chinese text, inlined as a data URI.

    ⚠️ Why this has to be inlined: the preview is rendered by headless Chromium on **the machine
    running the API**, and that machine has no CJK font installed for it — ``api/fonts/`` holds a
    README and nothing else, so the font stack below falls back to a face with no Chinese
    glyphs. Every
    Chinese character in a pptx/docx preview came out as a box (reported 2026-10-01: "docx 和
    pptx 里面…显示的都是问号问号"). A `file://` @font-face is not an option either: the page is
    loaded with `page.set_content()`, so its origin is `about:blank`.

    The bytes are the deck's own CoolSans SC Narrow — already committed, already the font this
    product draws Chinese in, so a preview matches the pages instead of inventing a look. Read
    once and cached: a preview PDF is rendered once per (file, updated_at) and then stored.

    ⚠️ Inlined by `html_to_pdf` ONLY. The HTML that is served to a BROWSER (`preview.html`) is
    left lean on purpose: a reader's machine has its own fonts, and that is the whole reason the
    reader page renders the preview instead of a server-made PDF (2026-10-01).
    """
    global _cjk_face_cache
    if _cjk_face_cache is not None:
        return _cjk_face_cache
    face = ""
    try:
        from core.config import resolve_frontend_dir

        path = os.path.join(resolve_frontend_dir(), "vendor", "fonts",
                            "coolsans-sc-narrow-400.woff2")
        with open(path, "rb") as handle:
            payload = base64.b64encode(handle.read()).decode("ascii")
        face = ("@font-face{font-family:'%s';font-style:normal;font-weight:400;"
                "font-display:block;src:url(data:font/woff2;base64,%s) format('woff2');}"
                % (_PREVIEW_FONT_FAMILY, payload))
    except Exception as exc:  # noqa: BLE001 — a preview without CJK beats no preview
        _LOG.info("preview CJK font unavailable: %s", exc)
    _cjk_face_cache = face
    return face


def _document(title: str, css: str, body: str, script: str = "") -> str:
    """Wrap ``body`` in a self-contained document: inline CSS, no scripts by default.

    ⚠️ `script` is an opt-in and exactly one caller uses it (the deck's
    fit-to-width). It exists because "no scripts" is what makes a preview a
    rendering rather than a program, and the only way to honour that is for the
    exception to be visible at the call site rather than assumed. Whatever goes
    in here must be safe to run in a reader's browser with no session and no
    network — a preview is served to guests through `/s/<token>` as well.
    """
    safe_title = _esc(title.strip() if title else "Preview")
    tail = f"<script>{script}</script>\n" if script else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title>\n"
        f"<style>{_BASE_CSS}\n{css}</style>\n"
        "</head><body>\n"
        f"{body}\n"
        f"{tail}"
        "</body></html>"
    )


_BASE_CSS = """
html,body{margin:0;padding:0;}
/* The desk: a document is a stack of white sheets on a grey surface, the way Word,
   PowerPoint and Excel show one. Every sheet is centred and casts a soft shadow, so a
   reader recognises "a page of a document" at a glance. */
body{background:#e9edf2;color:#1f2328;
  /* ⚠️ `PreviewCJK` last: it only fills the CJK gap, so Latin keeps whatever the system gives
     it and Chinese gets the inlined face (see `_cjk_font_face`). The named CJK families above
     are kept because a future image may well have them. */
  font-family:"Noto Sans CJK SC","Noto Sans CJK","Noto Sans","Helvetica Neue",
    Helvetica,Arial,"PingFang SC","Microsoft YaHei","PreviewCJK",sans-serif;}
*{box-sizing:border-box;}
.pv-canvas{display:flex;flex-direction:column;align-items:center;gap:24px;
  padding:24px 18px 44px;}
.pv-canvas > *{flex:0 0 auto;}
/* One sheet. Shared by a Word page, a rendered PDF page and a slide card. */
.pv-sheet-frame{background:#ffffff;
  box-shadow:0 1px 3px rgba(15,23,42,.20),0 10px 24px rgba(15,23,42,.10);
  outline:1px solid #d8dee6;}
.pv-caption{font:600 11px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",
  "PingFang SC","Microsoft YaHei",sans-serif;color:#64748b;letter-spacing:.02em;}
.pv-trunc-note{margin:2px 0 0;color:#6b7280;font-size:11px;}
.pv-empty{color:#8b93a1;}
"""


# --------------------------------------------------------------------------
# pptx -> HTML
# --------------------------------------------------------------------------

# ⚠️ The deck is a FIXED 1280x720 canvas — that is the file's own geometry, scaled
# once from EMU, and re-deriving it from percentages would mean re-implementing
# PowerPoint's layout engine. A fixed canvas in a narrower window does not reflow
# in PowerPoint either: it ZOOMS. So this scales the canvas to the window and
# stops, rather than clipping the right-hand third of every slide or handing the
# reader a horizontal scrollbar for a document they are meant to read.
#
# `zoom` (not `transform: scale`) on purpose: it scales LAYOUT, so the absolutely
# positioned shapes, the table's row heights and the run font sizes all scale by
# the same factor and the slide stays internally consistent. A transform scales
# pixels and leaves every box at its old size, which is the same picture at a
# different resolution — a deck whose text no longer fits its own text box.
#
# `PV_DECK_DESIGN_W` is the width the shapes were laid out for, and it is the
# denominator below; the two must not drift apart.
_PV_DECK_DESIGN_W = 1280
_PV_CANVAS_PAD = 18          # `.pv-canvas` horizontal padding, so the sheet has air

_DECK_FIT_JS = """
(function () {
  var DECK = %(design)d, PAD = %(pad)d;
  function fit() {
    var deck = document.querySelector('.pv-deck');
    if (!deck) return;
    // ⚠️ Print is skipped on purpose. `html_to_pdf` renders this same markup at
    // the design width, and a "fit the window" rule applied to a fixed-width
    // print viewport would shrink every exported PDF by the padding.
    if (window.matchMedia && window.matchMedia('print').matches) {
      deck.style.zoom = ''; return;
    }
    var room = (window.innerWidth || DECK) - PAD * 2;
    // Never below 45%%: past that the type is unreadable and a horizontal
    // scrollbar is the better answer, which is also what PowerPoint does.
    deck.style.zoom = Math.max(0.45, Math.min(1, room / DECK));
  }
  fit();
  window.addEventListener('resize', fit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
})();
"""


_SLIDE_CSS = """
.pv-deck{display:flex;flex-direction:column;align-items:center;gap:24px;}
.pv-slide-wrap{display:flex;flex-direction:column;align-items:flex-start;gap:6px;
  break-after:page;page-break-after:always;}
.pv-slide-wrap:last-child{break-after:auto;page-break-after:auto;}
.pv-slide{position:relative;overflow:hidden;background:#ffffff;
  width:1280px;height:720px;}
.pv-slide.pv-trunc{display:flex;align-items:center;justify-content:center;
  color:#6b7280;font-size:22px;}
.pv-text{position:absolute;display:flex;flex-direction:column;
  white-space:pre-wrap;overflow:hidden;font-size:24px;}
.pv-p{margin:0;padding:0;line-height:1.18;}
.pv-table-wrap{position:absolute;overflow:hidden;}
.pv-table{width:100%;border-collapse:collapse;
  font-size:14px;table-layout:fixed;}
.pv-table td{border:1px solid #c9ced6;padding:3px 5px;vertical-align:top;
  white-space:pre-wrap;overflow-wrap:break-word;}
.pv-img{position:absolute;object-fit:contain;}
.pv-unknown{position:absolute;display:flex;align-items:center;
  justify-content:center;border:1.5px dashed #b9bec7;color:#8b93a1;
  font-size:13px;background:rgba(140,148,160,.06);
  white-space:pre-wrap;text-align:center;overflow:hidden;padding:2px;}
@page{size:1280px 720px;margin:0;}
"""


def _pptx_runs_html(paragraph: Any) -> list[str]:
    """``<span>``s for one paragraph, preserving run-level character formatting."""
    from pptx.oxml.ns import qn

    pieces: list[str] = []
    for run in paragraph.runs:
        text = run.text
        if not text:
            continue
        font = run.font
        # ⚠️ Strike and baseline are DrawingML ATTRIBUTES on `a:rPr`
        # (`strike="sngStrike"`, `baseline="30000"`), which python-pptx's `font`
        # object has no property for — the same reason `_run_highlight_hex` below
        # reads `a:highlight` by hand. Rendered here for the same reason as in the
        # .docx path: an attribute the editor can change must be one the preview
        # draws, or the save has nothing to send.
        decor = []
        if font.underline:
            decor.append("underline")
        decor.extend(_pptx_run_decorations(run, qn))
        style = _css(
            font_weight="700" if font.bold else None,
            font_style="italic" if font.italic else None,
            text_decoration=" ".join(decor) or None,
            vertical_align=_pptx_run_baseline(run, qn),
            font_size=(f"{_num(_emu_to_px(font.size))}px" if font.size else None),
            color=_color_hex(font.color),
            background=_run_highlight_hex(run, qn),
        )
        body = _esc(text)
        pieces.append(f'<span style="{style}">{body}</span>' if style else body)
    return pieces


#: DrawingML writes superscript / subscript as a per-mille `baseline`, not as
#: two named values. 30000 is PowerPoint's own "raised 30%"; -25000 is the
#: lowered counterpart, and they are the numbers Office writes.
_PPTX_BASELINE = {"sup": "30000", "sub": "-25000"}


def _pptx_run_baseline(run: Any, qn: Any) -> Optional[str]:
    try:
        rpr = run._r.find(qn("a:rPr"))
        value = rpr.get("baseline") if rpr is not None else None
    except Exception:
        return None
    if not value:
        return None
    try:
        if int(value) >= 0:
            return "super"
        return "sub"
    except Exception:
        return None


def _pptx_run_decorations(run: Any, qn: Any) -> list[str]:
    try:
        rpr = run._r.find(qn("a:rPr"))
        strike = (rpr.get("strike") if rpr is not None else None) or "none"
    except Exception:
        return []
    return ["line-through"] if strike in ("sngStrike", "dblStrike") else []


def _run_highlight_hex(run: Any, qn: Any) -> Optional[str]:
    """Highlight colour of a run (python-pptx exposes no property for it)."""
    try:
        rpr = run._r.find(qn("a:rPr"))
        if rpr is None:
            return None
        highlight = rpr.find(qn("a:highlight"))
        if highlight is None:
            return None
        srgb = highlight.find(qn("a:srgbClr"))
        if srgb is None:
            return None
        value = srgb.get("val")
        return f"#{value}" if value else None
    except Exception:
        return None


def _pptx_paragraph_props(paragraph: Any, qn: Any) -> dict:
    """Alignment / indent / bullet / spacing for one paragraph."""
    props: dict[str, Any] = {}
    try:
        from pptx.enum.text import PP_ALIGN

        align = {
            PP_ALIGN.CENTER: "center",
            PP_ALIGN.RIGHT: "right",
            PP_ALIGN.JUSTIFY: "justify",
            PP_ALIGN.LEFT: "left",
        }.get(paragraph.alignment)
        if align:
            props["text_align"] = align
    except Exception:
        pass

    level = 0
    try:
        level = int(paragraph.level or 0)
    except Exception:
        level = 0
    indent_px = level * 22

    bullet: Optional[bool] = None
    try:
        ppr = paragraph._p.find(qn("a:pPr"))
        if ppr is not None:
            if ppr.find(qn("a:buNone")) is not None:
                bullet = False
            elif ppr.find(qn("a:buChar")) is not None:
                bullet = True
            elif ppr.find(qn("a:buAutoNum")) is not None:
                bullet = True
    except Exception:
        bullet = None
    if bullet is None and level > 0:
        bullet = True
    props["bullet"] = bool(bullet)
    if indent_px or bullet:
        props["padding_left"] = f"{_num(indent_px + (14 if bullet else 0))}px"
        props["text_indent"] = "-14px" if bullet else None

    try:
        if paragraph.space_before is not None:
            props["margin_top"] = f"{_num(_emu_to_px(paragraph.space_before))}px"
        if paragraph.space_after is not None:
            props["margin_bottom"] = f"{_num(_emu_to_px(paragraph.space_after))}px"
        spacing = paragraph.line_spacing
        if spacing is not None:
            if isinstance(spacing, float) or isinstance(spacing, int):
                props["line_height"] = _num(float(spacing), 3)
            else:
                px = _emu_to_px(spacing)
                if px:
                    props["line_height"] = f"{_num(px)}px"
    except Exception:
        pass
    return props


def _pptx_text_html(text_frame: Any, shape: Any = None) -> str:
    """All paragraphs of a text frame as ``<p>`` elements."""
    from pptx.oxml.ns import qn

    if text_frame is None:
        return ""
    out: list[str] = []
    paragraphs = list(text_frame.paragraphs)
    for index, paragraph in enumerate(paragraphs):
        pieces = _pptx_runs_html(paragraph)
        if not pieces:
            inline = (paragraph.text or "").strip()
            if not inline:
                # keep deliberate blank lines, but not a leading empty one
                if index == 0 or index == len(paragraphs) - 1:
                    continue
                out.append('<p class="pv-p">&nbsp;</p>')
                continue
            pieces = [
                f'<span style="{_css(font_size=f"{_DEFAULT_TEXT_PX}px")}">'
                f"{_esc(inline)}</span>"
            ]
        props = _pptx_paragraph_props(paragraph, qn)
        bullet = props.pop("bullet", False)
        prefix = "• " if bullet else ""
        style = _css(**props)
        attr = f' style="{style}"' if style else ""
        out.append(f'<p class="pv-p"{attr}>{prefix}{"".join(pieces)}</p>')
    return "\n".join(out)


def _pptx_text_container(
    text_frame: Any, box: tuple, extra: Optional[dict] = None, oid: str = ""
) -> str:
    """Absolute text box: ``<div class="pv-text">`` with the frame's anchor."""
    from pptx.enum.text import MSO_ANCHOR

    left, top, width, height = box
    anchor = "flex-start"
    try:
        anchor = {
            MSO_ANCHOR.MIDDLE: "center",
            MSO_ANCHOR.BOTTOM: "flex-end",
            MSO_ANCHOR.TOP: "flex-start",
        }.get(text_frame.vertical_anchor, "flex-start")
    except Exception:
        anchor = "flex-start"

    wrap = getattr(text_frame, "word_wrap", None)
    white_space = "normal" if wrap else ("pre" if wrap is False else "pre-wrap")

    style = _css(
        left=f"{_num(left)}px",
        top=f"{_num(top)}px",
        width=f"{_num(width)}px",
        height=f"{_num(height)}px",
        justify_content=anchor,
        white_space=white_space,
        **(extra or {}),
    )
    # `data-oid` names the shape this box came from, so a save can find it again.
    # It is `s<slide>r<shape id>` — the shape id is the file's own, not a render
    # position, so it survives re-opening and re-ordering the deck.
    attr = f' data-oid="{oid}"' if oid else ""
    return (f'<div class="pv-text"{attr} style="{style}">'
            f'{_pptx_text_html(text_frame)}</div>')


def _pptx_table_html(shape: Any, box: tuple) -> str:
    from pptx.oxml.ns import qn

    left, top, width, height = box
    table = shape.table
    rows = list(table.rows)
    cols = list(table.columns)
    scale = (width / float(shape.width)) if shape.width else 1.0

    col_widths = []
    for column in cols:
        px = (_emu_to_px(column.width) or 0) * scale
        col_widths.append(max(12.0, px))
    total = sum(col_widths) or width
    colgroup = "".join(f'<col style="width:{_num(w / total * 100)}%">' for w in col_widths)

    body_rows: list[str] = []
    for r_index, row in enumerate(rows):
        cells: list[str] = []
        for c_index, cell in enumerate(cols):
            try:
                target = table.cell(r_index, c_index)
            except Exception:
                continue
            if getattr(target, "is_spanned", False):
                continue
            span_w = 1
            span_h = 1
            if getattr(target, "is_merge_origin", False):
                span_w = max(1, int(getattr(target, "span_width", 1) or 1))
                span_h = max(1, int(getattr(target, "span_height", 1) or 1))
            attrs = []
            if span_w > 1:
                attrs.append(f'colspan="{span_w}"')
            if span_h > 1:
                attrs.append(f'rowspan="{span_h}"')
            bg = _solid_fill_bg(target.fill)
            if bg:
                attrs.append(f'style="background:{bg}"')
            content = _pptx_text_html(target.text_frame) or "&nbsp;"
            cells.append(f"<td {' '.join(attrs)}>{content}</td>")
        row_height = _emu_to_px(row.height)
        tag = "th" if r_index == 0 else "td"
        if tag == "th":
            cells = [c.replace("<td ", "<th ").replace("</td>", "</th>") for c in cells]
        attrs = f' style="height:{_num(row_height)}px"' if row_height else ""
        body_rows.append(f"<tr{attrs}>{''.join(cells)}</tr>")

    style = _css(
        left=f"{_num(left)}px",
        top=f"{_num(top)}px",
        width=f"{_num(width)}px",
        height=f"{_num(height)}px",
    )
    return (
        f'<div class="pv-table-wrap" style="{style}">'
        f'<table class="pv-table"><colgroup>{colgroup}</colgroup>'
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


#: label per shape type that has no faithful HTML form.  Resolved through
#: ``getattr`` because python-pptx renames/omits members between releases
#: (``MSO_SHAPE_TYPE.MOVIE`` does not exist in 1.0.2 -- touching it directly is
#: an AttributeError, not a missing member).
_PLACEHOLDER_LABELS = {
    "[chart]": ("CHART",),
    "[group]": ("GROUP",),
    "[media]": ("MEDIA", "MOVIE", "WEB_VIDEO"),
    "[smartart]": ("DIAGRAM", "IGX_GRAPHIC"),
    "[ole object]": ("EMBEDDED_OLE_OBJECT", "LINKED_OLE_OBJECT", "OLE_CONTROL_OBJECT"),
    "[ink]": ("INK",),
}

_placeholder_types: Optional[dict] = None
_plain_types: Optional[frozenset] = None


def _enum_members(enum_cls: Any, names: Iterable[str]) -> list:
    """Members that actually exist on the enum (missing ones are skipped)."""
    out = []
    for name in names:
        member = getattr(enum_cls, name, None)
        if member is not None:
            out.append(member)
    return out


def _placeholder_map() -> dict:
    """``{shape_type_member: label}``, built once and cached."""
    global _placeholder_types
    if _placeholder_types is not None:
        return _placeholder_types
    mapping: dict = {}
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        for label, names in _PLACEHOLDER_LABELS.items():
            for member in _enum_members(MSO_SHAPE_TYPE, names):
                mapping[member] = label
    except Exception:
        mapping = {}
    _placeholder_types = mapping
    return mapping


def _placeholder_label(shape_type: Any) -> Optional[str]:
    if shape_type is None:
        return None
    return _placeholder_map().get(shape_type)


def _shape_kind(shape: Any) -> str:
    """A coarse label used for placeholder boxes."""
    try:
        label = _placeholder_label(shape.shape_type)
    except Exception:
        label = None
    if label:
        return label
    try:
        if getattr(shape, "has_chart", False):
            return "[chart]"
    except Exception:
        pass
    return "[shape]"


def _is_chart(shape: Any) -> bool:
    try:
        return bool(shape.has_chart)
    except Exception:
        return False


def _plain_text_types() -> frozenset:
    """Shape types whose text is already handled by the text branch."""
    global _plain_types
    if _plain_types is None:
        try:
            from pptx.enum.shapes import MSO_SHAPE_TYPE

            members = _enum_members(
                MSO_SHAPE_TYPE, ("TEXT_BOX", "PLACEHOLDER", "AUTO_SHAPE", "LINE")
            )
        except Exception:
            members = []
        _plain_types = frozenset(members)
    return _plain_types


def _chart_title(shape: Any) -> str:
    try:
        if not shape.has_chart:
            return ""
        title = shape.chart.chart_title
        if title is None:
            return ""
        return (title.text_frame.text or "").strip()
    except Exception:
        return ""


def _pptx_placeholder_html(shape: Any, box: tuple) -> str:
    left, top, width, height = box
    label = _shape_kind(shape)
    title = _chart_title(shape)
    text = f"{label}\n{title}" if title else label
    style = _css(
        left=f"{_num(left)}px",
        top=f"{_num(top)}px",
        width=f"{_num(width)}px",
        height=f"{_num(height)}px",
    )
    return f'<div class="pv-unknown" style="{style}">{_esc(text)}</div>'


def _shape_radius(shape: Any) -> Optional[str]:
    """`border-radius` for the rounded / oval autoshapes, else None."""
    try:
        from pptx.enum.shapes import MSO_SHAPE

        auto = getattr(shape, "auto_shape_type", None)
        if auto is None:
            return None
        rounded = _enum_members(
            MSO_SHAPE,
            (
                "ROUNDED_RECTANGLE",
                "ROUND_1_RECTANGLE",
                "ROUND_2_SAME_RECTANGLE",
                "ROUND_2_DIAG_RECTANGLE",
                "ROUNDED_RECTANGULAR_CALLOUT",
            ),
        )
        if auto in rounded:
            return "12px"
        oval = _enum_members(MSO_SHAPE, ("OVAL", "ELLIPSE", "DONUT"))
        if auto in oval:
            return "50%"
    except Exception:
        return None
    return None


def _pptx_image_html(
    shape: Any, box: tuple, budget: Optional[_ImageBudget] = None
) -> Optional[str]:
    left, top, width, height = box
    try:
        image = shape.image
    except Exception:
        return None
    if image is None:
        return None
    uri = _data_uri(
        getattr(image, "blob", b""), getattr(image, "content_type", ""), budget
    )
    if uri is None:
        return None
    style = _css(
        left=f"{_num(left)}px",
        top=f"{_num(top)}px",
        width=f"{_num(width)}px",
        height=f"{_num(height)}px",
    )
    return f'<img class="pv-img" alt="" style="{style}" src="{uri}">'


def _render_pptx_shape(
    shape: Any, sx: float, sy: float, budget: Optional[_ImageBudget] = None,
    oid: str = "",
) -> Optional[str]:
    """One absolutely-positioned element, or None when the shape is skipped."""
    box = _clamp_box(
        (shape.left or 0) * sx if shape.left is not None else None,
        (shape.top or 0) * sy if shape.top is not None else None,
        (shape.width or 0) * sx if shape.width is not None else None,
        (shape.height or 0) * sy if shape.height is not None else None,
    )
    if box is None:
        return None
    left, top, width, height = box

    try:
        shape_type = shape.shape_type
    except Exception:
        shape_type = None

    # 1. real table
    try:
        if getattr(shape, "has_table", False):
            return _pptx_table_html(shape, box)
    except Exception:
        pass

    # 2. binary picture
    try:
        if getattr(shape, "image", None) is not None:
            markup = _pptx_image_html(shape, box, budget)
            if markup:
                return markup
    except Exception:
        pass

    # 3. neutral placeholder for everything with no faithful HTML form
    if _placeholder_label(shape_type) or _is_chart(shape):
        return _pptx_placeholder_html(shape, box)

    # 4. text
    has_text = False
    try:
        has_text = bool(shape.has_text_frame)
    except Exception:
        has_text = False

    background = _solid_fill_bg(getattr(shape, "fill", None))
    border_color, border_px = _solid_line(getattr(shape, "line", None))

    if has_text:
        text_frame = shape.text_frame
        if not (text_frame.text or "").strip() and not background and not border_color:
            return None  # empty, invisible box: never let it cover other shapes
        return _pptx_text_container(
            text_frame,
            box,
            {
                "background": background,
                "border": (
                    f"{_num(border_px)}px solid {border_color}" if border_color else None
                ),
                "border_radius": _shape_radius(shape),
            },
            oid,
        )

    if background or border_color:
        style = _css(
            left=f"{_num(left)}px",
            top=f"{_num(top)}px",
            width=f"{_num(width)}px",
            height=f"{_num(height)}px",
            background=background,
            border=(f"{_num(border_px)}px solid {border_color}" if border_color else None),
            border_radius=_shape_radius(shape),
        )
        return f'<div class="pv-shape" style="{style}"></div>'

    if shape_type is not None and shape_type not in _plain_text_types():
        return _pptx_placeholder_html(shape, box)
    return None


def _slide_background(slide: Any) -> Optional[str]:
    try:
        return _solid_fill_bg(slide.background.fill)
    except Exception:
        return None


def _pptx_html(data: bytes, title: str, max_slides: int) -> str:
    try:
        from pptx import Presentation
        from pptx.exc import PackageNotFoundError
    except Exception as exc:  # pragma: no cover - import failure is fatal
        raise PreviewError(f"python-pptx unavailable: {exc}") from exc

    try:
        presentation = Presentation(io.BytesIO(data))
    except PackageNotFoundError as exc:
        raise PreviewError(f"not a readable .pptx container: {exc}") from exc
    except Exception as exc:
        raise PreviewError(f"could not open the .pptx: {exc}") from exc

    try:
        slides = list(presentation.slides)
    except Exception as exc:
        raise PreviewError(f"could not read the .pptx slides: {exc}") from exc

    slide_w = float(presentation.slide_width or SLIDE_W * EMU_PER_PX * 1.0) or float(
        SLIDE_W
    )
    slide_h = float(presentation.slide_height or SLIDE_H) or float(SLIDE_H)
    sx = SLIDE_W / slide_w
    sy = SLIDE_H / slide_h

    limit = max(1, int(max_slides or 1))
    shown = slides[:limit]
    budget = _ImageBudget()
    parts: list[str] = []
    for slide_index, slide in enumerate(shown):
        background = _slide_background(slide)
        style = _css(background=background)
        attr = f' style="{style}"' if style else ""
        elements: list[str] = []
        try:
            shapes: Iterable[Any] = list(slide.shapes)
        except Exception:
            shapes = []
        for shape in shapes:
            # The shape's own id, not its position: re-ordering the deck must not
            # redirect a saved edit into a different shape.
            try:
                shape_id = int(getattr(shape, "shape_id", 0) or 0)
            except (TypeError, ValueError):
                shape_id = 0
            shape_oid = "s%dr%d" % (slide_index, shape_id)
            try:
                markup = _render_pptx_shape(shape, sx, sy, budget, shape_oid)
            except Exception:
                markup = None  # a broken shape must never kill the deck
            if markup:
                elements.append(markup)
        parts.append(
            f'<div class="pv-slide-wrap">'
            f'<span class="pv-caption">Slide {len(parts) + 1}</span>'
            f'<section class="pv-slide pv-sheet-frame"{attr}>{"".join(elements)}</section>'
            f"</div>"
        )

    if len(slides) > limit:
        parts.append(
            '<section class="pv-slide pv-slide--note pv-trunc">'
            f"{_esc(f'showing first {limit} of {len(slides)} slides')}"
            "</section>"
        )
    if not parts:
        parts.append('<section class="pv-slide pv-trunc">empty presentation</section>')

    heading = title.strip() if title else ""
    # The slides live in their own `.pv-deck` box so the fit-to-width script scales
    # the deck and nothing else: the canvas's own padding and the gap between
    # slides stay at their own scale instead of shrinking with the slides.
    body = ('<div class="pv-canvas pv-canvas--slides">'
            '<div class="pv-deck">' + "\n".join(parts) + "</div></div>")
    extra = f"\n/* {_esc(heading)} */" if heading else ""
    return _document(
        heading or "Presentation", _SLIDE_CSS + extra, body,
        script=_DECK_FIT_JS % {"design": _PV_DECK_DESIGN_W, "pad": _PV_CANVAS_PAD})


# --------------------------------------------------------------------------
# docx -> HTML
# --------------------------------------------------------------------------

_DOCX_CSS = """
.pv-page-wrap{display:flex;flex-direction:column;align-items:flex-start;gap:6px;
  break-after:page;page-break-after:always;}
.pv-page-wrap:last-child{break-after:auto;page-break-after:auto;}
/* One sheet of paper. Word's default page is A4 with 2.54 cm margins; at 96 dpi that is
   a 794x1123 px page with a ~96 px margin, and this is that shape at a comfortable size.
   `min-height` (not `height`) so short content still reads as a full page — the way an
   almost-empty Word file does — while long content pushes the sheet taller instead of
   being clipped. Real pagination needs a layout engine; see the knowledge doc. */
.pv-page{width:820px;min-height:1150px;padding:70px 80px;background:#ffffff;
  font-size:15px;line-height:1.62;}
.pv-page > :first-child{margin-top:0;}
.pv-page h1,.pv-page h2,.pv-page h3,.pv-page h4,.pv-page h5,.pv-page h6{
  margin:18px 0 8px;line-height:1.25;}
.pv-page h1{font-size:26px}.pv-page h2{font-size:22px}.pv-page h3{font-size:19px}
.pv-page h4{font-size:17px}.pv-page h5{font-size:16px}.pv-page h6{font-size:15px}
.pv-page p{margin:0 0 8px;white-space:pre-wrap;overflow-wrap:break-word;}
.pv-page ul,.pv-page ol{margin:0 0 10px;padding-left:26px;}
.pv-page li{margin:0 0 3px;white-space:pre-wrap;}
.pv-page img{max-width:100%;height:auto;display:block;margin:6px 0;}
.pv-page table{border-collapse:collapse;margin:8px 0 14px;width:100%;
  table-layout:fixed;}
.pv-page td,.pv-page th{border:1px solid #a9b0ba;padding:4px 6px;
  vertical-align:top;overflow-wrap:break-word;}
.pv-page th{background:#f1f3f5;font-weight:700;}
.pv-page td p,.pv-page th p{margin:0;white-space:pre-wrap;}
.pv-pagebreak{display:none;}
.pv-page a{color:#0b5cad;text-decoration:underline;}
.pv-empty{color:#8b93a1;}
.pv-page--pdf{padding:0;min-height:0;display:flex;}
.pv-page--pdf img{display:block;width:100%;height:auto;margin:0;}
"""

_PAGEBREAK_MARKUP = '<div class="pv-pagebreak"></div>'


def _docx_run_style(run: Any) -> str:
    """The inline style for one run — what the FILE says, so the editor can read it back.

    ⚠️ Every attribute the toolbar can change has to be RENDERED here, not just
    written. The reader's payload is built by asking the browser what it is
    drawing, so a strike-through the preview does not draw is a strike-through
    the save cannot send: the button lights up, the file changes, and the next
    load shows the old text as if nothing happened. `strike` and `vertAlign` were
    added for exactly that reason."""
    font = run.font
    # ⚠️ Underline and strike-through are ONE CSS property with two keywords, so
    # they are joined rather than assigned — `text-decoration: line-through` on
    # its own silently drops the underline.
    decor = []
    if font.underline:
        decor.append("underline")
    if font.strike:
        decor.append("line-through")
    vertical = None
    if font.superscript:
        vertical = "super"
    elif font.subscript:
        vertical = "sub"
    return _css(
        font_weight="700" if font.bold else None,
        font_style="italic" if font.italic else None,
        text_decoration=" ".join(decor) or None,
        vertical_align=vertical,
        font_size=(f"{_num(_emu_to_px(font.size))}px" if font.size else None),
        color=_color_hex(font.color),
    )


def _docx_hyperlink(run: Any) -> Optional[str]:
    """http(s) address of a hyperlink run, else None (never a raw scheme)."""
    try:
        link = run.hyperlink
    except Exception:
        return None
    if link is None:
        return None
    try:
        address = (link.address or "").strip()
    except Exception:
        return None
    lowered = address.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return address
    return None


def _docx_run_html(
    run: Any, doc: Any, budget: Optional[_ImageBudget] = None
) -> str:
    from docx.oxml.ns import qn

    out: list[str] = []
    for image in _docx_images_in(run._r, doc, budget):
        out.append(image)

    text = run.text
    if text:
        style = _docx_run_style(run)
        escaped = _esc(text).replace("\n", "<br>")
        if style:
            escaped = f'<span style="{style}">{escaped}</span>'
        href = _docx_hyperlink(run)
        if href:
            out.append(f'<a href="{_esc(href)}">{escaped}</a>')
        else:
            out.append(escaped)

    # ``w:br type="page"`` / ``w:lastRenderedPageBreak`` inside a run
    try:
        for br in run._r.findall(qn("w:br")):
            if (br.get(qn("w:type")) or "").lower() == "page":
                out.append(_PAGEBREAK_MARKUP)
        if run._r.findall(qn("w:lastRenderedPageBreak")):
            out.append(_PAGEBREAK_MARKUP)
    except Exception:
        pass

    for child in run._r.findall(qn("w:br")):
        if (child.get(qn("w:type")) or "").lower() == "textWrapping":
            out.append("<br>")
    return "".join(out)


def _docx_images_in(
    element: Any, doc: Any, budget: Optional[_ImageBudget] = None
) -> list[str]:
    """``<img>`` markup for every embedded picture below ``element``."""
    from docx.oxml.ns import qn

    out: list[str] = []
    try:
        blips = element.findall(".//" + qn("a:blip"))
    except Exception:
        return out
    container = None
    for blip in blips:
        rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if not rid:
            continue
        try:
            part = doc.part.related_parts[rid]
        except Exception:
            continue
        uri = _data_uri(
            getattr(part, "blob", b""), getattr(part, "content_type", ""), budget
        )
        if uri is None:
            continue
        width_px = None
        try:
            extent = element.find(".//" + qn("wp:extent"))
            if extent is not None:
                width_px = _emu_to_px(extent.get("cx"))
        except Exception:
            width_px = None
        if width_px:
            width_px = max(24.0, min(width_px, DOC_PAGE_W - 56))
            style = _css(width=f"{_num(width_px)}px")
            out.append(f'<img alt="" style="{style}" src="{uri}">')
        else:
            out.append(f'<img alt="" src="{uri}">')
    return out


def _docx_paragraph_kind(paragraph: Any) -> tuple[str, int, bool]:
    """``(kind, level, numbered)`` where kind is text|h1..h6|bullet|number."""
    from docx.oxml.ns import qn

    name = ""
    try:
        name = (paragraph.style.name or "").strip()
    except Exception:
        name = ""
    lowered = name.lower()

    for level in range(1, 7):
        if lowered == f"heading {level}":
            return f"h{level}", 0, False
    if lowered in ("title", "subtitle"):
        return "h1", 0, False

    numbered = "list number" in lowered or "listnumber" in lowered
    bulleted = "list bullet" in lowered or "list paragraph" in lowered

    numpr = False
    try:
        ppr = paragraph._p.find(qn("w:pPr"))
        if ppr is not None and ppr.find(qn("w:numPr")) is not None:
            numpr = True
    except Exception:
        numpr = False

    if bulleted or numbered or numpr:
        if "number" in lowered or "decimal" in lowered:
            numbered = True
        return ("number" if numbered else "bullet"), 0, numbered
    return "text", 0, numbered


def _docx_paragraph_align(paragraph: Any) -> Optional[str]:
    """``w:jc`` as a CSS ``text-align``, or None when the paragraph inherits it.

    ⚠️ None means "the file did not say", and it stays unsaid: writing the
    inherited value out would turn a paragraph that follows its style into one
    that hardcodes what the style already gives it."""
    try:
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        return {
            WD_ALIGN_PARAGRAPH.LEFT: "left",
            WD_ALIGN_PARAGRAPH.CENTER: "center",
            WD_ALIGN_PARAGRAPH.RIGHT: "right",
            WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
        }.get(paragraph.alignment)
    except Exception:
        return None


def _docx_paragraph_html(
    paragraph: Any, doc: Any, budget: Optional[_ImageBudget] = None, oid: str = ""
) -> str:
    kind, _level, _numbered = _docx_paragraph_kind(paragraph)
    inner = "".join(_docx_run_html(run, doc, budget) for run in paragraph.runs)
    attr = f' data-oid="{oid}"' if oid else ""
    # ⚠️ Alignment is a PARAGRAPH property, so it goes on the block and not on the
    # runs — the reader's alignment buttons change the whole paragraph, and the
    # payload reads this back off the same element. Rendered because a control
    # whose effect is invisible cannot be checked, and cannot be undone.
    align = _docx_paragraph_align(paragraph)
    style = f' style="text-align:{align}"' if align else ""

    # A ``<div>`` is not valid inside a ``<p>``: the browser would close the
    # paragraph early, so a page break is emitted as a sibling block instead.
    if _PAGEBREAK_MARKUP in inner:
        rest = inner.replace(_PAGEBREAK_MARKUP, "")
        out = []
        if kind.startswith("h") and rest.strip():
            out.append(f"<{kind}{attr}{style}>{rest}</{kind}>")
        elif rest.strip():
            out.append(f"<p{attr}{style}>{rest}</p>")
        out.append(_PAGEBREAK_MARKUP)
        return "".join(out)

    if not inner.strip():
        return ""
    if kind.startswith("h"):
        return f"<{kind}{attr}{style}>{inner}</{kind}>"
    return f"<p{attr}{style}>{inner}</p>"


def _docx_table_cells(table: Any) -> list[list[tuple[Any, int]]]:
    """The ``<w:tc>`` elements of a table, row by row, with their colspan.

    ⚠️ This is the ONE list of cells, and both `_docx_table_html` (which draws
    them) and `_docx_apply_edits` (which writes them) read it. A vertically merged
    cell's continuation is skipped here, so a renderer that numbered cells itself
    and a writer that walked `row.cells` independently would disagree by exactly
    the number of merges in the file — and the symptom is an edit landing in the
    wrong cell, which no rendering test can see.
    """
    from docx.oxml.ns import qn

    rows: list[list[tuple[Any, int]]] = []
    for row in table.rows:
        try:
            tcs = list(row._tr.tc_lst)
        except Exception:
            tcs = []
        cells: list[tuple[Any, int]] = []
        for tc in tcs:
            tcpr = tc.tcPr
            span = 1
            if tcpr is not None:
                grid_span = tcpr.find(qn("w:gridSpan"))
                if grid_span is not None:
                    try:
                        span = max(1, int(grid_span.get(qn("w:val")) or 1))
                    except (TypeError, ValueError):
                        span = 1
                vmerge = tcpr.find(qn("w:vMerge"))
                if vmerge is not None and (vmerge.get(qn("w:val")) or "continue") == "continue":
                    continue  # continuation of the cell above
            cells.append((tc, span))
        rows.append(cells)
    return rows


def _docx_table_html(
    table: Any, doc: Any, budget: Optional[_ImageBudget] = None,
    cell_oids: Optional[list] = None,
) -> str:
    from docx.oxml.ns import qn

    # ⚠️ oids are consumed BY POSITION, not by element identity. `id(tc)` looks
    # like the obvious key and is wrong: lxml hands out a fresh Python proxy on
    # every access, so a map built from one pass of `_docx_table_cells` matches
    # nothing on the second — and the symptom is that some cells silently lose
    # their oid and their edits have nowhere to land. Position is stable because
    # both passes walk the same list in the same order.
    pending = list(cell_oids or [])
    rows_html: list[str] = []
    for cells in _docx_table_cells(table):
        cells_html: list[str] = []
        for tc, span in cells:
            # A cell whose text is empty still gets an oid: it is not something
            # the reader can click, but if empty cells were skipped, one of them
            # would shift every later oid by one and each later edit would land in
            # the wrong cell.
            oid = pending.pop(0) if pending else ""
            attrs = [f'colspan="{span}"'] if span > 1 else []
            if oid:
                attrs.insert(0, f'data-oid="{oid}"')
            paras: list[str] = []
            for p in tc.findall(qn("w:p")):
                text = "".join(node.text or "" for node in p.findall(".//" + qn("w:t")))
                if text.strip():
                    paras.append(f"<p>{_esc(text)}</p>")
            for image in _docx_images_in(tc, doc, budget):
                paras.append(image)
            cells_html.append(
                f"<td {' '.join(attrs)}>{''.join(paras) or '&nbsp;'}</td>"
            )
        rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
    return f'<table>{"".join(rows_html)}</table>'


def _docx_html(data: bytes, title: str) -> str:
    try:
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception as exc:  # pragma: no cover
        raise PreviewError(f"python-docx unavailable: {exc}") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise PreviewError(f"not a readable .docx container: {exc}") from exc

    blocks: list[str] = []
    open_list: Optional[str] = None
    budget = _ImageBudget()

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            blocks.append(f"</{open_list}>")
            open_list = None

    try:
        children = list(document.element.body.iterchildren())
    except Exception as exc:
        raise PreviewError(f"could not read the .docx body: {exc}") from exc

    # ⚠️ `oid` is the contract between what the reader edits and what a save
    # writes. It is assigned HERE, in render order, and `_docx_apply_edits`
    # rebuilds the identical sequence from the same file — so the two cannot
    # disagree unless this function changes. A writer that walked the body on its
    # own would put every edit one paragraph off after the first block the
    # renderer skipped, and nothing about the rendered HTML would show it.
    unit = 0
    for child in children:
        try:
            if child.tag == qn("w:p"):
                paragraph = Paragraph(child, document)
                oid = "b%d" % unit
                unit += 1
                kind, _level, _numbered = _docx_paragraph_kind(paragraph)
                if kind in ("bullet", "number"):
                    tag = "ol" if kind == "number" else "ul"
                    if open_list != tag:
                        close_list()
                        blocks.append(f"<{tag}>")
                        open_list = tag
                    inner = "".join(
                        _docx_run_html(run, document, budget)
                        for run in paragraph.runs
                    )
                    blocks.append(f'<li data-oid="{oid}">{inner or "&nbsp;"}</li>')
                    continue
                close_list()
                markup = _docx_paragraph_html(paragraph, document, budget, oid)
                if markup:
                    blocks.append(markup)
            elif child.tag == qn("w:tbl"):
                close_list()
                table = Table(child, document)
                cell_oids: list[str] = []
                for cells in _docx_table_cells(table):
                    for _tc, _span in cells:
                        cell_oids.append("b%d" % unit)
                        unit += 1
                blocks.append(
                    _docx_table_html(table, document, budget, cell_oids))
        except Exception:
            continue  # a broken block is skipped, never fatal
    close_list()

    heading = title.strip() if title else ""
    # ── pages, not one long column ────────────────────────────────────────────
    # Word's own page breaks (`w:br type=page`, `w:lastRenderedPageBreak`) are the only
    # pagination the file states out loud, so they are the only breaks drawn: each run of
    # blocks between two breaks becomes one sheet of paper. Guessing at the others would mean
    # re-implementing Word's line breaker, and a wrong guess clips a line of text.
    markup = "".join(blocks)
    chunks = markup.split(_PAGEBREAK_MARKUP)
    while len(chunks) > 1 and not chunks[-1].strip():
        chunks.pop()                     # a trailing break is not an extra blank page
    if not any(chunk.strip() for chunk in chunks):
        chunks = ['<p class="pv-empty">empty document</p>']
    pages = "".join(
        f'<div class="pv-page-wrap">'
        f'<span class="pv-caption">Page {index + 1}</span>'
        f'<section class="pv-page pv-sheet-frame">{chunk}</section>'
        f"</div>"
        for index, chunk in enumerate(chunks)
    )
    # ``title`` names the document (browser tab); it is deliberately *not*
    # rendered as an extra <h1>, because the document almost always opens with
    # its own heading and the two would show up as a visible duplicate.
    return _document(heading or "Document", _DOCX_CSS,
                     f'<div class="pv-canvas pv-canvas--docx">{pages}</div>')


# --------------------------------------------------------------------------
# pdf -> HTML (page images)
# --------------------------------------------------------------------------

#: Rasterisation zoom: 72 dpi * 1.5 = 108 dpi. Body text stays crisp at that scale and a
#: 20-page report is still a few MB (measured: 64 KB per A4 text page at 1.6).
PDF_ZOOM = 1.5

#: Total inline-image budget for one PDF preview; past it the preview stops and says so.
PDF_IMAGE_BUDGET = 9 * 1024 * 1024

MAX_PDF_PAGES = 60


def _pdf_html(data: bytes, title: str, max_pages: int = MAX_PDF_PAGES) -> str:
    """
    A PDF as pages of paper — the same ``.pv-page`` a .docx renders to.

    Why not let the browser's own PDF viewer do it (which is what this used to do, and what
    browsers do very well)? Because a note layer cannot be drawn over a plugin: an iframe of
    ``application/pdf`` is a black box to us, so "leave a note on page 3" would be impossible.
    The cost is zoom fidelity and a cap on how many pages ride in one payload — and a preview
    is not the document; the download is still the download.
    """
    try:
        # Lazy on purpose: the PDF engine pulls in a shared library, and a broken or
        # missing install must fail the PDF preview, not every other document type.
        from services.pdf_io import PdfError, pdf_page_count, pdf_pages_to_png
    except Exception as exc:  # pragma: no cover - import failure is fatal
        raise PreviewError(f"PDF engine unavailable: {exc}") from exc

    limit = max(1, int(max_pages or 1))
    parts: list[str] = []
    try:
        total = pdf_page_count(data)
    except PdfError as exc:
        raise PreviewError(f"not a readable PDF: {exc}") from exc
    spent = 0
    try:
        with closing(pdf_pages_to_png(data, zoom=PDF_ZOOM, max_pages=limit)) as pages:
            for index, png, width, height in pages:
                if parts and spent + len(png) > PDF_IMAGE_BUDGET:
                    break
                spent += len(png)
                encoded = base64.b64encode(png).decode("ascii")
                parts.append(
                    f'<div class="pv-page-wrap">'
                    f'<span class="pv-caption">Page {index + 1}</span>'
                    f'<section class="pv-page pv-page--pdf pv-sheet-frame">'
                    f'<img src="data:image/png;base64,{encoded}" alt="" '
                    f'width="{width}" height="{height}">'
                    f"</section></div>"
                )
    except PdfError as exc:
        raise PreviewError(f"not a readable PDF: {exc}") from exc

    shown = len(parts)
    if not parts:
        parts.append('<div class="pv-page-wrap"><section class="pv-page pv-page--pdf '
                     'pv-sheet-frame"><p class="pv-empty">this PDF could not be drawn'
                     "</p></section></div>")
    elif shown < total:
        parts.append(
            f'<p class="pv-trunc-note">showing the first {shown} of {total} pages — '
            "download the file for the rest</p>"
        )
    heading = title.strip() if title else ""
    body = '<div class="pv-canvas pv-canvas--pdf">' + "\n".join(parts) + "</div>"
    return _document(heading or "PDF", _DOCX_CSS, body)


# --------------------------------------------------------------------------
# xlsx -> tables
# --------------------------------------------------------------------------


def _norm_cell(value: Any) -> Any:
    """str / int / float / None only -- dates become ISO, formulas stay empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if value.is_integer() and abs(value) < 1e16:
            return int(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    return str(value)


def table_preview(
    data: bytes,
    *,
    max_rows: int = 200,
    max_cols: int = 60,
    max_sheets: int = 12,
) -> list[dict]:
    """Every visible, non-empty sheet as ``{name, columns, rows, ...}``.

    ``rows`` holds *data* rows (the first sheet row is the header) and is cut to
    ``max_rows``; ``total_rows`` is the sheet's ``max_row`` (header included)
    and ``truncated`` says whether anything was cut, by row or by column.
    """
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise PreviewError(f"openpyxl unavailable: {exc}") from exc

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), data_only=True, read_only=True
        )
    except Exception as exc:
        raise PreviewError(f"not a readable .xlsx container: {exc}") from exc

    row_limit = max(1, int(max_rows or 1))
    col_limit = max(1, int(max_cols or 1))
    sheet_limit = max(1, int(max_sheets or 1))

    sheets: list[dict] = []
    try:
        names = list(getattr(workbook, "sheetnames", []) or [])
        for name in names:
            if len(sheets) >= sheet_limit:
                break
            try:
                worksheet = workbook[name]
            except Exception:
                continue
            if getattr(worksheet, "sheet_state", "visible") != "visible":
                continue
            if not hasattr(worksheet, "max_row"):
                continue  # chartsheet and friends
            try:
                total_rows = int(worksheet.max_row or 0)
                total_cols = int(worksheet.max_column or 0)
            except Exception:
                continue
            if total_rows <= 0 or total_cols <= 0:
                continue

            width = min(col_limit, total_cols)
            try:
                # ⚠️ Cells, not `values_only=True`: a plain number can be a figure that wants
                # thousands separators or a year that must NOT get them ("2,026"), and only the
                # cell's own number format can tell them apart. See `_cell_html`.
                iterator = worksheet.iter_rows(
                    min_row=1, max_row=total_rows, max_col=width
                )
                raw_rows = []
                raw_formats = []
                raw_fonts = []
                for index, row in enumerate(iterator):
                    raw_rows.append([getattr(c, "value", None) for c in row])
                    raw_formats.append([str(getattr(c, "number_format", "") or "")
                                        for c in row])
                    raw_fonts.append([_cell_font_css(c) for c in row])
                    if index >= row_limit:  # header + max_rows data rows
                        break
            except Exception:
                continue

            if not raw_rows:
                continue
            header_values = [_norm_cell(v) for v in raw_rows[0][:width]]
            if not any(v not in (None, "") for v in header_values):
                if len(raw_rows) == 1:
                    continue  # completely blank sheet
            columns = [
                (str(v) if v not in (None, "") else f"Column {i + 1}")
                for i, v in enumerate(header_values)
            ]

            rows: list[list[Any]] = []
            row_formats: list[list[str]] = []
            row_fonts: list[list[str]] = []
            for offset, row in enumerate(raw_rows[1 : row_limit + 1], start=1):
                values = [_norm_cell(v) for v in list(row)[:width]]
                if len(values) < width:
                    values.extend([None] * (width - len(values)))
                rows.append(values)
                formats = list(raw_formats[offset])[:width] if offset < len(raw_formats) else []
                formats.extend([""] * (width - len(formats)))
                row_formats.append(formats)
                fonts = list(raw_fonts[offset])[:width] if offset < len(raw_fonts) else []
                fonts.extend([""] * (width - len(fonts)))
                row_fonts.append(fonts)
            if not columns and not rows:
                continue
            data_rows_total = max(0, total_rows - 1)
            header_fonts = list(raw_fonts[0][:width]) if raw_fonts else []
            header_fonts.extend([""] * (width - len(header_fonts)))
            sheets.append(
                {
                    "name": str(getattr(worksheet, "title", None) or name),
                    "columns": columns,
                    "column_fonts": header_fonts,
                    "rows": rows,
                    "row_formats": row_formats,
                    "row_fonts": row_fonts,
                    "total_rows": total_rows,
                    "truncated": bool(
                        data_rows_total > len(rows) or total_cols > width
                    ),
                }
            )
    finally:
        try:
            workbook.close()
        except Exception:
            pass

    if not sheets:
        raise PreviewError("no visible non-empty sheet in the .xlsx")
    return sheets


_TABLES_CSS = """
/* ⚠️ A worksheet is a PAGE, and it starts at the TOP LEFT (2026-10-05).
 *
 * It used to be `align-items: center` on a narrow card, which is the shape of a
 * report illustration rather than the shape of a spreadsheet: Excel puts the grid
 * in the top-left corner of the window, flush against the column letters and the
 * row numbers, and that corner is what a reader is looking for. Centred, the
 * letters and the numbers sat in the middle of the screen and the sheet read as
 * something embedded rather than something opened.
 *
 * `min-width` is the other half: without a floor the card is exactly as wide as
 * its columns, so a five-column workbook was a 332px strip on a 1500px screen.
 * 100% plus a floor means it FILLS the pane like Excel's grid does, and the
 * `overflow:auto` below is what a wide sheet still uses instead of being squeezed. */
.pv-canvas--sheets{align-items:flex-start;padding:24px 0 44px;}
/* One sheet, one page: the block is a sheet of paper with a break after it, so
   printing and scrolling both treat the sheets as separate pages rather than as
   three cards in a stack. */
.pv-sheet-block{display:flex;flex-direction:column;align-items:flex-start;gap:6px;
  width:100%;max-width:100%;}
/* ONE SHEET ON SCREEN AT A TIME, and this is the rule that does it.
 *
 * The sheets used to be stacked - every one of them on the page at once, with the
 * tab strip as a picture of a control next to the content it was supposed to
 * control. A reader of a five-sheet workbook scrolled past four of them to reach
 * the fifth, and the strip said "Price bands" while "Notes" was on screen.
 *
 * Opening a workbook in Excel shows one grid, and the tabs switch which. That is
 * the whole interaction, and the reader asked for it by name. `hidden` (the
 * attribute) rather than a class, so the browser's own `display: none` applies
 * without this stylesheet having to win a specificity fight against itself. */
.pv-sheet-block[hidden]{display:none;}
/* But PRINT and PDF still get every sheet. Hiding two thirds of a workbook because
 * the screen is a screen is right on a monitor and wrong on paper, and a PDF of a
 * workbook is a thing people do. */
@media print{
  .pv-sheet-block{display:flex !important;break-after:page;page-break-after:always;}
  .pv-sheet-block:last-of-type{break-after:auto;page-break-after:auto;}
  .pv-tabs{display:none;}
}
/* The white page behind the grid, inset from the window edge the way a sheet of
   paper sits on a desk. */
.pv-sheet-block .pv-sheet{width:100%;}
/* The caption is the sheet's name, in Excel's grey, above the grid rather than as
   a title floating over the data. */
.pv-caption{font:600 11px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",
  "PingFang SC","Microsoft YaHei",sans-serif;color:#64748b;letter-spacing:.02em;
  padding:0 18px;}
/* The scroll container: a 60-column sheet is wider than any window, and Excel scrolls
   it rather than shrinking it. Sticky headings (below) need the scroll to happen here. */
.pv-grid{background:#ffffff;overflow:auto;max-height:none;position:relative;
  width:100%;}
/* ⚠️ The floor. A sheet with three narrow columns was 332px of grid on a wide
   screen; Excel's grid always fills the window and a wide sheet scrolls INSIDE
   it, so the columns are stretched to the pane before the scrollbar appears. */
.pv-cells{min-width:100%;}
.pv-cells{border-collapse:separate;border-spacing:0;font-size:13px;table-layout:fixed;
  font-family:Calibri,"Segoe UI","Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",
    "PreviewCJK",sans-serif;}
.pv-cells th,.pv-cells td{border-right:1px solid #d4d4d4;border-bottom:1px solid #d4d4d4;
  padding:2px 7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:340px;height:22px;}
/* The column letters and the row numbers: Excel's own chrome, in Excel's own grey. */
.pv-cells .pv-colhead,.pv-cells .pv-gutter{background:#f1f3f5;color:#6b7280;
  font-weight:600;font-size:11px;text-align:center;padding:2px 6px;
  border-right:1px solid #c9ced6;border-bottom:1px solid #c9ced6;}
.pv-cells .pv-colhead{position:sticky;top:0;z-index:2;}
.pv-cells .pv-gutter{position:sticky;left:0;z-index:1;min-width:34px;}
.pv-cells .pv-corner{position:sticky;left:0;top:0;z-index:3;background:#e8eaed;}
.pv-cells thead .pv-head{background:#f8fafc;font-weight:700;text-align:left;}
.pv-cells td.pv-num{text-align:right;font-variant-numeric:tabular-nums;}
/* The tab strip at the foot of the workbook — where Excel puts the sheet names. It is the
   one child that must NOT obey the canvas gap: tabs read as part of the window's bottom
   edge, and 24px of daylight makes them look like a stray link. */
.pv-tabs{display:flex;align-items:center;gap:2px;padding:4px 6px 0;background:#f1f3f5;
  border-top:1px solid #d4d4d4;width:100%;max-width:100%;overflow:auto;margin-top:-16px;
  margin-left:0;padding-left:18px;font-size:12px;color:#3f4652;position:sticky;
  bottom:0;z-index:4;}
/* ⚠️ A `<button>`, and `font: inherit` with it: a tab strip made of `<span>`s has
   no pointer, no keyboard focus and no click — it is a label that looks like a
   control, which is the thing the reader is right to complain about. `margin-top:-16px`
   on the strip is what welds it to the bottom edge of the sheet above it. */
.pv-tab{font:inherit;padding:4px 12px;border:1px solid #d4d4d4;border-bottom:none;
  border-radius:4px 4px 0 0;background:#f8f9fb;white-space:nowrap;cursor:pointer;}
.pv-tab:hover{background:#eef1f5;}
.pv-tab.is-on{background:#ffffff;font-weight:600;color:#0b5cad;
  box-shadow:inset 0 2px 0 #0b5cad;}
.pv-tab:focus-visible{outline:2px solid #0b5cad;outline-offset:-2px;}
.pv-empty{color:#8b93a1;}
"""


# The workbook's own navigation, in the workbook. Excel's tab strip is the way a
# reader moves between sheets, so here it moves: click a tab, the document scrolls
# to that sheet, and scrolling marks the tab you are on. Without this the strip was
# three grey rectangles that happened to contain the sheet names — a picture of a
# control, next to every sheet already on screen, doing nothing.
_SHEET_TABS_JS = """
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.pv-tab'));
  var blocks = Array.prototype.slice.call(document.querySelectorAll('.pv-sheet-block'));
  if (!tabs.length || !blocks.length) return;
  function show(i) {
    if (i < 0 || i >= blocks.length) return;
    blocks.forEach(function (b, n) { b.hidden = (n !== i); });
    tabs.forEach(function (t, n) { t.classList.toggle('is-on', n === i); });
    // The scroll goes back to the top. Switching sheets while scrolled into the old
    // one leaves the reader looking at empty paper, because the new sheet is
    // shorter than the scroll offset they are sitting at.
    window.scrollTo(0, 0);
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener('click', function () { show(i); });
  });
  show(0);
})();
"""


def _thousands(value: Any, number_format: str = "") -> str:
    """A number as a reader wants to read it: 18420000 becomes 18,420,000.

    Note that NOT every number wants grouping. A year (2026), or a four-digit count, stored as
    a plain number would become "2,026" - which is not a figure, it is a different fact. Two
    rules, in order: the cell's OWN number format wins when it asks for grouping (`#,##0` is
    the file saying how it wants to be read; date formats are excluded), otherwise group only
    from 10000 up - above any year, below any figure that needs grouping to be read at all.
    Asked for 2026-10-01: "单元格里的数值需要用千分符号".
    """
    fmt = (number_format or "").lower()
    wants_grouping = "," in fmt and not any(part in fmt for part in ("yy", "dd", "hh", "mm:"))
    plain = (not fmt) or fmt == "general"
    if wants_grouping or (plain and abs(float(value)) >= 10000):
        return f"{value:,}"
    return f"{value}"


def _cell_font_css(cell: Any) -> str:
    """One cell's OWN font and horizontal alignment as an inline style, or ''.

    ⚠️ ⚠️ This was missing entirely, and it cost real data rather than a
    missing feature. The editor builds its save payload by asking the browser
    what it is drawing — and the grid drew NONE of the cell's font, so a bold
    cell read back as plain. A reader who fixed one character inside a bold cell
    therefore saved it back unbolded, with no error anywhere: the file said bold,
    the preview said plain, and the preview was what got written back. The
    toolbar could not be honest about a workbook until the workbook was drawn
    honestly.

    Only what the file says is emitted. An unresolved colour (a theme or indexed
    one) is left alone rather than guessed — see `_color_hex`'s note; the same
    reasoning applies to `openpyxl`, whose `.rgb` is None for those.
    """
    try:
        font = cell.font
    except Exception:
        return ""
    if font is None:
        return ""
    decor = []
    if font.underline:
        decor.append("underline")
    if font.strike:
        decor.append("line-through")
    color = None
    try:
        if font.color is not None and getattr(font.color, "type", None) == "rgb":
            rgb = str(font.color.rgb or "")
            # openpyxl hands back aRGB — eight hex digits, alpha first.
            if len(rgb) == 8:
                color = "#" + rgb[2:]
            elif len(rgb) == 6:
                color = "#" + rgb
    except Exception:
        color = None
    size = None
    try:
        if font.size:
            size = f"{_num(float(font.size), 1)}pt"
    except Exception:
        size = None
    align = None
    try:
        horizontal = getattr(cell.alignment, "horizontal", None)
        if horizontal in ("left", "center", "right"):
            align = horizontal
    except Exception:
        align = None
    return _css(
        font_weight="700" if font.bold else None,
        font_style="italic" if font.italic else None,
        text_decoration=" ".join(decor) or None,
        vertical_align={"superscript": "super", "subscript": "sub"}.get(
            getattr(font, "vertAlign", None)),
        font_size=size,
        color=color,
        text_align=align,
    )


def _cell_html(value: Any, tag: str = "td", number_format: str = "",
               css_class: str = "", oid: str = "", font_css: str = "") -> str:
    classes = [css_class] if css_class else []
    if isinstance(value, bool):
        value = "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        classes.append("pv-num")
        inner = _esc(_thousands(value, number_format))
    elif value is None:
        inner = ""
    else:
        inner = _esc(value)
    parts = [f' class="{" ".join(classes)}"'] if classes else []
    # `data-oid` is the cell's REAL address (`x<sheet>!R3C2`), not a render index,
    # so a save can write straight to it. An index would break the moment the
    # preview truncates rows or skips a sheet — and the edit would land in a cell
    # that merely LOOKS like the right one.
    if oid:
        parts.insert(0, f' data-oid="{oid}"')
    if font_css:
        parts.append(f' style="{font_css}"')
    attr = "".join(parts)
    return f"<{tag}{attr}>{inner}</{tag}>"


def _xlsx_oid(sheet_name: str, excel_row: int, excel_col: int) -> str:
    """`x<sheet name>!R<row>C<col>` — 1-based, exactly as Excel addresses a cell."""
    return "x%s!R%dC%d" % (_esc(sheet_name).replace("/", "_"), excel_row, excel_col)


def _col_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA: the heading Excel prints above the grid."""
    letters = ""
    number = max(1, int(index))
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def tables_html(
    data: bytes,
    title: str = "",
    *,
    max_rows: int = 200,
    max_sheets: int = 12,
) -> str:
    """Self-contained HTML for an .xlsx: one grid per visible sheet, with Excel's chrome.

    Column letters, row numbers and gridlines are not decoration — a reader who is told
    "D7" has to be able to FIND D7, and a figure without its row number is a number without
    a place. The sheet names ride in a tab strip at the foot, where Excel keeps them.
    """
    sheets = table_preview(data, max_rows=max_rows, max_sheets=max_sheets)

    sections: list[str] = []
    tabs: list[str] = []
    for sheet_index, sheet in enumerate(sheets):
        columns: list[str] = sheet["columns"]
        column_fonts: list[str] = sheet.get("column_fonts") or []
        width = len(columns)
        letters = "".join(
            f'<th class="pv-colhead">{_col_letter(i + 1)}</th>' for i in range(width)
        )
        # Row 1 of the sheet IS the header row, so the first data row is Excel row 2 —
        # printing 1 in the gutter next to the data would misnumber every row the reader
        # might be asked to look at.
        # The first row of `columns` IS Excel row 1, so the header cells are R1.
        head = "".join(
            _cell_html(column, "th", css_class="pv-head",
                       oid=_xlsx_oid(sheet["name"], 1, i + 1),
                       font_css=(column_fonts[i] if i < len(column_fonts) else ""))
            for i, column in enumerate(columns)
        )
        formats = sheet.get("row_formats") or []
        fonts_rows = sheet.get("row_fonts") or []
        body_rows = []
        for index, row in enumerate(sheet["rows"]):
            row_fmt = formats[index] if index < len(formats) else []
            row_font = fonts_rows[index] if index < len(fonts_rows) else []
            # `index + 2` is the gutter this row is PRINTED with, and it is the
            # real Excel row. Reusing it as the oid is what keeps the reader's
            # "look at D7" and the writer's D7 the same cell.
            cells = "".join(
                _cell_html(v, number_format=(row_fmt[i] if i < len(row_fmt) else ""),
                           oid=_xlsx_oid(sheet["name"], index + 2, i + 1),
                           font_css=(row_font[i] if i < len(row_font) else ""))
                for i, v in enumerate(row)
            )
            body_rows.append(f'<tr><th class="pv-gutter">{index + 2}</th>{cells}</tr>')
        data_total = max(0, int(sheet["total_rows"]) - 1)
        note = ""
        if len(sheet["rows"]) < data_total:
            note = (
                '<p class="pv-trunc-note">'
                f"showing the first {len(sheet['rows'])} of {data_total} rows"
                "</p>"
            )
        # ⚠️ The first sheet is on screen; the rest carry `hidden` and are revealed
        # by their tab. Set in the MARKUP rather than only by the script, so the
        # document is already correct if the script is blocked.
        sections.append(
            f'<div class="pv-sheet-block"'
            f'{"" if sheet_index == 0 else " hidden"}>'
            f'<span class="pv-caption">{_esc(sheet["name"])}</span>'
            f'<div class="pv-sheet pv-sheet-frame">'
            f'<div class="pv-grid"><table class="pv-cells">'
            f'<thead><tr><th class="pv-gutter pv-corner"></th>{letters}</tr>'
            f'<tr><th class="pv-gutter">1</th>{head}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody>'
            f"</table></div></div>{note}</div>"
        )
        # ⚠️ `type="button"`: inside a form this would submit it, and a preview is
        # somebody else's file rendered inside our shell.
        tabs.append(
            f'<button type="button" class="pv-tab{" is-on" if sheet_index == 0 else ""}"'
            f' data-sheet="{sheet_index}">{_esc(sheet["name"])}</button>'
        )

    heading = title.strip() if title else ""
    if sections:
        body = (f'<div class="pv-canvas pv-canvas--sheets">{"".join(sections)}'
                f'<div class="pv-tabs">{"".join(tabs)}</div></div>')
    else:
        body = '<div class="pv-canvas pv-canvas--sheets"><p class="pv-empty">no sheets</p></div>'
    return _document(heading or "Spreadsheet", _TABLES_CSS, body,
                     script=_SHEET_TABS_JS)


# --------------------------------------------------------------------------
# HTML -> PDF (playwright / chromium)
# --------------------------------------------------------------------------

_browser_lock: Optional[asyncio.Lock] = None
_browser: Any = None
_playwright: Any = None


def _lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


async def _get_browser() -> Any:
    """The process-wide chromium, launched once and reused."""
    global _browser, _playwright
    browser = _browser
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover
        raise PreviewError(f"playwright unavailable: {exc}") from exc
    try:
        playwright = await async_playwright().start()
    except Exception as exc:
        raise PreviewError(f"could not start playwright: {exc}") from exc
    _playwright = playwright
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as exc:
        raise PreviewError(f"could not launch chromium: {exc}") from exc
    _browser = browser
    return browser


async def close_browser() -> None:
    """Release the shared chromium (tests / shutdown)."""
    global _browser, _playwright
    browser, playwright = _browser, _playwright
    _browser = None
    _playwright = None
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass


def _page_box(width: int, landscape: bool, height: Optional[int]) -> tuple:
    """The printable page in CSS px, as the caller asked for it."""
    if height:
        return int(width), int(height)
    if landscape:
        return int(width), int(round(width * 9 / 16))
    return int(width), int(round(width * 1414 / 1000))


async def html_to_pdf(
    html: str,
    *,
    width: int = 1280,
    landscape: bool = True,
    margin_mm: float = 0.0,
    height: Optional[int] = None,
) -> bytes:
    """Render ``html`` to PDF bytes with chromium; the browser is reused.

    ``width`` is the *printable* width in CSS px.  ``height`` defaults to a 16:9
    box when ``landscape`` (matching the slide canvas) and to an A4-ish portrait
    box otherwise; pass it explicitly for anything else.
    """
    if not isinstance(html, str) or not html.strip():
        raise PreviewError("cannot print an empty document")

    try:
        width = int(width)
    except (TypeError, ValueError) as exc:
        raise PreviewError(f"invalid width: {exc}") from exc
    if width < 64:
        raise PreviewError(f"page width {width}px is too small to render")

    page_w, page_h = _page_box(width, landscape, height)
    mm = float(margin_mm or 0.0)
    if mm < 0:
        raise PreviewError("margin_mm must not be negative")
    margin_value = "0" if mm == 0 else f"{mm}mm"

    options: dict[str, Any] = {
        "print_background": True,
        "margin": {
            "top": margin_value,
            "bottom": margin_value,
            "left": margin_value,
            "right": margin_value,
        },
    }
    if landscape:
        # Chromium swaps paper width/height for landscape pages, so hand it the
        # rotated box: without this the printed page is narrower than the slide
        # and the right-hand part of every slide is clipped away.
        options["width"] = f"{page_h}px"
        options["height"] = f"{page_w}px"
        options["landscape"] = True
    else:
        options["width"] = f"{page_w}px"
        options["height"] = f"{page_h}px"

    async with _lock():
        browser = await _get_browser()
        try:
            page = await browser.new_page(
                viewport={"width": page_w, "height": page_h}
            )
        except Exception as exc:
            raise PreviewError(f"could not open a chromium page: {exc}") from exc
        try:
            try:
                # ⚠️ The one place the CJK face is inlined: this runs in the API image, which has
                # no Chinese font, while the HTML served to a browser is left without it.
                face = _cjk_font_face()
                if face:
                    html = (html.replace("<head>", "<head><style>" + face + "</style>", 1)
                            if "<head>" in html else "<style>" + face + "</style>" + html)
                await page.set_content(html, wait_until="load")
            except Exception as exc:
                raise PreviewError(f"could not load the document: {exc}") from exc
            try:
                await page.evaluate(
                    "() => { try { return document.fonts ? document.fonts.ready : null; }"
                    " catch (e) { return null; } }"
                )
                await page.evaluate(
                    "() => Promise.all(Array.from(document.images).map(img =>"
                    " (img.complete && img.naturalWidth) ? null :"
                    " new Promise(res => { img.onload = img.onerror = res; })))"
                )
            except Exception:
                pass  # best effort: fonts/images may already be settled
            try:
                pdf = await page.pdf(**options)
            except Exception as exc:
                raise PreviewError(f"chromium could not print the document: {exc}") from exc
        finally:
            try:
                await page.close()
            except Exception:
                pass

    if not pdf or not pdf.startswith(b"%PDF"):
        raise PreviewError("chromium returned something that is not a PDF")
    return pdf


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------


def preview_html(
    data: bytes,
    doc_type: str,
    title: str = "",
    *,
    max_slides: int = 120,
) -> str:
    """Self-contained HTML for a pptx / docx / xlsx / pdf (inline CSS, data: images, no JS).

    ``PreviewError`` is raised for an unsupported ``doc_type`` or an unreadable
    container.  All four types render here on purpose: the reader page embeds one preview
    per card, and every one of them is a stack of pages a note can be pinned to.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PreviewError("no document bytes to preview")
    data = bytes(data)

    kind = (doc_type or "").strip().lower().lstrip(".")
    if kind not in HTML_DOC_TYPES:
        raise PreviewError(f"unsupported document type: {doc_type!r}")

    if kind == "pptx":
        return _pptx_html(data, title, max_slides)
    if kind == "docx":
        return _docx_html(data, title)
    if kind == "xlsx":
        return tables_html(data, title)
    return _pdf_html(data, title, max_pages=min(int(max_slides or 1) or MAX_PDF_PAGES,
                                                MAX_PDF_PAGES))


# ══ WRITING EDITS BACK ═══════════════════════════════════════════════════════
#
# The preview draws a file; these functions put text back into one. Two rules
# govern the whole section:
#
#   1. The oid an edit carries was minted by the RENDERER, above, walking the
#      document in its own order. So the writer must rebuild that same order
#      rather than invent a convenient one. A writer that walked the body itself
#      would be correct until the first block the renderer skipped, and every
#      edit after that would land one paragraph off — with nothing in the
#      rendered HTML to show it.
#   2. An oid the writer cannot resolve is DROPPED, not guessed. Reporting it
#      back is the only way a save can be honest about what it actually wrote.
#
# ⚠️ PDF is absent from this section on purpose. A PDF's text is glyphs at
# coordinates; changing a word means redrawing the page, and every tool that
# claims otherwise produces a different document. Offering it would be a lie
# about what "save" means, so `EDITABLE_DOC_TYPES` excludes it and the UI does
# not offer the affordance.

EDITABLE_DOC_TYPES = ("docx", "pptx", "xlsx")


class EditError(Exception):
    """An edit could not be applied. `applied` / `dropped` describe what happened."""


def _edit_text(edit: Any) -> str:
    """The text half of one edit.

    ⚠️ Both shapes are accepted because the SERVER is not the only thing that will
    ever write this file: an older client sends `{"oid": ..., "text": ...}` and a
    hand-written curl in a support answer sends the same. Refusing one of them
    would turn "the editor moved on" into "the document is broken"."""
    if isinstance(edit, dict):
        value = edit.get("text")
        return value if isinstance(value, str) else ""
    return edit if isinstance(edit, str) else ""


def _edit_runs(edit: Any) -> Any:
    return edit.get("runs") if isinstance(edit, dict) else None


def _edit_align(edit: Any) -> Optional[str]:
    """The paragraph alignment the reader asked for, or None.

    ⚠️ Optional for the same reason the four optional run keys are: an alignment a
    paragraph INHERITS from its style was never in the file, and writing it back
    would replace "follows the style" with "happens to match it today" — on every
    other paragraph that shares the style, the next edit to any of them would
    disagree with the rest."""
    value = edit.get("align") if isinstance(edit, dict) else None
    return value if value in _ALIGN_VALUES else None


# The three character attributes that are ALWAYS present in a run, and the
# OOXML element each maps to. Anything the editor does not offer it has no
# business rewriting, and silently dropping it would be the bug this whole
# mechanism exists to fix.
_RUN_FLAGS = (("b", "w:b"), ("i", "w:i"), ("u", "w:u"))

# ⚠️ The rest are OPTIONAL, and "optional" is the load-bearing word. A MISSING key
# means "the reader said nothing about this, leave what the file had" — which is
# not the same as "off". They are optional because the client only sends an
# attribute the reader actually touched: a size or a colour that the paragraph
# INHERITS from its style is drawn by the browser but was never in the file, and
# writing the inherited value back would hardcode it into the document on every
# keystroke. `None` (absent) therefore means "no opinion" and `False` means "off",
# and the two must never be collapsed.
_OPTIONAL_RUN_KEYS = ("s", "va", "sz", "color")

#: What a run that carries no formatting at all looks like. Compared as a whole
#: so that a run with `{"s": False}` — the reader turning strike OFF — is not
#: mistaken for a plain one and thrown away.
_PLAIN_RUN = (False, False, False, None, None, None, None)

#: Superscript / subscript, as each format spells it.
_ALIGN_VALUES = ("left", "center", "right")


def _run_state(run: dict) -> tuple:
    return (bool(run.get("b")), bool(run.get("i")), bool(run.get("u")),
            run.get("s"), run.get("va"), run.get("sz"), run.get("color"))


def _clean_optional_run(item: dict) -> dict:
    """The optional keys of one run, validated — a value that makes no sense is
    dropped rather than refused, for the same reason an unknown key is: the list
    describes what the reader did, and one bad field must not cost them their
    text edit."""
    out: dict = {}
    if "s" in item:
        out["s"] = bool(item.get("s"))
    va = item.get("va")
    if va in ("sup", "sub"):
        out["va"] = va
    size = item.get("sz")
    # ⚠️ Rounded to half a point because that is the resolution OOXML stores
    # (`w:sz` is half-points), so a size that does not land on one would be
    # written, reopened and found different from what the reader chose.
    if isinstance(size, (int, float)) and not isinstance(size, bool) \
            and 1 <= float(size) <= 400:
        out["sz"] = round(float(size) * 2) / 2
    color = item.get("color")
    if isinstance(color, str) and re.fullmatch(r"[0-9a-fA-F]{6}", color):
        out["color"] = color.upper()
    return out


def normalise_runs(value: Any, text: str) -> Optional[list]:
    """The reader's `runs` array, or None when there is nothing to apply.

    ⚠️ Two guards, and both exist because the alternative is writing a formatting
    bug into somebody's contract document:

    * **It must agree with `text`.** The runs are re-joined and compared. A
      payload whose runs do not add up to the text is refused (None → the plain
      text path), because writing runs would put words in the file in an order
      the reader never typed.
    * **There must be more than one run.** One run is the plain case wearing a
      hat, and the plain path is cheaper.

      ⚠️ "More than one run", NOT "more than one formatting state". Those are not
      the same condition, and the second one loses exactly the case the toolbar
      exists for: a reader selects a whole cell and presses B, the DOM reads back
      as two adjacent `<b>` runs, both bold, one formatting state — and returning
      None there means the cell silently stays unformatted. Coalescing adjacent
      runs with equal flags first is what makes "the whole thing is bold"
      expressible, and it is also what keeps the payload small.

    Unknown keys are ignored rather than rejected: the list is a description of
    what the reader did, and a newer reader sending one more key is not a reason
    to refuse their text edit."""
    if not isinstance(value, list) or not value:
        return None
    runs = []
    for item in value:
        if not isinstance(item, dict):
            return None
        chunk = item.get("text")
        if not isinstance(chunk, str):
            return None
        runs.append({"text": chunk,
                     "b": bool(item.get("b")), "i": bool(item.get("i")),
                     "u": bool(item.get("u")), **_clean_optional_run(item)})
    if "".join(r["text"] for r in runs) != text:
        return None
    merged: list = []
    for run in runs:
        if run["text"] == "":
            continue
        state = _run_state(run)
        if merged and _run_state(merged[-1]) == state:
            merged[-1]["text"] += run["text"]
        else:
            merged.append(dict(run))
    # ⚠️ "Plain", not "one run". After coalescing, a selection that is entirely
    # bold collapses to a single bold run — and that is the case the whole
    # mechanism exists for. A run carrying `s: False` is the reader turning
    # strike OFF, which is equally not plain, so the comparison is against the
    # full state rather than against "no flags at all".
    if not merged:
        return None
    if len(merged) == 1 and _run_state(merged[0]) == _PLAIN_RUN:
        return None          # plain text: the single-run path is identical
    return merged


def _apply_flags(run: Any, flags: dict, qn: Any) -> None:
    """Set (or clear) b / i / u, plus whatever else this run was sent.

    ⚠️ b / i / u are always in the payload and are therefore always WRITTEN, on
    every piece of the paragraph — including as an explicit "off", because a
    heading's second half must not inherit the first half's bold. The four
    optional attributes are the opposite: written only when the key is present,
    and otherwise left as the clone of run 0 already had them. See
    `_OPTIONAL_RUN_KEYS` for why that distinction is the whole design.

    ⚠️ These four go through python-docx's own `font` properties rather than
    hand-built elements. `w:vertAlign` in particular has one element with two
    values, so "turn subscript off" and "turn superscript off" are the same
    element removal, and a version that appended `w:vertAlign` per flag left a
    run claiming to be both."""
    from docx.oxml import OxmlElement

    rpr = run._r.get_or_add_rPr()
    for key, tag in _RUN_FLAGS:
        for existing in rpr.findall(qn(tag)):
            rpr.remove(existing)
        if not flags.get(key):
            continue
        node = OxmlElement(tag)
        # ⚠️ `w:u` needs a value or Word shows the run as underlined-by-default
        # single; the others are on/off switches where an empty element IS "on".
        node.set(qn("w:val"), "single" if key == "u" else "1")
        rpr.append(node)

    font = run.font
    if "s" in flags:
        font.strike = bool(flags["s"])
    if "va" in flags:
        font.superscript = flags["va"] == "sup"
        font.subscript = flags["va"] == "sub"
    if "sz" in flags:
        from docx.shared import Pt

        font.size = Pt(float(flags["sz"]))
    if "color" in flags:
        from docx.shared import RGBColor

        font.color.rgb = RGBColor.from_string(flags["color"])


def _write_runs(paragraph: Any, runs: list) -> None:
    """Rebuild a paragraph as `runs`, each copy of the FIRST run plus its flags.

    ⚠️ Why a copy of run 0 and not a new run: a new run takes the paragraph's
    default style, so a blue heading's second half loses the blue — which is the
    same failure `_set_paragraph_text` documents for the whole-paragraph case,
    just narrower. Cloning run 0's XML carries its size, colour, font and
    highlight into every piece, and the only thing that varies is what the reader
    actually pressed."""
    import copy

    from docx.oxml.ns import qn

    existing = list(paragraph.runs)
    template = existing[0] if existing else None
    # ⚠️ The parent is captured BEFORE anything is removed. `template._r` is one of
    # the runs about to be deleted, and a detached element has no parent to append
    # to — so reading it afterwards is an `AttributeError` on `None`, which the
    # caller catches and turns into a silently DROPPED edit. The reader would be
    # told their save matched nothing, with no reason given.
    parent = existing[0]._r.getparent() if existing else None
    for run in existing:
        run._r.getparent().remove(run._r)
    for flags in runs:
        if template is None or parent is None:
            new = paragraph.add_run(flags["text"])
        else:
            parent.append(copy.deepcopy(template._r))
            new = paragraph.runs[-1]
            for node in list(new._r.findall(qn("w:t"))):
                new._r.remove(node)
        new.text = flags["text"]
        _apply_flags(new, flags, qn)


def _write_runs_pptx(paragraph: Any, runs: list) -> None:
    """`runs` into a DrawingML paragraph — python-pptx's own font API, not OOXML tags.

    ⚠️ ⚠️ The .docx path above and this one look interchangeable and are NOT.
    WordprocessingML stores emphasis as CHILD ELEMENTS (`<w:b/>`); DrawingML stores
    it as an ATTRIBUTE on the run properties (`b="1"`, `i="1"`, `u="sng"`). Writing
    `w:b` into a slide produces a file that opens, shows every word unformatted, and
    raises nothing — the first version of this did exactly that, and the round-trip
    test caught it only because it re-opened the saved bytes and measured.

    So the flags go through `run.font`, which is the one thing that knows which of
    the two vocabularies it is writing.

    ⚠️ Three of the four new attributes have no `font` property at all — strike,
    superscript and subscript are DrawingML run ATTRIBUTES (`strike="sngStrike"`,
    `baseline="30000"`), and a version that reached for `w:strike` here would
    produce a deck that opens, renders unformatted, and raises nothing. Measured
    round-trip through a saved file, both attributes come back exactly."""
    import copy

    from pptx.enum.text import MSO_UNDERLINE

    existing = list(paragraph.runs)
    template = existing[0] if existing else None
    parent = existing[0]._r.getparent() if existing else None
    for run in existing:
        run._r.getparent().remove(run._r)
    for flags in runs:
        if template is None or parent is None:
            new = paragraph.add_run()
        else:
            parent.append(copy.deepcopy(template._r))
            new = paragraph.runs[-1]
        new.text = flags["text"]
        # None means "no opinion", not "off": a run cloned from the template already
        # carries whatever the file said, and None leaves it alone.
        new.font.bold = True if flags["b"] else None
        new.font.italic = True if flags["i"] else None
        if flags["u"]:
            new.font.underline = MSO_UNDERLINE.SINGLE_LINE
        elif new.font.underline is not None:
            new.font.underline = False
        if any(key in flags for key in _OPTIONAL_RUN_KEYS):
            _apply_optional_pptx(new, flags)


def _apply_optional_pptx(run: Any, flags: dict) -> None:
    """Strike / baseline / size / colour on a DrawingML run.

    ⚠️ Split out from `_write_runs_pptx` only because it is a DIFFERENT VOCABULARY
    from the docx path, and putting the two side by side in one function is how
    `w:b` ended up in a slide once already. Everything here is either a
    `run.font` property that exists or an attribute on `a:rPr` that does not."""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    rpr = run._r.get_or_add_rPr()
    if "s" in flags:
        rpr.set("strike", "sngStrike" if flags["s"] else "none")
    if "va" in flags:
        # ⚠️ ONE attribute, two meanings. `baseline` absent is ordinary text, so
        # "neither" has to remove the attribute rather than write a 0 — a 0 is a
        # legal per-mille value and PowerPoint renders it as ordinary text too,
        # but the file would then claim the reader chose it.
        baseline = _PPTX_BASELINE.get(flags["va"])
        if baseline:
            rpr.set("baseline", baseline)
        elif "baseline" in rpr.attrib:
            del rpr.attrib["baseline"]
    if "sz" in flags:
        run.font.size = Pt(float(flags["sz"]))
    if "color" in flags:
        run.font.color.rgb = RGBColor.from_string(flags["color"])


def _set_paragraph_text(paragraph: Any, text: str, runs: Optional[list] = None,
                        align: Any = None, align_map: Any = None) -> None:
    """Put `text` in a paragraph, keeping the FIRST run's character formatting.

    ⚠️ Why not `paragraph.text = text`: python-docx implements that by deleting
    every run and making a new one from the paragraph's default style, so a
    heading that was blue loses the blue, and a run that was bold loses the bold —
    from the WHOLE paragraph, because the reader only touched part of it.

    Writing into run 0 and emptying the rest keeps the paragraph's style and the
    formatting of the text the reader was typing in. That is the fallback, and it
    is still the whole answer for an edit that carries no formatting of its own.

    `runs` is the richer form, sent when the reader pressed B / I / U: the
    paragraph is then rebuilt as several runs, each cloned from run 0 and carrying
    its own flags. That is what makes the toolbar honest — without it a bold word
    would be flattened back to the run it was written into on every save."""
    if runs:
        _write_runs(paragraph, runs)
    elif not paragraph.runs:
        if text:
            paragraph.add_run(text)
    else:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    # ⚠️ Applied on BOTH paths, including the no-runs one. An alignment-only edit
    # carries no formatting, so it arrives here with `runs` empty — and a version
    # that returned early for the runs case and set alignment afterwards anyway
    # would have been fine, while the reverse order loses it. It is last, and
    # unconditional, for that reason.
    _apply_alignment(paragraph, align, align_map or _docx_align_enum())


def _set_cell_text(tc: Any, text: str, runs: Optional[list] = None,
                   align: Any = None) -> None:
    """Put `text` in a `<w:tc>`, keeping the first paragraph's formatting.

    Extra paragraphs in a cell are kept as empty rather than deleted: removing
    them would renumber the cells the renderer numbered, so an unrelated edit
    elsewhere in the same table would move.
    """
    from docx.oxml.ns import qn

    paras = tc.findall(qn("w:p"))
    if not paras:
        if text:
            tc.append(_new_w_p(text))
        return
    _set_paragraph_text(_wrap_paragraph(paras[0]), text, runs, align)
    for extra in paras[1:]:
        for node in extra.findall(".//" + qn("w:t")):
            node.text = ""


def _new_w_p(text: str) -> Any:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p = OxmlElement("w:p")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    p.append(run)
    return p


def _wrap_paragraph(el: Any) -> Any:
    from docx.text.paragraph import Paragraph
    return Paragraph(el, None)


def _docx_apply_edits(data: bytes, edits: dict) -> tuple[bytes, list, list]:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise EditError(f"not a readable .docx container: {exc}") from exc

    # Rebuild the renderer's numbering. Same file, same walk, same order.
    targets: dict[str, tuple] = {}
    unit = 0
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            targets["b%d" % unit] = ("p", child)
            unit += 1
        elif child.tag == qn("w:tbl"):
            for cells in _docx_table_cells(Table(child, document)):
                for tc, _span in cells:
                    targets["b%d" % unit] = ("tc", tc)
                    unit += 1

    applied, dropped = [], []
    for oid, edit in edits.items():
        hit = targets.get(oid)
        if hit is None:
            dropped.append(oid)
            continue
        kind, el = hit
        text = _edit_text(edit)
        runs = normalise_runs(_edit_runs(edit), text)
        align = _edit_align(edit)
        try:
            if kind == "p":
                _set_paragraph_text(Paragraph(el, document), text, runs, align,
                                    _docx_align_enum())
            else:
                _set_cell_text(el, text, runs, align)
            applied.append(oid)
        except Exception:
            dropped.append(oid)

    # ⚠️ Nothing was written, so hand back the ORIGINAL bytes. A save here rebuilds
    # the OOXML zip with fresh entry timestamps, so the archive comes out different
    # from the input for no reason at all — a no-op edit would look like it had
    # rewritten the file, and whether the clock ticked inside the zip's 2-second
    # timestamp granularity is luck. That luck is what made
    # `test_unknown_oid_is_dropped_not_guessed` fail roughly one run in five.
    if not applied:
        return data, applied, dropped

    out = io.BytesIO()
    document.save(out)
    return out.getvalue(), applied, dropped


def _pptx_apply_edits(data: bytes, edits: dict) -> tuple[bytes, list, list]:
    from pptx import Presentation

    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise EditError(f"not a readable .pptx container: {exc}") from exc

    slides = list(presentation.slides)
    applied, dropped = [], []
    for oid, edit in edits.items():
        text = _edit_text(edit)
        # ⚠️ Validated against the FIRST LINE, not the whole text. A slide's text
        # box is several paragraphs in OOXML, and the reader's runs describe the
        # line the cursor was in — so comparing them against text that still has
        # its newlines would fail the agreement check every time, and the toolbar
        # would be inert on every multi-line box in the deck. That is the same
        # "the guard is right and the caller is wrong" shape as everywhere else.
        first_line = text.split("\n", 1)[0]
        runs = normalise_runs(_edit_runs(edit), first_line)
        parts = oid.split("r")
        if len(parts) != 2 or not parts[0].startswith("s"):
            dropped.append(oid)
            continue
        try:
            slide_index = int(parts[0][1:])
            shape_id = int(parts[1])
        except ValueError:
            dropped.append(oid)
            continue
        # The preview only ever shows the first `max_slides` slides, so an oid
        # past that is not a stale id — it is an edit for something the reader
        # could not have seen. Dropped for the same reason.
        if not (0 <= slide_index < len(slides)):
            dropped.append(oid)
            continue
        shape = None
        for candidate in slides[slide_index].shapes:
            try:
                if int(getattr(candidate, "shape_id", 0) or 0) == shape_id:
                    shape = candidate
                    break
            except (TypeError, ValueError):
                continue
        if shape is None or not getattr(shape, "has_text_frame", False):
            dropped.append(oid)
            continue
        try:
            frame = shape.text_frame
            if not frame.paragraphs:
                dropped.append(oid)
                continue
            # ⚠️ Newlines are PARAGRAPHS in a text frame, not soft breaks. The
            # preview reads a box with `textContent`, which does not carry the
            # paragraph mark, so the browser sends the lines back joined. Writing
            # that as one run would silently reflow every multi-line text box in
            # the deck the first time anyone edited something else. One line in,
            # one paragraph out.
            lines = text.split("\n")
            # ⚠️ `runs` belongs to the FIRST line only, and that is not a shortcut:
            # a text box is several paragraphs in OOXML, so a two-line box with a
            # bold first line is two paragraphs with different runs. The toolbar
            # sends the runs of the line the cursor is in, because that is the line
            # it applied them to.
            if runs:
                _write_runs_pptx(frame.paragraphs[0], runs)
            else:
                # ⚠️ The map is this caller's, because the writer is shared: see
                # `_apply_alignment`. Left to its default it would hand PowerPoint
                # a Word enum.
                _set_paragraph_text(frame.paragraphs[0], lines[0], None, _edit_align(edit),
                                    _pptx_align_enum())
            if runs:
                # ⚠️ The FIRST line only, for the same reason the runs are: a text
                # box is several paragraphs and the reader aligned the one the
                # cursor was in. Done here rather than inside the writer because
                # the runs path returns early.
                _apply_alignment(frame.paragraphs[0], _edit_align(edit),
                                 _pptx_align_enum())
            for index, extra in enumerate(frame.paragraphs[1:], start=1):
                _set_paragraph_text(extra, lines[index] if index < len(lines) else "")
            for line in lines[len(frame.paragraphs):]:
                frame.add_paragraph().text = line
            applied.append(oid)
        except Exception:
            dropped.append(oid)

    # ⚠️ Nothing was written, so hand back the ORIGINAL bytes. A save here rebuilds
    # the OOXML zip with fresh entry timestamps, so the archive comes out different
    # from the input for no reason at all — a no-op edit would look like it had
    # rewritten the file, and whether the clock ticked inside the zip's 2-second
    # timestamp granularity is luck. That luck is what made
    # `test_unknown_oid_is_dropped_not_guessed` fail roughly one run in five.
    if not applied:
        return data, applied, dropped

    out = io.BytesIO()
    presentation.save(out)
    return out.getvalue(), applied, dropped


_XLSX_OID = re.compile(r"^x(?P<sheet>.+?)!R(?P<row>\d+)C(?P<col>\d+)$")


def _xlsx_apply_flags(cell: Any, runs: list) -> None:
    """A cell's bold / italic / underline — for the WHOLE cell, and only if uniform.

    ⚠️ ⚠️ This is a real limitation and it is stated here rather than discovered by
    a reader later: **a spreadsheet cell cannot hold two different formats.**
    Excel's own cell is one styled box, and openpyxl's `cell.font` is a property of
    the cell, not of a span inside it. So "bold the first word of B3" is not
    something this file format has a place to put.

    ⚠️ When the runs DISAGREE, this returns without touching the cell. An earlier
    version took the leading run's flags, and it was wrong in the exact case it was
    written for: the reader selected the second word of a cell and pressed B, the
    leading run was the plain first word, and the cell came back not bold — the
    button silently did nothing. A rule that picks a winner is a rule that is wrong
    half the time, and "wrong half the time" is not a rule.

    ⚠️ "Disagree" is the WHOLE state, not just bold / italic / underline. A cell
    whose words disagree only about size is just as un-writable as one that
    disagrees about bold, and comparing three of the seven attributes would have
    let a half-applied size through.

    The text is already written by the time this runs, so the reader keeps their
    words; only the formatting is left alone. The client is expected not to ask:
    for a workbook it widens the selection to the whole cell, which is what Excel
    does when you select part of a cell and press B, and that makes the runs
    uniform by construction. This is the backstop for a client that does not.

    Copying the existing font is what keeps the cell's size, colour and name: a
    fresh `Font()` would reset all of them to the workbook default, which is the
    same class of bug as `paragraph.text = x` in the .docx path."""
    from openpyxl.styles import Font

    states = {_run_state(r) for r in runs}
    if len(states) > 1:
        return
    head = runs[0]
    current = cell.font
    kwargs: dict = {
        "name": current.name,
        "size": current.size,
        "bold": head["b"] or None,
        "italic": head["i"] or None,
        "underline": "single" if head["u"] else None,
        "strike": current.strike,
        "color": current.color if current.color is not None else None,
    }
    # ⚠️ Only what the reader sent. `sz` and `color` in particular are routinely
    # INHERITED from the workbook's default style, and writing the inherited value
    # into the cell would pin it there for good.
    if "s" in head:
        kwargs["strike"] = bool(head["s"]) or None
    if "va" in head:
        kwargs["vertAlign"] = {"sup": "superscript", "sub": "subscript"}.get(head["va"])
    if "sz" in head:
        kwargs["size"] = float(head["sz"])
    if "color" in head:
        kwargs["color"] = head["color"]
    try:
        cell.font = Font(**kwargs)
    except Exception:
        # A workbook whose colour is a theme or an index openpyxl will not copy
        # back is not a reason to lose the edit; the text is already written.
        pass


def _apply_alignment(target: Any, align: Any, align_map: Any = None) -> None:
    """Left / centre / right on whatever this format calls a paragraph.

    ⚠️ `align_map` is the CALLER's enum, passed in rather than guessed from the
    argument's type. `_set_paragraph_text` is shared by the .docx and the .pptx
    paths, and the two `alignment` properties take members of DIFFERENT enums; a
    version that worked out the format from its imports would hand Word's
    `WD_PARAGRAPH_ALIGNMENT` to a DrawingML paragraph, which accepts any object
    and stores it — so the slide would be written with a Word enum in it and the
    error would surface in PowerPoint, not here.

    ⚠️ Justify is deliberately absent. Word and PowerPoint both have it and a
    spreadsheet has no way to express it, so a button for it would be a control
    that silently does nothing on one of the three formats — the one failure mode
    this editor is built to avoid."""
    if align not in _ALIGN_VALUES or not align_map:
        return
    try:
        target.alignment = align_map[align]
    except Exception:
        # Formatting the reader did not ask for is not a reason to lose their text.
        pass


def _apply_alignment_xlsx(cell: Any, align: Any) -> None:
    """Horizontal alignment on a CELL — a different property from a paragraph's.

    ⚠️ Copied rather than rebuilt: a cell's alignment also carries vertical,
    wrapping, indent and rotation, and a fresh `Alignment(horizontal=…)` would
    quietly reset all four on every alignment change."""
    if align not in _ALIGN_VALUES:
        return
    try:
        import copy as _copy

        value = _copy.copy(cell.alignment)
        value.horizontal = align
        cell.alignment = value
    except Exception:
        pass


def _docx_align_enum() -> Any:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    return {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
            "right": WD_ALIGN_PARAGRAPH.RIGHT}


def _pptx_align_enum() -> Any:
    from pptx.enum.text import PP_ALIGN

    return {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
            "right": PP_ALIGN.RIGHT}


def _xlsx_apply_edits(data: bytes, edits: dict) -> tuple[bytes, list, list]:
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(data))
    except Exception as exc:
        raise EditError(f"not a readable .xlsx container: {exc}") from exc

    applied, dropped = [], []
    for oid, edit in edits.items():
        text = _edit_text(edit)
        runs = normalise_runs(_edit_runs(edit), text)
        match = _XLSX_OID.match(oid or "")
        if not match:
            dropped.append(oid)
            continue
        name = match.group("sheet")
        if name not in book.sheetnames:
            dropped.append(oid)
            continue
        try:
            cell = book[name].cell(row=int(match.group("row")),
                                   column=int(match.group("col")))
            # A cell that held a number now holds the reader's text, and a cell
            # that held text can be cleared. Excel stores both in one field, so
            # an empty string is written rather than None to keep the cell's own
            # existence — deleting it would shift nothing but would also drop any
            # formatting attached to it.
            cell.value = text if text != "" else None
            if runs:
                _xlsx_apply_flags(cell, runs)
            _apply_alignment_xlsx(cell, _edit_align(edit))
            applied.append(oid)
        except Exception:
            dropped.append(oid)

    # ⚠️ Nothing was written, so hand back the ORIGINAL bytes. A save here rebuilds
    # the OOXML zip with fresh entry timestamps, so the archive comes out different
    # from the input for no reason at all — a no-op edit would look like it had
    # rewritten the file, and whether the clock ticked inside the zip's 2-second
    # timestamp granularity is luck. That luck is what made
    # `test_unknown_oid_is_dropped_not_guessed` fail roughly one run in five.
    if not applied:
        return data, applied, dropped

    out = io.BytesIO()
    book.save(out)
    return out.getvalue(), applied, dropped


def apply_edits(data: bytes, doc_type: str, edits: dict) -> tuple:
    """Apply `{oid: text}` to a document's bytes. Returns `(bytes, applied, dropped)`.

    `edits` maps the `data-oid` the preview rendered to the text the reader typed.
    Oids that no longer resolve — the file was replaced under us, the shape was
    deleted, the slide is past the preview's limit — are returned in `dropped`
    rather than guessed at, because a save that quietly wrote the wrong text is
    worse than one that says it wrote less than asked.
    """
    kind = (doc_type or "").strip().lower().lstrip(".")
    if kind not in EDITABLE_DOC_TYPES:
        raise EditError(f"{kind or doc_type!r} cannot be edited in place")
    if not isinstance(edits, dict) or not edits:
        raise EditError("no edits to apply")
    if kind == "docx":
        return _docx_apply_edits(data, edits)
    if kind == "pptx":
        return _pptx_apply_edits(data, edits)
    return _xlsx_apply_edits(data, edits)
