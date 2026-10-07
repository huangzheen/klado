"""Guards for :mod:`services.pdf_io` — the module that replaced an AGPL PDF library.

Two things here are load-bearing and easy to lose:

* the PNG→PDF writer is **lossless by contract** (the visual report export advertises
  itself as pixel-faithful), and
* the dependency tree Klado declares must stay free of strong copyleft, because that is
  the entire reason this module exists and because the product is distributed as a
  container image under Apache-2.0.

The licence tests walk the dependency graph **declared** in ``api/requirements.txt``
rather than everything installed in the interpreter, so they answer "what does this
product promise to ship" and stay deterministic between a developer machine and the
container CI builds.
"""
from __future__ import annotations

import ast
import contextlib
import io
import math
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from services import license_audit, pdf_io
from services.pdf_io import PdfError

API_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = API_DIR.parent


class LosslessWriteTests(unittest.TestCase):
    """The report export is documented as pixel-faithful; a lossy codec here is a bug."""

    @staticmethod
    def _source(width: int = 1920, height: int = 1080) -> Image.Image:
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for x in range(width):                       # per-column colour: hostile to JPEG
            draw.line([(x, 0), (x, height)], fill=(x % 256, (x * 3) % 256, (x * 7) % 256))
        for y in range(0, height, 6):                # 1px hard edges, like glyphs
            draw.rectangle([40, y, 40 + (y % 400) + 40, y + 4], fill=(0, 0, 0))
        return image

    def _round_trip(self, image: Image.Image) -> np.ndarray:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pdf = pdf_io.pngs_to_pdf([buffer.getvalue()])
        self.assertEqual(pdf_io.pdf_page_count(pdf), 1)
        scale = image.width / (image.width * pdf_io.PX_TO_PT)
        with contextlib.closing(pdf_io.pdf_pages_to_png(pdf, zoom=scale, max_pages=1)) as pages:
            rendered = list(pages)
        self.assertEqual(len(rendered), 1)
        _, png, width, height = rendered[0]
        self.assertEqual((width, height), image.size)
        back = Image.open(io.BytesIO(png)).convert("RGB")
        return np.abs(np.asarray(image, np.int16) - np.asarray(back, np.int16))

    def test_a_rendered_page_survives_the_pdf_round_trip_pixel_for_pixel(self):
        difference = self._round_trip(self._source())
        self.assertEqual(
            int(difference.max()), 0,
            "PNG->PDF->PNG changed pixels; the export is supposed to be pixel-faithful",
        )

    def test_a_flat_ui_page_survives_too(self):
        """The gradient is the worst case; a real slide is mostly flat colour."""
        image = Image.new("RGB", (1920, 1080), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, 1920, 72], fill=(31, 41, 55))
        for row in range(18):
            draw.rectangle([60, 120 + row * 46, 760, 142 + row * 46], fill=(226, 232, 240))
        difference = self._round_trip(image)
        self.assertEqual(int(difference.max()), 0)

    def test_the_writer_never_emits_a_lossy_image_codec(self):
        buffer = io.BytesIO()
        self._source(400, 300).save(buffer, format="PNG")
        pdf = pdf_io.pngs_to_pdf([buffer.getvalue()])
        self.assertNotIn(b"/DCTDecode", pdf)          # JPEG
        self.assertNotIn(b"/JPXDecode", pdf)          # JPEG 2000
        self.assertIn(b"/FlateDecode", pdf)

    def test_each_png_becomes_one_page_sized_in_css_pixels(self):
        sizes = ((1920, 1080), (1240, 1754))
        buffers = []
        for size in sizes:
            buffer = io.BytesIO()
            Image.new("RGB", size, "white").save(buffer, format="PNG")
            buffers.append(buffer.getvalue())
        pdf = pdf_io.pngs_to_pdf(buffers)
        self.assertEqual(pdf_io.pdf_page_count(pdf), len(sizes))
        # At zoom 1 the raster covers the page box, so a 1920x1080 screenshot is a
        # 1440x810 pt page. Odd pixel sizes land on a half point (1240x1754 -> 930x1315.5);
        # the raster rounds up so it still covers the whole page rather than clipping a row.
        with contextlib.closing(pdf_io.pdf_pages_to_png(pdf, zoom=1.0, max_pages=len(sizes))) as pages:
            rendered = [(width, height) for _, _, width, height in pages]
        self.assertEqual(
            rendered,
            [(math.ceil(w * pdf_io.PX_TO_PT), math.ceil(h * pdf_io.PX_TO_PT)) for w, h in sizes],
        )

    def test_a_page_beyond_the_pdf_limit_is_refused_not_mangled(self):
        huge = io.BytesIO()
        Image.new("RGB", (20000, 20), "white").save(huge, format="PNG")
        with self.assertRaises(PdfError) as caught:
            pdf_io.pngs_to_pdf([huge.getvalue()], max_page_pt=14400)
        self.assertIn("14400", str(caught.exception))


class ReadingTests(unittest.TestCase):
    def test_the_page_cap_actually_caps(self):
        pdf = pdf_io.text_pdf([[(72, 72, 12, f"page {n}")] for n in range(5)])
        self.assertEqual(pdf_io.pdf_page_count(pdf), 5)
        with contextlib.closing(pdf_io.pdf_pages_to_png(pdf, zoom=1.0, max_pages=2)) as pages:
            self.assertEqual(len(list(pages)), 2)

    def test_a_corrupt_upload_is_reported_rather_than_crashing(self):
        with self.assertRaises(PdfError):
            pdf_io.pdf_page_count(b"this is not a pdf at all")

    def test_an_unreadable_png_is_reported_rather_than_crashing(self):
        with self.assertRaises(PdfError):
            pdf_io.pngs_to_pdf([b"not an image"])

    def test_the_rasteriser_is_lazy_so_a_capped_preview_wastes_no_work(self):
        """The preview stops pulling once its byte budget is full; rendering up front
        would burn CPU on pages nobody ever sees."""
        calls = []

        class _Counting(float):
            pass

        pdf = pdf_io.text_pdf([[(72, 72, 12, f"page {n}")] for n in range(5)])
        calls: list[int] = []
        original = pdf_io.pdfium.PdfDocument

        def counting_open(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        pdf_io.pdfium.PdfDocument = counting_open
        try:
            stream = pdf_io.pdf_pages_to_png(pdf, zoom=1.0, max_pages=5)
            next(stream)
            stream.close()
        finally:
            pdf_io.pdfium.PdfDocument = original
        self.assertEqual(len(calls), 1, "the document should be opened once, not per page")


class TextPdfTests(unittest.TestCase):
    def test_text_pages_carry_the_lines_that_were_asked_for(self):
        pdf = pdf_io.text_pdf([[(72, 72, 18, "One Pager page 1")]])
        self.assertEqual(pdf_io.pdf_page_count(pdf), 1)
        with contextlib.closing(pdf_io.pdf_pages_to_png(pdf, zoom=1.0, max_pages=1)) as pages:
            _, png, _, _ = next(iter(pages))
        # The ink in the rendered page must contain the dark pixels the text produced.
        pixels = np.asarray(Image.open(io.BytesIO(png)).convert("L"), np.int16)
        self.assertGreater(int((pixels < 128).sum()), 200, "the page rendered blank")

    def test_a_glyph_the_font_cannot_draw_is_refused_not_silently_tofu(self):
        """Pillow's bundled font has no em-dash, no bullet, no CJK. Shipping one would
        put a row of empty boxes into a customer-facing demo document."""
        for text in ("Q4 — One Pager", "bullet • item", "中文标题"):
            with self.subTest(text=text):
                with self.assertRaises(PdfError) as caught:
                    pdf_io.text_pdf([[(60, 70, 12, text)]])
                self.assertIn("no glyph", str(caught.exception))

    def test_plain_latin_text_is_accepted(self):
        self.assertEqual(
            pdf_io.pdf_page_count(pdf_io.text_pdf([[(60, 70, 12, "Q4: One Pager, 2026-10-19")]])), 1
        )


class VisualExportWiringTests(unittest.TestCase):
    """The report export's own wrapper. The rewrite moved the pixels into ``pdf_io``, but
    the wrapper is still what decides page limits and what the user is told when a page is
    too big — and it had no test at all, before or after."""

    @staticmethod
    def _png(width: int, height: int) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (width, height), (240, 240, 240)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_one_page_per_slide_in_the_order_given(self):
        from services.report_visual_export import _pdf_from_pngs
        pages = [self._png(1920, 1080), self._png(1240, 1754), self._png(800, 600)]
        pdf = _pdf_from_pngs(pages)
        self.assertEqual(pdf_io.pdf_page_count(pdf), 3)
        with contextlib.closing(pdf_io.pdf_pages_to_png(pdf, zoom=1.0, max_pages=3)) as rendered:
            sizes = [(width, height) for _, _, width, height in rendered]
        self.assertEqual(
            sizes,
            [(1440, 810), (930, 1316), (600, 450)],
            "each slide must become one page, in order, at CSS-pixel page size",
        )

    def test_an_oversized_slide_is_refused_in_both_languages(self):
        from services.report_visual_export import VisualExportError, _pdf_from_pngs
        with self.assertRaises(VisualExportError) as caught:
            _pdf_from_pngs([self._png(20000, 100)])
        message = str(caught.exception)
        self.assertIn("too large", message)          # English half
        self.assertIn("太大", message)                 # Chinese half
        self.assertNotIsInstance(caught.exception, PdfError)

    def test_the_wrapper_does_not_re_encode(self):
        """A regression guard on the reason the wrapper exists: Pillow's PDF writer would
        silently make every exported report lossy, and nothing else would report it."""
        from services.report_visual_export import _pdf_from_pngs
        pdf = _pdf_from_pngs([self._png(1920, 1080)])
        self.assertNotIn(b"/DCTDecode", pdf)
        self.assertIn(b"/FlateDecode", pdf)


class DeclaredLicenceTests(unittest.TestCase):
    """The reason this module exists: the shipped dependency tree must stay permissive."""

    def test_the_declared_dependency_tree_has_no_blocking_licence(self):
        tree = license_audit.declared_tree(REPO_ROOT)
        offending = {
            name: text for name, text in tree.items()
            if license_audit.classify(text) == "blocking"
        }
        self.assertEqual(
            offending, {},
            "these declared dependencies carry a licence incompatible with Apache-2.0 "
            "distribution; replace them or drop them from api/requirements.txt",
        )

    def test_no_dependency_licence_is_left_unclassified(self):
        """An unreadable licence string is not a pass. It has to be looked at."""
        tree = license_audit.declared_tree(REPO_ROOT)
        unknown = {name: text for name, text in tree.items()
                   if license_audit.classify(text) == "unknown"}
        self.assertEqual(unknown, {}, "unclassified licences in the declared tree")

    def test_the_pdf_engines_are_the_permissive_ones_we_audited(self):
        tree = license_audit.declared_tree(REPO_ROOT)
        self.assertIn("pypdfium2", tree)
        self.assertIn("pypdf", tree)
        self.assertNotIn("pymupdf", tree)
        self.assertNotIn("extract-msg", tree)
        self.assertNotIn("extract-msg", license_audit.declared_roots(REPO_ROOT))
        self.assertNotIn("pymupdf", license_audit.declared_roots(REPO_ROOT))

    def test_every_weak_copyleft_dependency_is_named_in_the_third_party_notices(self):
        """LGPL and MPL may ship, but only if they are attributed."""
        weak = {name for name, text in license_audit.declared_tree(REPO_ROOT).items()
                if license_audit.classify(text) == "weak-copyleft"}
        self.assertTrue(weak, "expected psycopg2 to still be LGPL; if that changed, "
                              "re-read the notice rather than deleting this test")
        notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
        missing = {name for name in weak if name.lower() not in notices}
        self.assertEqual(missing, set(), "weak-copyleft dependencies missing from NOTICE")

    def test_no_source_file_references_a_removed_copyleft_library(self):
        """Checked with the AST, not with text matching: prose that merely *mentions*
        PyMuPDF (a docstring saying why it was removed, a comment describing what changed)
        must not read as a dependency coming back."""
        banned = {"fitz", "pymupdf", "pymupdf4llm", "extract_msg", "extract-msg", "oletools"}
        offenders = []
        for root in (API_DIR / "services", API_DIR / "routers", REPO_ROOT / "scripts"):
            for path in root.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""]
                    else:
                        continue
                    for name in names:
                        if name.split(".")[0].lower() in banned:
                            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "AGPL/GPL PDF or .msg code came back")


if __name__ == "__main__":
    unittest.main()