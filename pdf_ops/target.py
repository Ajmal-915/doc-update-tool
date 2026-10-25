"""Locating a target phrase on a page (exact match after normalisation)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .document import Document
from .errors import AmbiguousTargetError, TargetNotFoundError
from .fonts import PdfFont
from .geometry import Rect, union_all
from .layout import PageLayout
from .textnorm import is_line_end_hyphen, normalize, normalize_with_map

BLOCK_SEPARATOR = "\x00"


@dataclass
class SeqItem:
    text: str
    gid: int | None  # None for a synthetic space or separator
    line: int | None


def page_sequence(layout: PageLayout) -> list[SeqItem]:
    """Page text in reading order, one item per glyph plus synthetic spaces.

    Words are separated by one space even when the PDF draws no space glyph.
    Lines of one block are joined by a space, or joined directly when the
    first line ends with a hyphen and the next starts with a lowercase letter
    (a word hyphenated across lines).  Blocks are separated by a character
    that never matches, so a phrase cannot run from one block into another.
    """
    model = layout.model
    seq: list[SeqItem] = []
    for block in layout.blocks:
        if seq:
            seq.append(SeqItem(BLOCK_SEPARATOR, None, None))
        for li_pos, li in enumerate(block.lines):
            line = layout.lines[li]
            if li_pos > 0:
                prev_text = seq[-1].text if seq else ""
                nxt = model.glyphs[line.words[0].glyphs[0]].text if line.words else ""
                if is_line_end_hyphen(prev_text) and nxt[:1].islower():
                    seq[-1] = SeqItem("", seq[-1].gid, seq[-1].line)  # drop the hyphen from the text
                else:
                    seq.append(SeqItem(" ", None, None))
            for wi, word in enumerate(line.words):
                if wi > 0:
                    # use real space glyphs between the words when present
                    prev_last = line.words[wi - 1].glyphs[-1]
                    first = word.glyphs[0]
                    pos_a = line.glyphs.index(prev_last)
                    pos_b = line.glyphs.index(first)
                    spaces = line.glyphs[pos_a + 1:pos_b]
                    if spaces:
                        for gid in spaces:
                            seq.append(SeqItem(" ", gid, line.index))
                    else:
                        seq.append(SeqItem(" ", None, line.index))
                for gid in word.glyphs:
                    seq.append(SeqItem(model.glyphs[gid].text, gid, line.index))
    return seq


def normalized_sequence(seq: list[SeqItem]) -> tuple[str, list[int]]:
    """Normalised text and, per character, the index into ``seq``."""
    return normalize_with_map([s.text for s in seq])


@dataclass
class LinePart:
    line: int
    glyphs: list[int]
    bbox: Rect
    before: list[int]
    after: list[int]
    x0: float
    x1: float
    line_x1: float = 0.0  # end of the last glyph on the line part (target + text after it)
    has_text_after: bool = False  # visible (non-space) text follows the target on its line


@dataclass
class Target:
    page_index: int
    text: str
    glyphs: list[int]
    parts: list[LinePart]
    bbox: Rect
    font: PdfFont
    font_res: str
    size: Decimal
    effective_size: float
    block: int
    kind: str = "text"
    extra: dict = field(default_factory=dict)

    @property
    def page_number(self) -> int:
        return self.page_index + 1

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "page": self.page_number,
            "text": self.text,
            "bbox": self.bbox.as_list(),
            "lines": [{"line": p.line, "bbox": p.bbox.as_list(), "glyphs": len(p.glyphs),
                       "text_after_on_line": p.has_text_after} for p in self.parts],
            "font": self.font.base_font,
            "font_resource": self.font_res,
            "font_size": float(self.size),
            "effective_size": round(self.effective_size, 3),
            "block": self.block,
        }


def target_from_span(doc: Document, page_index: int, seq: list[SeqItem], start: int, end: int) -> Target:
    """Build a Target from sequence positions [start, end] (inclusive)."""
    layout = doc.layout(page_index)
    model = layout.model
    gids_ordered = []
    for item in seq[start:end + 1]:
        if item.gid is not None:
            gids_ordered.append(item.gid)
    # strip leading/trailing spaces
    while gids_ordered and model.glyphs[gids_ordered[0]].text.isspace():
        gids_ordered.pop(0)
    while gids_ordered and model.glyphs[gids_ordered[-1]].text.isspace():
        gids_ordered.pop()
    if not gids_ordered:
        raise TargetNotFoundError("target maps to no glyphs")
    by_line: dict[int, list[int]] = {}
    order: list[int] = []
    for gid in gids_ordered:
        li = layout.line_of_glyph[gid]
        if li not in by_line:
            by_line[li] = []
            order.append(li)
        by_line[li].append(gid)
    parts = []
    for li in order:
        line = layout.lines[li]
        mine = set(by_line[li])
        positions = [line.glyphs.index(g) for g in mine]
        lo, hi = min(positions), max(positions)
        span = line.glyphs[lo:hi + 1]
        glyphs = [model.glyphs[g] for g in span]
        ink = [g for g in glyphs if not g.text.isspace()] or glyphs
        bbox = union_all(g.bbox for g in ink)
        line_glyphs = [model.glyphs[g] for g in line.glyphs]
        after_ids = line.glyphs[hi + 1:]
        parts.append(LinePart(li, span, bbox, line.glyphs[:lo], after_ids, min(g.x for g in glyphs),
                              max(g.x_end for g in glyphs), max(g.x_end for g in line_glyphs),
                              any(not model.glyphs[g].text.isspace() for g in after_ids)))
    all_gids = [g for p in parts for g in p.glyphs]
    first = model.glyphs[all_gids[0]]
    # text as read: from the first to the last target glyph in the sequence, with synthetic spaces
    pos = {item.gid: k for k, item in enumerate(seq) if item.gid is not None}
    k0, k1 = pos[gids_ordered[0]], pos[gids_ordered[-1]]
    text = " ".join("".join(item.text for item in seq[k0:k1 + 1]).split())
    return Target(
        page_index=page_index,
        text=text,
        glyphs=all_gids,
        parts=parts,
        bbox=union_all(p.bbox for p in parts),
        font=first.font,
        font_res=first.font_res,
        size=first.size,
        effective_size=first.effective_size,
        block=layout.lines[parts[0].line].block,
    )


def find_all(doc: Document, page_index: int, text: str) -> list[Target]:
    layout = doc.layout(page_index)
    seq = page_sequence(layout)
    hay, idx = normalized_sequence(seq)
    needle = normalize(text)
    if not needle:
        return []
    found = []
    pos = hay.find(needle)
    while pos >= 0:
        found.append(target_from_span(doc, page_index, seq, idx[pos], idx[pos + len(needle) - 1]))
        pos = hay.find(needle, pos + 1)
    return found


def find_target(doc: Document, page: int, text: str, near=None) -> Target:
    """Find ``text`` on ``page`` (numbered from 1).

    ``near`` is an optional [x0, y0, x1, y1] box in PDF points used to choose
    between several identical occurrences.  Raises TargetNotFoundError or
    AmbiguousTargetError.
    """
    page_index = int(page) - 1
    doc.check_page(page_index)
    found = find_all(doc, page_index, text)
    if not found:
        raise TargetNotFoundError(f"text not found on page {page}", page=page, text=text)
    if len(found) == 1:
        return found[0]
    if near is not None:
        box = Rect.from_list(near)

        def score(t: Target) -> float:
            """Share of the candidate's box that lies inside ``near``."""
            b = t.bbox
            ix = max(0.0, min(b.x1, box.x1) - max(b.x0, box.x0))
            iy = max(0.0, min(b.y1, box.y1) - max(b.y0, box.y0))
            area = max(b.width * b.height, 1e-6)
            return ix * iy / area

        def distance(t: Target) -> float:
            b = t.bbox
            return abs((b.x0 + b.x1) / 2 - (box.x0 + box.x1) / 2) + abs((b.y0 + b.y1) / 2 - (box.y0 + box.y1) / 2)

        ranked = sorted(found, key=lambda t: (-score(t), distance(t)))
        if score(ranked[0]) >= 0.5 and score(ranked[1]) < score(ranked[0]) - 0.25:
            return ranked[0]
        if score(ranked[0]) == 0 and distance(ranked[0]) * 2 < distance(ranked[1]):
            return ranked[0]
    raise AmbiguousTargetError(
        f"text occurs {len(found)} times on page {page}",
        candidates=[{"page": page, "bbox": t.bbox.as_list(), "text": t.text} for t in found],
    )
