"""High-level replacement methods used by generated scripts.

Each method has a ``fit_*`` function that only measures (used by the
pipeline to decide which methods can work) and an action function that
performs the edit.  All of them keep the target's own font resource; none
ever switches to another font.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from . import geometry as G
from .content import Glyph
from .document import Document
from .edit import BlockInsert, BlockLine, EditPlan, EditResult, Insertion, apply_plan
from .errors import DoesNotFitError, MissingGlyphsError, UnsupportedError
from .fonts import PdfFont, advance_of_codes
from .target import Target

GAP_TOLERANCE_SPACES = 0.6  # a shorter replacement may leave at most this much extra gap (in spaces)
# method a may spread a small shortfall over the replacement instead (as justification does), up to:
JUSTIFY_MAX_PER_SPACE = 0.75  # extra width per space, in spaces
JUSTIFY_MAX_PER_GLYPH = 0.04  # extra width between the glyphs of a single word, in ems
LETTER_SPACING_MAX = 0.12  # letter spacing copied from the original (method a), at most this many ems


@dataclass
class PartFit:
    line: int
    text: str
    codes: list
    natural_width: float  # points at full size
    width: float  # points at the chosen size
    original_width: float
    x0: float
    shift_after: float = 0.0
    after: list = field(default_factory=list)
    word_kern: float = 0.0  # extra text-space advance after each space (fills a justified box exactly)
    char_kern: float = 0.0  # extra text-space advance between letters (the original's letter spacing)
    lead: float = 0.0  # text-space advance before the first glyph (keeps a centred or right-aligned line so)
    after_shift: dict = field(default_factory=dict)  # gid -> points, overrides shift_after (re-justified line)


@dataclass
class Fit:
    method: str
    feasible: bool
    reason: str = ""
    parts: list = field(default_factory=list)
    scale: float = 1.0
    font_size: float = 0.0
    new_font_size: float = 0.0
    boundary: float | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "feasible": self.feasible,
            "reason": self.reason,
            "scale": round(self.scale, 4),
            "font_size": round(self.font_size, 3),
            "new_font_size": round(self.new_font_size, 3),
            "boundary": None if self.boundary is None else round(self.boundary, 3),
            "lines": [{"text": p.text, "width": round(p.width, 3), "original_width": round(p.original_width, 3),
                       "shift_after": round(p.shift_after, 3)} for p in self.parts],
            **self.details,
        }


@dataclass
class MethodResult:
    method: str
    target: dict
    replacement: str
    fit: dict
    edit: dict
    new_bbox: list | None
    font: str
    font_size: float
    new_font_size: float

    def to_dict(self) -> dict:
        return {
            "status": "ok",
            "method": self.method,
            "target": self.target,
            "replacement": self.replacement,
            "fit": self.fit,
            "edit": self.edit,
            "new_bbox": self.new_bbox,
            "font": self.font,
            "font_size": self.font_size,
            "new_font_size": self.new_font_size,
        }


# ----------------------------------------------------------------- helpers
def clean_replacement(text: str) -> str:
    return " ".join(text.split())


def _glyphs(doc: Document, target: Target) -> list[Glyph]:
    model = doc.model(target.page_index)
    return [model.glyphs[g] for g in target.glyphs]


def check_uniform_style(doc: Document, target: Target) -> None:
    gl = [g for g in _glyphs(doc, target) if not g.text.isspace()] or _glyphs(doc, target)
    first = gl[0]
    for g in gl[1:]:
        if g.font is not first.font or g.size != first.size:
            raise UnsupportedError("the target mixes fonts or font sizes; it cannot be rewritten in one font")
        # character / word spacing may vary (Quartz adjusts Tc per run); scaling, rise and render mode may not
        if (g.th, g.ts, g.tr) != (first.th, first.ts, first.tr):
            raise UnsupportedError("the target mixes horizontal scaling, rise or render modes")
        if g.fill != first.fill or g.fill_cs != first.fill_cs:
            raise UnsupportedError("the target mixes text colours")
        if not g.upright:
            raise UnsupportedError("rotated or skewed text is not supported")
    if not first.upright:
        raise UnsupportedError("rotated or skewed text is not supported")
    if not first.font.editable:
        raise UnsupportedError(f"font {first.font.base_font} cannot be used for new text: "
                               + "; ".join(first.font.problems or ["Type3 or vertical writing"]))


def _space_advance(doc: Document, target: Target) -> Decimal:
    """Text-space advance (unscaled by Th) to use for a space with no glyph."""
    model = doc.model(target.page_index)
    layout = doc.layout(target.page_index)
    first = model.glyphs[target.glyphs[0]]
    font = first.font
    if font.space_code is not None:
        return font.width(font.space_code) * first.size + first.tc
    gaps = []
    for part in target.parts:
        line = layout.lines[part.line]
        for a, b in zip(line.words, line.words[1:]):
            ga = model.glyphs[a.glyphs[-1]]
            gb = model.glyphs[b.glyphs[0]]
            if ga.font is font:
                gaps.append(gb.x - ga.x_end)
    if gaps:
        gap_user = statistics.median(gaps)
        return Decimal(str(gap_user / first.x_scale)) / first.th
    return Decimal("0.25") * first.size


def _encode(font: PdfFont, text: str) -> list:
    return font.encode(text)


def _width_points(first: Glyph, font: PdfFont, codes: list, size: Decimal, space_adv: Decimal) -> float:
    adv = advance_of_codes(font, codes, size, first.tc, first.tw, first.th, space_adv)
    return float(adv) * first.x_scale


def _part_geometry(doc: Document, target: Target):
    model = doc.model(target.page_index)
    out = []
    for p in target.parts:
        gl = [model.glyphs[g] for g in p.glyphs]
        x0 = gl[0].x
        x1 = max(g.x_end for g in gl)
        after = [model.glyphs[g] for g in p.after]
        out.append((p, x0, x1, after))
    return out


def _space_width_points(doc: Document, target: Target) -> float:
    first = _glyphs(doc, target)[0]
    return float(_space_advance(doc, target) * first.th) * first.x_scale


def _split_words(words: list[str], k: int, widths_of, limits: list[float], prefer: list[float] | None = None):
    """Split words over the k lines of a multi-line target the way text wraps.

    Each line takes as many words as fit its limit (greedy, like a word
    processor); the last line takes the rest.  When the words need fewer
    lines than the target has, or more, None is returned: the paragraph would
    have to be rewrapped (method d) instead of leaving a gap or an odd break.
    """
    n = len(words)
    if k == 1:
        w = widths_of(" ".join(words))
        return [words] if w <= limits[0] + 0.01 else None
    groups: list[list[str]] = []
    i = 0
    for part in range(k):
        if part == k - 1:
            rest = words[i:]
            if not rest or widths_of(" ".join(rest)) > limits[part] + 0.01:
                return None
            groups.append(rest)
            break
        j = i
        while j < n and widths_of(" ".join(words[i:j + 1])) <= limits[part] + 0.01:
            j += 1
        if j == i or j >= n:
            return None  # first word does not fit, or everything fits before the last line
        groups.append(words[i:j])
        i = j
    return groups


def _split_failure(words: list[str], widths_of, limits: list[float], space: str) -> str:
    """Say why ``_split_words`` found no natural split (for the refusal message)."""
    k = len(limits)
    need = widths_of(" ".join(words))
    if k == 1:
        return f"replacement needs {need:.1f} pt but {space} is {limits[0]:.1f} pt wide"
    widest = max(words, key=widths_of)
    if widths_of(widest) > min(limits) + 0.01:
        return f"the word {widest!r} needs {widths_of(widest):.1f} pt but a line of the target is only {min(limits):.1f} pt wide"
    lines, i = 0, 0
    while i < len(words):
        limit = limits[min(lines, k - 1)]
        j = i + 1
        while j < len(words) and widths_of(" ".join(words[i:j + 1])) <= limit + 0.01:
            j += 1
        lines, i = lines + 1, j
    if lines < k:
        return (f"the target spans {k} lines but the replacement wraps onto {lines}; "
                "the paragraph would have to be rewrapped (paragraph_reflow) instead of leaving a gap")
    return (f"the replacement wraps onto {lines} lines but the target spans only {k} "
            f"({need:.1f} pt of text, {sum(limits):.1f} pt of line space in {space})")


# ------------------------------------------------------------------- fits
def _common(doc: Document, target: Target, replacement: str):
    check_uniform_style(doc, target)
    first = _glyphs(doc, target)[0]
    font = first.font
    text = clean_replacement(replacement)
    if not text:
        raise UnsupportedError("the replacement is empty; deleting text is not a supported method")
    missing = font.missing_chars(text)
    if missing:
        raise MissingGlyphsError(
            f"the embedded font {font.base_font} does not contain: " + ", ".join(repr(c) for c in missing),
            missing=missing, font=font.base_font)
    return first, font, text


def fit_in_place(doc: Document, target: Target, replacement: str) -> Fit:
    """Method a: same font and size, inside the original target box."""
    try:
        first, font, text = _common(doc, target, replacement)
    except (UnsupportedError, MissingGlyphsError) as exc:
        return Fit("original_box", False, exc.message, details={"code": exc.code})
    return _fit_fixed_box(doc, target, text, first, font, Decimal(1), "original_box")


def _letter_pairs(s: str) -> int:
    return sum(1 for a, b in zip(s, s[1:]) if a != " " and b != " ")


def _letter_spacing(doc: Document, target: Target, em: float) -> float:
    """Average extra gap (points) between the target's letters as the original was set.

    Some producers position every glyph (OpenOffice, Distiller's TJ kerning, letter-spaced headings);
    the same letters at natural spacing then come out wider or narrower than the original box.
    """
    model = doc.model(target.page_index)
    gaps = []
    for p in target.parts:
        gl = [model.glyphs[g] for g in p.glyphs]
        gaps += [b.x - a.x_end for a, b in zip(gl, gl[1:]) if not a.text.isspace() and not b.text.isspace()]
    # a larger gap is between words drawn without a space glyph, not letter spacing
    gaps = [g for g in gaps if abs(g) <= LETTER_SPACING_MAX * em]
    if len(gaps) < 2:
        return 0.0
    mean = sum(gaps) / len(gaps)
    return mean if abs(mean) > 0.002 else 0.0


def _fit_fixed_box(doc, target, text, first, font, scale: Decimal, method: str) -> Fit:
    size = first.size * scale
    space_adv = _space_advance(doc, target)
    geo = _part_geometry(doc, target)
    space_pts = _space_width_points(doc, target) * float(scale)
    words = text.split(" ")
    unit = first.x_scale * float(first.th)  # points per text-space unit
    em = float(size) * first.x_scale
    # method a reproduces the original's letter spacing; a smaller size (method c) keeps natural spacing
    letter = _letter_spacing(doc, target, em) if method == "original_box" else 0.0

    def widths_of(s: str) -> float:
        return _width_points(first, font, font.encode(s), size, space_adv * scale) + letter * _letter_pairs(s)

    limits, prefer = [], []
    for p, x0, x1, after in geo:
        limits.append(x1 - x0)
        prefer.append(x1 - x0)
    groups = _split_words(words, len(geo), widths_of, limits, prefer)
    if groups is None:
        need = widths_of(text)
        return Fit(method, False, _split_failure(words, widths_of, limits, "the original box"),
                   font_size=float(first.size), new_font_size=float(size), scale=float(scale),
                   details={"needed_width": round(need, 3), "available_width": round(sum(limits), 3)})
    parts = []
    for (p, x0, x1, after), g in zip(geo, groups):
        t = " ".join(g)
        codes = font.encode(t)
        w = widths_of(t)
        pf = PartFit(p.line, t, codes, w / float(scale), w, x1 - x0, x0, 0.0, [a.gid for a in after])
        visible_after = [a for a in after if not a.text.isspace()]
        slack = (x1 - x0) - w
        pf.char_kern = letter / unit
        if visible_after and slack > GAP_TOLERANCE_SPACES * space_pts + 0.01:
            # the original was spaced out (justified text): spread the shortfall over the replacement's spaces
            # (or, for one word, its letters) as justification does, so the text after it keeps its exact gap
            n_spaces, pairs = t.count(" "), _letter_pairs(t)
            if method == "original_box" and n_spaces and slack / n_spaces <= JUSTIFY_MAX_PER_SPACE * space_pts:
                pf.word_kern = slack / n_spaces / unit
            elif method == "original_box" and not n_spaces and pairs and slack / pairs <= JUSTIFY_MAX_PER_GLYPH * em:
                pf.char_kern += slack / pairs / unit
            else:
                return Fit(method, False,
                           f"replacement is {slack:.1f} pt shorter than the original and text follows on the line; "
                           "the word spacing would be wrong",
                           font_size=float(first.size), new_font_size=float(size), scale=float(scale))
            pf.width = x1 - x0
        elif slack > 0.01 and len(geo) == 1:
            pf.lead = _alignment_lead(doc, target, p, slack) / unit
        parts.append(pf)
    return Fit(method, True, "fits", parts, float(scale), float(first.size), float(size))


def _alignment_lead(doc: Document, target: Target, part, slack: float) -> float:
    """Points to move a shorter replacement right so that a whole line of a centred (right-aligned) block stays
    centred (flush right) in the original box; 0 when the line is left-aligned or has other text on it."""
    layout = doc.layout(target.page_index)
    line = layout.lines[part.line]
    block = layout.blocks[line.block] if 0 <= line.block < len(layout.blocks) else None
    if block is None or block.alignment not in ("center", "right"):
        return 0.0
    model = doc.model(target.page_index)
    mine = set(part.glyphs)
    if any(gid not in mine and not model.glyphs[gid].text.isspace() for gid in line.glyphs):
        return 0.0
    return slack / 2 if block.alignment == "center" else slack


def fit_horizontal(doc: Document, target: Target, replacement: str, boundary: float) -> Fit:
    """Method b: the line may grow (or shrink) up to ``boundary`` (x in points)."""
    try:
        first, font, text = _common(doc, target, replacement)
    except (UnsupportedError, MissingGlyphsError) as exc:
        return Fit("horizontal_expansion", False, exc.message, details={"code": exc.code})
    size = first.size
    space_adv = _space_advance(doc, target)
    geo = _part_geometry(doc, target)
    words = text.split(" ")

    def widths_of(s: str) -> float:
        return _width_points(first, font, font.encode(s), size, space_adv)

    limits, prefer = [], []
    for p, x0, x1, after in geo:
        after_w = (max(a.x_end for a in after) - x1) if after else 0.0
        limits.append(boundary - x0 - after_w)
        prefer.append(x1 - x0)
    groups = _split_words(words, len(geo), widths_of, limits, prefer)
    if groups is None:
        need = widths_of(text)
        return Fit("horizontal_expansion", False,
                   _split_failure(words, widths_of, limits, f"the space up to the boundary at x={boundary:.1f}"),
                   boundary=boundary, font_size=float(size), new_font_size=float(size),
                   details={"needed_width": round(need, 3), "available_width": round(sum(limits), 3)})
    parts = []
    for (p, x0, x1, after), g in zip(geo, groups):
        t = " ".join(g)
        w = widths_of(t)
        pf = PartFit(p.line, t, font.encode(t), w, w, x1 - x0, x0, w - (x1 - x0), [a.gid for a in after])
        if len(geo) == 1 and w < x1 - x0 - 0.01:
            _rejustify(doc, target, p, pf, _space_width_points(doc, target), first.x_scale * float(first.th))
        parts.append(pf)
    return Fit("horizontal_expansion", True, "fits", parts, 1.0, float(size), float(size), boundary=boundary)


def _rejustify(doc: Document, target: Target, part, pf: PartFit, space_pts: float, unit: float) -> None:
    """A full line of a justified paragraph got shorter: spread the shortfall over the gaps from the replacement
    to the end of the line (as justification does) so that the line still ends at the paragraph's right edge."""
    layout = doc.layout(target.page_index)
    line = layout.lines[part.line]
    if not 0 <= line.block < len(layout.blocks):
        return
    block = layout.blocks[line.block]
    if block.alignment != "justify" or line.index == block.lines[-1] or abs(line.x1 - block.right) > 0.6:
        return
    after = set(pf.after)
    following = [w for w in line.words if w.glyphs and set(w.glyphs) <= after]
    n_own = pf.text.count(" ")
    gaps = n_own + len(following)
    slack = pf.original_width - pf.width
    if not following or slack / gaps > JUSTIFY_MAX_PER_SPACE * space_pts:
        return
    model = doc.model(target.page_index)
    word_of = {gid: j for j, w in enumerate(following, 1) for gid in w.glyphs}
    if any(gid not in word_of and not model.glyphs[gid].text.isspace() for gid in pf.after):
        return  # the target ends inside a word: the rest of that word must stay attached to it
    per_gap = slack / gaps
    pf.word_kern = per_gap / unit if n_own else 0.0
    starts = [(model.glyphs[w.glyphs[0]].x, j) for j, w in enumerate(following, 1)]
    for gid in pf.after:
        k = word_of.get(gid)
        if k is None:  # a space moves with the word after it; anything after the last word keeps its place
            x = model.glyphs[gid].x
            k = next((j for sx, j in starts if sx >= x), len(following))
        pf.after_shift[gid] = -slack + (n_own + k) * per_gap
    pf.width = pf.original_width


def fit_scaled(doc: Document, target: Target, replacement: str, min_scale: float = 0.9) -> Fit:
    """Method c: a smaller size for the replaced text only, never below ``min_scale``."""
    try:
        first, font, text = _common(doc, target, replacement)
    except (UnsupportedError, MissingGlyphsError) as exc:
        return Fit("reduced_font", False, exc.message, details={"code": exc.code})
    natural = _fit_fixed_box(doc, target, text, first, font, Decimal(1), "reduced_font")
    if natural.feasible:
        return Fit("reduced_font", False, "the replacement already fits at full size; no reduction needed",
                   font_size=float(first.size), new_font_size=float(first.size))
    needed = natural.details.get("needed_width")
    available = natural.details.get("available_width")
    if not needed or not available:
        return natural
    # try scales from the largest that could fit, downwards, in 0.01 pt font-size steps
    size = first.size
    start = min(Decimal(1), Decimal(str(available / needed)))
    new_size = (size * start).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    floor_size = size * Decimal(str(min_scale))
    while new_size >= floor_size and new_size > 0:
        scale = new_size / size
        fit = _fit_fixed_box(doc, target, text, first, font, scale, "reduced_font")
        if fit.feasible:
            fit.details["min_font_scale"] = min_scale
            return fit
        new_size -= Decimal("0.01")
    return Fit("reduced_font", False,
               f"even at {min_scale:.0%} of the original size the replacement does not fit "
               f"(it would need {needed / available:.0%} of the space)",
               font_size=float(size), new_font_size=float(floor_size), scale=min_scale,
               details={"needed_width": needed, "available_width": available, "min_font_scale": min_scale})


# ---------------------------------------------------------------- actions
def _apply_fit(doc: Document, target: Target, fit: Fit, replacement: str) -> MethodResult:
    if not fit.feasible:
        raise DoesNotFitError(fit.reason, fit=fit.to_dict())
    first = _glyphs(doc, target)[0]
    scale = Decimal(str(fit.scale))
    size = None if fit.scale == 1.0 else (first.size * scale).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    space_adv = _space_advance(doc, target) * (scale if size is not None else 1)
    plan = EditPlan(target.page_index)
    plan.remove = set(target.glyphs)
    model = doc.model(target.page_index)
    for tp, pf in zip(target.parts, fit.parts):
        anchor = tp.glyphs[0]
        plan.inserts.append(Insertion(anchor, pf.codes, size, space_adv, pf.text,
                                      Decimal(str(round(pf.word_kern, 6))), Decimal(str(round(pf.char_kern, 6))),
                                      Decimal(str(round(pf.lead, 6)))))
        if pf.shift_after or pf.after_shift:
            for gid in pf.after:
                plan.shift[gid] = pf.after_shift.get(gid, pf.shift_after)
    target_dict = target.to_dict()
    result: EditResult = apply_plan(doc, plan)
    bbox = result.bbox(first.font)
    return MethodResult(fit.method, target_dict, clean_replacement(replacement), fit.to_dict(), result.to_dict(),
                        bbox.as_list() if bbox else None, first.font.base_font, float(first.size),
                        float(size) if size is not None else float(first.size))


def replace_in_place(doc: Document, target: Target, replacement: str) -> MethodResult:
    """Method a (original_box): rewrite the text in place in the same font and size."""
    return _apply_fit(doc, target, fit_in_place(doc, target, replacement), replacement)


def expand_horizontal(doc: Document, target: Target, replacement: str, boundary: float) -> MethodResult:
    """Method b (horizontal_expansion): let the line grow up to ``boundary`` (x, points)."""
    return _apply_fit(doc, target, fit_horizontal(doc, target, replacement, float(boundary)), replacement)


def replace_with_scale(doc: Document, target: Target, replacement: str, min_scale: float = 0.9,
                       scale: float | None = None) -> MethodResult:
    """Method c (reduced_font): shrink only the replaced text, never below ``min_scale``."""
    if scale is not None:
        if scale < min_scale:
            raise DoesNotFitError(f"requested scale {scale} is below the minimum {min_scale}")
        first, font, text = _common(doc, target, replacement)
        fit = _fit_fixed_box(doc, target, text, first, font, Decimal(str(scale)), "reduced_font")
    else:
        fit = fit_scaled(doc, target, replacement, min_scale)
    return _apply_fit(doc, target, fit, replacement)


# ------------------------------------------------------- low-level helpers
def remove_text_run(doc: Document, target: Target) -> EditResult:
    """Remove the target glyphs from the content stream; everything else stays in place."""
    plan = EditPlan(target.page_index, remove=set(target.glyphs))
    return apply_plan(doc, plan)


def write_text(doc: Document, like: Target, text: str, x: float, y: float, size: float | None = None) -> EditResult:
    """Draw ``text`` at page position (x, y) in the font, colour and state of ``like``.

    The new text is written in its own BT/ET block right after the block that
    contains the first glyph of ``like``, using the same font resource.
    """
    model = doc.model(like.page_index)
    g = model.glyphs[like.glyphs[0]]
    check_uniform_style(doc, like)
    font = g.font
    codes = font.encode(clean_replacement(text))
    bt = model.bt_blocks.get(g.bt)
    if bt is None or bt.et_index is None:
        raise UnsupportedError("the target's text block has no end")
    if bt.ctm_changed_inside:
        raise UnsupportedError("the CTM changes inside the text block")
    a, b, c, d = g.tm[0], g.tm[1], g.tm[2], g.tm[3]
    inv = G.inverse(g.ctm)
    tx, ty = G.apply(inv, x, y)
    fsize = Decimal(str(size)) if size is not None else g.size
    block = BlockInsert(
        after=(g.stream, bt.et_index), font=font, font_res=g.font_res, size=fsize, tm_linear=(a, b, c, d),
        lines=[BlockLine(tx, ty, codes, Decimal(0), text)], tc=g.tc, th=g.th, ts=g.ts, tr=g.tr,
        fill_ops=g.fill_ops, space_advance=_space_advance(doc, like), ctm=g.ctm,
    )
    return apply_plan(doc, EditPlan(like.page_index, blocks=[block]))
