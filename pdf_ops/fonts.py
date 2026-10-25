"""Font model used by the interpreter and the edit engine.

Supported for reading: Type1, MMType1, TrueType, Type3 (read only) and Type0
fonts.  Supported for writing new text: simple fonts (Type1 / TrueType with
WinAnsi, MacRoman, Standard or custom Differences encodings) and Type0 fonts
with the Identity-H encoding.

The important question answered here is "which characters can be written
with this font *as embedded in this PDF*".  A character is only reported as
available when it maps to a code in the font's encoding and the embedded font
program really contains an outline for it (subset fonts usually contain only
the glyphs the original document used).
"""

from __future__ import annotations

import io
import re
import unicodedata
from decimal import Decimal
from functools import cached_property

import pikepdf

from .cmap import ToUnicodeCMap
from .encodings import MAC_ROMAN, NAMED, STANDARD, glyph_to_unicode, unicode_to_glyph_names
from .errors import MissingGlyphsError, UnsupportedError

try:  # pdfminer ships the Adobe AFM metrics of the standard 14 fonts.
    from pdfminer.fontmetrics import FONT_METRICS as _AFM
except Exception:  # pragma: no cover - pdfminer is a hard dependency
    _AFM = {}

_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")

_AFM_ALIASES = {
    "ArialMT": "Arial",
    "Arial-BoldMT": "Arial,Bold",
    "Arial-ItalicMT": "Arial,Italic",
    "Arial-BoldItalicMT": "Arial,BoldItalic",
    "TimesNewRomanPSMT": "TimesNewRoman",
    "TimesNewRomanPS-BoldMT": "TimesNewRoman,Bold",
    "TimesNewRomanPS-ItalicMT": "TimesNewRoman,Italic",
    "TimesNewRomanPS-BoldItalicMT": "TimesNewRoman,BoldItalic",
    "CourierNewPSMT": "CourierNew",
    "Times": "Times-Roman",
}

_WHITESPACE = {" ", " "}


def strip_subset_tag(name: str) -> str:
    return _SUBSET_TAG.sub("", name)


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


class PdfFont:
    """A font resource as used on a page (one per font dictionary object)."""

    def __init__(self, obj: pikepdf.Object):
        self.obj = obj
        self.objgen = obj.objgen if obj.is_indirect else (0, 0)
        self.subtype = str(obj.get("/Subtype", pikepdf.Name("/Type1")))[1:]
        self.base_font = str(obj.get("/BaseFont", pikepdf.Name("/Unknown")))[1:]
        self.name = strip_subset_tag(self.base_font)
        self.is_subset = bool(_SUBSET_TAG.match(self.base_font))
        self.is_type0 = self.subtype == "Type0"
        self.is_type3 = self.subtype == "Type3"
        self.used_codes: dict[int, int] = {}
        self.problems: list[str] = []

        self._descendant = None
        if self.is_type0:
            desc = obj.get("/DescendantFonts")
            if desc is not None and len(desc) > 0:
                self._descendant = desc[0]
            enc = obj.get("/Encoding")
            self.cmap_name = str(enc)[1:] if isinstance(enc, pikepdf.Name) else "embedded-cmap"
            self.code_length = 2
            self.vertical = self.cmap_name.endswith("-V")
            if self.cmap_name not in ("Identity-H", "Identity-V"):
                self.problems.append(f"Type0 font uses the {self.cmap_name} CMap, only Identity-H is supported")
        else:
            self.cmap_name = None
            self.code_length = 1
            self.vertical = False
        self.descriptor = self._get_descriptor()
        self.flags = int(_num(self.descriptor.get("/Flags", 0))) if self.descriptor is not None else 0
        self._tounicode = self._load_tounicode()
        self._encoding, self.encoding_name, self.has_differences = self._load_encoding()
        self._widths = self._load_widths()
        self.font_matrix = [float(v) for v in obj.get("/FontMatrix", [0.001, 0, 0, 0.001, 0, 0])] if self.is_type3 else None

    # ------------------------------------------------------------------ setup
    def _get_descriptor(self):
        src = self._descendant if self.is_type0 else self.obj
        if src is None:
            return None
        return src.get("/FontDescriptor")

    def _load_tounicode(self) -> ToUnicodeCMap | None:
        tu = self.obj.get("/ToUnicode")
        if isinstance(tu, pikepdf.Stream):
            try:
                return ToUnicodeCMap.parse(tu.read_bytes())
            except Exception as exc:  # pragma: no cover - defensive
                self.problems.append(f"unreadable ToUnicode CMap: {exc}")
        return None

    def _load_encoding(self):
        if self.is_type0:
            return {}, self.cmap_name, False
        enc = self.obj.get("/Encoding")
        differences = False
        base: dict[int, str] | None = None
        name = "builtin"
        if isinstance(enc, pikepdf.Name):
            name = str(enc)[1:]
            base = dict(NAMED.get(name, {}))
        elif isinstance(enc, pikepdf.Dictionary):
            be = enc.get("/BaseEncoding")
            if isinstance(be, pikepdf.Name):
                name = str(be)[1:]
                base = dict(NAMED.get(name, {}))
            diffs = enc.get("/Differences")
            if diffs is not None:
                differences = True
                if base is None:
                    base = dict(self._builtin_encoding())
                code = 0
                for item in diffs:
                    if isinstance(item, pikepdf.Name):
                        base[code] = str(item)[1:]
                        code += 1
                    else:
                        code = int(item)
                name = f"{name}+Differences" if name != "builtin" else "custom Differences"
        if base is None:
            base = dict(self._builtin_encoding())
        return base, name, differences

    def _builtin_encoding(self) -> dict[int, str]:
        if self.is_type3:
            return {}
        prog = self._program
        if prog[0] == "type1" and prog[2]:
            return prog[2]
        if prog[0] == "cff":
            enc = getattr(prog[1], "Encoding", None)
            if isinstance(enc, list):
                return {i: n for i, n in enumerate(enc) if n and n != ".notdef"}
        if self.name in ("Symbol", "ZapfDingbats"):
            return {}
        if self.subtype == "TrueType" and self.is_symbolic:
            return {}
        return STANDARD

    def _load_widths(self):
        if self.is_type0:
            d = self._descendant
            dw = _dec(d.get("/DW", 1000)) if d is not None else Decimal(1000)
            table: dict[int, Decimal] = {}
            w = d.get("/W") if d is not None else None
            if w is not None:
                items = list(w)
                i = 0
                while i < len(items):
                    first = int(items[i])
                    nxt = items[i + 1] if i + 1 < len(items) else None
                    if isinstance(nxt, pikepdf.Array):
                        for k, val in enumerate(nxt):
                            table[first + k] = _dec(val)
                        i += 2
                    else:
                        last = int(nxt)
                        val = _dec(items[i + 2])
                        if last - first <= 65535:
                            for c in range(first, last + 1):
                                table[c] = val
                        i += 3
            return ("cid", dw, table)
        widths = self.obj.get("/Widths")
        missing = Decimal(0)
        if self.descriptor is not None:
            missing = _dec(self.descriptor.get("/MissingWidth", 0))
        if widths is not None:
            first = int(self.obj.get("/FirstChar", 0))
            return ("simple", first, [_dec(v) for v in widths], missing)
        afm = self._afm
        if afm is not None:
            return ("afm", afm[1], missing)
        return ("program", None, missing)

    # ----------------------------------------------------------- properties
    @property
    def is_symbolic(self) -> bool:
        return bool(self.flags & 4) and not bool(self.flags & 32)

    @property
    def is_embedded(self) -> bool:
        d = self.descriptor
        return d is not None and any(k in d for k in ("/FontFile", "/FontFile2", "/FontFile3"))

    @cached_property
    def _afm(self):
        for key in (self.name, _AFM_ALIASES.get(self.name, "")):
            if key and key in _AFM:
                return _AFM[key]
        return None

    @property
    def ascent(self) -> float:
        v = _num(self.descriptor.get("/Ascent", 0)) if self.descriptor is not None else 0
        if not v and self._afm:
            v = self._afm[0].get("Ascent", 0)
        return (v or 800.0) / 1000.0

    @property
    def descent(self) -> float:
        v = _num(self.descriptor.get("/Descent", 0)) if self.descriptor is not None else 0
        if not v and self._afm:
            v = self._afm[0].get("Descent", 0)
        v = v or -200.0
        return (v if v < 0 else -v) / 1000.0

    @property
    def program_type(self) -> str | None:
        return self._program[0]

    @property
    def editable(self) -> bool:
        return not self.is_type3 and not self.vertical and not self.problems

    def describe(self) -> dict:
        return {
            "base_font": self.base_font,
            "subtype": self.subtype if not self.is_type0 else f"Type0/{self._descendant_subtype}",
            "encoding": self.encoding_name,
            "has_differences": self.has_differences,
            "embedded": self.is_embedded,
            "subset": self.is_subset,
            "program": self.program_type,
            "has_tounicode": self._tounicode is not None,
            "editable": self.editable,
            "problems": list(self.problems),
            "available_characters": "".join(sorted(self.available_chars())) if self.editable else "",
            "space_glyph": self.space_code is not None if self.editable else False,
        }

    @property
    def _descendant_subtype(self) -> str:
        if self._descendant is None:
            return "?"
        return str(self._descendant.get("/Subtype", "/?"))[1:]

    # ------------------------------------------------------------- decoding
    def split_codes(self, data: bytes) -> list[tuple[int, int, int]]:
        """Split a string operand into (code, byte_offset, byte_length)."""
        if self.code_length == 2:
            return [(int.from_bytes(data[i:i + 2], "big"), i, 2) for i in range(0, len(data) - 1, 2)]
        return [(b, i, 1) for i, b in enumerate(data)]

    def to_unicode(self, code: int) -> str | None:
        if self._tounicode is not None:
            text = self._tounicode.lookup(self.code_length, code)
            if text is not None:
                return text
        if self.is_type0:
            return None
        name = self._encoding.get(code)
        if name:
            text = glyph_to_unicode(name)
            if text:
                return text
        if self.subtype == "TrueType" and self.is_symbolic and 32 <= code < 127:
            return chr(code)
        return None

    def glyph_name(self, code: int) -> str | None:
        return None if self.is_type0 else self._encoding.get(code)

    def width(self, code: int) -> Decimal:
        """Advance width in text space for a font size of 1 (i.e. w0 / 1000)."""
        kind = self._widths[0]
        if kind == "cid":
            _, dw, table = self._widths
            return table.get(code, dw) / 1000
        if kind == "simple":
            _, first, widths, missing = self._widths
            idx = code - first
            w = widths[idx] if 0 <= idx < len(widths) else missing
            if self.is_type3 and self.font_matrix:
                return w * _dec(self.font_matrix[0])
            return w / 1000
        if kind == "afm":
            _, table, missing = self._widths
            text = self.to_unicode(code)
            if text and text in table:
                return _dec(table[text]) / 1000
            return missing / 1000
        w = self._program_width(code)
        return (w if w is not None else self._widths[2]) / 1000

    def _program_width(self, code: int):
        prog = self._program
        if prog[0] != "truetype":
            return None
        gname = self._truetype_glyph(code)
        if gname is None:
            return None
        tt = prog[1]
        adv = tt["hmtx"][gname][0]
        return _dec(adv) * 1000 / tt["head"].unitsPerEm

    # -------------------------------------------------- glyph availability
    @cached_property
    def _program(self):
        """(kind, font_object, builtin_encoding_or_glyph_set)."""
        d = self.descriptor
        if d is None:
            return (None, None, None)
        try:
            if "/FontFile2" in d:
                from fontTools.ttLib import TTFont

                return ("truetype", TTFont(io.BytesIO(d.FontFile2.read_bytes()), lazy=False), None)
            if "/FontFile3" in d:
                ff = d.FontFile3
                sub = str(ff.get("/Subtype", "/"))[1:]
                data = ff.read_bytes()
                if sub == "OpenType":
                    from fontTools.ttLib import TTFont

                    tt = TTFont(io.BytesIO(data))
                    if "CFF " in tt:
                        cff = tt["CFF "].cff
                        return ("cff", cff[cff.fontNames[0]], None)
                    return ("truetype", tt, None)
                from fontTools.cffLib import CFFFontSet

                cff = CFFFontSet()
                cff.decompile(io.BytesIO(data), None)
                return ("cff", cff[cff.fontNames[0]], None)
            if "/FontFile" in d:
                names, encoding = _parse_type1(d.FontFile.read_bytes())
                return ("type1", names, encoding)
        except Exception as exc:
            self.problems_program = str(exc)
            return ("unreadable", None, None)
        return (None, None, None)

    def _truetype_glyph(self, code: int) -> str | None:
        tt = self._program[1]
        cmap = tt["cmap"] if "cmap" in tt else None
        order = tt.getGlyphOrder()
        if self.is_type0:
            gid = self._cid_to_gid(code)
            return order[gid] if gid is not None and 0 <= gid < len(order) else None
        tables = {}
        if cmap is not None:
            for t in cmap.tables:
                tables.setdefault((t.platformID, t.platEncID), t.cmap)
        gname = self._encoding.get(code)
        if not self.is_symbolic and gname:
            text = glyph_to_unicode(gname)
            if (3, 1) in tables and text and len(text) == 1:
                g = tables[(3, 1)].get(ord(text))
                if g:
                    return g
            if (1, 0) in tables:
                mac = {n: c for c, n in MAC_ROMAN.items()}.get(gname)
                if mac is not None and tables[(1, 0)].get(mac):
                    return tables[(1, 0)][mac]
            if gname in order:
                return gname
        for key in ((3, 0), (1, 0), (3, 1)):
            t = tables.get(key)
            if not t:
                continue
            for c in (code, 0xF000 + code, 0xF100 + code, 0xF200 + code):
                if t.get(c):
                    return t[c]
        if gname and gname in order:
            return gname
        return None

    def _cid_to_gid(self, cid: int) -> int | None:
        d = self._descendant
        m = d.get("/CIDToGIDMap") if d is not None else None
        if m is None or isinstance(m, pikepdf.Name):
            return cid
        data = self._cidtogid_bytes
        if 2 * cid + 1 < len(data):
            return int.from_bytes(data[2 * cid:2 * cid + 2], "big")
        return None

    @cached_property
    def _cidtogid_bytes(self) -> bytes:
        return self._descendant.CIDToGIDMap.read_bytes()

    def glyph_present(self, code: int) -> bool | None:
        """True / False when the embedded program can be inspected, None if unknown."""
        kind, prog, extra = self._program
        text = self.to_unicode(code) or ""
        if kind == "truetype":
            gname = self._truetype_glyph(code)
            if not gname or gname == ".notdef":
                return False
            if "glyf" in prog:
                g = prog["glyf"][gname]
                if g.numberOfContours != 0:
                    return True
                return text in _WHITESPACE and prog["hmtx"][gname][0] > 0
            if "CFF " in prog:
                return gname in prog["CFF "].cff[prog["CFF "].cff.fontNames[0]].CharStrings
            return True
        if kind == "cff":
            charstrings = prog.CharStrings
            if self.is_type0:
                charset = getattr(prog, "charset", None) or []
                if hasattr(prog, "ROS"):
                    name = "cid%05d" % code
                    return name in charstrings
                return 0 <= code < len(charset) and charset[code] != ".notdef"
            gname = self._encoding.get(code)
            return bool(gname) and gname != ".notdef" and gname in charstrings
        if kind == "type1":
            gname = self._encoding.get(code)
            return bool(gname) and gname != ".notdef" and gname in prog
        if kind is None and not self.is_embedded:
            afm = self._afm
            if afm is not None:
                return bool(text) and text in afm[1]
            return None
        return None

    # ------------------------------------------------------------- encoding
    @cached_property
    def _reverse(self) -> dict[str, int]:
        """Unicode character -> code, restricted to glyphs that are really present."""
        candidates: dict[str, list[int]] = {}
        if self.is_type0:
            if self._tounicode is not None:
                for (nb, code), text in self._tounicode.map.items():
                    if nb == 2 and text:
                        candidates.setdefault(text, []).append(code)
        else:
            for code in range(256):
                text = self.to_unicode(code)
                if text:
                    candidates.setdefault(text, []).append(code)
        out: dict[str, int] = {}
        for text, codes in candidates.items():
            if len(text) != 1:
                continue
            good = []
            for code in codes:
                present = self.glyph_present(code)
                if present is False:
                    continue
                if present is None and self.is_embedded:
                    # Program unreadable: trust only codes the document already uses.
                    if code not in self.used_codes:
                        continue
                if self.width(code) <= 0 and not unicodedata.combining(text):
                    continue
                good.append(code)
            if good:
                good.sort(key=lambda c: (-self.used_codes.get(c, 0), c))
                out[text] = good[0]
        if " " not in out and " " in out:
            out[" "] = out[" "]
        return out

    def invalidate(self) -> None:
        self.__dict__.pop("_reverse", None)

    @property
    def space_code(self) -> int | None:
        return self._reverse.get(" ")

    def available_chars(self) -> set[str]:
        return set(self._reverse)

    def missing_chars(self, text: str) -> list[str]:
        rev = self._reverse
        missing = []
        for ch in text:
            if ch in rev:
                continue
            if ch in _WHITESPACE:
                continue  # spaces can always be written as positioning
            if ch not in missing:
                missing.append(ch)
        return missing

    def encode(self, text: str) -> list[int | None]:
        """Codes for each character; None marks a space with no glyph (use positioning)."""
        if not self.editable:
            raise UnsupportedError(
                f"font {self.base_font} cannot be used to write text: " + "; ".join(self.problems or ["Type3 or vertical font"]),
                font=self.base_font,
            )
        missing = self.missing_chars(text)
        if missing:
            raise MissingGlyphsError(
                f"font {self.base_font} does not contain: " + ", ".join(repr(c) for c in missing),
                missing=missing,
                font=self.base_font,
            )
        rev = self._reverse
        out: list[int | None] = []
        for ch in text:
            if ch in rev:
                out.append(rev[ch])
            elif ch in _WHITESPACE:
                out.append(rev.get(" "))
        return out

    def code_bytes(self, code: int) -> bytes:
        return code.to_bytes(self.code_length, "big")

    def is_word_space(self, code: int, nbytes: int) -> bool:
        """Word spacing (Tw) applies to the single-byte code 32 only."""
        return nbytes == 1 and code == 32


def _parse_type1(data: bytes) -> tuple[set[str], dict[int, str]]:
    """Glyph names and built-in encoding of an embedded Type 1 font program."""
    if data[:1] == b"\x80":  # PFB segments
        out = b""
        i = 0
        while i + 6 <= len(data) and data[i] == 0x80 and data[i + 1] in (1, 2):
            n = int.from_bytes(data[i + 2:i + 6], "little")
            out += data[i + 6:i + 6 + n]
            i += 6 + n
        data = out
    encoding: dict[int, str] = {}
    head_end = data.find(b"eexec")
    clear = data[: head_end if head_end >= 0 else len(data)]
    if b"StandardEncoding def" in clear:
        encoding = dict(STANDARD)
    for m in re.finditer(rb"dup\s+(\d+)\s*/([^\s/\[\]{}()<>]+)\s+put", clear):
        encoding[int(m.group(1))] = m.group(2).decode("latin-1")
    names: set[str] = set()
    if head_end >= 0:
        body = data[head_end + 5:].lstrip(b"\r\n \t")
        if re.fullmatch(rb"[0-9A-Fa-f\s]+", body[:64] or b"x"):
            hexdigits = re.sub(rb"[^0-9A-Fa-f]", b"", body.split(b"0000000000")[0])
            body = bytes.fromhex(hexdigits[: len(hexdigits) // 2 * 2].decode())
        plain = _eexec_decrypt(body, 55665)[4:]
        idx = plain.find(b"/CharStrings")
        if idx >= 0:
            for m in re.finditer(rb"/([^\s/\[\]{}()<>]+)\s+(\d+)\s+(?:RD|-\|)\s", plain[idx:]):
                names.add(m.group(1).decode("latin-1"))
    return names, encoding


def _eexec_decrypt(data: bytes, r: int) -> bytes:
    out = bytearray()
    c1, c2 = 52845, 22719
    for b in data:
        out.append(b ^ (r >> 8))
        r = ((b + r) * c1 + c2) & 0xFFFF
    return bytes(out)


class FontCache:
    """One PdfFont per font object in a document."""

    def __init__(self) -> None:
        self._fonts: dict = {}

    def get(self, obj: pikepdf.Object) -> PdfFont:
        key = obj.objgen if obj.is_indirect else id(obj)
        font = self._fonts.get(key)
        if font is None:
            font = PdfFont(obj)
            self._fonts[key] = font
        return font

    def all(self) -> list[PdfFont]:
        return list(self._fonts.values())


def measure_text(font: PdfFont, text: str, size: float, char_spacing: float = 0.0,
                 word_spacing: float = 0.0, horizontal_scale: float = 1.0,
                 space_width: float | None = None) -> float:
    """Width of ``text`` in unscaled text space for the given font size.

    Characters without glyphs raise MissingGlyphsError.  Spaces that the font
    cannot draw are measured as ``space_width`` (default: the font's space
    width or 0.25 em).
    """
    codes = font.encode(text)
    return float(advance_of_codes(font, codes, Decimal(str(size)), Decimal(str(char_spacing)),
                                  Decimal(str(word_spacing)), Decimal(str(horizontal_scale)),
                                  None if space_width is None else Decimal(str(space_width))))


def advance_of_codes(font: PdfFont, codes, size: Decimal, tc: Decimal, tw: Decimal, th: Decimal,
                     space_width: Decimal | None = None) -> Decimal:
    total = Decimal(0)
    for code in codes:
        if code is None:
            sw = space_width if space_width is not None else Decimal("0.25") * size
            total += sw * th
            continue
        adv = font.width(code) * size + tc
        if font.is_word_space(code, font.code_length):
            adv += tw
        total += adv * th
    return total


def glyphs_available(font: PdfFont, text: str) -> tuple[bool, list[str]]:
    """(True, []) when every character of ``text`` can be drawn with ``font``."""
    if not font.editable:
        return False, list(dict.fromkeys(text))
    missing = font.missing_chars(text)
    return (not missing), missing
