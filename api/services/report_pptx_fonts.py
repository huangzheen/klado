"""Embed the Report Deck's licensed web fonts as editable PowerPoint faces.

PowerPoint font parts contain EOT-wrapped TrueType data, not WOFF2.  Only the
two bundled, installably embeddable families are accepted here; an arbitrary
font referenced by a report is never fetched or silently bundled.
"""
from __future__ import annotations

import io
import struct
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile

from fontTools.merge import Merger
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from lxml import etree

from core.config import resolve_frontend_dir

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_FONT_REL = _R + "/font"
_FONT_TYPE = "application/x-fontdata"
_FACES = {
    "Roboto Condensed": ("roboto-condensed-latin.woff2", "roboto-condensed-latin-ext.woff2"),
    "CoolSans SC Narrow": ("coolsans-sc-narrow-400.woff2", "coolsans-sc-narrow-700.woff2"),
}


def _font_dir() -> Path:
    paths = (
        Path(resolve_frontend_dir(), "vendor", "fonts"),   # the bundle the SPA is served from
        Path(__file__).resolve().parents[2] / "frontend/out/vendor/fonts",
    )
    for path in paths:
        if all((path / name).is_file() for names in _FACES.values() for name in names):
            return path
    raise RuntimeError("the bundled Report Deck fonts are missing")


def _name(font: TTFont, name_id: int, fallback: str) -> str:
    candidates = [n for n in font["name"].names if n.nameID == name_id]
    english = next((n for n in candidates if n.langID == 0x409), None)
    return (english or (candidates[0] if candidates else None)).toUnicode() if candidates else fallback


def _sfnt(source: Path, weight: int | None = None) -> TTFont:
    font = TTFont(source)
    if font["OS/2"].fsType & 0x0002:
        raise RuntimeError(f"font does not permit embedding: {source.name}")
    font.flavor = None
    if weight is not None:
        font = instantiateVariableFont(font, {"wght": weight}, inplace=False)
        font["OS/2"].usWeightClass = weight
        bold = weight >= 600
        family = _name(font, 1, "Roboto Condensed")
        font["name"].setName("Bold" if bold else "Regular", 2, 3, 1, 0x409)
        font["name"].setName(f"{family} {'Bold' if bold else 'Regular'}", 4, 3, 1, 0x409)
        font["name"].setName("RobotoCondensed-Bold" if bold else "RobotoCondensed-Regular", 6, 3, 1, 0x409)
        font["OS/2"].fsSelection = ((font["OS/2"].fsSelection | 0x20) & ~0x40) if bold else ((font["OS/2"].fsSelection & ~0x20) | 0x40)
        font["head"].macStyle = (font["head"].macStyle | 1) if bold else (font["head"].macStyle & ~1)
    return font


def _ttf_bytes(font: TTFont) -> bytes:
    stream = io.BytesIO()
    font.save(stream)
    return stream.getvalue()


def _face(family: str, bold: bool, directory: Path) -> TTFont:
    if family == "CoolSans SC Narrow":
        return _sfnt(directory / _FACES[family][int(bold)])
    # The two web subsets are disjoint.  Instantiate the variable font at a
    # fixed Office weight before merging, so PPT has one complete Latin face.
    parts = []
    for filename in _FACES[family]:
        part = io.BytesIO(_ttf_bytes(_sfnt(directory / filename, 700 if bold else 400)))
        parts.append(part)
    return Merger().merge(parts)


def _eot(font: TTFont) -> bytes:
    """EOT 1.0 with an uncompressed full TrueType face (editable after export)."""
    data = _ttf_bytes(font)
    os2, head = font["OS/2"], font["head"]
    out = io.BytesIO()
    out.write(b"\0\0\0\0")  # patched EOTSize
    out.write(struct.pack("<III", len(data), 0x00010000, 0))
    out.write(bytes(getattr(os2.panose, key) for key in (
        "bFamilyType", "bSerifStyle", "bWeight", "bProportion", "bContrast",
        "bStrokeVariation", "bArmStyle", "bLetterForm", "bMidline", "bXHeight")))
    out.write(struct.pack("<BBIHH", 1, int(bool(os2.fsSelection & 1)),
                          os2.usWeightClass, os2.fsType, 0x504C))
    out.write(struct.pack("<4I2I", *(getattr(os2, f"ulUnicodeRange{i}") for i in range(1, 5)),
                          os2.ulCodePageRange1, os2.ulCodePageRange2))
    out.write(struct.pack("<I4I", head.checkSumAdjustment, 0, 0, 0, 0))
    for name_id, fallback in ((1, "Font"), (2, "Regular"), (5, "Version 1"), (4, "Font")):
        encoded = _name(font, name_id, fallback).encode("utf-16le")
        out.write(struct.pack("<HH", 0, len(encoded)))
        out.write(encoded)
    out.write(data)
    payload = out.getvalue()
    return struct.pack("<I", len(payload)) + payload[4:]


@lru_cache(maxsize=4)
def _prepared_eot(family: str, bold: bool) -> bytes:
    # All reports use the same four bundled faces.  Converting WOFF2 and
    # merging the Roboto subsets on every download adds several seconds.
    return _eot(_face(family, bold, _font_dir()))


def embed_project_fonts(pptx_bytes: bytes, families: set[str]) -> bytes:
    """Add full-font parts and PresentationML relationships to a PPTX archive."""
    wanted = sorted(families & _FACES.keys())
    if not wanted:
        return pptx_bytes
    original, result = io.BytesIO(pptx_bytes), io.BytesIO()
    with ZipFile(original) as source, ZipFile(result, "w") as target:
        files = {info.filename: (info, source.read(info.filename)) for info in source.infolist()}
        presentation = etree.fromstring(files["ppt/presentation.xml"][1])
        relationships = etree.fromstring(files["ppt/_rels/presentation.xml.rels"][1])
        content_types = etree.fromstring(files["[Content_Types].xml"][1])
        used_ids = {rel.get("Id") for rel in relationships}
        next_id = 1

        def relation(part: str) -> str:
            nonlocal next_id
            while f"rId{next_id}" in used_ids:
                next_id += 1
            rid = f"rId{next_id}"
            used_ids.add(rid)
            etree.SubElement(relationships, f"{{{_PKG}}}Relationship",
                             Id=rid, Type=_FONT_REL, Target="fonts/" + part)
            return rid

        existing = presentation.find(f"{{{_P}}}embeddedFontLst")
        if existing is not None:
            presentation.remove(existing)
        font_list = etree.Element(f"{{{_P}}}embeddedFontLst")
        count = 0
        for family in wanted:
            item = etree.SubElement(font_list, f"{{{_P}}}embeddedFont")
            etree.SubElement(item, f"{{{_P}}}font", typeface=family, pitchFamily="2", charset="0")
            for bold in (False, True):
                count += 1
                part = f"font{count}.fntdata"
                data = _prepared_eot(family, bold)
                files["ppt/fonts/" + part] = (None, data)
                face = etree.SubElement(item, f"{{{_P}}}{'bold' if bold else 'regular'}")
                face.set(f"{{{_R}}}id", relation(part))
        notes_size = presentation.find(f"{{{_P}}}notesSz")
        presentation.insert(list(presentation).index(notes_size) + 1 if notes_size is not None else len(presentation), font_list)
        presentation.set("embedTrueTypeFonts", "1")
        presentation.set("saveSubsetFonts", "0")
        if not any(e.get("Extension") == "fntdata" for e in content_types):
            etree.SubElement(content_types, f"{{{_CT}}}Default",
                             Extension="fntdata", ContentType=_FONT_TYPE)
        files["ppt/presentation.xml"] = (files["ppt/presentation.xml"][0], etree.tostring(presentation, xml_declaration=True, encoding="UTF-8", standalone=True))
        files["ppt/_rels/presentation.xml.rels"] = (files["ppt/_rels/presentation.xml.rels"][0], etree.tostring(relationships, xml_declaration=True, encoding="UTF-8", standalone=True))
        files["[Content_Types].xml"] = (files["[Content_Types].xml"][0], etree.tostring(content_types, xml_declaration=True, encoding="UTF-8", standalone=True))
        for name, (info, data) in files.items():
            target.writestr(info or name, data)
    return result.getvalue()
