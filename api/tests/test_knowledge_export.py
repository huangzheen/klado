"""Knowledge-page exports: Word, PDF, picture, and the images inside them.

The property under test: **what the reader saw is what the file contains.** The page is
rendered by the SPA and posted to the exporter, so the server holds no second markdown
renderer — which means the tests here are about the *conversion*, not about markdown:
scripts must not survive, a heading must become a Word heading, a table a Word table, and
an image must be embedded at the width the page gave it (`{width=560}` / `{width=60%}`),
because a re-sized picture that comes back full-bleed is the whole reason the size exists.
"""
import base64
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers import business_knowledge as router_module
from services import knowledge_export as ke
from services.ai import business_knowledge as store
from services.ai.business_knowledge import KnowledgeItem

OWNER = "owner@example.com"

# A real (tiny) PNG. The Word export inlines the bytes as a data URI, so the header has to
# be the real thing for the type to come out right.
PNG_8X8 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAADklEQVQI12P4"
    "AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII=")

BODY_HTML = """
<h2>渠道口径</h2>
<p>甲公司归 <strong>KA1</strong>，口径以 <code>data-klado-dictionary</code> 为准。</p>
<ul><li>甲公司 KA1</li><li>乙公司 B2B</li></ul>
<pre><code>SELECT channel FROM sales_invoice;</code></pre>
<table><tr><th>渠道</th><th>口径</th></tr><tr><td>甲公司</td><td>sell-out</td></tr></table>
<p><img src="/api/storage/serve?path=knowledge-assets/a.png" alt="图1" data-kb-w="560px"
        style="width:560px"></p>
<p><a href="https://example.com">链接</a></p>
"""


def _item(**overrides) -> KnowledgeItem:
    base = dict(id=1, slug="channel-notes", title="渠道笔记", summary="口径笔记",
                category="渠道", tags="渠道", document_type="note",
                body="# 渠道笔记\n\n正文", owner_email=OWNER, visibility="private")
    base.update(overrides)
    return KnowledgeItem(**base)


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):
        request.state.current_user = {"email": OWNER}
        request.state.auth_kind = "browser"
        return await call_next(request)

    app.include_router(router_module.router, prefix="/api/knowledge")
    return TestClient(app, raise_server_exceptions=False)


def _word(html: str, **kwargs) -> str:
    return ke.build_word_document(html, kwargs.pop("title", "渠道笔记"), **kwargs)


# ── sanitising what the client posted ────────────────────────────────────────

class SanitizeTests(unittest.TestCase):
    def test_scripts_and_frames_never_reach_the_document(self):
        dirty = ('<p>ok</p><script>alert(1)</script>'
                 '<iframe src="https://evil.example"></iframe>'
                 '<object data="x"></object>')
        clean = ke.sanitize_fragment(dirty)
        self.assertIn("<p>ok</p>", clean)
        for fragment in ("<script", "alert", "<iframe", "<object"):
            self.assertNotIn(fragment, clean)

    def test_inline_event_handlers_are_stripped(self):
        clean = ke.sanitize_fragment('<p onclick="steal()" onmouseover=x() >文字</p>')
        self.assertIn("文字", clean)
        self.assertNotIn("onclick", clean)
        self.assertNotIn("onmouseover", clean)

    def test_a_document_is_never_injected_into_the_export_shell(self):
        # Closing our own wrapper is the interesting attempt: the result must still be one
        # document, with the page's content inside it.
        document = ke.build_document("</div></main><h1>HIJACK</h1>", "标题",
                                     base_href="/")
        self.assertEqual(document.count("<main>"), 1)
        self.assertTrue(document.rstrip().endswith("</html>"))
        self.assertIn("<h1>HIJACK</h1>", document)


# ── the printable shell ──────────────────────────────────────────────────────

class DocumentShellTests(unittest.TestCase):
    def test_the_shell_carries_the_mount_point_and_the_title(self):
        # A sub-path mount on purpose: with "/" this assertion would hold for any
        # implementation that emits a constant, and the mount point is exactly the
        # thing a copied link gets wrong.
        document = ke.build_document("<p>x</p>", "渠道笔记", meta="渠道 · 2026-09-30",
                                     base_href="/klado/")
        self.assertIn("<base href='/klado/'>", document)
        self.assertIn("渠道笔记", document)
        self.assertIn("渠道 · 2026-09-30", document)
        # The reader's own syntax colours are what the export shows, not the screen palette.
        self.assertIn(".kb-t-k", document)

    def test_the_document_carries_its_own_fonts(self):
        """⚠️ The container has no usable Chinese font on purpose of a lie.

        `api/fonts/*.otf` — copied to /usr/share/fonts for fontconfig — turned out to be
        GitHub 404 pages saved with a font extension, so a document that relies on system
        fonts renders every Chinese glyph as a tofu box: a PDF whose text layer is perfect
        and whose pages are unreadable. The export therefore embeds the faces the image
        already ships for the report deck.
        """
        document = ke.build_document("<p>中文</p>", "t")
        for needed in ("@font-face", "coolsans-sc-narrow-400.woff2",
                       "coolsans-sc-narrow-700.woff2", "CoolSans SC Narrow",
                       "roboto-condensed-latin.woff2"):
            with self.subTest(needed=needed):
                self.assertIn(needed, document)

    def test_a_diagram_inherits_those_fonts_and_cannot_own_a_whole_page(self):
        document = ke.build_document("<p>x</p>", "t")
        # A Mermaid SVG carries its own font stack and does not inherit the body's; without
        # this override the diagram's Chinese labels were the last place still rendering as
        # boxes while the rest of the page was already correct.
        self.assertIn(".kb-mermaid svg text", document)
        self.assertIn("max-height", document)   # an unbreakable tall block leaves a blank page

    def test_screen_only_chrome_is_not_in_the_print_sheet(self):
        document = ke.build_document("<p>x</p>", "t")
        self.assertNotIn("kb-toc", document)
        self.assertNotIn("kb-ctx", document)


# ── Word ─────────────────────────────────────────────────────────────────────

class RouteKindTests(unittest.TestCase):
    """Which request is which, during an export.

    ⚠️ This pins down a defect that produced a *plausible* export: the first version matched
    the page by prefix, so `/api/storage/serve?...` — the page's own images — was answered
    with the page's HTML. The PDF still came out, the images still "loaded", and every one of
    them was the wrong bytes. Nothing but looking at the pixels would have caught it.
    """

    ORIGIN = "http://127.0.0.1:8000"

    def kind(self, url, page_path=""):
        return ke.route_kind(url, origin=self.ORIGIN, page_path=page_path)

    def test_the_page_itself(self):
        self.assertEqual(self.kind("http://127.0.0.1:8000/"), "page")
        self.assertEqual(self.kind("http://127.0.0.1:8000/",
                                   "/"), "page")

    def test_the_pages_own_images_are_not_the_page(self):
        for url in ("http://127.0.0.1:8000/api/storage/serve?path=knowledge-assets%2Fa.png",
                    "http://127.0.0.1:8000/api/storage/serve?path=x",
                    "http://127.0.0.1:8000/vendor/logo.png"):
            with self.subTest(url=url):
                self.assertEqual(self.kind(url), "same-origin")

    def test_a_query_on_the_page_path_is_a_resource_not_the_page(self):
        # `/` with a query is not the shell we meant to deliver.
        self.assertEqual(self.kind("http://127.0.0.1:8000/?v=2"), "same-origin")

    def test_an_authored_external_image_is_left_alone(self):
        self.assertEqual(self.kind("https://cdn.example.com/a.png"), "external")

    def test_anything_else_is_aborted(self):
        self.assertEqual(self.kind("data:image/png;base64,AAAA"), "other")
        self.assertEqual(self.kind(""), "other")


class WordWidthTests(unittest.TestCase):
    def test_pixels_become_points_and_percentages_are_left_to_word(self):
        # Word's HTML import maps 1px to 0.75pt, but it is happier being told.
        self.assertEqual(ke._word_width("560px", ), "420pt")
        self.assertEqual(ke._word_width("560"), "420pt")
        self.assertEqual(ke._word_width("50%"), "50%")
        self.assertEqual(ke._word_width(""), "")
        self.assertEqual(ke._word_width("wide"), "")
        # A nonsense width must not become a nonsense document.
        self.assertEqual(ke._word_width("120%"), "")
        self.assertEqual(ke._word_width("0%"), "")


class WordDocumentTests(unittest.TestCase):
    def test_the_document_is_word_shaped(self):
        # `w:WordDocument` + `@page WordSection1` are what stop Word showing the
        # "this file is in a different format" warning for an .doc that holds HTML.
        document = _word(BODY_HTML)
        self.assertIn("w:WordDocument", document)
        self.assertIn("WordSection1", document)
        self.assertIn("@page WordSection1", document)
        self.assertTrue(document.lstrip().startswith("<html"))
        self.assertIn("Microsoft YaHei", document)      # CJK must not fall back to a serif
        self.assertIn("渠道笔记", document)

    def test_every_block_lands_in_the_file(self):
        document = _word(BODY_HTML, meta="渠道 · 2026-09-30")
        self.assertIn("<h2>渠道口径</h2>", document)
        self.assertIn("<li>甲公司 KA1</li>", document)
        self.assertIn("SELECT channel", document)
        self.assertIn("渠道 · 2026-09-30", document)
        self.assertIn("<table>", document)

    def test_a_table_keeps_its_cells_and_its_header(self):
        document = _word(BODY_HTML)
        table = re.search(r"<table>.*?</table>", document, re.S).group(0)
        self.assertIn("<th>渠道</th>", table)
        self.assertIn("<td>sell-out</td>", table)

    def test_an_image_is_inlined_at_the_width_the_page_gave_it(self):
        # Two things have to be true at once: the bytes are inside the file (a link would be
        # fetched by Word with the reader's session — which is a hole when the file is
        # mailed on), and 560px must still be 560px rather than full-bleed.
        document = _word(BODY_HTML, images={"/api/storage/serve?path=knowledge-assets/a.png": PNG_8X8})
        self.assertIn("data:image/png;base64," + base64.b64encode(PNG_8X8).decode(), document)
        self.assertIn("width:420pt", document)
        self.assertNotIn("src=\"/api/storage/serve", document)

    def test_a_percentage_width_is_left_for_word_to_resolve(self):
        document = _word('<p><img src="/a.png" alt="" style="width:50%"></p>',
                         images={"/a.png": PNG_8X8})
        self.assertIn("width:50%", document)

    def test_an_image_that_cannot_be_fetched_leaves_a_marker_not_a_hole(self):
        # Offline, revoked, or simply gone: the reader still gets the page, and something
        # says a picture was meant to be here.
        document = _word(BODY_HTML, images={})
        self.assertNotIn("data:image", document)
        self.assertIn("[image: 图1]", document)

    def test_a_link_stays_a_link(self):
        self.assertIn('<a href="https://example.com">链接</a>', _word(BODY_HTML))

    def test_nothing_from_the_page_can_execute_in_the_file(self):
        document = _word('<p onclick="x()">ok</p><script>alert(1)</script>')
        self.assertNotIn("<script", document)
        self.assertNotIn("onclick", document)


# ── the endpoints ────────────────────────────────────────────────────────────

class ExportEndpointTests(unittest.TestCase):
    def test_word_returns_a_word_openable_document_with_the_body_in_it(self):
        with patch.object(store, "get_item", return_value=_item()), \
             patch.object(ke, "fetch_bytes", new=AsyncMock(return_value=None)):
            response = _client().post("/api/knowledge/items/channel-notes/export/word",
                                      json={"html": BODY_HTML, "title": "渠道笔记", "meta": "渠道"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], ke.WORD_MEDIA_TYPE)
        self.assertIn("channel-notes.doc", response.headers["content-disposition"])
        text = response.content.decode("utf-8")
        self.assertTrue(text.lstrip().startswith("<html"))
        self.assertIn("w:WordDocument", text)
        self.assertIn("<h2>渠道口径</h2>", text)
        self.assertIn("渠道笔记", text)

    def test_pdf_and_picture_go_through_the_renderer(self):
        rendered = AsyncMock(return_value=(b"%PDF-1.7 ok", "application/pdf", "pdf"))
        with patch.object(store, "get_item", return_value=_item()), \
             patch.object(ke, "render_visual", new=rendered):
            response = _client().post("/api/knowledge/items/channel-notes/export/pdf",
                                      json={"html": "<p>x</p>"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.7 ok")
        # The renderer is given this caller's credentials, so an export can never read a
        # file the requester could not open themselves.
        self.assertIn("auth_headers", rendered.call_args.kwargs)

    def test_a_page_the_caller_cannot_read_is_404(self):
        with patch.object(store, "get_item", return_value=None):
            response = _client().post("/api/knowledge/items/secret/export/word", json={})
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_kind_is_refused_not_guessed(self):
        with patch.object(store, "get_item", return_value=_item()):
            response = _client().post("/api/knowledge/items/channel-notes/export/pptx",
                                      json={"html": "<p>x</p>"})
        self.assertEqual(response.status_code, 400)

    def test_markdown_downloads_the_source_not_a_render(self):
        with patch.object(store, "get_item", return_value=_item(body="# 渠道笔记\n\n正文")):
            response = _client().get("/api/knowledge/items/channel-notes/markdown")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertEqual(response.content.decode(), "# 渠道笔记\n\n正文")


class AssetUploadTests(unittest.TestCase):
    def test_a_png_is_stored_and_comes_back_as_a_relative_url(self):
        stored = {}
        with patch("services.oss_storage.put_object",
                   side_effect=lambda bucket, key, data, **kw: stored.update(
                       bucket=bucket, key=key, data=data, content_type=kw.get("content_type"))):
            response = _client().post("/api/knowledge/assets", json={
                "filename": "渠道图.png",
                "content_base64": base64.b64encode(PNG_8X8).decode(),
            })
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["url"].startswith("/api/storage/serve?path=knowledge-assets%2F"))
        self.assertEqual(body["content_type"], "image/png")
        self.assertIn("![", body["markdown"])
        self.assertEqual(stored["bucket"], "knowledge-assets")
        self.assertEqual(stored["data"], PNG_8X8)
        # A relative URL is deliberate: the browser resolves it against the mount point and
        # the exporter resolves it against the same one.
        self.assertNotIn("http", body["url"])

    def test_a_data_uri_is_accepted(self):
        with patch("services.oss_storage.put_object"):
            response = _client().post("/api/knowledge/assets", json={
                "content_base64": "data:image/png;base64," + base64.b64encode(PNG_8X8).decode()})
        self.assertEqual(response.status_code, 201)

    def test_the_type_is_sniffed_from_the_bytes_not_the_name(self):
        with patch("services.oss_storage.put_object") as put:
            response = _client().post("/api/knowledge/assets", json={
                "filename": "innocent.png",
                "content_base64": base64.b64encode(b"MZ\x90\x00 not an image").decode()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("PNG", response.json()["detail"])
        put.assert_not_called()

    def test_garbage_and_oversize_and_empty_are_refused(self):
        cases = [
            ({"content_base64": "!!!not base64!!!"}, 400),
            ({"content_base64": ""}, 400),
            ({"content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" *
                                                 (router_module.MAX_ASSET_BYTES + 1)).decode()}, 413),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                with patch("services.oss_storage.put_object"):
                    self.assertEqual(
                        _client().post("/api/knowledge/assets", json=payload).status_code, expected)


class SniffTests(unittest.TestCase):
    def test_known_signatures(self):
        self.assertEqual(router_module._sniff_image(b"\x89PNG\r\n\x1a\n....")[0], ".png")
        self.assertEqual(router_module._sniff_image(b"\xff\xd8\xff\xe0....")[0], ".jpg")
        self.assertEqual(router_module._sniff_image(b"GIF89a....")[0], ".gif")
        self.assertEqual(router_module._sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")[0], ".webp")
        self.assertIsNone(router_module._sniff_image(b"<html>"))


if __name__ == "__main__":
    unittest.main()