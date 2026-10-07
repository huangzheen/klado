"""Ship the supplied knowledge-base nav icon.

The artwork is an image **supplied by the user** (like ``workspace-icon.png`` and the
email banners), so this script does not draw anything — it only brings it to the
shipped format, which has to be identical every time:

* resample to 266x266, the same canvas ``workspace-icon.png`` and
  ``calendar-icon.png`` use, so all three render at 22x22 in the nav bar through the
  same ``.workspace-mark`` rule (``object-fit: contain``, so the whole canvas maps into
  the 22px box — nothing is cropped);
* **preserve the alpha channel**, and resample it in *premultiplied* space (see
  ``_resize_premultiplied`` — a plain ``Image.resize`` on RGBA darkens the soft edges);
* write the shipped frontend copy (``frontend/out``; the second copy under ``api/``
  existed only for the retired container build and is gone — the cloud
  build takes the latter).

⚠️ **The background is transparent — do not "restore" it.** History:

* 2026-09-29 (first round): the user supplied the artwork as an opaque RGB image (white
  background) and asked to keep the white box, because the nav bar's own background is
  ``var(--surface)`` = pure white. ``convert("RGB")`` here was correct *then* — with one
  known cosmetic wart: ``.nav-tab:hover`` paints ``rgba(0,0,0,.05)`` and
  ``.nav-tab.active`` paints ``rgba(37,99,235,.10)`` behind the icon, so the white square
  showed as a paler patch inside the tinted pill while the tab was hovered/selected.
* 2026-09-29 (second round, current): the user re-supplied the **same artwork** with a
  true transparent background and asked for it to replace the shipped icon. Verified
  against the old file before swapping: the pixels the new file reports as fully
  transparent are exactly the old file's white (mean ``rgb(249,246,245)``) and the pixels
  it reports as opaque are the old file's paint (mean ``rgb(219,202,197)``) — i.e. same
  art, same scale, background now genuinely absent. So a straight proportional resize
  keeps the on-screen footprint identical and only removes the white box, which is what
  fixes the hover/active wart above.

⚠️ Because of that history, this script **refuses to ship an opaque source**: if the file
has no alpha channel, or every pixel is opaque, it exits instead of quietly baking a back
into the nav again. If an opaque background is ever genuinely wanted, pass
``--allow-opaque`` and say why in the commit message.

Source of the shipped icon, not kept in the repo (same precedent as the email banners):

    ChatGPT share link: https://chatgpt.com/s/m_6abc04418ba48191a7a6062de7329bb9
    Downloaded as 1254x1254 RGBA PNG (74.2% of the canvas fully transparent)

    python3 scripts/prepare_knowledge_icon.py --source /tmp/kb-new-icon.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageChops

SIZE = 266              # the shipped canvas, same as workspace-icon.png / calendar-icon.png

FRONTEND = "frontend/out"      # the only frontend copy there is


def _unpremultiply(channel: Image.Image, alpha: Image.Image) -> Image.Image:
    out = Image.new("L", channel.size)
    src, a, dst = channel.load(), alpha.load(), out.load()
    for y in range(channel.size[1]):
        for x in range(channel.size[0]):
            av = a[x, y]
            dst[x, y] = src[x, y] * 255 // av if av else 0
    return out


def _resize_premultiplied(image: Image.Image, size: int) -> Image.Image:
    """LANCZOS downscale that does not darken the soft edges.

    ⚠️ ``Image.resize`` on an RGBA image averages the colour channels **without**
    premultiplying them, so the RGB of *fully transparent* pixels is pulled into the
    neighbouring semi-transparent ones. This artwork's transparent area is black
    ``(0,0,0,0)``, so a straight 1254 → 266 resize baked a dark rim along every soft
    edge — measured against a correctly premultiplied resize of the same source: 9.2% of
    the inked pixels came out darker by more than 24 levels, and at 22x22 that made the
    glyph read noticeably heavier than the artwork actually is (mean ink 27.4 vs the
    correct 17.8, where the old white-backed copy measured 17.8).

    So: multiply the colour channels by alpha, resize, then divide alpha back out.
    """
    red, green, blue, alpha = image.split()
    premultiplied = Image.merge("RGBA", (
        ImageChops.multiply(red, alpha), ImageChops.multiply(green, alpha),
        ImageChops.multiply(blue, alpha), alpha,
    )).resize((size, size), Image.LANCZOS)
    p_red, p_green, p_blue, p_alpha = premultiplied.split()
    return Image.merge("RGBA", (_unpremultiply(p_red, p_alpha),
                                _unpremultiply(p_green, p_alpha),
                                _unpremultiply(p_blue, p_alpha), p_alpha))


def _alpha_stats(image: Image.Image) -> tuple[bool, float]:
    """Return ``(has_alpha_channel, fully_opaque_ratio)``."""
    has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    total = rgba.size[0] * rgba.size[1]
    opaque = sum(alpha.histogram()[255:256])
    return has_alpha, opaque / total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="the supplied artwork (transparent PNG)")
    parser.add_argument("--allow-opaque", action="store_true",
                        help="ship even if the source has no transparency (explain in the commit)")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        raise SystemExit(f"source artwork not found: {source}")

    original = Image.open(source)
    has_alpha, opaque_ratio = _alpha_stats(original)
    if (not has_alpha or opaque_ratio > 0.999) and not args.allow_opaque:
        raise SystemExit(
            f"refusing to ship {source}: no transparent background "
            f"(has_alpha_channel={has_alpha}, fully-opaque pixels={opaque_ratio:.2%}).\n"
            "The nav icon must be transparent — an opaque copy paints a box behind the "
            "glyph that shows through on .nav-tab:hover / .nav-tab.active. "
            "Pass --allow-opaque only if an opaque background is genuinely wanted."
        )

    icon = _resize_premultiplied(original.convert("RGBA"), SIZE)
    root = Path(__file__).resolve().parents[1]
    target = root / FRONTEND / "knowledge-icon.png"
    icon.save(target, optimize=True)
    print(f"wrote {target} ({target.stat().st_size} bytes, {icon.size[0]}x{icon.size[1]})")


if __name__ == "__main__":
    main()