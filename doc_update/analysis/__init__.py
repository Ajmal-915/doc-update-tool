"""Step 1: plain-code analysis of the current document version."""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

import pdf_ops
from pdf_ops.content import PageModel
from pdf_ops.forms import list_fields
from pdf_ops.geometry import Rect
from pdf_ops.layout import PageLayout

from ..checks.structure import _is_signed


@dataclass
class DocumentAnalysis:
    path: str
    page_count: int
    encrypted: bool
    signed: bool
    signature_fields: list
    fields: list
    pages: dict = field(default_factory=dict)  # page_index -> page summary
    fonts: dict = field(default_factory=dict)  # base font -> description
    shared_streams: dict = field(default_factory=dict)  # stream objgen -> pages using it

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "page_count": self.page_count,
            "encrypted": self.encrypted,
            "signed": self.signed,
            "signature_fields": self.signature_fields,
            "fields": self.fields,
            "fonts": self.fonts,
            "shared_content_streams": [{"pages": [p + 1 for p in v]} for v in self.shared_streams.values()],
            "pages": {str(k + 1): v for k, v in self.pages.items()},
        }


def _stream_structure(model: PageModel) -> dict:
    counts = collections.Counter()
    tj_kerning = 0
    for info in model.streams.values():
        for ins in info.instructions:
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                counts["inline_image"] += 1
                continue
            op = str(ins.operator)
            if op in ("BT", "Tm", "Td", "TD", "T*", "Tj", "TJ", "'", '"', "Do", "Tf", "Tw", "Tc", "Tz"):
                counts[op] += 1
            if op == "TJ" and ins.operands and isinstance(ins.operands[0], pikepdf.Array) and \
                    any(not isinstance(e, pikepdf.String) for e in ins.operands[0]):  # malformed streams exist
                tj_kerning += 1
    xobjects = []
    for key, info in model.streams.items():
        if info.parent is not None:
            xobjects.append({"name": info.name, "nesting": (len(key) - 2) // 3, "uses_on_page": info.invocations,
                             "has_text": any(g.stream == key for g in model.glyphs)})
    page_streams = [k for k in model.streams if len(k) == 2]
    return {
        "content_streams": len(page_streams),
        "text_blocks": counts["BT"],
        "operators": {k: v for k, v in counts.items()},
        "tj_with_kerning": tj_kerning,
        "form_xobjects": xobjects,
        "mac_style_1_tf": any(g.size == 1 and g.effective_size > 2 for g in model.glyphs),
    }


def _shared_objects(pdf: pikepdf.Pdf) -> dict:
    """Content streams and XObjects referenced from more than one page."""
    stream_pages = collections.defaultdict(set)
    for i, page in enumerate(pdf.pages):
        c = page.obj.get("/Contents")
        items = list(c) if isinstance(c, pikepdf.Array) else ([c] if c is not None else [])
        for s in items:
            if s.is_indirect:
                stream_pages[s.objgen].add(i)
    shared = {og: sorted(p) for og, p in stream_pages.items() if len(p) > 1}
    return shared


def page_summary(doc: pdf_ops.Document, page_index: int, include_chars: bool = True) -> dict:
    model = doc.model(page_index)
    layout: PageLayout = doc.layout(page_index)
    page = doc.pdf.pages[page_index]
    mb = [float(v) for v in page.mediabox]
    words = []
    for line in layout.lines:
        for w in line.words:
            g0 = model.glyphs[w.glyphs[0]]
            words.append({"text": w.text, "bbox": w.bbox.as_list(2), "line": line.index, "block": line.block,
                          "font": g0.font.base_font, "size": round(g0.effective_size, 2),
                          "color": [g0.fill_cs, [round(float(c), 3) if isinstance(c, float) else c for c in g0.fill]]})
    chars = [g.to_dict() for g in model.glyphs] if include_chars else []
    page_area = max(1.0, (mb[2] - mb[0]) * (mb[3] - mb[1]))
    image_cover = 0.0
    for im in model.images:
        r = im.bbox
        image_cover = max(image_cover, max(0.0, r.width) * max(0.0, r.height) / page_area)
    visible = [g for g in model.glyphs if g.visible and not g.text.isspace()]
    invisible = [g for g in model.glyphs if not g.visible]
    scanned = image_cover > 0.6 and len(visible) < 20
    fonts = {}
    for g in model.glyphs:
        fonts.setdefault(g.font.base_font, g.font)
    annots = []
    for a in page.obj.get("/Annots", []) or []:
        annots.append({"subtype": str(a.get("/Subtype", ""))[1:], "rect": Rect.from_list([float(v) for v in a.get("/Rect", [0, 0, 0, 0])]).as_list(),
                       "field": str(a.get("/T", "")) if "/T" in a else None})
    graphics = [g.to_dict() for g in model.graphics if g.kind != "shading"]
    return {
        "page": page_index + 1,
        "mediabox": mb,
        "rotate": int(page.obj.get("/Rotate", 0)),
        "words": words,
        "chars": chars,
        "lines": [{"index": ln.index, "text": ln.text, "bbox": ln.bbox.as_list(2), "baseline": round(ln.baseline, 3),
                   "size": round(ln.size, 3), "block": ln.block, "fonts": ln.fonts} for ln in layout.lines],
        "blocks": [{"index": b.index, "lines": b.lines, "bbox": b.bbox.as_list(2), "leading": round(b.leading, 3),
                    "alignment": b.alignment, "left": round(b.left, 2), "right": round(b.right, 2)} for b in layout.blocks],
        "structure": _stream_structure(model),
        "fonts": {name: f.describe() for name, f in fonts.items()},
        "images": [{"name": im.name, "bbox": im.bbox.as_list(2), "pixels": [im.width_px, im.height_px]} for im in model.images],
        "graphics": graphics,
        "annotations": annots,
        "image_coverage": round(image_cover, 3),
        "visible_glyphs": len(visible),
        "invisible_glyphs": len(invisible),
        "scanned": scanned,
        "ocr_layer": scanned and len(invisible) > 0,
        "warnings": model.warnings[:20],
        "unsupported": sorted(set(model.unsupported)),
    }


def analyse(path: str | Path, pages: list[int] | None = None, include_chars: bool = True) -> tuple[DocumentAnalysis, pdf_ops.Document]:
    """Analyse the given pages (0-based; default all) of the PDF at ``path``."""
    doc = pdf_ops.open(path)
    pdf = doc.pdf
    fields = []
    sig_fields = []
    try:
        for f in list_fields(doc):
            d = f.to_dict()
            fields.append(d)
            if f.field_type == "Sig":
                sig_fields.append({"name": f.name, "signed": bool(f.value)})
    except Exception as exc:  # malformed forms must not break analysis
        fields.append({"error": str(exc)})
    da = DocumentAnalysis(
        path=str(path), page_count=len(pdf.pages), encrypted=pdf.is_encrypted, signed=_is_signed(pdf),
        signature_fields=sig_fields, fields=fields,
    )
    da.shared_streams = _shared_objects(pdf)
    for i in (range(len(pdf.pages)) if pages is None else pages):
        if 0 <= i < len(pdf.pages):
            da.pages[i] = page_summary(doc, i, include_chars)
            da.fonts.update(da.pages[i]["fonts"])
    return da, doc
