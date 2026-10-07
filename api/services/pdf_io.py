"""PDF reading, rasterising and writing — one module, two permissive engines.

Klado needs three PDF jobs: count the pages of an uploaded document, rasterise pages
to PNG for the in-app preview, and wrap the browser's slide screenshots back into a
PDF for the visual report export. Every one of them used to be done by PyMuPDF, which is
AGPL-3.0 — incompatible with distributing this product under a permissive licence.

The replacement is split because neither engine can do both halves:

* ``pypdfium2`` (BSD-3-Clause OR Apache-2.0) renders. It opens a PDF and rasterises
  pages; it cannot author a document with real text.
* ``pypdf`` (BSD-3-Clause) writes. It assembles pages and image XObjects; it cannot
  rasterise.

Call sites import this module, never the engines, so that the next engine swap is one
file and so that the licence of anything reading a PDF stays auditable in one place.

**The writer is lossless by contract.** :func:`pngs_to_pdf` stores each PNG's pixels as a
``FlateDecode`` RGB image; it never re-encodes them through a lossy codec. The visual
report export documents itself as "pixel-faithful", and the pages it wraps are Chromium
screenshots full of 1px glyph edges — a JPEG there would be a visible regression. The
cost is size: raw-plus-Flate is roughly 3x the PNG, because PNG's per-scanline filtering
beats a plain deflate. That is a good trade for a capped 80-page export.
"""
from __future__ import annotations

import io
import zlib
from collections.abc import Iterator, Sequence

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, StreamObject

import pypdfium2 as pdfium

#: PDF user-space unit is 1/72 inch, so this maps a PNG's pixel grid onto page points the
#: way a browser does at 96 dpi — a 1920x1080 screenshot becomes a 1440x810 pt page.
PX_TO_PT = 0.75

#: Largest page dimension a PDF may declare. Beyond this, viewers refuse or degrade, so
#: a screenshot bigger than this is refused rather than silently mangled.
MAX_PAGE_PT = 14400


class PdfError(Exception):
    """A PDF could not be read, or is too large to write."""


def pdf_page_count(data: bytes) -> int:
    """Number of pages in ``data``. Raises :class:`PdfError` if it is not a readable PDF."""
    try:
        with pdfium.PdfDocument(data) as document:
            return len(document)
    except PdfError:
        raise
    except Exception as exc:  # noqa: BLE001 — every engine error is a bad document here
        raise PdfError(f"not a readable PDF: {exc}") from exc


def pdf_pages_to_png(
    data: bytes, *, zoom: float, max_pages: int
) -> Iterator[tuple[int, bytes, int, int]]:
    """Lazily rasterise the first ``max_pages`` pages to PNG.

    ``zoom`` is a plain scale factor on PDF points (1.5 = 108 dpi). Yields
    ``(page_index, png_bytes, width, height)`` per page, skipping any page that fails to
    render — one damaged page must not take the whole preview down.

    This is a generator on purpose: the preview has a byte budget and stops pulling once
    it is full, so rendering every page up front would burn CPU on pages nobody sees.
    Callers that stop early should wrap it in ``contextlib.closing`` to release the
    document deterministically.
    """
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:  # noqa: BLE001
        raise PdfError(f"not a readable PDF: {exc}") from exc
    try:
        limit = max(1, int(max_pages or 1))
        for index in range(min(len(document), limit)):
            image = None
            try:
                page = document[index]
                bitmap = page.render(scale=zoom)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
                    page.close()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
            except Exception:  # noqa: BLE001, PERF203 — one bad page must not kill the rest
                continue
            yield index, buffer.getvalue(), image.width, image.height
    finally:
        document.close()


def pngs_to_pdf(pages: Sequence[bytes], *, max_page_pt: int = MAX_PAGE_PT) -> bytes:
    """Wrap PNG images into a PDF, one page each, without touching a single pixel.

    Raises :class:`PdfError` if any image is larger than ``max_page_pt`` on either side.
    """
    writer = PdfWriter()
    for png in pages:
        try:
            with Image.open(io.BytesIO(png)) as opened:
                image = opened.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise PdfError(f"not a readable PNG: {exc}") from exc
        width_pt, height_pt = image.width * PX_TO_PT, image.height * PX_TO_PT
        if width_pt > max_page_pt or height_pt > max_page_pt:
            raise PdfError(
                f"page is {image.width}x{image.height} px, "
                f"over the {max_page_pt:g} pt PDF limit"
            )
        page = writer.add_blank_page(width=width_pt, height=height_pt)
        stream = StreamObject()
        stream.set_data(zlib.compress(image.tobytes(), 9))
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(image.width),
            NameObject("/Height"): NumberObject(image.height),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        })
        image_ref = writer._add_object(stream)
        # One content stream per page: scale the unit image square onto the whole page.
        content = StreamObject()
        content.set_data(
            f"q {width_pt} 0 0 {height_pt} 0 0 cm /Im0 Do Q".encode("latin-1")
        )
        page[NameObject("/Contents")] = writer._add_object(content)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({
                NameObject("/Im0"): image_ref,
            }),
        })
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


#: A page is a list of ``(x_pt, y_pt, font_pt, text)`` lines, top-down.
TextPage = Sequence[tuple[float, float, float, str]]

#: Rasterisation scale for :func:`text_pdf`. 2.0 = 144 dpi, which keeps small fixture
#: text legible when the document is later rasterised for a preview at 108 dpi.
TEXT_PDF_SCALE = 2.0


#: A codepoint FreeType is guaranteed to have no glyph for, so it always rasterises to the
#: same ".notdef" box. Comparing a character's raster against it is how a missing glyph is
#: detected with the public Pillow API.
_NOTDEF_SAMPLE = "\ufffe"


def _unsupported_glyphs(font: ImageFont.FreeTypeFont, text: str) -> set[str]:
    """Characters in ``text`` the font will draw as an empty .notdef box."""
    def raster(ch: str) -> tuple[tuple[int, int], bytes]:
        mask = font.getmask(ch, mode="L")
        return mask.size, bytes(mask)

    notdef = raster(_NOTDEF_SAMPLE)
    return {ch for ch in text if ch not in " \r\n\t" and raster(ch) == notdef}


def text_pdf(
    pages: Sequence[TextPage], *, page_size: tuple[float, float] = (595.0, 842.0)
) -> bytes:
    """Build a PDF of typeset text pages — for test fixtures and demo documents.

    Text is drawn with Pillow's bundled default font and embedded through
    :func:`pngs_to_pdf`, so the result is pixel-identical to the drawing but is raster
    text, not selectable text. That is the right trade for the only two callers
    (document-upload fixtures and the seed demo one-pager): neither needs text extraction,
    and both are read back through a rasteriser anyway. Anything that needs a real text
    layer wants a different tool.

    That font is Latin-only: em-dashes, bullet, currency symbols, accented letters and all
    CJK have no glyph. Those are **rejected**, not drawn as blank boxes — a demo document
    that silently shows a row of tofu is worse than one that refuses to be written.
    """
    width_pt, height_pt = page_size
    rendered: list[bytes] = []
    for lines in pages:
        image = Image.new("RGB", (int(width_pt * TEXT_PDF_SCALE), int(height_pt * TEXT_PDF_SCALE)), "white")
        draw = ImageDraw.Draw(image)
        for x_pt, y_pt, font_pt, text in lines:
            pixels = max(1, round(font_pt * TEXT_PDF_SCALE))
            font = ImageFont.load_default(size=pixels)
            unsupported = _unsupported_glyphs(font, text)
            if unsupported:
                raise PdfError(
                    "the built-in PDF font has no glyph for "
                    + ", ".join(f"{ch!r} (U+{ord(ch):04X})" for ch in sorted(unsupported))
                )
            draw.text(
                (x_pt * TEXT_PDF_SCALE, y_pt * TEXT_PDF_SCALE),
                text,
                fill=(17, 24, 39),
                font=font,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        rendered.append(buffer.getvalue())
    return pngs_to_pdf(rendered)