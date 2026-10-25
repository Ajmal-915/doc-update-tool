"""Independent text extraction for the safety checks.

The checks deliberately do not use pdf_ops' interpreter (which produced the
edit).  Characters are read with pdfminer.six, and whether a glyph really
exists in an embedded font is decided here with fontTools, from the font
program pdfminer resolved.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1

logging.getLogger("pdfminer").setLevel(logging.ERROR)


@dataclass
class XChar:
    page: int
    text: str
    cid: int
    font_key: int
    fontname: str
    size: float
    x0: float
    y0: float
    x1: float
    y1: float
    ox: float  # origin (baseline start)
    oy: float
    color: tuple
    render: int
    upright: bool

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def core(self) -> tuple[float, float, float, float]:
        """Tight box for overlap tests: x extent, baseline-0.15em .. baseline+0.7em."""
        return (self.x0, self.oy - 0.15 * self.size, self.x1, self.oy + 0.7 * self.size)

    def key(self) -> tuple:
        return (self.text, round(self.ox, 2), round(self.oy, 2), self.fontname, round(self.size, 2), self.render, _color_key(self.color))


def _color_key(c) -> tuple:
    if c is None:
        return ()
    if isinstance(c, (int, float)):
        return (round(float(c), 3),)
    try:
        return tuple(round(float(v), 3) if isinstance(v, (int, float)) else str(v) for v in c)
    except TypeError:
        return (str(c),)


class _RM(PDFResourceManager):
    def __init__(self):
        super().__init__(caching=True)
        self.specs: dict[int, dict] = {}

    def get_font(self, objid, spec):
        font = super().get_font(objid, spec)
        self.specs[id(font)] = spec
        return font


class _Device(PDFLayoutAnalyzer):
    def __init__(self, rsrcmgr, page_index: int):
        super().__init__(rsrcmgr, pageno=page_index + 1, laparams=None)
        self.page_index = page_index
        self.chars: list[XChar] = []
        self._render = 0
        self.fonts: dict[int, object] = {}

    def render_string(self, textstate, seq, ncs, graphicstate):
        self._render = textstate.render
        return super().render_string(textstate, seq, ncs, graphicstate)

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        adv = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        item = self.cur_item._objs[-1]
        if isinstance(item, LTChar):
            a, b, c, d, e, f = item.matrix
            self.fonts[id(font)] = font
            self.chars.append(XChar(
                page=self.page_index, text=item.get_text(), cid=cid, font_key=id(font), fontname=item.fontname,
                size=float(item.size), x0=item.x0, y0=item.y0, x1=item.x1, y1=item.y1, ox=e, oy=f,
                color=graphicstate.ncolor, render=self._render, upright=bool(item.upright and abs(b) < 1e-6 and abs(c) < 1e-6),
            ))
        return adv

    def receive_layout(self, ltpage):
        pass


@dataclass
class Extraction:
    pages: dict[int, list[XChar]]
    fonts: dict[int, object]  # font_key -> pdfminer font
    specs: dict[int, dict]  # font_key -> font dictionary (pdfminer objects)


def extract(path: str | Path, only: set | None = None) -> Extraction:
    """Characters of every page (or only the pages in ``only``)."""
    pages: dict[int, list[XChar]] = {}
    fonts: dict[int, object] = {}
    with open(path, "rb") as fh:
        parser = PDFParser(fh)
        document = PDFDocument(parser)
        rm = _RM()
        for i, page in enumerate(PDFPage.create_pages(document)):
            if only is not None and i not in only:
                continue
            dev = _Device(rm, i)
            interp = PDFPageInterpreter(rm, dev)
            interp.process_page(page)
            pages[i] = dev.chars
            fonts.update(dev.fonts)
        # resolve font specs while the file is open (streams are read lazily)
        specs = {k: _materialize(v) for k, v in rm.specs.items()}
    return Extraction(pages, fonts, specs)


def _materialize(spec, depth=0):
    """Resolve the parts of a font dictionary the glyph check needs."""
    out = {}
    if depth > 4 or not isinstance(spec, dict):
        return out
    for key in ("Subtype", "BaseFont", "Encoding", "FirstChar", "Widths", "ToUnicode"):
        if key in spec:
            out[key] = _plain(resolve1(spec[key]))
    fd = resolve1(spec.get("FontDescriptor"))
    if isinstance(fd, dict):
        desc = {"Flags": resolve1(fd.get("Flags", 0))}
        for k in ("FontFile", "FontFile2", "FontFile3"):
            if k in fd:
                stream = resolve1(fd[k])
                try:
                    desc[k] = stream.get_data()
                    if k == "FontFile3":
                        sub = resolve1(stream.attrs.get("Subtype"))
                        desc["FontFile3Subtype"] = getattr(sub, "name", str(sub))
                except Exception:
                    desc[k] = None
        out["FontDescriptor"] = desc
    if "DescendantFonts" in spec:
        df = resolve1(spec["DescendantFonts"])
        if df:
            d0 = resolve1(df[0])
            sub = _materialize(d0, depth + 1)
            m = resolve1(d0.get("CIDToGIDMap")) if isinstance(d0, dict) else None
            if m is not None and hasattr(m, "get_data"):
                sub["CIDToGIDMap"] = m.get_data()
            elif m is not None:
                sub["CIDToGIDMap"] = "Identity"
            out["Descendant"] = sub
    return out


def _plain(v):
    v = resolve1(v)
    if hasattr(v, "name"):
        return v.name
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if hasattr(v, "get_data"):
        return "<stream>"
    return v


# ------------------------------------------------------------ glyph check
class GlyphChecker:
    """Decides whether a (font, code) pair has a real glyph in the embedded program."""

    def __init__(self, extraction: Extraction):
        self.ex = extraction
        self._programs: dict[int, tuple] = {}

    def _program(self, key: int):
        if key in self._programs:
            return self._programs[key]
        spec = self.ex.specs.get(key, {})
        sub = spec.get("Subtype")
        top = spec
        if sub == "Type0":
            spec = spec.get("Descendant", {})
        fd = spec.get("FontDescriptor", {})
        prog = (None, None)
        try:
            if fd.get("FontFile2"):
                from fontTools.ttLib import TTFont

                prog = ("tt", TTFont(io.BytesIO(fd["FontFile2"])))
            elif fd.get("FontFile3"):
                data = fd["FontFile3"]
                if fd.get("FontFile3Subtype") == "OpenType":
                    from fontTools.ttLib import TTFont

                    tt = TTFont(io.BytesIO(data))
                    prog = ("tt", tt) if "glyf" in tt else ("cff", tt["CFF "].cff[tt["CFF "].cff.fontNames[0]])
                else:
                    from fontTools.cffLib import CFFFontSet

                    cff = CFFFontSet()
                    cff.decompile(io.BytesIO(data), None)
                    prog = ("cff", cff[cff.fontNames[0]])
            elif fd.get("FontFile"):
                prog = ("t1", _type1_names(fd["FontFile"]))
            elif "FontFile" not in fd and "FontFile2" not in fd and "FontFile3" not in fd:
                prog = ("none", None)
        except Exception:
            prog = ("unreadable", None)
        self._programs[key] = (prog, top, spec)
        return self._programs[key]

    def check(self, ch: XChar) -> tuple[bool, str]:
        if ch.text.startswith("(cid:") or ch.text == "�":
            return False, f"character code {ch.cid} has no Unicode meaning (possibly missing glyph)"
        (kind, prog), top, spec = self._program(ch.font_key)
        if ch.text.isspace():
            return True, ""
        is_cid = top.get("Subtype") == "Type0"
        if kind == "tt":
            gname = _tt_glyph(prog, ch, is_cid, spec, top)
            if gname is None or gname == ".notdef":
                return False, f"no glyph for {ch.text!r} in {ch.fontname}"
            if "glyf" in prog:
                g = prog["glyf"][gname]
                if g.numberOfContours == 0:
                    return False, f"glyph for {ch.text!r} in {ch.fontname} is empty"
            return True, ""
        if kind == "cff":
            names = prog.CharStrings
            if is_cid:
                if hasattr(prog, "ROS"):
                    return ("cid%05d" % ch.cid in names), f"no glyph for {ch.text!r} in {ch.fontname}"
                charset = prog.charset
                ok = 0 <= ch.cid < len(charset) and charset[ch.cid] != ".notdef"
                return ok, "" if ok else f"no glyph for {ch.text!r} in {ch.fontname}"
            gname = _simple_glyph_name(top, ch.cid, prog)
            ok = bool(gname) and gname in names and gname != ".notdef"
            return ok, "" if ok else f"no glyph for {ch.text!r} in {ch.fontname}"
        if kind == "t1":
            gname = _simple_glyph_name(top, ch.cid, None)
            ok = bool(gname) and gname in prog
            return ok, "" if ok else f"no glyph for {ch.text!r} in {ch.fontname}"
        if kind == "none":
            font = self.ex.fonts.get(ch.font_key)
            try:
                w = font.char_width(ch.cid) if font is not None else 0
            except Exception:
                w = 0
            return (w > 0), "" if w > 0 else f"no width for {ch.text!r} in non-embedded {ch.fontname}"
        return False, f"cannot verify glyphs of font {ch.fontname} ({kind})"


def _tt_glyph(tt, ch: XChar, is_cid: bool, spec: dict, top: dict):
    order = tt.getGlyphOrder()
    if is_cid:
        m = spec.get("CIDToGIDMap", "Identity")
        if m == "Identity" or m is None:
            gid = ch.cid
        else:
            gid = int.from_bytes(m[2 * ch.cid:2 * ch.cid + 2], "big") if 2 * ch.cid + 1 < len(m) else None
        return order[gid] if gid is not None and 0 <= gid < len(order) else None
    tables = {}
    if "cmap" in tt:
        for t in tt["cmap"].tables:
            tables.setdefault((t.platformID, t.platEncID), t.cmap)
    flags = int((spec.get("FontDescriptor") or {}).get("Flags") or 0)
    symbolic = bool(flags & 4) and not bool(flags & 32)
    if not symbolic and len(ch.text) == 1 and (3, 1) in tables:
        g = tables[(3, 1)].get(ord(ch.text))
        if g:
            return g
    gname = _simple_glyph_name(top, ch.cid, None)
    if gname and gname in order and not symbolic:
        return gname
    for key in ((3, 0), (1, 0), (3, 1)):
        t = tables.get(key)
        if t:
            for c in (ch.cid, 0xF000 + ch.cid, 0xF100 + ch.cid, 0xF200 + ch.cid):
                if t.get(c):
                    return t[c]
    return gname if gname in order else None


def _simple_glyph_name(top: dict, code: int, cff) -> str | None:
    from pdfminer.latin_enc import ENCODING

    enc = top.get("Encoding")
    base = None
    diffs = None
    if isinstance(enc, str):
        base = enc
    elif isinstance(enc, dict):
        base = enc.get("BaseEncoding")
        diffs = enc.get("Differences")
    if diffs:
        cur = 0
        name = None
        for item in diffs:
            if isinstance(item, (int, float)):
                cur = int(item)
            else:
                if cur == code:
                    name = item
                cur += 1
        if name:
            return name
    col = {"StandardEncoding": 1, "MacRomanEncoding": 2, "WinAnsiEncoding": 3}.get(base or "")
    if col is None and cff is not None:
        e = getattr(cff, "Encoding", None)
        if isinstance(e, list) and 0 <= code < len(e):
            return e[code]
        col = 1
    if col is None:
        col = 1
    for row in ENCODING:
        if row[col] == code:
            return row[0]
    if base == "WinAnsiEncoding" and code == 0xA0:
        return "space"
    return None


def _type1_names(data: bytes) -> set[str]:
    if data[:1] == b"\x80":
        out = b""
        i = 0
        while i + 6 <= len(data) and data[i] == 0x80 and data[i + 1] in (1, 2):
            n = int.from_bytes(data[i + 2:i + 6], "little")
            out += data[i + 6:i + 6 + n]
            i += 6 + n
        data = out
    idx = data.find(b"eexec")
    if idx < 0:
        return set()
    body = data[idx + 5:].lstrip(b"\r\n \t")
    r = 55665
    plain = bytearray()
    for byte in body:
        plain.append(byte ^ (r >> 8))
        r = ((byte + r) * 52845 + 22719) & 0xFFFF
    plain = bytes(plain[4:])
    pos = plain.find(b"/CharStrings")
    if pos < 0:
        return set()
    return {m.group(1).decode("latin-1") for m in re.finditer(rb"/([^\s/\[\]{}()<>]+)\s+\d+\s+(?:RD|-\|)\s", plain[pos:])}
