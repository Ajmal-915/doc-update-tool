"""Step 3: measure the space available around the target (plain code)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import pdf_ops
from pdf_ops.geometry import Rect, union_all
from pdf_ops.target import Target

MIN_MARGIN = 36.0  # never treat less than half an inch from the page edge as usable
MIN_BOTTOM_MARGIN = 54.0  # when nothing else on the page shows where the bottom margin is


@dataclass
class Measurements:
    page_index: int
    page_box: Rect
    margins: dict
    content_box: Rect
    target: dict
    lines: list
    paragraph: dict
    column_right_edge: float
    column_sources: list
    right_obstacle: dict | None
    below_obstacle: dict | None
    free_space_below: float
    extra_lines_possible: int
    boundary: float  # right boundary for horizontal expansion (x, points)
    region_type: str
    risks: list
    missing_chars: list
    font: dict
    space_width: float
    leading: float
    table_cell: Rect | None = None
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page": self.page_index + 1,
            "page_box": self.page_box.as_list(1),
            "margins": {k: round(v, 1) for k, v in self.margins.items()},
            "content_box": self.content_box.as_list(1),
            "target": self.target,
            "lines": self.lines,
            "paragraph": self.paragraph,
            "column_right_edge": round(self.column_right_edge, 2),
            "column_right_edge_sources": self.column_sources,
            "nearest_right": self.right_obstacle,
            "nearest_below_paragraph": self.below_obstacle,
            "free_space_below_paragraph": round(self.free_space_below, 2),
            "extra_lines_possible": self.extra_lines_possible,
            "horizontal_boundary": round(self.boundary, 2),
            "region_type": self.region_type,
            "risks": self.risks,
            "missing_characters": self.missing_chars,
            "font": self.font,
            "space_width": round(self.space_width, 3),
            "leading": round(self.leading, 3),
            "table_cell": self.table_cell.as_list(1) if self.table_cell else None,
            "notes": self.notes,
        }


def _page_box(doc: pdf_ops.Document, page_index: int) -> Rect:
    page = doc.pdf.pages[page_index]
    box = page.obj.get("/CropBox") or page.mediabox
    return Rect.from_list([float(v) for v in box])


def _content_box(doc: pdf_ops.Document, page_index: int, exclude: set | None = None) -> Rect | None:
    model = doc.model(page_index)
    pb = _page_box(doc, page_index)
    area = pb.width * pb.height
    exclude = exclude or set()
    rects = [g.bbox for g in model.glyphs if g.visible and not g.text.isspace() and g.gid not in exclude]
    for g in model.graphics:
        if g.kind == "shading":
            continue
        if g.bbox.width * g.bbox.height < 0.8 * area:
            rects.append(g.bbox)
    for im in model.images:
        if im.bbox.width * im.bbox.height < 0.8 * area:
            rects.append(im.bbox)
    rects = [r for r in rects if pb.intersects(r)]
    return union_all(rects)


def measure(doc: pdf_ops.Document, target: Target, replacement: str, sample_pages: int = 3) -> Measurements:
    p = target.page_index
    model = doc.model(p)
    layout = doc.layout(p)
    pb = _page_box(doc, p)
    first = model.glyphs[target.glyphs[0]]
    size = target.effective_size

    # margins from this page (without the target's own paragraph) and its neighbours
    own_block = {g for i in layout.blocks[target.block].lines for g in layout.lines[i].glyphs}
    boxes = []
    for q in range(max(0, p - sample_pages), min(doc.page_count, p + sample_pages + 1)):
        try:
            b = _content_box(doc, q, own_block if q == p else None)
        except Exception:
            b = None
        if b is not None:
            boxes.append(b)
    content = union_all(boxes) or pb
    right_limit = min(pb.x1 - MIN_MARGIN, max(content.x1, target.bbox.x1))
    block_bottom = layout.blocks[target.block].bbox.y0
    if content.y0 >= block_bottom - 0.5:
        # nothing lower on this page or its neighbours shows the bottom margin: keep a conventional one
        bottom_limit = min(pb.y0 + MIN_BOTTOM_MARGIN, block_bottom)
    else:
        bottom_limit = min(max(pb.y0 + MIN_MARGIN, content.y0), block_bottom)
    margins = {"left": content.x0 - pb.x0, "right": pb.x1 - right_limit, "top": pb.y1 - content.y1, "bottom": bottom_limit - pb.y0}

    block = layout.blocks[target.block]
    block_lines = [layout.lines[i] for i in block.lines]
    target_lines = [layout.lines[part.line] for part in target.parts]
    part_end = max(part.x1 for part in target.parts)
    # the line already reaches here (text after the target included): the boundary is never left of it
    line_end = max([part_end] + [part.line_x1 for part in target.parts if part.has_text_after])

    # column right edge from neighbouring lines (same left edge) and table rules
    sources = []
    siblings = [ln for ln in layout.lines if ln.block != target.block and abs(ln.x0 - block.left) <= 3.0
                and abs(ln.size - block.size) <= 0.2 * block.size]
    line_edge = max([ln.x1 for ln in block_lines] + [ln.x1 for ln in siblings])
    sources.append({"kind": "neighbouring_lines", "x": round(line_edge, 2), "lines": len(block_lines) + len(siblings)})
    band_lines = target_lines
    band = union_all(ln.bbox for ln in band_lines)
    rules_right, rules_left = [], []
    cell = None
    for g in model.graphics:
        r = g.bbox
        verticals = []
        if g.kind in ("line", "polygon") and r.width <= 2.5 and r.height > 4:
            verticals.append((r.x0 + r.x1) / 2)
        elif g.kind == "rect" and g.stroke and r.width > 2 and r.height > 2:
            verticals += [r.x0, r.x1]
            if r.contains(band, tol=1.0):
                cell = r if cell is None or r.width * r.height < cell.width * cell.height else cell
        elif g.kind == "rect" and r.width <= 2.5 and r.height > 4:
            verticals.append((r.x0 + r.x1) / 2)
        for x in verticals:
            if r.y0 <= band.y0 + 0.2 * size and r.y1 >= band.y1 - 0.2 * size:
                if x >= part_end - 0.5:
                    rules_right.append(x)
                elif x <= band.x0 + 0.5:
                    rules_left.append(x)
    padding = (band.x0 - max(rules_left)) if rules_left else 2.0
    padding = max(1.0, min(padding, 12.0))
    column_edge = line_edge
    is_table = bool(rules_right and rules_left) or cell is not None
    if rules_right:
        rx = min(rules_right)
        sources.append({"kind": "table_rule", "x": round(rx, 2), "padding": round(padding, 2)})
        column_edge = rx - padding if is_table else min(column_edge, rx - padding)
    if is_table and cell is not None:
        column_edge = min(column_edge, cell.x1 - padding)
    if not is_table and len(block_lines) == 1 and not siblings:
        column_edge = right_limit
        sources.append({"kind": "page_margin", "x": round(right_limit, 2)})

    # nearest text or graphic to the right of the target on its band
    right_obstacle = None
    own_line_glyphs = {g for ln in target_lines for g in ln.glyphs}
    for g in model.glyphs:
        if g.gid in own_line_glyphs or not g.visible or g.text.isspace():
            continue
        if g.bbox.y1 > band.y0 + 0.1 * size and g.bbox.y0 < band.y1 - 0.1 * size and g.bbox.x0 >= part_end - 0.1:
            if right_obstacle is None or g.bbox.x0 < right_obstacle["x"]:
                right_obstacle = {"x": round(g.bbox.x0, 2), "kind": "text", "text": g.text}
    for g in model.graphics:
        r = g.bbox
        if g.kind == "shading" or (cell is not None and r == cell):
            continue
        if r.y1 > band.y0 and r.y0 < band.y1 and r.x0 >= part_end - 0.1:
            if right_obstacle is None or r.x0 < right_obstacle["x"]:
                right_obstacle = {"x": round(r.x0, 2), "kind": g.kind}
    for im in model.images:
        r = im.bbox
        if r.y1 > band.y0 and r.y0 < band.y1 and r.x0 >= part_end - 0.1:
            if right_obstacle is None or r.x0 < right_obstacle["x"]:
                right_obstacle = {"x": round(r.x0, 2), "kind": "image"}

    space_w = _space_width(doc, target)
    boundary = min(column_edge, right_limit)
    if right_obstacle is not None:
        gap = padding if right_obstacle["kind"] != "text" else max(2 * space_w, 0.8 * size)
        if right_obstacle["x"] - gap < column_edge:
            sources.append({"kind": "next_" + right_obstacle["kind"], "x": round(right_obstacle["x"] - gap, 2)})
            column_edge = right_obstacle["x"] - gap
        boundary = min(boundary, right_obstacle["x"] - gap)

    # below the paragraph
    below = None
    x_lo, x_hi = block.left - 1, max(column_edge, block.right) + 1
    block_glyphs = {g for ln in block_lines for g in ln.glyphs}
    for g in model.glyphs:
        if g.gid in block_glyphs or not g.visible or g.text.isspace():
            continue
        if g.bbox.x1 > x_lo and g.bbox.x0 < x_hi and g.bbox.y1 <= block.bbox.y0 + 0.5:
            if below is None or g.bbox.y1 > below["y"]:
                below = {"y": round(g.bbox.y1, 2), "kind": "text", "text": g.text}
    for g in model.graphics:
        r = g.bbox
        if g.kind == "shading":
            continue
        if r.x1 > x_lo and r.x0 < x_hi and r.y1 <= block.bbox.y0 + 0.5 and r.height < pb.height * 0.8:
            if below is None or r.y1 > below["y"]:
                below = {"y": round(r.y1, 2), "kind": g.kind}
    for im in model.images:
        r = im.bbox
        if r.x1 > x_lo and r.x0 < x_hi and r.y1 <= block.bbox.y0 + 0.5:
            if below is None or r.y1 > below["y"]:
                below = {"y": round(r.y1, 2), "kind": "image"}
    leading = block.leading
    floor_y = below["y"] if below is not None else bottom_limit
    floor_y = max(floor_y, bottom_limit) if below is None else floor_y
    min_sep = max(0.6 * leading, 4.0)
    free = max(0.0, block.bbox.y0 - floor_y - (min_sep if below is not None else 0.0))
    extra_lines = int(free // leading) if leading > 0 else 0

    # region type and risks
    risks = []
    line_text = " ".join(ln.text for ln in target_lines)
    after_glyph_text = "".join(model.glyphs[g].text for part in target.parts for g in part.after)
    dot_leader = "...." in after_glyph_text.replace(" ", "") or "……" in after_glyph_text or ". . . ." in after_glyph_text
    if dot_leader:
        risks.append({"type": "dot_leader", "description": "dot leaders follow the target on its line"})
    if right_obstacle is not None and right_obstacle["x"] - part_end < 2 * size:
        risks.append({"type": "adjacent_text" if right_obstacle["kind"] == "text" else "table_line",
                      "description": f"{right_obstacle['kind']} {right_obstacle['x'] - part_end:.1f} pt to the right of the target"})
    if is_table:
        risks.append({"type": "table_line", "description": "the target sits in a table cell"})
    if right_limit - part_end < size:
        risks.append({"type": "margin", "description": "the target ends close to the right margin"})
    sizes = [g.effective_size for g in model.glyphs if g.visible and not g.text.isspace()]
    body = statistics.median(sizes) if sizes else size
    bold = "bold" in first.font.base_font.lower() or "black" in first.font.base_font.lower()
    if dot_leader:
        region = "table_of_contents_line"
    elif is_table:
        region = "table_cell"
    elif len(block_lines) == 1 and (size >= 1.15 * body or bold) and len(line_text) < 120:
        region = "heading"
    elif len(block_lines) >= 2 or len(line_text) > 40:
        region = "paragraph"
    else:
        region = "other"

    missing = target.font.missing_chars(" ".join(replacement.split()))
    lines_info = []
    for part in target.parts:
        ln = layout.lines[part.line]
        lines_info.append({"line": ln.index, "text": ln.text, "bbox": ln.bbox.as_list(2), "target_x0": round(part.x0, 2),
                           "target_x1": round(part.x1, 2), "text_after_target": part.has_text_after})
    return Measurements(
        page_index=p, page_box=pb, margins=margins, content_box=content, target=target.to_dict(), lines=lines_info,
        paragraph={"block": block.index, "bbox": block.bbox.as_list(2), "left": round(block.left, 2),
                   "right": round(block.right, 2), "lines": len(block_lines), "alignment": block.alignment,
                   "leading": round(block.leading, 3), "first_line_indent": round(block.first_indent, 2),
                   "target_line_numbers": [block.lines.index(part.line) + 1 for part in target.parts if part.line in block.lines]},
        column_right_edge=column_edge, column_sources=sources, right_obstacle=right_obstacle, below_obstacle=below,
        free_space_below=free, extra_lines_possible=extra_lines, boundary=max(boundary, line_end), region_type=region,
        risks=risks, missing_chars=missing,
        font={"name": first.font.base_font, "resource": first.font_res, **first.font.describe(),
              "size": float(first.size), "effective_size": round(size, 3)},
        space_width=space_w, leading=leading, table_cell=cell,
    )


def _space_width(doc: pdf_ops.Document, target: Target) -> float:
    from pdf_ops.methods import _space_width_points

    try:
        return _space_width_points(doc, target)
    except Exception:
        return 0.25 * target.effective_size
