"""Build a SUBSET of the Tabler Icons webfont, for the Workspace edit prototype only.

    .venv312/bin/python scripts/build_tabler_subset.py

Why a subset and not the shipped font
-------------------------------------
`@tabler/icons-webfont@3.48.0` ships every icon it has: `tabler-icons.woff2` is
492 KB for ~5900 glyphs, and `tabler-icons.min.css` is 207 KB of `:before`
rules. Klado would be paying 700 KB to draw twenty glyphs — and this repo
already has a lesson written down about exactly this shape of problem
(AGENTS.md, the inlined Remix block: 367 KB of CSS inlined into a single file,
which is why `ri-translate-2-line` could exist in the catalogue and not in the
page). A subset is the difference between vendoring a library and vendoring a
dependency.

⚠️ The scope is deliberately small. This font is used by the EDIT MODE ONLY —
the 3,080 `ri-*` classes in the app are untouched, and this font is not a
replacement for them. `assert` below fails the build if `ICONS` ever grows past
what the prototype actually draws, so the file cannot quietly become a
half-migration.

No build step is introduced: the output is a static CSS + woff2 dropped into
`frontend/out/vendor/tabler-icons/`, loaded by one <link> with a relative path,
exactly like the fonts already in `frontend/out/vendor/fonts/`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = Path("/tmp/tabler/package/dist")          # unpacked @tabler/icons-webfont
OUT = ROOT / "frontend" / "out" / "vendor" / "tabler-icons"

# Every glyph the edit prototype draws, and nothing else. Names are Tabler's own
# (`icons/outline/<name>.svg` in the upstream repo) so a reader can look one up.
ICONS = [
    # toolbar / chrome
    "pencil", "x", "eye", "settings", "dots", "plus", "chevron-down", "chevron-right",
    # ribbon — clipboard
    "clipboard-copy", "scissors", "brush",
    # ribbon — slide
    "layout", "typography", "photo", "table", "chart-bar", "shape",
    # ribbon — font
    "bold", "italic", "notes",
    # ribbon — history / file
    "arrow-back-up", "arrow-forward-up", "device-floppy", "trash", "download",
    # outline panel
    "list", "zoom-in", "zoom-out",
]

#: A subset this size is a prototype, not a migration. Crossing it means someone
#: started replacing `ri-*` with `ti-*`, which is a different piece of work with
#: its own review.
MAX_ICONS = 40

_RULE = re.compile(r'\.ti-([a-z0-9-]+):before\{content:"\\([0-9a-fA-F]+)"\}')


def codepoints(css: str) -> dict[str, int]:
    return {name: int(hexed, 16) for name, hexed in _RULE.findall(css)}


def main() -> int:
    if not (DIST / "fonts" / "tabler-icons.ttf").exists():
        print("unpack @tabler/icons-webfont first:\n"
              "  mkdir -p /tmp/tabler && cd /tmp/tabler\n"
              "  curl -sL -o pkg.tgz "
              "https://registry.npmjs.org/@tabler/icons-webfont/-/icons-webfont-3.48.0.tgz\n"
              "  tar xzf pkg.tgz", file=sys.stderr)
        return 2
    if len(ICONS) > MAX_ICONS:
        print("ICONS has %d entries, over the %d cap — is this becoming a migration?"
              % (len(ICONS), MAX_ICONS), file=sys.stderr)
        return 1

    all_codes = codepoints((DIST / "tabler-icons.min.css").read_text(encoding="utf-8"))
    missing = [n for n in ICONS if n not in all_codes]
    if missing:
        print("not in Tabler 3.48.0:", missing, file=sys.stderr)
        return 1
    wanted = {all_codes[n] for n in ICONS}

    from fontTools import subset
    from fontTools.ttLib import TTFont

    font = TTFont(str(DIST / "fonts" / "tabler-icons.ttf"))
    # `.notdef` and the space glyph are kept implicitly by subsetter; the point is
    # that every codepoint above is in the `unicodes` set and nothing else is.
    subsetter = subset.Subsetter()
    subsetter.populate(unicodes=wanted)
    subsetter.subset(font)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    font.flavor = "woff2"
    font.save(str(OUT / "tabler-subset.woff2"))
    font.flavor = None
    font.save(str(OUT / "tabler-subset.ttf"))     # kept for `file` / inspection
    shutil.copy(DIST.parent / "LICENSE", OUT / "LICENSE-Tabler-Icons.txt")

    # ⚠️ The CSS is written from `all_codes` while the font is subset from `wanted`.
    # Those are two different derivations of the same idea, so they can drift — and
    # when they do, every icon still renders *something* (a fallback font's tofu),
    # which is why nothing but this comparison catches it. Read the cmap back off
    # the file that actually shipped.
    from fontTools.ttLib import TTFont as _TTFont
    shipped = _TTFont(str(OUT / "tabler-subset.woff2"))
    cmap = set()
    for table in shipped["cmap"].tables:
        cmap |= set(table.cmap.keys())
    missing = sorted(c for c in wanted if c not in cmap)
    if missing:
        raise SystemExit("CSS declares codepoints the shipped font does not have: %s"
                         % ", ".join("U+%04X" % c for c in missing))

    rules = "\n".join('  .ti-%s::before { content: "\\%04x"; }' % (n, all_codes[n])
                      for n in sorted(ICONS))
    (OUT / "tabler-subset.css").write_text(
        "/*! Tabler Icons 3.48.0 (subset) — MIT, https://github.com/tabler/tabler-icons\n"
        " *  GENERATED by scripts/build_tabler_subset.py — do not edit by hand.\n"
        " *  %d glyphs, for the Workspace edit prototype. NOT a replacement for the\n"
        " *  app's `ri-*` (Remix Icon) set, which is untouched.\n"
        " */\n"
        "@font-face {\n"
        '  font-family: "tabler-subset";\n'
        "  font-style: normal;\n"
        "  font-weight: 400;\n"
        # ⚠️ ONE `src` declaration, both sources comma-separated. Split across two
        # lines — `src: url(…woff2)…;` then a bare `url(…ttf)…;` — the second line
        # is not a declaration at all, so the parser discards it and the TTF
        # fallback silently stops existing.
        #
        # Precise about what that costs, because the obvious guess is wrong and was
        # measured here: the woff2 STILL loads (the parser recovers at the first
        # `;`), the icons still render, and every check that looks at `::before` —
        # family, content, glyph width — reports green. What is actually gone is the
        # fallback for a browser without woff2. So this is tidiness with a real but
        # narrow cost, NOT the catastrophe the first draft of this comment claimed,
        # and a mutation test that "proves" otherwise is measuring its own regex.
        '  src: url("./tabler-subset.woff2") format("woff2"),\n'
        '       url("./tabler-subset.ttf") format("truetype");\n'
        "  font-display: block;   /* same reason remixicon is: a wrong glyph that\n"
        "                          * renders instantly is worse than a short blank */\n"
        "}\n"
        ".ti {\n"
        '  font-family: "tabler-subset" !important;\n'
        "  speak: none;\n"
        "  font-style: normal;\n"
        "  font-weight: normal;\n"
        "  font-variant: normal;\n"
        "  text-transform: none;\n"
        "  line-height: 1;\n"
        "  -webkit-font-smoothing: antialiased;\n"
        "  -moz-osx-font-smoothing: grayscale;\n"
        "  display: inline-block;\n"
        "  vertical-align: -0.125em;\n"
        "}\n"
        "%s\n" % (len(ICONS), rules), encoding="utf-8")

    for f in ("tabler-subset.woff2", "tabler-subset.ttf", "tabler-subset.css"):
        print("  %-22s %8d bytes" % (f, (OUT / f).stat().st_size))
    # Verify the subset really is small — a subsetter that silently kept the whole
    # font would still pass every "does it render" check above.
    check = TTFont(str(OUT / "tabler-subset.ttf"))
    n = check["maxp"].numGlyphs
    print("  glyphs in subset: %d (upstream %d)" % (n, TTFont(str(DIST / "fonts" / "tabler-icons.ttf"))["maxp"].numGlyphs))
    return 0 if n <= len(ICONS) + 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
