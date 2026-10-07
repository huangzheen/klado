"""The calendar's event palette — now PER THEME, and read out of `theme.css`.

Two complaints shaped this file, and both were measurable before they were fixed.

**1. "这套色系看起来不令人愉悦" (2026-10-06).** The closest pair of fills sat **ΔE 0.016**
apart in OKLab — `meeting` and `research` were the same paint, and four types read as one
grey-blue. Every other check in the suite passed at the time: all twelve were individually
valid, all twelve had correct contrast, and the set still did not work. So this file leads
with the property that was missing — **a categorical palette is judged by how far apart its
two CLOSEST members are**, not by how pleasant each one is alone.

**2. "不同甘特图底色上文本颜色一样也不舒服" (2026-10-06, later the same day).** The obvious
response — re-pick twelve prettier fills — is the one that had already been tried and had
produced the complaint above. Measuring it explains why it fails *forever*: the palette was
solved for a fill at ~3:1 on the page, and a mid-tone fill **physically forces its ink into
the near-black band** (ink at 4.5:1 against a mid-tone fill has nowhere colourful to go).
All twelve inks had landed inside L 0.219–0.308 with a median chroma of **0.018** — twelve
greys — and a pairwise ΔE of 0.025. A constant contrast target is what does it: hold every
ratio equal and the inks collapse onto one lightness.

So the fix had to be structural, and these are the invariants that keep it:

* **the palette exists once per theme.** One triple cannot serve both surfaces — the
  mid-tone one measured 2.6:1 on the light card (loud) and 6.4:1 on the dark one (calm).
  Light theme is a light tint + saturated mid-dark ink; dark theme is a dark tint +
  saturated light ink. Both are checked.
* **the fill must stay a TINT.** This is the assertion that catches a regression back to the
  loud set, and it is why the fill ΔE floor moved DOWN while the ink floor did not: quiet
  tints are supposed to resemble one another, and a type's identity is now carried by the
  text, which is the element that has to be read.
* **the ink must stay colourful** — median OKLab chroma, and the closest pair of inks.
* **the hues are held fixed across themes.** A type must not change colour when the reader
  flips the theme; only L and C are re-solved.
* `other` carries **no hue** — the neutral that says "not one of the eleven", held out of
  the distance arithmetic and checked separately.
* the colours live in CSS, not in a JS table, and every kind has a matching rule.

Nothing here renders a browser: the palette is a property of the stylesheet, and asserting
it in CI means a colour change cannot pass the browser suites by never rendering the kind
that broke. `verify_calendar_ui.py` carries the matching check against what the page
actually paints, which is what proves the wiring rather than the intention.

    .venv312/bin/python -m unittest test_calendar_palette
"""
import math
import re
import unittest
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "frontend" / "out"
THEME = OUT / "theme.css"
INDEX = OUT / "index.html"
APP_CSS = OUT / "app.css"

KINDS = ["meeting", "review", "launch", "promotion", "campaign", "media",
         "content", "offline", "training", "research", "planning", "other"]
SLOTS = ("fill", "ink", "edge")

# ⚠️ The check that protects the complaint that was actually made. The near-black inks scored
# 0.025 here — barely above the 0.016 that had been rejected one round earlier. It is
# asserted on the INKS now, because with a tint fill the text is what carries the type.
MIN_INK_PAIRWISE_DE = 0.035
# ⚠️ Deliberately much lower than the ink floor, and this is a change of requirement rather
# than a guard loosened to pass. Light tints are SUPPOSED to be similar — that is what
# "quiet" means. The old floor of 0.048 only held because the fills were saturated blocks,
# which is exactly what the reader rejected.
MIN_FILL_PAIRWISE_DE = 0.015
# ⚠️ The other half of the same trade. A fill above this stops being a tint and starts being
# the "wall of colour" the first complaint was about.
MAX_FILL_ON_PAGE = {"light": 1.8, "dark": 2.4}
# ⚠️ The set that measured 0.018. Twelve greys are not twelve types.
MIN_MEDIAN_INK_CHROMA = 0.09


def _srgb_to_lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    r, g, b = (_srgb_to_lin(c / 255) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _oklab(rgb):
    r, g, b = (_srgb_to_lin(c / 255) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _oklch(rgb):
    L, a, b = _oklab(rgb)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def _chroma(rgb):
    return _oklch(rgb)[1]


def _delta_e(hex_a, hex_b):
    return math.dist(_oklab(_hex_to_rgb(hex_a)), _oklab(_hex_to_rgb(hex_b)))


def _top_level_blocks(css):
    """[(selector, body)] for every top-level `{ ... }`, brace-matched.

    Splitting on a selector regex is not enough here: `theme.css` has several `:root`
    blocks, and the palette tokens are only in two of them."""
    out, i, n = [], 0, len(css)
    while i < n:
        open_at = css.find("{", i)
        if open_at < 0:
            break
        selector = css[i:open_at].strip().splitlines()[-1].strip()
        depth, j = 0, open_at
        while j < n:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append((selector, css[open_at + 1:j]))
        i = j + 1
    return out


def read_theme(theme_name):
    """The palette for one theme: {kind: {fill, ink, edge}} plus its `--surface`."""
    css = THEME.read_text()
    want_dark = theme_name == "dark"
    found = None
    for selector, body in _top_level_blocks(css):
        is_dark = 'data-theme="dark"' in selector
        if is_dark != want_dark or "--cal-" not in body:
            continue
        found = (selector, body)
        break
    assert found, ("no %s block with --cal-* tokens in theme.css — the palette must be "
                   "defined once per theme, or a theme silently keeps the other's colours"
                   % theme_name)
    body = found[1]
    out = {}
    for kind in KINDS:
        entry = {}
        for slot in SLOTS:
            m = re.search(r"--cal-%s-%s:\s*(#[0-9a-fA-F]{6})\s*;" % (kind, slot), body)
            assert m, "%s: --cal-%s-%s is missing from the %s block" % (kind, slot, theme_name)
            entry[slot] = m.group(1)
        out[kind] = entry
    m = re.search(r"--surface:\s*(#[0-9a-fA-F]{6})\s*;", body)
    assert m, "%s: --surface not found; the bar's page must be read, not guessed" % theme_name
    return out, m.group(1)


class PaletteShapeTests(unittest.TestCase):
    """Per theme: three distinct values, in an order, with the two ratios that make a bar
    readable. Runs for BOTH themes, because "it works in light" is half an answer."""

    def setUp(self):
        # ⚠️ palettes and surfaces in SEPARATE dicts: keeping them in one meant every
        # `for name, pal in self.themes.items()` loop also iterated the surface strings, and
        # `sorted("#ffffff")` is a list of characters.
        self.pal, self.page = {}, {}
        for name in ("light", "dark"):
            self.pal[name], self.page[name] = read_theme(name)

    def test_every_kind_has_three_colours_in_both_themes(self):
        for name, pal in self.pal.items():
            self.assertEqual(sorted(pal), sorted(KINDS),
                             "%s palette and this file disagree about the event types" % name)

    def test_the_three_values_are_distinct_per_kind(self):
        for name, pal in self.pal.items():
            for kind, p in pal.items():
                self.assertEqual(len({p[s] for s in SLOTS}), 3, "%s/%s" % (name, kind))

    def test_ink_sits_on_its_fill_in_both_themes(self):
        # 11px bold needs 4.5:1. This is the one ratio the whole two-theme structure exists
        # to keep, and it is checked on the values the browser will actually use.
        for name, pal in self.pal.items():
            for kind, p in pal.items():
                ratio = _contrast(_hex_to_rgb(p["ink"]), _hex_to_rgb(p["fill"]))
                self.assertGreaterEqual(ratio, 4.5,
                                        "%s/%s: ink on fill is %.2f:1" % (name, kind, ratio))

    def test_edge_is_visible_on_the_page(self):
        # The 1px rule is what gives a light body the shape of a bar.
        for name, pal in self.pal.items():
            page = _hex_to_rgb(self.page[name])
            for kind, p in pal.items():
                ratio = _contrast(_hex_to_rgb(p["edge"]), page)
                self.assertGreaterEqual(ratio, 2.5,
                                        "%s/%s: edge on the page is %.2f:1" % (name, kind, ratio))

    def test_the_fill_stays_a_tint_and_not_a_block(self):
        # ⚠️ THE regression guard for "令人愉悦". The rejected set sat at 2.7-3.6:1 on the
        # page and read as a wall of colour; a tint sits quietly under its own label.
        for name, pal in self.pal.items():
            page = _hex_to_rgb(self.page[name])
            for kind, p in pal.items():
                ratio = _contrast(_hex_to_rgb(p["fill"]), page)
                self.assertLessEqual(
                    ratio, MAX_FILL_ON_PAGE[name],
                    "%s/%s: fill on the page is %.2f:1 — that is a block again, not a tint"
                    % (name, kind, ratio))

    def test_ink_carries_the_hue_it_is_supposed_to_carry(self):
        # ⚠️ The set that measured 0.018: twelve inks inside L 0.219-0.308, which is
        # "the text is the same colour on every bar" stated as a number.
        for name, pal in self.pal.items():
            real = [k for k in pal if k != "other"]
            chromas = sorted(_chroma(_hex_to_rgb(pal[k]["ink"])) for k in real)
            median = chromas[len(chromas) // 2]
            self.assertGreaterEqual(
                median, MIN_MEDIAN_INK_CHROMA,
                "%s: median ink chroma is %.4f — the inks are greys again" % (name, median))

    def test_other_is_the_neutral_that_says_not_one_of_the_eleven(self):
        for name, pal in self.pal.items():
            self.assertLess(_chroma(_hex_to_rgb(pal["other"]["fill"])), 0.10,
                            "%s: other's fill is chromatic; it should be neutral" % name)


class PaletteSeparationTests(unittest.TestCase):
    """THE metric, on the element that carries the type."""

    def setUp(self):
        self.pal = {n: read_theme(n)[0] for n in ("light", "dark")}

    def _worst(self, slot, pal):
        real = sorted(k for k in pal if k != "other")
        worst, pair = math.inf, None
        for i, a in enumerate(real):
            for b in real[i + 1:]:
                d = _delta_e(pal[a][slot], pal[b][slot])
                if d < worst:
                    worst, pair = d, (a, b)
        return worst, pair

    def test_the_closest_pair_of_inks_is_far_enough_apart(self):
        for name, pal in self.pal.items():
            worst, pair = self._worst("ink", pal)
            # ⚠️ The pair is in the message on purpose: a bare number says "0.025" and the
            # next person has to go and find which two colours that was.
            self.assertGreaterEqual(
                worst, MIN_INK_PAIRWISE_DE,
                "%s: closest ink pair is %s/%s at ΔE %.4f (need %.3f) — that is "
                "'the text is the same colour on every bar' coming back"
                % (name, pair[0], pair[1], worst, MIN_INK_PAIRWISE_DE))

    def test_the_closest_pair_of_fills_is_still_distinguishable(self):
        for name, pal in self.pal.items():
            worst, pair = self._worst("fill", pal)
            self.assertGreaterEqual(
                worst, MIN_FILL_PAIRWISE_DE,
                "%s: closest fill pair is %s/%s at ΔE %.4f (need %.3f)"
                % (name, pair[0], pair[1], worst, MIN_FILL_PAIRWISE_DE))

    def test_other_is_distinguishable_from_every_real_type(self):
        for name, pal in self.pal.items():
            for kind, p in pal.items():
                if kind == "other":
                    continue
                d = _delta_e(pal["other"]["ink"], p["ink"])
                self.assertGreaterEqual(
                    d, MIN_INK_PAIRWISE_DE * 0.6,
                    "%s: other/%s inks are too close at ΔE %.4f" % (name, kind, d))


class WiringTests(unittest.TestCase):
    """The colours are in CSS now. These stop the JS from quietly re-inlining them, and
    stop a type from existing in the table with no rule to paint it."""

    def test_index_html_no_longer_names_a_calendar_colour(self):
        src = INDEX.read_text()
        block = re.search(r"const KIND_META = \{(.*?)\n  \};", src, re.S)
        assert block, "KIND_META not found in index.html"
        self.assertNotRegex(block.group(1), r"(fill|ink|edge)\s*:\s*'#",
                            "KIND_META is carrying hexes again — one triple cannot serve "
                            "both themes, which is the bug this palette was rebuilt for")

    def test_every_kind_has_a_rule_that_paints_its_bar(self):
        css = APP_CSS.read_text()
        for kind in KINDS:
            rule = re.search(r"\.cal-bar\.k-%s\s*\{(.*?)\n\}" % kind, css, re.S)
            self.assertIsNotNone(rule, "app.css has no .cal-bar.k-%s rule" % kind)
            for slot in SLOTS:
                self.assertIn("--cal-%s-%s" % (kind, slot), rule.group(1),
                              ".cal-bar.k-%s does not resolve %s" % (kind, slot))

    def test_a_type_does_not_change_colour_when_the_theme_flips(self):
        light, dark = (read_theme(n)[0] for n in ("light", "dark"))
        for kind in KINDS:
            for slot in SLOTS:
                lh = _oklch(_hex_to_rgb(light[kind][slot]))[2]
                dh = _oklch(_hex_to_rgb(dark[kind][slot]))[2]
                # hue wraps at 360; `other` is neutral and its hue is meaningless
                if kind == "other":
                    continue
                diff = abs(lh - dh)
                diff = min(diff, 360 - diff)
                self.assertLess(diff, 4.0,
                                "%s/%s hue moves %.1f° between themes; a type must not "
                                "change colour when the reader flips the theme"
                                % (kind, slot, diff))

    def test_light_theme_ink_is_darker_than_its_fill_and_dark_theme_is_not(self):
        # The structural reason two themes are needed, asserted so the structure stays.
        light, dark = (read_theme(n)[0] for n in ("light", "dark"))
        for kind in KINDS:
            self.assertLess(_lum(_hex_to_rgb(light[kind]["ink"])),
                            _lum(_hex_to_rgb(light[kind]["fill"])), "light/" + kind)
            self.assertGreater(_lum(_hex_to_rgb(dark[kind]["ink"])),
                               _lum(_hex_to_rgb(dark[kind]["fill"])), "dark/" + kind)


if __name__ == "__main__":
    unittest.main()
