"""Groups glyphs into words, lines and paragraph blocks.

A *line* here is one horizontal run of text on one baseline without a large
gap and without a vertical rule crossing it, so the cells of a table row, or
the two columns of a two-column page, are separate lines.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .content import Glyph, PageModel
from .geometry import Rect, union_all

WORD_GAP_EM = 0.15  # gap (in em) that separates two words when no space glyph is drawn
LINE_GAP_EM = 1.1  # gap that splits a baseline into separate lines (columns, cells)


@dataclass
class Word:
    glyphs: list[int]
    text: str
    bbox: Rect


@dataclass
class Line:
    index: int
    glyphs: list[int]  # in visual (left to right) order, includes space glyphs
    words: list[Word]
    bbox: Rect
    baseline: float
    size: float
    fonts: list[str]
    block: int = -1

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return self.bbox.x0

    @property
    def x1(self) -> float:
        return self.bbox.x1


@dataclass
class Block:
    index: int
    lines: list[int]
    bbox: Rect
    leading: float
    size: float
    left: float
    right: float
    alignment: str = "left"
    first_indent: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class PageLayout:
    model: PageModel
    lines: list[Line]
    blocks: list[Block]
    line_of_glyph: dict[int, int]

    def block_of_line(self, line_index: int) -> Block:
        return self.blocks[self.lines[line_index].block]

    def reading_order(self) -> list[int]:
        """Line indices in reading order: blocks in drawing order, lines top to bottom."""
        out = []
        for b in self.blocks:
            out.extend(b.lines)
        return out


RULE_THICKNESS = 2.5  # filled shapes thinner than this act as table rules (Word / Quartz draw rules as fills)


def _vertical_rules(model: PageModel) -> list[Rect]:
    rules = []
    for g in model.graphics:
        if g.kind in ("line", "polygon") and g.bbox.width <= RULE_THICKNESS and g.bbox.height > 4:
            rules.append(g.bbox)
        elif g.kind == "rect":
            if g.bbox.width <= RULE_THICKNESS and g.bbox.height > 4:
                rules.append(g.bbox)
            elif g.stroke:
                # the left and right edges of a stroked rectangle (cell borders)
                rules.append(Rect(g.bbox.x0, g.bbox.y0, g.bbox.x0, g.bbox.y1))
                rules.append(Rect(g.bbox.x1, g.bbox.y0, g.bbox.x1, g.bbox.y1))
    return rules


def _horizontal_rules(model: PageModel) -> list[Rect]:
    rules = []
    for g in model.graphics:
        if g.kind in ("line", "polygon") and g.bbox.height <= RULE_THICKNESS and g.bbox.width > 4:
            rules.append(g.bbox)
        elif g.kind == "rect":
            if g.bbox.height <= RULE_THICKNESS and g.bbox.width > 4:
                rules.append(g.bbox)
            elif g.stroke:
                rules.append(Rect(g.bbox.x0, g.bbox.y0, g.bbox.x1, g.bbox.y0))
                rules.append(Rect(g.bbox.x0, g.bbox.y1, g.bbox.x1, g.bbox.y1))
    return rules


def _rule_between(rules: list[Rect], x_left: float, x_right: float, y: float, size: float) -> bool:
    for r in rules:
        cx = (r.x0 + r.x1) / 2
        if x_left - 0.1 <= cx <= x_right + 0.1 and r.y0 <= y + 0.3 * size and r.y1 >= y:
            return True
    return False


def build_layout(model: PageModel) -> PageLayout:
    glyphs = [g for g in model.glyphs if g.upright and g.visible]
    vrules = _vertical_rules(model)
    hrules = _horizontal_rules(model)

    # 1. sequential pass in drawing order
    fragments: list[list[Glyph]] = []
    cur: list[Glyph] = []
    for g in glyphs:
        if cur:
            last = cur[-1]
            size = max(float(last.effective_size), 1e-3)
            same_base = abs(g.y - last.y) <= 0.25 * size
            gap = g.x - last.x_end
            if same_base and -0.5 * size <= gap <= LINE_GAP_EM * size and not _rule_between(vrules, last.x_end, g.x, g.y, size):
                cur.append(g)
                continue
            fragments.append(cur)
        cur = [g]
    if cur:
        fragments.append(cur)

    # 2. merge fragments that continue each other on the same baseline
    fragments.sort(key=lambda fr: (-round(fr[0].y, 1), fr[0].x))
    merged: list[list[Glyph]] = []
    for fr in fragments:
        for m in merged:
            last = max(m, key=lambda g: g.x_end)
            first = min(m, key=lambda g: g.x)
            size = max(float(last.effective_size), 1e-3)
            if abs(fr[0].y - last.y) > 0.25 * size:
                continue
            gap = fr[0].x - last.x_end
            gap_before = first.x - max(g.x_end for g in fr)
            if -0.5 * size <= gap <= LINE_GAP_EM * size and not _rule_between(vrules, last.x_end, fr[0].x, fr[0].y, size):
                m.extend(fr)
                break
            if -0.5 * size <= gap_before <= LINE_GAP_EM * size and not _rule_between(vrules, max(g.x_end for g in fr), first.x, fr[0].y, size):
                m.extend(fr)
                break
        else:
            merged.append(list(fr))

    lines: list[Line] = []
    for m in merged:
        m.sort(key=lambda g: (g.x, g.gid))
        lines.append(_make_line(len(lines), m))
    line_of_glyph = {gid: ln.index for ln in lines for gid in ln.glyphs}

    blocks = _build_blocks(lines, hrules, model)
    return PageLayout(model, lines, blocks, line_of_glyph)


def _make_line(index: int, glyphs: list[Glyph]) -> Line:
    words: list[Word] = []
    cur: list[Glyph] = []
    for g in glyphs:
        if g.text.isspace() or g.text == " ":
            if cur:
                words.append(_make_word(cur))
                cur = []
            continue
        if cur:
            size = max(float(cur[-1].effective_size), 1e-3)
            if g.x - cur[-1].x_end > WORD_GAP_EM * size:
                words.append(_make_word(cur))
                cur = []
        cur.append(g)
    if cur:
        words.append(_make_word(cur))
    sizes = [float(g.effective_size) for g in glyphs if not g.text.isspace()] or [float(glyphs[0].effective_size)]
    ink = [g for g in glyphs if not g.text.isspace()] or glyphs
    bbox = union_all(g.bbox for g in ink)
    return Line(
        index=index, glyphs=[g.gid for g in glyphs], words=words, bbox=bbox,
        baseline=statistics.median(g.y for g in ink), size=statistics.median(sizes),
        fonts=sorted({g.font.base_font for g in glyphs}),
    )


def _make_word(glyphs: list[Glyph]) -> Word:
    return Word([g.gid for g in glyphs], "".join(g.text for g in glyphs), union_all(g.bbox for g in glyphs))


def _build_blocks(lines: list[Line], hrules: list[Rect], model: PageModel) -> list[Block]:
    order = sorted(range(len(lines)), key=lambda i: (-lines[i].baseline, lines[i].x0))
    blocks: list[dict] = []
    for i in order:
        ln = lines[i]
        best = None
        for b in blocks:
            last = lines[b["lines"][-1]]
            d = last.baseline - ln.baseline
            size = max(ln.size, last.size)
            if d <= 0.5 * size or d > 1.9 * size:
                continue
            if abs(ln.size - last.size) > 0.15 * size:
                continue
            if b["leading"] is not None and abs(d - b["leading"]) > 0.2 * size:
                continue
            overlap = min(ln.x1, last.x1) - max(ln.x0, last.x0)
            if overlap <= 0 and abs(ln.x0 - last.x0) > 2:
                continue
            # a horizontal rule between the two lines separates table rows
            if any(r.x0 < min(ln.x1, last.x1) and r.x1 > max(ln.x0, last.x0) and ln.baseline < (r.y0 + r.y1) / 2 < last.baseline
                   for r in hrules):
                continue
            # the lines must not be separated by another line in between
            score = abs(ln.x0 - last.x0)
            if best is None or score < best[0]:
                best = (score, b, d)
        if best is not None:
            _, b, d = best
            b["lines"].append(i)
            if b["leading"] is None:
                b["leading"] = d
        else:
            blocks.append({"lines": [i], "leading": None})

    # order blocks by the drawing order of their first glyph
    blocks.sort(key=lambda b: min(min(lines[i].glyphs) for i in b["lines"]))
    out: list[Block] = []
    for bi, b in enumerate(blocks):
        ls = [lines[i] for i in b["lines"]]
        for ln in ls:
            ln.block = bi
        bbox = union_all(ln.bbox for ln in ls)
        leading = b["leading"] or ls[0].size * 1.2
        lefts = [ln.x0 for ln in ls]
        rights = [ln.x1 for ln in ls]
        left = min(lefts[1:]) if len(ls) > 1 else lefts[0]
        block = Block(bi, b["lines"], bbox, leading, statistics.median(ln.size for ln in ls), left, max(rights))
        block.first_indent = lefts[0] - left
        block.alignment = _alignment(ls)
        out.append(block)
    return out


def _alignment(ls: list[Line]) -> str:
    if len(ls) < 2:
        return "left"
    tol = max(1.0, 0.1 * ls[0].size)
    lefts = [ln.x0 for ln in ls]
    rights = [ln.x1 for ln in ls]
    centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
    rest = lefts[1:] if len(ls) > 2 else lefts
    left_same = max(rest) - min(rest) <= tol
    if left_same:
        non_last = rights[:-1]
        if len(non_last) >= 2 and max(non_last) - min(non_last) <= tol:
            return "justify"
        return "left"
    if max(centers) - min(centers) <= tol:
        return "center"
    if max(rights) - min(rights) <= tol:
        return "right"
    return "left"
