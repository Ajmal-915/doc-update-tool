"""Method d: paragraph reflow.

The paragraph is rewrapped from the first line that contains the target to
its end, in the same font, size, colour and line spacing, between the
paragraph's own left and right edges.  Lines above the target line are not
touched.  Extra lines may use free space below the paragraph down to
``max_bottom``; nothing else on the page moves.

The old lines' glyphs are removed from their show operators and the new
lines are drawn in one new text block, inserted right after the text block
that held the first affected line (same stream, same graphics state).
"""

from __future__ import annotations

from decimal import Decimal

from . import geometry as G
from .content import Glyph
from .document import Document
from .edit import BlockInsert, BlockLine, EditPlan, apply_plan
from .errors import DoesNotFitError
from .fonts import advance_of_codes
from .methods import Fit, MethodResult, _common, _space_advance, clean_replacement
from .target import Target
from .textnorm import is_line_end_hyphen


def _style(g: Glyph) -> tuple:
    return (g.font, g.size, g.th, g.ts, g.tr, g.fill_cs, g.fill, g.ctm, g.tm[:4], g.stream)


def fit_reflow(doc: Document, target: Target, replacement: str, boundary: float | None = None,
               max_bottom: float | None = None) -> Fit:
    """Measure a reflow without changing anything."""
    try:
        first, font, text = _common(doc, target, replacement)
    except Exception as exc:  # MissingGlyphsError / UnsupportedError
        return Fit("paragraph_reflow", False, getattr(exc, "message", str(exc)), details={"code": getattr(exc, "code", "")})
    model = doc.model(target.page_index)
    layout = doc.layout(target.page_index)
    block = layout.blocks[target.block]
    lines = [layout.lines[i] for i in block.lines]
    first_line = target.parts[0].line
    if first_line not in block.lines:
        return Fit("paragraph_reflow", False, "the target is not inside a recognised paragraph")
    k = block.lines.index(first_line)
    affected = lines[k:]
    glyphs = [model.glyphs[g] for ln in affected for g in ln.glyphs]
    ink = [g for g in glyphs if not g.text.isspace()]
    if not ink:
        return Fit("paragraph_reflow", False, "the paragraph has no visible text")
    style = _style(ink[0])
    for g in ink:
        if _style(g) != style:
            return Fit("paragraph_reflow", False, "the paragraph mixes fonts, sizes, colours or text blocks; "
                                                  "it cannot be rewrapped in one font")
        if not g.upright:
            return Fit("paragraph_reflow", False, "rotated or skewed text cannot be rewrapped")
    for ln in affected[:-1]:
        last = model.glyphs[ln.words[-1].glyphs[-1]].text if ln.words else ""
        if is_line_end_hyphen(last):
            return Fit("paragraph_reflow", False, "the paragraph has words hyphenated across lines; rewrapping "
                                                  "could change the wording")
    bt = model.bt_blocks.get(ink[0].bt)
    if bt is None or bt.et_index is None or bt.ctm_changed_inside:
        return Fit("paragraph_reflow", False, "the paragraph's text block cannot be extended safely")

    # ----- the new word sequence (target replaced)
    chars: list[tuple[str, int | None]] = []
    for li, ln in enumerate(affected):
        if li:
            chars.append((" ", None))
        for wi, w in enumerate(ln.words):
            if wi:
                chars.append((" ", None))
            for gid in w.glyphs:
                chars.append((model.glyphs[gid].text, gid))
    tset = set(target.glyphs)
    pos = [i for i, (_, gid) in enumerate(chars) if gid in tset]
    if not pos:
        return Fit("paragraph_reflow", False, "could not locate the target inside the paragraph")
    new_chars = [c for c, _ in chars[:pos[0]]] + list(text) + [c for c, _ in chars[pos[-1] + 1:]]
    words = "".join(new_chars).split()

    # ----- geometry (user space)
    g0 = ink[0]
    size = g0.size
    space_adv = _space_advance(doc, target)
    xs = g0.x_scale

    def width(s: str) -> float:
        return float(advance_of_codes(font, font.encode(s), size, g0.tc, Decimal(0), g0.th, space_adv)) * xs

    space_w = width(" ") if font.space_code is not None else float(space_adv * g0.th) * xs
    right = block.right if boundary is None else min(boundary, block.right)
    right = max(right, max(ln.x1 for ln in affected))
    starts = [affected[0].x0] + [block.left] * 1000
    out_lines: list[list[str]] = []
    cur: list[str] = []
    cur_w = 0.0
    for w in words:
        ww = width(w)
        avail = right - starts[len(out_lines)]
        if ww > avail + 0.01:
            return Fit("paragraph_reflow", False, f"the word {w!r} is wider than the paragraph")
        trial = cur_w + (space_w if cur else 0.0) + ww
        if cur and trial > avail + 0.01:
            out_lines.append(cur)
            cur, cur_w = [w], ww
        else:
            cur.append(w)
            cur_w = trial
    if cur:
        out_lines.append(cur)

    # baselines: reuse the original lines' baselines, continue with the leading below
    def baseline(ln) -> float:
        g = model.glyphs[ln.words[0].glyphs[0]] if ln.words else model.glyphs[ln.glyphs[0]]
        return G.apply(g.ctm, g.tm[4], g.tm[5])[1]

    bases = [baseline(ln) for ln in affected]
    leading = block.leading
    while len(bases) < len(out_lines):
        bases.append(bases[-1] - leading)
    eff = float(g0.effective_size)
    new_bottom = bases[len(out_lines) - 1] + font.descent * eff
    old_bottom = min(ln.bbox.y0 for ln in affected)
    extra = len(out_lines) - len(affected)
    if extra > 0:
        limit = max_bottom if max_bottom is not None else old_bottom
        if new_bottom < limit - 0.01:
            return Fit("paragraph_reflow", False,
                       f"the rewrapped paragraph needs {extra} more line(s) ({extra * leading:.1f} pt) but only "
                       f"{max(0.0, old_bottom - limit):.1f} pt of free space is available below it",
                       details={"needed_lines": len(out_lines), "original_lines": len(affected)})
    top = max(ln.bbox.y1 for ln in affected)
    left = min(block.left, affected[0].x0)
    area = G.Rect(left, min(new_bottom, old_bottom), right, top)
    # every removed glyph, trailing spaces included, lies inside the area
    removed = [model.glyphs[g] for ln in affected for g in ln.glyphs]
    area = area.union(G.Rect(min(g.x for g in removed), area.y0, max(g.x_end for g in removed), area.y1)).expand(0.25)
    line_texts = [" ".join(ws) for ws in out_lines]
    fit = Fit("paragraph_reflow", True, "fits", [], 1.0, float(size), float(size), boundary=right, details={
        "area": area.as_list(3), "max_bottom": max_bottom, "lines": line_texts, "new_line_count": len(out_lines),
        "original_line_count": len(affected), "alignment": block.alignment, "right_edge": round(right, 3),
        "extra_space_used": round(max(0.0, old_bottom - new_bottom), 3),
    })
    fit._plan = (affected, out_lines, bases, starts, right, space_w, width, bt, g0)  # used by reflow_paragraph
    return fit


def reflow_paragraph(doc: Document, target: Target, replacement: str, boundary: float | None = None,
                     max_bottom: float | None = None) -> MethodResult:
    fit = fit_reflow(doc, target, replacement, boundary, max_bottom)
    if not fit.feasible:
        raise DoesNotFitError(fit.reason, fit=fit.to_dict())
    affected, out_lines, bases, starts, right, space_w, width, bt, g0 = fit._plan
    font = g0.font
    inv = G.inverse(g0.ctm)
    a, b, c, d = g0.tm[0], g0.tm[1], g0.tm[2], g0.tm[3]
    justify = fit.details["alignment"] == "justify"
    centre = fit.details["alignment"] == "center"
    align_right = fit.details["alignment"] == "right"
    block_lines = []
    for i, ws in enumerate(out_lines):
        text = " ".join(ws)
        natural = width(text)
        x = starts[i]
        word_kern = Decimal(0)
        is_last = i == len(out_lines) - 1
        if justify and not is_last and len(ws) > 1:
            gap = (right - x - natural) / (len(ws) - 1)
            word_kern = Decimal(str(gap / g0.x_scale)) / g0.th
        elif centre:
            x = x + (right - x - natural) / 2
        elif align_right:
            x = right - natural
        tx, ty = G.apply(inv, x, bases[i])
        block_lines.append(BlockLine(tx, ty, font.encode(text), word_kern, text))
    block = BlockInsert(
        after=(g0.stream, bt.et_index), font=font, font_res=g0.font_res, size=g0.size, tm_linear=(a, b, c, d),
        lines=block_lines, tc=g0.tc, th=g0.th, ts=g0.ts, tr=g0.tr, fill_ops=g0.fill_ops,
        space_advance=_space_advance(doc, target), ctm=g0.ctm,
    )
    plan = EditPlan(target.page_index, remove={gid for ln in affected for gid in ln.glyphs}, blocks=[block])
    target_dict = target.to_dict()
    result = apply_plan(doc, plan)
    bbox = result.bbox(font)
    return MethodResult("paragraph_reflow", target_dict, clean_replacement(replacement), fit.to_dict(), result.to_dict(),
                        bbox.as_list() if bbox else None, font.base_font, float(g0.size), float(g0.size))
