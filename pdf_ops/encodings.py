"""Standard PDF simple-font encodings and glyph-name <-> Unicode helpers.

The code tables come from pdfminer.six's ``latin_enc`` data (MIT licence),
which lists, for every Latin glyph name, its code in StandardEncoding,
MacRomanEncoding, WinAnsiEncoding and PDFDocEncoding.  Glyph names are mapped
to Unicode with fontTools' Adobe Glyph List implementation.
"""

from __future__ import annotations

from functools import lru_cache

from fontTools import agl
from pdfminer.latin_enc import ENCODING as _LATIN

_COLUMNS = {"StandardEncoding": 1, "MacRomanEncoding": 2, "WinAnsiEncoding": 3, "PDFDocEncoding": 4}


def _build(column: int) -> dict[int, str]:
    table: dict[int, str] = {}
    for row in _LATIN:
        name, code = row[0], row[column]
        if code is not None and code not in table:
            table[code] = name
    return table


STANDARD = _build(1)
MAC_ROMAN = _build(2)
WIN_ANSI = _build(3)
PDF_DOC = _build(4)

# WinAnsiEncoding: the specification encodes the bullet at otherwise unused
# positions, and code 0xA0 / 0xAD are the no-break space and soft hyphen.
for _code in (0x7F, 0x81, 0x8D, 0x8F, 0x90, 0x9D):
    WIN_ANSI.setdefault(_code, "bullet")
WIN_ANSI.setdefault(0xA0, "space")
WIN_ANSI.setdefault(0xAD, "hyphen")
# MacRomanEncoding in PDF uses "space" at 0xCA as well.
MAC_ROMAN.setdefault(0xCA, "space")

NAMED = {
    "StandardEncoding": STANDARD,
    "MacRomanEncoding": MAC_ROMAN,
    "WinAnsiEncoding": WIN_ANSI,
    "PDFDocEncoding": PDF_DOC,
}


@lru_cache(maxsize=4096)
def glyph_to_unicode(name: str) -> str:
    """Unicode text for a glyph name, or '' if the name has no standard meaning."""
    if not name:
        return ""
    if name in ("space", "nbspace", "nonbreakingspace"):
        return " "
    text = agl.toUnicode(name)
    if text:
        return text
    # Names such as "g123", "cid123", "glyph12" carry no Unicode meaning.
    return ""


@lru_cache(maxsize=4096)
def unicode_to_glyph_names(char: str) -> tuple[str, ...]:
    """Candidate glyph names for one Unicode character (most common first)."""
    cp = ord(char)
    names = []
    if cp in agl.UV2AGL:
        names.append(agl.UV2AGL[cp])
    for name, value in agl.AGL2UV.items():
        if value == cp and name not in names:
            names.append(name)
    names.append("uni%04X" % cp if cp <= 0xFFFF else "u%06X" % cp)
    if char == " ":
        names.extend(["space", "nbspace"])
    return tuple(dict.fromkeys(names))
