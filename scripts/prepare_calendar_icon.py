"""Ship the supplied Calendar nav icon.

Same contract as ``scripts/prepare_knowledge_icon.py``: the artwork comes from the user,
so this script does not draw or restyle it — it only brings it to the shipped format and
writes the shipped frontend copy (``frontend/out``).

What is different from the knowledge icon, and why
--------------------------------------------------
⚠️ The supplied file is a **JPEG with a white background**, not a transparent PNG — the
name says `.png`, the bytes are JPEG, and there is no alpha channel at all. So there is a
background to deal with, and it cannot simply be left alone: ``.nav-tab:hover`` paints
``rgba(0,0,0,.05)`` and ``.nav-tab.active`` paints ``rgba(37,99,235,.10)`` behind the
icon, and an opaque white square shows up as a paler patch inside that tinted pill. So:

* the subject is **cropped to its own bounding box** (the source leaves ~30-40% of the
  canvas empty, and at 22px that empty margin is most of the icon);
* the white is turned into **real transparency** — but **flood-filled from the border
  only**. A blanket "white → transparent" would punch holes through any light region
  *inside* the artwork (the knowledge script warns about exactly this), and this artwork
  has bright highlights on the dial;
* the result is pasted on the same **266x266 canvas** ``workspace-icon.png`` uses, so all
  three nav marks render at 22x22 through the same ``.workspace-mark`` rule.

⚠️ Recorded, not solved: the artwork is a 3-D shaded render with fine detail (dial
markings, sun rays) and ~22px is small for it. It is legible as "gold-and-blue disc"
but the fine markings are gone. A flat, single-colour mark would hold up better if that
ever matters.

Source of the shipped icon, **not kept in the repo** (same rule as the email banners and
the knowledge icon):

    ~/.minimax/v2/assets/2026/09/29/11-19-17-323-...-image.png   (1254x1254, JPEG, white bg)

    python3 scripts/prepare_calendar_icon.py --source <path>
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from PIL import Image

SIZE = 266                     # the shipped canvas, same as workspace-icon.png
WHITE = 244                    # a channel at or above this counts as "background white"
PAD = 6                        # px of breathing room around the subject, at SIZE scale
DEFAULT_SOURCE = Path.home() / ".minimax/v2/assets/2026/09/29/11-19-17-323-asset_20260929-111917-323_72a69e82b867_a66aa806-image.png"


def _subject_box(image: Image.Image) -> tuple[int, int, int, int]:
    """The bounding box of everything that is not background white."""
    width, height = image.size
    pixels = image.load()
    left, top, right, bottom = width, height, 0, 0
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y][:3]
            if min(r, g, b) < WHITE:
                if x < left:
                    left = x
                if y < top:
                    top = y
                if x > right:
                    right = x
                if y > bottom:
                    bottom = y
    if right <= left or bottom <= top:
        raise SystemExit("the image is blank — nothing to crop to")
    return left, top, right + 1, bottom + 1


def _clear_background(image: Image.Image) -> Image.Image:
    """White reachable from the border becomes transparent; white inside does not.

    Flood fill, not a threshold: the artwork has light highlights, and a threshold would
    hollow them out into holes that only show once the icon sits on a coloured pill.
    """
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()

    def is_white(x: int, y: int) -> bool:
        r, g, b, a = pixels[x, y]
        return a > 0 and min(r, g, b) >= WHITE

    seen = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            if is_white(x, y) and not seen[y * width + x]:
                seen[y * width + x] = 1
                queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if is_white(x, y) and not seen[y * width + x]:
                seen[y * width + x] = 1
                queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        pixels[x, y] = (255, 255, 255, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < width and 0 <= ny < height and not seen[ny * width + nx] and is_white(nx, ny):
                seen[ny * width + nx] = 1
                queue.append((nx, ny))
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default="frontend/out/calendar-icon.png")
    args = parser.parse_args()

    source = Path(args.source).expanduser()
    if not source.is_file():
        raise SystemExit(f"source image not found: {source}")

    image = Image.open(source)
    box = _subject_box(image.convert("RGB"))
    subject = _clear_background(image.convert("RGBA").crop(box))

    inner = SIZE - PAD * 2
    subject = subject.resize((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(subject, (PAD, PAD), subject)

    out = Path(args.out)
    canvas.save(out, "PNG", optimize=True)
    mirror = Path("api") / out                              # the copy the cloud build ships
    mirror.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(mirror, "PNG", optimize=True)

    opaque = sum(1 for p in canvas.getdata() if p[3] > 0)
    print(f"source   : {source} ({image.size[0]}x{image.size[1]}, {image.format})")
    print(f"crop     : {box}  ({box[2]-box[0]}x{box[3]-box[1]})")
    print(f"shipped  : {out} + {mirror}  ({SIZE}x{SIZE}, "
          f"{opaque}/{SIZE*SIZE} px opaque, {out.stat().st_size}B)")


if __name__ == "__main__":
    main()