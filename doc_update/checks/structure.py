"""Structural snapshots used by the checks: page boxes, fonts, annotations,
form fields, images and vector graphics (read with pikepdf and pdfplumber)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pikepdf

from pdf_ops.geometry import Rect

_SKIP_KEYS = {"/Parent", "/P", "/Length"}


def deep_hash(obj, _seen=None, depth=0, memo: dict | None = None) -> str:
    """Content hash of a PDF object graph (independent of object numbers).

    ``memo`` caches the hashes of indirect objects so shared fonts and
    resources are hashed once per document.
    """
    h = hashlib.sha256()
    _feed(h, obj, set() if _seen is None else _seen, depth, memo)
    return h.hexdigest()


def _feed(h, obj, seen, depth, memo=None):
    if depth > 40:
        h.update(b"<deep>")
        return
    if isinstance(obj, pikepdf.Object) and obj.is_indirect:
        og = obj.objgen
        if og in seen:
            h.update(b"<ref>")
            return
        if memo is not None:
            if og not in memo:
                sub = hashlib.sha256()
                memo[og] = "pending"
                _feed_body(sub, obj, seen | {og}, depth, memo)
                memo[og] = sub.hexdigest()
            if memo[og] != "pending":
                h.update(b"R" + memo[og].encode())
                return
        seen = seen | {og}
    _feed_body(h, obj, seen, depth, memo)


def _feed_body(h, obj, seen, depth, memo):
    if isinstance(obj, pikepdf.Stream):
        h.update(b"S")
        try:
            h.update(hashlib.sha256(obj.read_bytes()).digest())
        except Exception:
            h.update(hashlib.sha256(obj.read_raw_bytes()).digest())
        for k in sorted(obj.keys()):
            if k in _SKIP_KEYS or k in ("/Filter", "/DecodeParms"):
                continue
            h.update(k.encode())
            _feed(h, obj[k], seen, depth + 1, memo)
    elif isinstance(obj, pikepdf.Dictionary):
        h.update(b"D")
        for k in sorted(obj.keys()):
            if k in _SKIP_KEYS:
                continue
            h.update(k.encode())
            _feed(h, obj[k], seen, depth + 1, memo)
    elif isinstance(obj, pikepdf.Array):
        h.update(b"A")
        for v in obj:
            _feed(h, v, seen, depth + 1, memo)
    elif isinstance(obj, pikepdf.Name):
        h.update(b"N" + str(obj).encode())
    elif isinstance(obj, pikepdf.String):
        h.update(b"T" + bytes(obj))
    else:
        h.update(b"V" + repr(obj).encode())


def _inherited(node, key):
    for _ in range(64):
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


@dataclass
class Snapshot:
    page_boxes: list
    fonts_per_page: list[set]
    all_fonts: set
    font_objects: int
    annots_per_page: list[list]
    fields: dict
    images_per_page: list[list]
    graphics_per_page: list[list]
    signed: bool
    document_level: dict
    page_attributes: list[str]


def _fonts_of_resources(res, seen_res: set, out: set, objs: set, depth=0, memo=None):
    if res is None or depth > 12:
        return
    if res.is_indirect:
        if res.objgen in seen_res:
            return
        seen_res.add(res.objgen)
    fonts = res.get("/Font")
    if fonts is not None:
        for name in fonts.keys():
            f = fonts[name]
            fh = deep_hash(f, memo=memo)
            out.add(fh)
            objs.add(f.objgen if f.is_indirect else ("direct", fh))
    xo = res.get("/XObject")
    if xo is not None:
        for name in xo.keys():
            x = xo[name]
            if str(x.get("/Subtype", "")) == "/Form":
                _fonts_of_resources(x.get("/Resources"), seen_res, out, objs, depth + 1, memo)


def _annot_record(a) -> dict:
    rec = {
        "subtype": str(a.get("/Subtype", "")),
        "rect": tuple(round(float(v), 2) for v in a.get("/Rect", [])),
        "contents": str(a.get("/Contents", "")),
        "flags": int(a.get("/F", 0)),
        "name": str(_inherited(a, "/T") or ""),
        "value": deep_hash(_inherited(a, "/V")) if _inherited(a, "/V") is not None else "",
        "action": deep_hash(a.get("/A")) if a.get("/A") is not None else "",
        "dest": deep_hash(a.get("/Dest")) if a.get("/Dest") is not None else "",
        "ap": deep_hash(a.get("/AP")) if a.get("/AP") is not None else "",
    }
    return rec


def _field_records(pdf) -> dict:
    acro = pdf.Root.get("/AcroForm")
    out = {}
    if acro is None:
        return out

    def name_of(node):
        parts = []
        n = node
        for _ in range(32):
            if n is None:
                break
            if "/T" in n:
                parts.append(str(n.T))
            n = n.get("/Parent")
        return ".".join(reversed(parts))

    def visit(node, depth=0):
        if depth > 32:
            return
        kids = node.get("/Kids")
        if kids is not None and any("/T" in k for k in kids):
            for k in kids:
                visit(k, depth + 1)
            return
        widgets = list(kids) if kids is not None else [node]
        v = _inherited(node, "/V")
        out[name_of(node)] = {
            "type": str(_inherited(node, "/FT") or ""),
            "value": str(v) if isinstance(v, (pikepdf.String, pikepdf.Name)) else (deep_hash(v) if v is not None else ""),
            "flags": int(_inherited(node, "/Ff") or 0),
            "rects": [tuple(round(float(x), 2) for x in w.get("/Rect", [])) for w in widgets],
            "ap": [deep_hash(w.get("/AP")) if w.get("/AP") is not None else "" for w in widgets],
        }

    for f in acro.get("/Fields", []) or []:
        visit(f)
    return out


def _is_signed(pdf) -> bool:
    acro = pdf.Root.get("/AcroForm")
    if acro is not None and int(acro.get("/SigFlags", 0)) & 1:
        return True
    if pdf.Root.get("/Perms") is not None:
        return True
    if acro is not None:
        stack = list(acro.get("/Fields", []) or [])
        n = 0
        while stack and n < 10000:
            n += 1
            f = stack.pop()
            if str(_inherited(f, "/FT") or "") == "/Sig" and _inherited(f, "/V") is not None:
                return True
            stack.extend(f.get("/Kids", []) or [])
    return False


def page_hashes(path: str | Path) -> list[str]:
    """One content hash per page: contents, resources (fonts, XObjects), annotations and boxes."""
    out = []
    memo: dict = {}
    with pikepdf.open(str(path)) as pdf:
        for page in pdf.pages:
            h = hashlib.sha256()
            for key in ("/Contents", "/Resources", "/Annots", "/MediaBox", "/CropBox", "/Rotate", "/Group", "/UserUnit"):
                v = page.obj.get(key) if key in ("/Contents", "/Annots", "/Group", "/UserUnit") else _inherited(page.obj, key)
                h.update(key.encode())
                if v is not None:
                    h.update(deep_hash(v, memo=memo).encode())
            out.append(h.hexdigest())
    return out


def snapshot(path: str | Path, pages: set | None = None) -> Snapshot:
    """Structure of the document; graphics and images only for ``pages`` (default all)."""
    boxes, fonts_pp, annots_pp = [], [], []
    all_fonts: set = set()
    font_objs: set = set()
    memo: dict = {}
    with pikepdf.open(str(path)) as pdf:
        for page in pdf.pages:
            mb = _inherited(page.obj, "/MediaBox")
            cb = page.obj.get("/CropBox")
            boxes.append((tuple(round(float(v), 3) for v in mb), tuple(round(float(v), 3) for v in cb) if cb is not None else None,
                          int(_inherited(page.obj, "/Rotate") or 0)))
            s: set = set()
            _fonts_of_resources(_inherited(page.obj, "/Resources"), set(), s, font_objs, memo=memo)
            fonts_pp.append(s)
            all_fonts |= s
            annots_pp.append([_annot_record(a) for a in (page.obj.get("/Annots", []) or [])])
        fields = _field_records(pdf)
        signed = _is_signed(pdf)
        root = pdf.Root
        doc_level = {}
        for key in sorted(root.keys()):
            if key in ("/Pages", "/AcroForm"):
                continue
            doc_level["Root" + key] = deep_hash(root[key], memo=memo)
        acro = root.get("/AcroForm")
        if acro is not None:
            for key in sorted(acro.keys()):
                if key != "/Fields":
                    doc_level["AcroForm" + key] = deep_hash(acro[key], memo=memo)
        info = pdf.trailer.get("/Info")
        if info is not None:
            for key in sorted(info.keys()):
                if key not in ("/ModDate", "/Producer"):
                    doc_level["Info" + key] = deep_hash(info[key], memo=memo)
        page_attrs = []
        for page in pdf.pages:
            h = hashlib.sha256()
            for key in sorted(page.obj.keys()):
                if key in ("/Contents", "/Resources", "/Annots", "/Parent"):
                    continue
                h.update(key.encode() + deep_hash(page.obj[key], memo=memo).encode())
            page_attrs.append(h.hexdigest())
    images_pp, graphics_pp = [], []
    with pdfplumber.open(str(path)) as pl:
        for index, p in enumerate(pl.pages):
            if pages is not None and index not in pages:
                images_pp.append(None)
                graphics_pp.append(None)
                continue
            ox, oy = float(p.mediabox[0]), float(p.mediabox[1])
            images_pp.append(sorted(
                (round(i["x0"] + ox, 2), round(i["y0"] + oy, 2), round(i["x1"] + ox, 2), round(i["y1"] + oy, 2), tuple(i.get("srcsize", ())))
                for i in p.images))
            g = []
            for kind, items in (("rect", p.rects), ("line", p.lines), ("curve", p.curves)):
                for it in items:
                    g.append((kind, round(it["x0"] + ox, 2), round(it["y0"] + oy, 2), round(it["x1"] + ox, 2), round(it["y1"] + oy, 2),
                              bool(it.get("fill")), bool(it.get("stroke")), str(it.get("non_stroking_color")),
                              str(it.get("stroking_color"))))
            graphics_pp.append(sorted(g))
    return Snapshot(boxes, fonts_pp, all_fonts, len(font_objs), annots_pp, fields, images_pp, graphics_pp, signed,
                    doc_level, page_attrs)


def graphics_rects(snapshot: Snapshot, page: int) -> list[tuple[str, Rect, bool, bool]]:
    """Graphics of a page as (kind, rect in PDF coordinates, fill, stroke)."""
    return [(kind, Rect(x0, y0, x1, y1), fill, stroke)
            for kind, x0, y0, x1, y1, fill, stroke, _, _ in snapshot.graphics_per_page[page]]


def image_rects(snapshot: Snapshot, page: int) -> list[Rect]:
    return [Rect(x0, y0, x1, y1) for x0, y0, x1, y1, _ in snapshot.images_per_page[page]]
