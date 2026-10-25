"""AcroForm text fields: listing values and updating one value (method e)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

import pikepdf

from . import geometry as G
from .content import Interpreter
from .document import Document
from .edit import EditPlan, Insertion, _FlowRewriter, _num_obj
from .errors import DoesNotFitError, MissingGlyphsError, PdfOpsError, SelfCheckError, TargetNotFoundError, UnsupportedError
from .fonts import PdfFont, advance_of_codes
from .methods import clean_replacement

FF_MULTILINE = 1 << 12
FF_PASSWORD = 1 << 13
FF_COMB = 1 << 24
FF_READONLY = 1


@dataclass
class FieldInfo:
    name: str
    value: str
    field_type: str
    flags: int
    widgets: list  # [(page_index, widget object, rect)]
    obj: pikepdf.Object
    da: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "type": self.field_type,
            "flags": self.flags,
            "widgets": [{"page": p + 1, "rect": G.Rect.from_list(r).as_list()} for p, _, r in self.widgets],
        }


def _text(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, pikepdf.String):
        return str(obj)
    if isinstance(obj, pikepdf.Name):
        return str(obj)[1:]
    return str(obj)


def _inherited(field, key):
    node = field
    for _ in range(32):
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _full_name(field) -> str:
    parts = []
    node = field
    for _ in range(32):
        if node is None:
            break
        if "/T" in node:
            parts.append(_text(node.T))
        node = node.get("/Parent")
    return ".".join(reversed(parts))


def list_fields(doc: Document) -> list[FieldInfo]:
    pdf = doc.pdf
    acro = pdf.Root.get("/AcroForm")
    if acro is None:
        return []
    page_of = {}
    for i, page in enumerate(pdf.pages):
        for annot in page.obj.get("/Annots", []) or []:
            if annot.is_indirect:
                page_of[annot.objgen] = i
    out: dict[str, FieldInfo] = {}

    def visit(node, depth=0):
        if depth > 32:
            return
        kids = node.get("/Kids")
        is_terminal = kids is None or all("/T" not in k for k in kids)
        if not is_terminal:
            for k in kids:
                visit(k, depth + 1)
            return
        name = _full_name(node)
        widgets = []
        for w in (kids if kids is not None else [node]):
            rect = [float(v) for v in w.get("/Rect", [0, 0, 0, 0])]
            page_index = page_of.get(w.objgen) if w.is_indirect else None
            if page_index is None and "/P" in w:
                for i, page in enumerate(pdf.pages):
                    if page.obj.objgen == w.P.objgen:
                        page_index = i
                        break
            widgets.append((page_index if page_index is not None else -1, w, rect))
        ft = _inherited(node, "/FT")
        out[name] = FieldInfo(
            name=name, value=_text(_inherited(node, "/V")), field_type=str(ft)[1:] if ft is not None else "",
            flags=int(_inherited(node, "/Ff") or 0), widgets=widgets, obj=node,
            da=_text(_inherited(node, "/DA") or acro.get("/DA")),
        )

    for f in acro.get("/Fields", []) or []:
        visit(f)
    return list(out.values())


def find_field(doc: Document, name: str) -> FieldInfo:
    for f in list_fields(doc):
        if f.name == name:
            return f
    raise TargetNotFoundError(f"form field {name!r} not found")


_DA_TF = re.compile(r"/([^\s/]+)\s+([-+]?[0-9]*\.?[0-9]+)\s+Tf")


def _parse_da(da: str):
    m = _DA_TF.search(da)
    if not m:
        raise UnsupportedError(f"cannot read the field's default appearance {da!r}")
    color = da[m.end():].strip() or "0 g"
    return m.group(1), Decimal(m.group(2)), color


def update_form_field(doc: Document, field_name: str, new_value: str, min_scale: float = 0.9) -> dict:
    """Method e: set a text field's value and regenerate its appearance streams.

    The existing appearance stream is copied and only its text is rewritten
    (borders, backgrounds and clipping stay byte-identical).  Returns a dict
    describing the change.
    """
    info = find_field(doc, field_name)
    if info.field_type != "Tx":
        raise UnsupportedError(f"field {field_name!r} is a {info.field_type or 'unknown'} field; only text fields are supported")
    if info.flags & (FF_COMB | FF_PASSWORD):
        raise UnsupportedError("comb and password fields are not supported")
    value = clean_replacement(new_value) if not info.flags & FF_MULTILINE else new_value
    acro = doc.pdf.Root.AcroForm
    results = []
    for page_index, widget, rect in info.widgets:
        results.append(_regenerate(doc, info, widget, rect, value, acro, min_scale, info.value))
    info.obj.V = pikepdf.String(value)
    return {
        "status": "ok",
        "method": "form_field_update",
        "field": info.name,
        "old_value": info.value,
        "new_value": value,
        "widgets": results,
    }


def _regenerate(doc, info: FieldInfo, widget, rect, value: str, acro, min_scale: float, old_value: str = "") -> dict:
    r = G.Rect.from_list(rect)
    ap = widget.get("/AP")
    normal = ap.get("/N") if ap is not None else None
    if normal is not None and not isinstance(normal, pikepdf.Stream):
        raise UnsupportedError("the field's appearance has several states; not a plain text field")
    da = _text(widget.get("/DA")) or info.da
    font_name, da_size, _ = _parse_da(da)
    interp = Interpreter(doc.pdf, doc.fonts)
    if normal is None:
        raise UnsupportedError("the field has no appearance stream to regenerate from")
    model = interp.interpret_form(normal)
    glyphs = [g for g in model.glyphs]
    if not glyphs:
        raise UnsupportedError("the field's appearance stream draws no text to replace")
    if len({g.font for g in glyphs}) != 1 or len({g.size for g in glyphs}) != 1:
        raise UnsupportedError("the field appearance mixes fonts or sizes")
    if len({round(g.y, 2) for g in glyphs if not g.text.isspace()}) > 1:
        return _regenerate_multiline(doc, interp, widget, r, normal, model, value, old_value)
    first = glyphs[0]
    font: PdfFont = first.font
    missing = font.missing_chars(value)
    if missing:
        raise MissingGlyphsError(f"the field font {font.base_font} does not contain: " + ", ".join(map(repr, missing)),
                                 missing=missing, font=font.base_font)
    codes = font.encode(value)
    bbox = normal.get("/BBox", [0, 0, r.width, r.height])
    bx0, by0, bx1, by1 = (float(v) for v in bbox)
    pad = max(first.x - bx0, 1.0)
    avail = (bx1 - bx0) - 2 * pad
    size = first.size
    space_adv = font.width(font.space_code) * size if font.space_code is not None else Decimal("0.25") * size
    width = float(advance_of_codes(font, codes, size, first.tc, first.tw, first.th, space_adv)) * first.x_scale
    new_size = None
    if width > avail:
        scale = Decimal(str(avail / width))
        if scale < Decimal(str(min_scale)):
            raise DoesNotFitError(
                f"the new value needs {width:.1f} pt but the field is {avail:.1f} pt wide "
                f"(it would need {float(scale):.0%} of the font size; the minimum is {min_scale:.0%})")
        new_size = (size * scale).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        width = width * float(new_size / size)
    q = int(_inherited(info.obj, "/Q") or acro.get("/Q", 0) or 0)
    old_width = max(g.x_end for g in glyphs) - first.x
    if q == 1:
        dx_start = ((bx1 - bx0) - width) / 2 - (first.x - bx0)
    elif q == 2:
        dx_start = (bx1 - pad - width) - first.x
    else:
        dx_start = 0.0
    plan = EditPlan(-1, remove={g.gid for g in glyphs})
    key = first.stream
    plan.inserts.append(Insertion(first.gid, codes, new_size, space_adv / size * (new_size or size), value))
    expected: list = []
    instrs = _FlowRewriter(model, key, plan, expected).rewrite()
    if abs(dx_start) > 1e-9:
        instrs = _shift_first_tj(instrs, first, dx_start, new_size if new_size is not None else size)
    _install_appearance(doc, interp, widget, normal, instrs, value, (bx0, by0, bx1, by1))
    return {"rect": r.as_list(), "font": font.base_font, "font_size": float(size),
            "new_font_size": float(new_size if new_size is not None else size), "alignment": q,
            "old_width": round(old_width, 3), "new_width": round(width, 3)}


def _install_appearance(doc, interp, widget, normal, instrs, value: str, bbox) -> None:
    """Write the new appearance as a new stream on this widget, after checking it."""
    bx0, by0, bx1, by1 = bbox
    data = pikepdf.unparse_content_stream(instrs)
    new_ap = doc.pdf.make_stream(data)
    for k in normal.keys():
        if k in ("/Length", "/Filter", "/DecodeParms"):
            continue
        new_ap[k] = normal[k]
    check = interp.interpret_form(new_ap)
    got = "".join(g.text for g in check.glyphs)
    if " ".join(got.split()) != " ".join(value.split()):
        raise SelfCheckError(f"regenerated appearance shows {got[:200]!r}, expected {value[:200]!r}")
    for g in check.glyphs:
        if not g.text.isspace() and (g.bbox.x1 > bx1 + 0.5 or g.bbox.x0 < bx0 - 0.5):
            raise SelfCheckError("regenerated appearance text runs outside the field box")
    widget.AP = pikepdf.Dictionary({k: widget.AP[k] for k in widget.AP.keys()}) if widget.get("/AP") is not None \
        else pikepdf.Dictionary()
    widget.AP.N = new_ap


def _diff_segment(old: str, new: str) -> tuple[int, int, str]:
    """The changed part of ``old`` as whole words: (start, end, new text for it)."""
    n = min(len(old), len(new))
    i = 0
    while i < n and old[i] == new[i]:
        i += 1
    j = 0
    while j < n - i and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
        j += 1
    os_, oe, ns, ne = i, len(old) - j, i, len(new) - j
    # widen to whole words only where the change starts or ends inside a word
    while os_ > 0 and os_ < len(old) and not old[os_].isspace() and not old[os_ - 1].isspace():
        os_ -= 1
        ns -= 1
    while oe < len(old) and oe > 0 and not old[oe - 1].isspace() and not old[oe].isspace():
        oe += 1
        ne += 1
    if not old[os_:oe].strip():
        # only whitespace changed or a pure insertion: anchor on the following word
        while oe < len(old) and old[oe].isspace():
            oe += 1
            ne += 1
        while oe < len(old) and not old[oe].isspace():
            oe += 1
            ne += 1
    return os_, oe, new[ns:ne]


def _regenerate_multiline(doc, interp, widget, r, normal, model, value: str, old_value: str) -> dict:
    """Multi-line text field: replace the changed words in the existing appearance.

    The rest of the changed line moves by the width difference; other lines are
    untouched.  If the line would pass the field's edge the field text would
    need rewrapping, which is left to a person.
    """
    glyphs = model.glyphs
    ink = [g for g in glyphs if not g.text.isspace()]
    value_chars = [k for k, ch in enumerate(old_value) if not ch.isspace()]
    if "".join(g.text for g in ink) != "".join(old_value[k] for k in value_chars):
        raise UnsupportedError("the field's appearance does not show its current value; it cannot be updated safely")
    if len({g.font for g in ink}) != 1 or len({g.size for g in ink}) != 1:
        raise UnsupportedError("the field appearance mixes fonts or sizes")
    os_, oe, new_seg = _diff_segment(old_value, value)
    if new_seg != new_seg.strip("\r\n") or "\r" in new_seg or "\n" in new_seg:
        raise UnsupportedError("the change adds or removes line breaks inside the field")
    ks = [n for n, k in enumerate(value_chars) if os_ <= k < oe]
    if not ks:
        raise UnsupportedError("could not locate the changed words in the field's appearance")
    g_first, g_last = ink[ks[0]], ink[ks[-1]]
    last_gid = g_last.gid
    if oe > os_ and old_value[oe - 1].isspace():
        # the removed words end with a space: remove the space glyphs up to the next word as well
        nxt = ink[ks[-1] + 1].gid if ks[-1] + 1 < len(ink) else len(glyphs)
        last_gid = nxt - 1
    removed = [g for g in glyphs if g_first.gid <= g.gid <= last_gid]
    line_y = round(g_first.y, 2)
    if any(round(g.y, 2) != line_y for g in removed if not g.text.isspace()):
        raise UnsupportedError("the change spans two lines of the field; the field text would need rewrapping")
    font: PdfFont = g_first.font
    missing = font.missing_chars(new_seg)
    if missing:
        raise MissingGlyphsError(f"the field font {font.base_font} does not contain: " + ", ".join(map(repr, missing)),
                                 missing=missing, font=font.base_font)
    codes = font.encode(new_seg) if new_seg else []
    size = g_first.size
    space_adv = font.width(font.space_code) * size if font.space_code is not None else Decimal("0.25") * size
    new_width = float(advance_of_codes(font, codes, size, g_first.tc, g_first.tw, g_first.th, space_adv)) * g_first.x_scale
    after = [g for g in glyphs if g.gid > last_gid and round(g.y, 2) == line_y]
    after_ink = [g for g in after if not g.text.isspace()]
    old_span_end = after[0].x if after else max(g.x_end for g in removed)
    delta = new_width - (old_span_end - g_first.x)
    bbox = normal.get("/BBox", [0, 0, r.width, r.height])
    bx0, by0, bx1, by1 = (float(v) for v in bbox)
    pad = max(min(g.x for g in ink) - bx0, 1.0)
    line_end = (max(g.x_end for g in after_ink) if after_ink else g_first.x + new_width) + (delta if after_ink else 0)
    if line_end > bx1 - pad + 0.01:
        raise DoesNotFitError(f"the changed line would end at {line_end:.1f} pt, past the field edge at {bx1 - pad:.1f} pt; "
                              "the field text would need rewrapping")
    plan = EditPlan(-1, remove={g.gid for g in removed})
    plan.inserts.append(Insertion(g_first.gid, codes, None, space_adv, new_seg))
    if abs(delta) > 1e-9:
        for g in after:
            plan.shift[g.gid] = delta
    expected: list = []
    instrs = _FlowRewriter(model, g_first.stream, plan, expected).rewrite()
    _install_appearance(doc, interp, widget, normal, instrs, value, (bx0, by0, bx1, by1))
    return {"rect": r.as_list(), "font": font.base_font, "font_size": float(size), "new_font_size": float(size),
            "alignment": 0, "multiline": True, "changed_words": {"old": old_value[os_:oe], "new": new_seg},
            "line_shift": round(delta, 3), "old_width": round(old_span_end - g_first.x, 3), "new_width": round(new_width, 3)}


def _shift_first_tj(instrs: list, first, dx: float, size: Decimal) -> list:
    """Prepend a kerning number to the TJ that now starts at the anchor (moves the whole value)."""
    out = list(instrs)
    for i, ins in enumerate(out):
        if isinstance(ins, pikepdf.ContentStreamInstruction) and str(ins.operator) == "TJ":
            arr = list(ins.operands[0])
            n = Decimal(str(-dx / first.x_scale)) * 1000 / (size * first.th)
            out[i] = pikepdf.ContentStreamInstruction([pikepdf.Array([_num_obj(n)] + arr)], pikepdf.Operator("TJ"))
            return out
    raise PdfOpsError("could not position the regenerated field text")
