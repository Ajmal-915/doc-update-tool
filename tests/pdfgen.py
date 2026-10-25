"""Builds synthetic test PDFs with full control over the content streams.

Everything is written by hand with pikepdf (no PDF-generation library), so the
tests can exercise exactly the constructs real documents use:

* subset-embedded TrueType fonts with WinAnsi, MacRoman or custom Differences
  encodings, Type0 / Identity-H fonts, and non-embedded Helvetica;
* Td / TD / T* / ' / " positioning, several lines per BT block, TJ kerning,
  "1 Tf" with the real size in the text matrix (as Quartz on macOS writes);
* form XObjects (shared between pages), several content streams per page,
  shared content streams, tables, dot leaders, form fields, signatures and
  scanned pages.
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pikepdf
from fontTools import subset as ft_subset
from fontTools.ttLib import TTFont

from pdf_ops.encodings import MAC_ROMAN, WIN_ANSI, glyph_to_unicode

FONT_DIR = Path(__file__).parent / "data" / "fonts"
REGULAR = FONT_DIR / "LiberationSans-Regular.ttf"
BOLD = FONT_DIR / "LiberationSans-Bold.ttf"

_TTF_CACHE: dict = {}


def _load_ttf(path: Path) -> TTFont:
    if path not in _TTF_CACHE:
        _TTF_CACHE[path] = TTFont(str(path))
    return _TTF_CACHE[path]


def _num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def pdf_string(data: bytes) -> bytes:
    out = bytearray(b"(")
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += b"\\%03o" % b
        else:
            out.append(b)
    out += b")"
    return bytes(out)


@dataclass
class FontInfo:
    res_name: str
    obj: pikepdf.Object
    kind: str  # "simple" | "type0" | "std"
    code_of: dict = field(default_factory=dict)  # char -> code
    width_of: dict = field(default_factory=dict)  # code -> width (1/1000 em)
    space_code: int | None = 32

    def encode(self, text: str) -> bytes:
        if self.kind == "type0":
            return b"".join(self.code_of[c].to_bytes(2, "big") for c in text)
        return bytes(self.code_of[c] for c in text)

    def lit(self, text: str) -> bytes:
        data = self.encode(text)
        if self.kind == "type0":
            return b"<" + data.hex().upper().encode() + b">"
        return pdf_string(data)

    def width(self, text: str, size: float) -> float:
        return sum(self.width_of[self.code_of[c]] for c in text) * size / 1000.0


class Builder:
    def __init__(self) -> None:
        self.pdf = pikepdf.new()
        self._counter = 0

    # -------------------------------------------------------------- fonts
    def _tag(self) -> str:
        self._counter += 1
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        n = self._counter
        tag = ""
        for _ in range(6):
            tag += letters[n % 26]
            n //= 26
        return tag

    def _subset_program(self, path: Path, chars: str, keep_order=False) -> tuple[bytes, TTFont]:
        font = TTFont(str(path))
        opts = ft_subset.Options()
        opts.notdef_outline = True
        opts.glyph_names = True
        opts.layout_features = []
        opts.name_IDs = ["*"]
        opts.drop_tables += ["GSUB", "GPOS", "GDEF", "kern", "DSIG"]
        opts.retain_gids = keep_order
        sub = ft_subset.Subsetter(opts)
        sub.populate(unicodes=sorted({ord(c) for c in chars} | {32}))
        sub.subset(font)
        buf = io.BytesIO()
        font.save(buf)
        data = buf.getvalue()
        return data, TTFont(io.BytesIO(data))

    def _descriptor(self, base: str, full: TTFont, program: bytes, symbolic=False):
        upm = full["head"].unitsPerEm
        scale = 1000.0 / upm
        hhea = full["hhea"]
        head = full["head"]
        os2 = full["OS/2"]
        return self.pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor,
            FontName=pikepdf.Name("/" + base),
            Flags=4 if symbolic else 32,
            FontBBox=[int(head.xMin * scale), int(head.yMin * scale), int(head.xMax * scale), int(head.yMax * scale)],
            ItalicAngle=0,
            Ascent=int(hhea.ascent * scale),
            Descent=int(hhea.descent * scale),
            CapHeight=int(getattr(os2, "sCapHeight", 700) * scale),
            StemV=80,
            FontFile2=self.pdf.make_stream(program),
        ))

    def font_truetype(self, res_name: str, chars: str, encoding: str = "WinAnsiEncoding", path: Path = REGULAR,
                      differences: bool = False, tounicode: bool = False) -> FontInfo:
        """Subset TrueType font.  ``differences`` assigns codes 1..N through /Differences."""
        full = _load_ttf(path)
        upm = full["head"].unitsPerEm
        cmap = full.getBestCmap()
        hmtx = full["hmtx"]
        program, _ = self._subset_program(path, chars)
        base = self._tag() + "+" + full["name"].getDebugName(6)
        info = FontInfo(res_name, None, "simple")
        if differences:
            ordered = list(dict.fromkeys(" " + chars))
            diff = [1]
            for i, ch in enumerate(ordered):
                gname = full.getGlyphOrder()[full.getGlyphID(cmap[ord(ch)])]
                # post names in Liberation are standard AGL names
                diff.append(pikepdf.Name("/" + gname))
                info.code_of[ch] = i + 1
            info.space_code = info.code_of[" "]
            first, last = 1, len(ordered)
            widths = []
            for code in range(first, last + 1):
                ch = ordered[code - 1]
                widths.append(round(hmtx[cmap[ord(ch)]][0] * 1000 / upm))
                info.width_of[code] = widths[-1]
            enc_obj = pikepdf.Dictionary(Type=pikepdf.Name.Encoding, Differences=diff)
        else:
            table = WIN_ANSI if encoding == "WinAnsiEncoding" else MAC_ROMAN
            first, last = 32, 255
            widths = []
            for code in range(first, last + 1):
                name = table.get(code)
                u = glyph_to_unicode(name) if name else ""
                w = 0
                if u and len(u) == 1 and ord(u) in cmap:
                    w = round(hmtx[cmap[ord(u)]][0] * 1000 / upm)
                    info.code_of.setdefault(u, code)
                widths.append(w)
                info.width_of[code] = w
            enc_obj = pikepdf.Name("/" + encoding)
        font = pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name("/" + base),
            FirstChar=first, LastChar=last, Widths=widths, Encoding=enc_obj,
            FontDescriptor=self._descriptor(base, full, program),
        )
        if tounicode:
            font.ToUnicode = self.pdf.make_stream(_tounicode_cmap({c: ch for ch, c in info.code_of.items()}, 1))
        info.obj = self.pdf.make_indirect(font)
        return info

    def font_type0(self, res_name: str, chars: str, path: Path = REGULAR) -> FontInfo:
        full = _load_ttf(path)
        program, sub = self._subset_program(path, chars)
        upm = sub["head"].unitsPerEm
        order = sub.getGlyphOrder()
        cmap = sub.getBestCmap()
        info = FontInfo(res_name, None, "type0")
        w = []
        to_u = {}
        for ch in sorted(set(chars) | {" "}):
            gname = cmap.get(ord(ch))
            if gname is None:
                continue
            cid = order.index(gname)
            info.code_of[ch] = cid
            info.width_of[cid] = round(sub["hmtx"][gname][0] * 1000 / upm)
            to_u[cid] = ch
        for cid in sorted(info.width_of):
            w += [cid, [info.width_of[cid]]]
        info.space_code = info.code_of.get(" ")
        base = self._tag() + "+" + full["name"].getDebugName(6)
        cidfont = pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2, BaseFont=pikepdf.Name("/" + base),
            CIDSystemInfo=pikepdf.Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0),
            FontDescriptor=self._descriptor(base, full, program, symbolic=True), DW=1000, W=w,
            CIDToGIDMap=pikepdf.Name.Identity,
        )
        font = pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0, BaseFont=pikepdf.Name("/" + base),
            Encoding=pikepdf.Name("/Identity-H"), DescendantFonts=[self.pdf.make_indirect(cidfont)],
            ToUnicode=self.pdf.make_stream(_tounicode_cmap(to_u, 2)),
        )
        info.obj = self.pdf.make_indirect(font)
        return info

    def font_helvetica(self, res_name: str, base: str = "Helvetica") -> FontInfo:
        from pdfminer.fontmetrics import FONT_METRICS

        _, widths = FONT_METRICS[base]
        info = FontInfo(res_name, None, "std")
        for code, name in WIN_ANSI.items():
            u = glyph_to_unicode(name)
            if u and len(u) == 1 and u in widths:
                info.code_of.setdefault(u, code)
                info.width_of[code] = widths[u]
        info.obj = self.pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name("/" + base),
            Encoding=pikepdf.Name.WinAnsiEncoding))
        return info

    # -------------------------------------------------------------- pages
    def resources(self, fonts: list[FontInfo], xobjects: dict | None = None, extra: dict | None = None):
        res = pikepdf.Dictionary(Font=pikepdf.Dictionary({"/" + f.res_name: f.obj for f in fonts}))
        if xobjects:
            res.XObject = pikepdf.Dictionary({"/" + k: v for k, v in xobjects.items()})
        if extra:
            for k, v in extra.items():
                res[k] = v
        return res

    def page(self, content: bytes | list, resources, size=(612, 792), indirect_resources=False):
        if isinstance(content, list):
            contents = pikepdf.Array([c if isinstance(c, pikepdf.Object) else self.pdf.make_stream(c) for c in content])
        elif isinstance(content, pikepdf.Object):
            contents = content
        else:
            contents = self.pdf.make_stream(content)
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=[0, 0, size[0], size[1]], Contents=contents,
            Resources=self.pdf.make_indirect(resources) if indirect_resources else resources,
        )
        self.pdf.pages.append(pikepdf.Page(page))
        return self.pdf.pages[-1]

    def form_xobject(self, content: bytes, resources, bbox=(0, 0, 612, 792)):
        return self.pdf.make_stream(content, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form,
                                    BBox=list(bbox), Resources=resources)

    def save(self, path) -> Path:
        path = Path(path)
        self.pdf.save(path)
        return path


def _tounicode_cmap(mapping: dict[int, str], nbytes: int) -> bytes:
    lines = [
        b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def", b"1 begincodespacerange",
        (b"<00> <FF>" if nbytes == 1 else b"<0000> <FFFF>"), b"endcodespacerange",
    ]
    items = sorted(mapping.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(b"%d beginbfchar" % len(chunk))
        for code, ch in chunk:
            lines.append(b"<" + code.to_bytes(nbytes, "big").hex().upper().encode() + b"> <" + ch.encode("utf-16-be").hex().upper().encode() + b">")
        lines.append(b"endbfchar")
    lines += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(lines)


# ---------------------------------------------------------------- layout
def wrap(font: FontInfo, text: str, size: float, width: float) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if cur and font.width(trial, size) > width:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def paragraph_ops(font: FontInfo, lines: list[str], x: float, y: float, size: float, leading: float,
                  style: str = "td", justify_width: float | None = None, color: bytes = b"0 g") -> bytes:
    """Content for one paragraph in several real-world styles.

    td     : one BT block, first line with Td, then "0 -L Td" per line
    tstar  : TL + T* between lines
    quote  : ' operator for following lines
    mac    : "1 Tf" with size in Tm, every line repositioned with Tm, TJ kerning
    lines  : one BT/ET block per line (Word style)
    words  : every word positioned separately, no space glyphs
    """
    f = font.res_name.encode()
    out = []
    if style == "lines":
        for i, line in enumerate(lines):
            tw = b""
            if justify_width and i < len(lines) - 1 and line.count(" "):
                extra = (justify_width - font.width(line, size)) / line.count(" ")
                tw = _num(extra).encode() + b" Tw "
            out.append(b"BT " + color + b" /" + f + b" " + _num(size).encode() + b" Tf " + tw + _num(x).encode() + b" "
                       + _num(y - i * leading).encode() + b" Td " + font.lit(line) + b" Tj ET")
        return b"\n".join(out)
    out.append(b"BT")
    out.append(color)
    if style == "mac":
        out.append(b"/" + f + b" 1 Tf")
        for i, line in enumerate(lines):
            out.append(b"%s 0 0 %s %s %s Tm" % (_num(size).encode(), _num(size).encode(), _num(x).encode(), _num(y - i * leading).encode()))
            out.append(_kerned_tj(font, line))
        out.append(b"ET")
        return b"\n".join(out)
    out.append(b"/" + f + b" " + _num(size).encode() + b" Tf")
    if style in ("tstar", "quote"):
        out.append(_num(leading).encode() + b" TL")
    for i, line in enumerate(lines):
        if style == "words":
            words = line.split(" ")
            sp = font.width(" ", size)
            cx = x
            for j, w in enumerate(words):
                if i == 0 and j == 0:
                    out.append(b"%s %s Td" % (_num(x).encode(), _num(y).encode()))
                elif j == 0:
                    out.append(b"%s %s Td" % (_num(x - prev_x).encode(), _num(-leading).encode()))
                else:
                    out.append(b"%s 0 Td" % _num(cx - prev_x).encode())
                out.append(font.lit(w) + b" Tj")
                prev_x = cx
                cx += font.width(w, size) + sp
            continue
        if i == 0:
            out.append(b"%s %s Td" % (_num(x).encode(), _num(y).encode()))
            out.append(font.lit(line) + b" Tj")
        elif style == "td":
            out.append(b"0 %s Td" % _num(-leading).encode())
            out.append(font.lit(line) + b" Tj")
        elif style == "tstar":
            out.append(b"T*")
            out.append(font.lit(line) + b" Tj")
        elif style == "quote":
            out.append(font.lit(line) + b" '")
    out.append(b"ET")
    return b"\n".join(out)


def _kerned_tj(font: FontInfo, line: str) -> bytes:
    """TJ array with small kerning adjustments between some letter pairs."""
    parts = []
    chunk = ""
    for i, ch in enumerate(line):
        chunk += ch
        nxt = line[i + 1] if i + 1 < len(line) else ""
        if (ch, nxt) in {("T", "o"), ("A", "V"), ("W", "a"), ("y", "e"), ("r", "e"), ("o", "n")}:
            parts.append(font.lit(chunk))
            parts.append(b"-35" if ch != "r" else b"12")
            chunk = ""
    if chunk:
        parts.append(font.lit(chunk))
    return b"[" + b" ".join(parts) + b"] TJ"


def single_line(font: FontInfo, text: str, x: float, y: float, size: float, color: bytes = b"0 g") -> bytes:
    return b"BT " + color + b" /" + font.res_name.encode() + b" " + _num(size).encode() + b" Tf " + \
        _num(x).encode() + b" " + _num(y).encode() + b" Td " + font.lit(text) + b" Tj ET"


def rect_stroke(x0, y0, x1, y1, width=0.75) -> bytes:
    return b"q %s w 0 G %s %s %s %s re S Q" % tuple(_num(v).encode() for v in (width, x0, y0, x1 - x0, y1 - y0))


def hline(x0, x1, y, width=0.75) -> bytes:
    return b"q %s w 0 G %s %s m %s %s l S Q" % tuple(_num(v).encode() for v in (width, x0, y, x1, y))


def vline(x, y0, y1, width=0.75) -> bytes:
    return b"q %s w 0 G %s %s m %s %s l S Q" % tuple(_num(v).encode() for v in (width, x, y0, x, y1))


def filled_rect(x0, y0, x1, y1, gray=0.9) -> bytes:
    return b"q %s g %s %s %s %s re f Q" % tuple(_num(v).encode() for v in (gray, x0, y0, x1 - x0, y1 - y0))


def add_text_field(builder: Builder, page, name: str, value: str, rect, font: FontInfo, size=10):
    """AcroForm text field with a widget annotation and an appearance stream."""
    pdf = builder.pdf
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    dr = pikepdf.Dictionary(Font=pikepdf.Dictionary({"/" + font.res_name: font.obj}))
    ap_content = b"/Tx BMC q BT /" + font.res_name.encode() + b" " + _num(size).encode() + b" Tf 0 g 2 " + \
        _num((h - size) / 2 + 0.22 * size).encode() + b" Td " + font.lit(value) + b" Tj ET Q EMC"
    ap = pdf.make_stream(ap_content, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form, BBox=[0, 0, w, h],
                         Resources=dr)
    field_obj = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Tx, T=pikepdf.String(name),
        V=pikepdf.String(value), Rect=[x0, y0, x1, y1], F=4, P=page.obj,
        DA=pikepdf.String(f"/{font.res_name} {_num(size)} Tf 0 g"), AP=pikepdf.Dictionary(N=ap),
    ))
    if "/Annots" not in page.obj:
        page.obj.Annots = pdf.make_indirect(pikepdf.Array())
    page.obj.Annots.append(field_obj)
    root = pdf.Root
    if "/AcroForm" not in root:
        root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array(), DR=dr,
                                                              DA=pikepdf.String(f"/{font.res_name} 0 Tf 0 g")))
    root.AcroForm.Fields.append(field_obj)
    return field_obj


def add_signature(builder: Builder, page):
    """A signature field with a (structurally valid, cryptographically empty) value."""
    pdf = builder.pdf
    sig = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Sig, Filter=pikepdf.Name("/Adobe.PPKLite"), SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
        ByteRange=[0, 0, 0, 0], Contents=pikepdf.String(b"\x00" * 64), M=pikepdf.String("D:20240101000000Z"),
    ))
    widget = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Sig, T=pikepdf.String("Signature1"),
        V=sig, Rect=[400, 50, 550, 90], F=132, P=page.obj,
    ))
    if "/Annots" not in page.obj:
        page.obj.Annots = pikepdf.Array()
    page.obj.Annots.append(widget)
    if "/AcroForm" not in pdf.Root:
        pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array()))
    pdf.Root.AcroForm.Fields.append(widget)
    pdf.Root.AcroForm.SigFlags = 3


def image_xobject(builder: Builder, width=200, height=260, seed=7):
    """A grey 'scan' image with some dark text-like bars."""
    rows = []
    for y in range(height):
        row = bytearray([235] * width)
        if (y // 6) % 3 == 0 and 20 < y < height - 20:
            for x in range(20, width - 20):
                if ((x * 7 + y * 3 + seed) % 11) < 6:
                    row[x] = 40
        rows.append(bytes(row))
    data = zlib.compress(b"".join(rows))
    return builder.pdf.make_stream(data, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=width,
                                   Height=height, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8,
                                   Filter=pikepdf.Name.FlateDecode)
