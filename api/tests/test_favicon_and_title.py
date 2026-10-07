"""The tab icon and the document title: the two things a browser shows a person
before they have clicked anything, and the two things nothing else in this
repository tests.

Why this file exists at all — the change that motivated it was invisible to
every guard that was already here:

  the icon was `logo.png`, a 1219×257 lockup — the cloud plus the wordmark.
  A browser draws that in a 16–32px square tab, which means scaling the cloud
  down to a quarter of its natural size and then using most of what is left for
  letters nobody can read at that size. The result was reported from outside as
  "the tab icon is squashed". Swapping in a crop of the cloud alone fixes it.

  Fixing it touched two lines in two HTML files and produced no failure any
  existing test could see, for three separate reasons, each checked below:

  ①  Nothing asserts the icon is *reachable*. A favicon is fetched by the
     browser outside the application's own request paths, so a wrong `href`, a
     renamed file or a base-path mismatch shows up as a blank tab icon and
     nothing else — no console error the SPA trips over, no failing route, no
     failing endpoint. Hence `test_the_icon_is_served...`, over real HTTP.

  ②  Nothing asserts the icon is *shaped right*. A `logo.png` → `logo-mark.png`
     edit that silently pointed back at the lockup would pass every "is it
     there" test in existence while re-creating the exact bug being fixed.
     So the teeth are on the decoded PNG header, not on the file name —
     `logo-mark.png` that was actually a 1219×257 image fails.

  ③  The two pages could disagree, and did. `index.html` and `welcome.html` load
     the same `app.css` but had their `?v=` cache-busters resolved separately
     (`avatar1` on one side, `office9` on the other), so the same bytes were
     served under two keys depending on which page you opened. That is the same
     warm-cache failure the CSS comment above the link describes, split across
     two entry points instead of one, and it was only caught by hand.

⚠️ On the assertions that look negative. "This page does not declare the
lockup" and "neither page still points at the old icon" are the kind of
statement that stays green when the *query* stops matching anything at all, so
every negative claim here is paired with a positive control in the same file:

  · the icon extractor is run against a synthetic snippet first, so a regex
    that silently matches nothing cannot masquerade as "no bad icons";
  · the square-ness measurement is applied to the lockup and is *required* to
    report non-square, so the measurement is known to discriminate before it is
    trusted to say the mark is square;
  · the "both pages agree" assertion is preceded by an assertion that the
    extraction returned a non-empty answer from each page.

Dimensions come from the PNG IHDR chunk, 24 bytes in, with `struct`. Deliberately
not PIL: `ci_check.py` runs in a container whose only guarantee is the
dependencies the application itself needs, and an image library is not one of
them.
"""
import os
import re
import struct
import unittest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(API_DIR)
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend", "out")

# The two entry points a person can arrive at. A document served at `/` is
# rewritten to one of these, so these are exactly the files that can carry a
# wrong icon — and they must not drift apart again.
PAGES = ("index.html", "welcome.html")

LOCKUP = "logo.png"          # 1219×257, the cloud plus the wordmark
MARK = "logo-mark.png"       # the cloud alone, on a square transparent canvas

ICON_LINK = re.compile(r"""<link\b[^>]*\brel=["']icon["'][^>]*>""", re.I)
APPLE_LINK = re.compile(r"""<link\b[^>]*\brel=["']apple-touch-icon["'][^>]*>""", re.I)
HREF = re.compile(r"""\bhref=["']([^"']+)["']""", re.I)
TITLE = re.compile(r"<title>(.*?)</title>", re.I | re.S)


def _png_size(path):
    """(width, height) from a PNG's IHDR, or None if it is not a PNG at all."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _is_square(path, tolerance=2):
    size = _png_size(path)
    if size is None:
        return False
    return abs(size[0] - size[1]) <= tolerance


def _declared_targets(html, pattern):
    """Every href a `<link rel=...>` of the given kind points at, query string
    stripped so `?v=` cache-busters do not read as different files."""
    out = []
    for tag in pattern.findall(html):
        href = HREF.search(tag)
        if href:
            out.append(href.group(1).split("?", 1)[0])
    return out


def _read(page):
    with open(os.path.join(FRONTEND_DIR, page), encoding="utf-8") as fh:
        return fh.read()


class TheExtractorWorksTests(unittest.TestCase):
    """Positive control. Run before any assertion that leans on `_declared_targets`.

    Without this, "this page declares no bad icon" is equally satisfied by a
    page that declares no icon at all, a page whose link tag moved, and a
    regex that quietly stopped matching — three states in which the negative
    assertions below report nothing while checking nothing.
    """

    SNIPPET = (
        '<link rel="icon" type="image/png" href="logo-mark.png?v=20261006-mark">'
        '<link rel="apple-touch-icon" href="logo-mark.png?v=20261006-mark">'
        '<link rel="stylesheet" href="app.css?v=x">'
    )

    def test_it_finds_the_icon_and_ignores_the_stylesheet(self):
        self.assertEqual(_declared_targets(self.SNIPPET, ICON_LINK), ["logo-mark.png"])
        self.assertEqual(_declared_targets(self.SNIPPET, APPLE_LINK), ["logo-mark.png"])

    def test_it_strips_the_cache_buster_so_one_file_is_not_two_targets(self):
        html = '<link rel="icon" href="a.png?v=1"><link rel="icon" href="b.png?v=2">'
        self.assertEqual(_declared_targets(html, ICON_LINK), ["a.png", "b.png"])

    def test_every_page_really_does_declare_an_icon(self):
        """The control, on the real files. If this fails, the negatives below
        are void and the failure has to be read as 'no assertion ran'."""
        for page in PAGES:
            with self.subTest(page=page):
                self.assertTrue(
                    _declared_targets(_read(page), ICON_LINK),
                    "%s 没有声明 <link rel=icon>" % page)


class TheIconIsShapedLikeAnIconTests(unittest.TestCase):
    """The teeth: shape, decoded from the bytes, with the shape reader itself
    pinned against the image this change was made to stop using."""

    def test_the_measurement_distinguishes_the_lockup_from_the_mark(self):
        """Control on `_is_square`. Required to report FALSE for the lockup.

        If the lockup ever became square the measurement would stop being able
        to tell the two files apart, and the assertion below would pass on
        either one — a guard that cannot fail is not a guard.
        """
        lockup = os.path.join(FRONTEND_DIR, LOCKUP)
        self.assertIsNotNone(_png_size(lockup), "%s 读不出尺寸" % LOCKUP)
        self.assertFalse(_is_square(lockup),
                         "%s 不再是非方形 —— 下面的断言已经无法分辨两个文件了" % LOCKUP)

    def test_the_mark_is_square(self):
        """The regression. A 5:1 wordmark scaled into a tab square is the bug."""
        mark = os.path.join(FRONTEND_DIR, MARK)
        self.assertIsNotNone(_png_size(mark), "%s 读不出尺寸" % MARK)
        self.assertTrue(_is_square(mark),
                        "%s 不是方形：%r" % (MARK, _png_size(mark)))

    def test_the_mark_is_big_enough_to_survive_being_scaled_down(self):
        """Square is necessary, not sufficient — a 16px source scaled up is blur.

        The desktop case is the binding one: browsers request 32px and larger
        for the mark they draw in a tab strip, and some request 512px for an
        install shortcut.
        """
        mark = os.path.join(FRONTEND_DIR, MARK)
        self.assertGreaterEqual(_png_size(mark)[0], 180)

    def test_both_themes_have_a_mark(self):
        """The mark is cropped from a lockup, and there are two lockups.

        A dark-theme page that declares the light mark does not error and does
        not look obviously broken; it draws a cloud that is missing its own
        outline against the browser's own chrome.
        """
        for name in (MARK, "logo-mark-dark.png"):
            with self.subTest(mark=name):
                path = os.path.join(FRONTEND_DIR, name)
                self.assertTrue(os.path.exists(path), "缺少 %s" % name)
                self.assertTrue(_is_square(path), "%s 不是方形" % name)


class TheIconIsActuallyServedTests(unittest.TestCase):
    """Over real HTTP, because that is the only thing a browser knows."""

    def test_the_declared_icon_is_what_the_application_serves(self):
        """Same bytes as on disk — which also proves the mount resolves the same
        directory the pages were read from, instead of a `FRONTEND_DIR` override
        pointing somewhere else."""
        from fastapi.testclient import TestClient
        import main

        for page in PAGES:
            targets = _declared_targets(_read(page), ICON_LINK)
            self.assertTrue(targets, "%s 没有声明 icon" % page)
            for target in targets:
                with self.subTest(page=page, icon=target):
                    r = TestClient(main.app).get("/" + target)
                    self.assertEqual(r.status_code, 200,
                                     "%s 声明的 %s 没有被提供（%s）"
                                     % (page, target, r.status_code))
                    with open(os.path.join(FRONTEND_DIR, target), "rb") as fh:
                        on_disk = fh.read()
                    self.assertEqual(r.content, on_disk,
                                     "%s 拿到的字节与磁盘上的不一致" % target)


class TheTwoPagesAgreeTests(unittest.TestCase):
    def test_both_pages_declare_the_same_icon_and_apple_touch_icon(self):
        """One icon, declared once. `welcome.html` reached this by drifting."""
        icons = {p: _declared_targets(_read(p), ICON_LINK) for p in PAGES}
        for page, targets in icons.items():
            self.assertTrue(targets, "%s 没有声明 icon" % page)
        self.assertEqual(len({tuple(v) for v in icons.values()}), 1,
                         "两个页面声明的 icon 不一致：%r" % icons)

        apples = {p: _declared_targets(_read(p), APPLE_LINK) for p in PAGES}
        for page, targets in apples.items():
            self.assertTrue(targets, "%s 没有声明 apple-touch-icon" % page)
        self.assertEqual(len({tuple(v) for v in apples.values()}), 1,
                         "两个页面声明的 apple-touch-icon 不一致：%r" % apples)

    def test_no_page_still_points_at_the_lockup_as_its_icon(self):
        """The negative claim, now that it is known the query matches.

        Declared as a list rather than a `not in` so a failure names the file
        instead of rendering as "True is not false".
        """
        offenders = {p: [t for t in _declared_targets(_read(p), ICON_LINK)
                         if t == LOCKUP]
                     for p in PAGES}
        for page, found in offenders.items():
            with self.subTest(page=page):
                self.assertEqual(found, [],
                                 "%s 仍然把横版字标当 icon，会在标签页里被压扁" % page)

    def test_the_css_cache_busters_agree_across_both_pages(self):
        """Not a favicon assertion, but it is the same shape of bug, found here.

        Two pages loading the same stylesheet under different `?v=` keys serve
        the same bytes to whoever happens to have one of them cached — the
        failure the CSS comment above the `<link>` is written about, which is
        why it is worth a machine check rather than a comment.
        """
        keys = {}
        for page in PAGES:
            html = _read(page)
            for sheet in ("app.css", "theme.css"):
                with self.subTest(page=page, sheet=sheet):
                    found = re.findall(r"""href=["'](%s\?v=[^"']+)["']"""
                                       % re.escape(sheet), html)
                    self.assertTrue(found, "%s 没有引用 %s" % (page, sheet))
                    keys.setdefault(sheet, set()).update(found)
        for sheet, seen in keys.items():
            with self.subTest(sheet=sheet):
                self.assertEqual(len(seen), 1,
                                 "%s 在两个页面里用了不同的缓存 key：%r"
                                 % (sheet, sorted(seen)))


class TheTitleTests(unittest.TestCase):
    EXPECTED_APP = "Klado · your agent office"

    def test_the_application_page_names_what_the_application_is(self):
        """A blank tab is indistinguishable from a broken tab in a tab strip of
        twenty, and the previous title described a department rather than the
        product."""
        found = TITLE.search(_read("index.html"))
        self.assertIsNotNone(found, "index.html 没有 <title>")
        self.assertEqual(found.group(1).strip(), self.EXPECTED_APP)

    def test_the_landing_page_keeps_its_own_title(self):
        """Not `the same title` — the landing page explains itself in both
        languages and its title has to keep doing that."""
        found = TITLE.search(_read("welcome.html"))
        self.assertIsNotNone(found, "welcome.html 没有 <title>")
        self.assertTrue(found.group(1).strip())


if __name__ == "__main__":
    unittest.main()