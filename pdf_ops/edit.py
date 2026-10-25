"""The edit engine: rewrites text-showing instructions in place.

An :class:`EditPlan` says, for one page,

* which glyphs to remove,
* how far (in PDF points, along the line) other glyphs should move,
* which new text to insert, starting at the original position of an
  *anchor* glyph, in the anchor's own font resource (optionally at a smaller
  size), and
* optional new text blocks (used by paragraph reflow).

The engine walks each affected *flow* (a run of show instructions with no
repositioning in between) and rebuilds the TJ operands.  Removed glyphs are
cut out of their strings; wherever the actual pen position would differ from
the planned one, a TJ kerning number is inserted, computed with exact decimal
arithmetic, so every glyph that is not meant to move lands exactly where it
was.  Tj, ' and " instructions are converted to equivalent TJ forms when they
have to change.

Streams are never edited in place: every changed content stream is written
as a new stream object attached to this page only, and a form XObject is
copied (with a page-private resource dictionary) before its content changes,
so pages that share streams or XObjects are not affected.

After writing, the page is interpreted again and every glyph is compared with
the plan; any difference raises SelfCheckError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, getcontext

import pikepdf

from . import geometry as G
from .content import FLOW_BREAK_OPS, SHOW_OPS, D, Glyph, PageModel, page_resources
from .document import Document
from .errors import PdfOpsError, SelfCheckError, UnsupportedError
from .fonts import PdfFont

getcontext().prec = 34
Q6 = Decimal("0.000001")
POSITION_TOLERANCE = 0.02  # points


@dataclass
class Insertion:
    anchor: int  # gid; new text starts at this glyph's original origin
    codes: list  # int codes; None = a space drawn as positioning only
    size: Decimal | None = None  # temporary Tf size (reduced_font), None = unchanged
    space_advance: Decimal | None = None  # text-space advance for None codes (unscaled by Th)
    text: str = ""


@dataclass
class BlockLine:
    x: float  # text-space origin (inside the Tm linear part), i.e. the Tm e/f values
    y: float
    codes: list  # int codes or None (positioned space)
    word_kern: Decimal = Decimal(0)  # extra text-space advance added at each space (justification)
    text: str = ""


@dataclass
class BlockInsert:
    after: tuple  # (stream key, instruction index) the new block is inserted after
    font: PdfFont
    font_res: str
    size: Decimal
    tm_linear: tuple  # (a, b, c, d) of the text matrix used by the original lines
    lines: list[BlockLine]
    tc: Decimal = Decimal(0)
    th: Decimal = Decimal(1)
    ts: Decimal = Decimal(0)
    tr: int = 0
    fill_ops: tuple = ()
    space_advance: Decimal | None = None
    ctm: tuple = G.IDENTITY


@dataclass
class EditPlan:
    page_index: int
    remove: set = field(default_factory=set)
    shift: dict = field(default_factory=dict)  # gid -> dx in points (user space)
    inserts: list = field(default_factory=list)
    blocks: list = field(default_factory=list)


@dataclass
class NewGlyph:
    text: str
    x: float
    y: float
    x_end: float
    size: float  # effective size


@dataclass
class EditResult:
    new_glyphs: list[NewGlyph]
    streams_changed: list[str]
    xobjects_copied: int
    warnings: list[str] = field(default_factory=list)

    def bbox(self, font: PdfFont | None = None) -> G.Rect | None:
        if not self.new_glyphs:
            return None
        asc = font.ascent if font else 0.8
        desc = font.descent if font else -0.2
        return G.union_all(G.Rect(g.x, g.y + desc * g.size, g.x_end, g.y + asc * g.size) for g in self.new_glyphs)

    def to_dict(self) -> dict:
        return {
            "streams_changed": self.streams_changed,
            "xobjects_copied": self.xobjects_copied,
            "new_text": "".join(g.text for g in self.new_glyphs),
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
def _q(value: Decimal) -> Decimal:
    return value.quantize(Q6, rounding=ROUND_HALF_EVEN).normalize() if value != 0 else Decimal(0)


def _num_obj(value: Decimal):
    v = _q(value)
    if v == v.to_integral_value():
        return int(v)
    return v


def _flow_segments(instructions: list) -> list[list[int]]:
    """Instruction indices of show ops, grouped by flow."""
    segments: list[list[int]] = []
    cur: list[int] = []
    for idx, ins in enumerate(instructions):
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            continue
        op = str(ins.operator)
        if op in FLOW_BREAK_OPS or op == "ET":
            if cur:
                segments.append(cur)
            cur = []
        if op in SHOW_OPS:
            cur.append(idx)
    if cur:
        segments.append(cur)
    return segments


class _Committer:
    """Writes changed streams as new, page-private objects."""

    def __init__(self, doc: Document, model: PageModel):
        self.doc = doc
        self.pdf = doc.pdf
        self.model = model
        self.page = model.page.obj
        self.private: dict[tuple, pikepdf.Object] = {}
        self.pending: dict[tuple, list] = {}
        self.page_res_done = False
        self.private_subdicts: set = set()
        self.copied = 0

    def _shallow(self, d) -> pikepdf.Dictionary:
        return pikepdf.Dictionary({k: d[k] for k in d.keys()})

    def private_resources(self, key: tuple) -> pikepdf.Dictionary:
        if len(key) == 2:
            if not self.page_res_done:
                res = page_resources(self.page)
                self.page.Resources = self._shallow(res) if res is not None else pikepdf.Dictionary()
                self.page_res_done = True
            return self.page.Resources
        return self.private_object(key).Resources

    def _private_sub(self, res: pikepdf.Dictionary, name: str) -> pikepdf.Dictionary:
        marker = (id(res), name)
        if marker not in self.private_subdicts:
            sub = res.get(name)
            res[name] = self._shallow(sub) if sub is not None else pikepdf.Dictionary()
            self.private_subdicts.add(marker)
        return res[name]

    def private_object(self, key: tuple) -> pikepdf.Object:
        if key in self.private:
            return self.private[key]
        info = self.model.streams[key]
        old = info.obj
        new = self.pdf.make_stream(old.read_bytes())
        for k in old.keys():
            if k in ("/Length", "/Filter", "/DecodeParms", "/Resources"):
                continue
            new[k] = old[k]
        res = old.get("/Resources")
        if res is None:
            res = info.resources
        new.Resources = self._shallow(res) if res is not None else pikepdf.Dictionary()
        parent_res = self.private_resources(info.parent)
        xo = self._private_sub(parent_res, "/XObject")
        if info.invocations > 1:
            n = 1
            while pikepdf.Name("/" + f"{info.name}_e{n}") in xo:
                n += 1
            new_name = f"{info.name}_e{n}"
            xo["/" + new_name] = new
            instrs = self.pending.setdefault(info.parent, list(self.model.streams[info.parent].instructions))
            instrs[info.do_index] = pikepdf.ContentStreamInstruction([pikepdf.Name("/" + new_name)], pikepdf.Operator("Do"))
        else:
            xo["/" + info.name] = new
        self.private[key] = new
        self.copied += 1
        return new

    def commit(self, edits: dict[tuple, list]) -> list[str]:
        self.pending.update(edits)
        for key in sorted([k for k in self.pending if len(k) > 2], key=len, reverse=True):
            self.private_object(key)
        changed = []
        for key, instrs in self.pending.items():
            data = pikepdf.unparse_content_stream(instrs)
            if len(key) == 2:
                k = key[1]
                new = self.pdf.make_stream(data)
                contents = self.page.get("/Contents")
                if isinstance(contents, pikepdf.Array):
                    arr = pikepdf.Array(list(contents))
                    arr[k] = new
                    self.page.Contents = arr
                else:
                    self.page.Contents = new
            else:
                self.private_object(key).write(data)
            changed.append("/".join(str(p) for p in key))
        return changed


# --------------------------------------------------------------------------
class _FlowRewriter:
    """Rebuilds the show instructions of one stream."""

    def __init__(self, model: PageModel, key: tuple, plan: EditPlan, expected: list):
        self.model = model
        self.key = key
        self.info = model.streams[key]
        self.plan = plan
        self.expected = expected
        self.glyph_at: dict[tuple, Glyph] = {}
        self.by_op: dict[int, list[Glyph]] = {}
        for g in model.glyphs:
            if g.stream == key:
                self.glyph_at[(g.op_index, g.elem_index, g.byte_offset)] = g
                self.by_op.setdefault(g.op_index, []).append(g)
        self.anchors = {ins.anchor: ins for ins in plan.inserts}

    def displacement(self, g: Glyph) -> Decimal:
        dx = self.plan.shift.get(g.gid, 0.0)
        if not dx:
            return Decimal(0)
        xs = g.x_scale
        if xs <= 0:
            raise UnsupportedError("cannot shift text that is mirrored or rotated")
        return D(dx / xs)

    def rewrite(self) -> list:
        instrs = list(self.info.instructions)
        replacements: dict[int, list] = {}
        for segment in _flow_segments(instrs):
            gl = [g for op in segment for g in self.by_op.get(op, [])]
            touched = any(g.gid in self.plan.remove or g.gid in self.plan.shift or g.gid in self.anchors for g in gl)
            if not touched:
                continue
            self._rewrite_segment(instrs, segment, replacements)
        out = []
        for idx, ins in enumerate(instrs):
            if idx in replacements:
                out.extend(replacements[idx])
            else:
                out.append(ins)
        return out

    def _rewrite_segment(self, instrs, segment, replacements):
        offset = Decimal(0)  # actual pen position minus original, text space
        for op_index in segment:
            ins = instrs[op_index]
            op = str(ins.operator)
            st = self.info.show_state.get(op_index)
            if st is None or st.font is None:
                raise UnsupportedError("show instruction without a known font")
            font = st.font
            if op == "TJ":
                elements = list(ins.operands[0])
            elif op == '"':
                elements = [ins.operands[2]]
            else:
                elements = [ins.operands[0]]
            size, th = st.size, st.th
            out_ops: list = []  # finished instructions for this op
            cur: list = []  # current TJ array elements
            buf = bytearray()
            changed = False

            def flush_buf():
                nonlocal buf
                if buf:
                    cur.append(pikepdf.String(bytes(buf)))
                    buf = bytearray()

            def emit_kern(dx: Decimal, fsize: Decimal) -> Decimal:
                """Move the pen by dx (text space); returns the realised move."""
                if dx == 0:
                    return Decimal(0)
                if fsize == 0 or th == 0:
                    raise UnsupportedError("zero font size or horizontal scale")
                n = _q(-dx * 1000 / (fsize * th))
                flush_buf()
                cur.append(_num_obj(n))
                return -n / 1000 * fsize * th

            def close_tj():
                flush_buf()
                if cur:
                    out_ops.append(pikepdf.ContentStreamInstruction([pikepdf.Array(list(cur))], pikepdf.Operator("TJ")))
                cur.clear()

            for elem_index, elem in enumerate(elements):
                if not isinstance(elem, pikepdf.String):
                    flush_buf()
                    cur.append(elem)
                    continue
                data = bytes(elem)
                for code, offs, nbytes in font.split_codes(data):
                    g = self.glyph_at.get((op_index, elem_index if op == "TJ" else 0, offs))
                    if g is None:
                        raise PdfOpsError("internal error: glyph lookup failed", op_index=op_index)
                    if g.gid in self.anchors:
                        ins_ = self.anchors[g.gid]
                        offset += emit_kern(-offset, size)
                        changed = True
                        new_size = ins_.size if ins_.size is not None else size
                        if ins_.size is not None:
                            if not st.font_res:
                                raise UnsupportedError("font set through ExtGState cannot be resized")
                            close_tj()
                            out_ops.append(pikepdf.ContentStreamInstruction(
                                [pikepdf.Name("/" + st.font_res), _num_obj(new_size)], pikepdf.Operator("Tf")))
                        pen = Decimal(0)
                        for c in ins_.codes:
                            if c is None:
                                sp = (ins_.space_advance if ins_.space_advance is not None else Decimal("0.25") * new_size)
                                pen += emit_kern(sp * th, new_size)
                                continue
                            adv = (font.width(c) * new_size + st.tc + (st.tw if font.is_word_space(c, font.code_length) else 0)) * th
                            self._expect_new(g, pen, adv, font, c, new_size)
                            buf.extend(font.code_bytes(c))
                            pen += adv
                        offset += pen
                        if ins_.size is not None:
                            close_tj()
                            out_ops.append(pikepdf.ContentStreamInstruction(
                                [pikepdf.Name("/" + st.font_res), _num_obj(size)], pikepdf.Operator("Tf")))
                    if g.gid in self.plan.remove:
                        offset -= g.advance
                        changed = True
                        continue
                    want = self.displacement(g)
                    if offset != want:
                        offset += emit_kern(want - offset, size)
                        changed = True
                    self.expected.append(("orig", g, float(want) * g.x_scale))
                    buf.extend(data[offs:offs + nbytes])
            flush_buf()
            if not changed:
                continue
            close_tj()
            prefix = []
            if op == "'":
                prefix = [pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*"))]
            elif op == '"':
                prefix = [
                    pikepdf.ContentStreamInstruction([ins.operands[0]], pikepdf.Operator("Tw")),
                    pikepdf.ContentStreamInstruction([ins.operands[1]], pikepdf.Operator("Tc")),
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")),
                ]
            if not out_ops:
                # everything was removed: keep an empty TJ so the instruction count stays sensible
                out_ops = [pikepdf.ContentStreamInstruction([pikepdf.Array([])], pikepdf.Operator("TJ"))]
            replacements[op_index] = prefix + out_ops

    def _expect_new(self, anchor: Glyph, pen: Decimal, adv: Decimal, font: PdfFont, code: int, size: Decimal):
        user = anchor.user_matrix
        xs = user[0]
        x = anchor.x + float(pen) * xs
        eff = float(size) * G.vertical_scale(user)
        self.expected.append(("new", font.to_unicode(code) or "", x, anchor.y, x + float(adv) * xs, eff))


# --------------------------------------------------------------------------
def _block_instructions(b: BlockInsert, expected: list) -> list:
    I = pikepdf.ContentStreamInstruction
    O = pikepdf.Operator
    out = [I([], O("q"))]
    for operands, op in b.fill_ops:
        out.append(I(list(operands), O(op)))
    out.append(I([], O("BT")))
    out.append(I([pikepdf.Name("/" + b.font_res), _num_obj(b.size)], O("Tf")))
    out.append(I([_num_obj(b.tc)], O("Tc")))
    out.append(I([0], O("Tw")))
    out.append(I([_num_obj(b.th * 100)], O("Tz")))
    out.append(I([_num_obj(b.ts)], O("Ts")))
    out.append(I([b.tr], O("Tr")))
    a, bb, c, d = b.tm_linear
    font = b.font
    for line in b.lines:
        out.append(I([_num_obj(D(a)), _num_obj(D(bb)), _num_obj(D(c)), _num_obj(D(d)), _num_obj(D(line.x)), _num_obj(D(line.y))], O("Tm")))
        arr: list = []
        buf = bytearray()
        pen = Decimal(0)
        user = G.mul((float(a), float(bb), float(c), float(d), line.x, line.y), b.ctm)
        ox, oy = G.apply(user, 0, 0)
        xs = user[0]
        eff = float(b.size) * G.vertical_scale(user)

        def kern(dx: Decimal) -> Decimal:
            nonlocal buf
            if dx == 0:
                return Decimal(0)
            if buf:
                arr.append(pikepdf.String(bytes(buf)))
                buf = bytearray()
            n = _q(-dx * 1000 / (b.size * b.th))
            arr.append(_num_obj(n))
            return -n / 1000 * b.size * b.th

        for code in line.codes:
            if code is None:
                sp = b.space_advance if b.space_advance is not None else Decimal("0.25") * b.size
                pen += kern((sp + line.word_kern) * b.th)
                continue
            adv = (font.width(code) * b.size + b.tc) * b.th
            x = ox + float(pen) * xs
            ch = font.to_unicode(code) or ""
            expected.append(("new", ch, x, oy, x + float(adv) * xs, eff))
            buf.extend(font.code_bytes(code))
            pen += adv
            if ch == " " and line.word_kern:
                pen += kern(line.word_kern * b.th)
        if buf:
            arr.append(pikepdf.String(bytes(buf)))
        out.append(I([pikepdf.Array(arr)], O("TJ")))
    out.append(I([], O("ET")))
    out.append(I([], O("Q")))
    return out


def apply_plan(doc: Document, plan: EditPlan) -> EditResult:
    model = doc.model(plan.page_index)
    expected: list = []
    for gid in list(plan.remove) + list(plan.shift) + [i.anchor for i in plan.inserts]:
        g = model.glyphs[gid]
        if not g.upright:
            raise UnsupportedError("rotated, mirrored or skewed text is not supported")
        if g.font.is_type3:
            raise UnsupportedError("Type3 fonts are not supported")
    for ins in plan.inserts:
        g = model.glyphs[ins.anchor]
        if ins.codes and not g.font.editable:
            raise UnsupportedError(f"font {g.font.base_font} cannot be written: {g.font.problems}")

    keys = {model.glyphs[g].stream for g in list(plan.remove) + list(plan.shift)} | {model.glyphs[i.anchor].stream for i in plan.inserts}
    edits: dict[tuple, list] = {}
    for key in keys:
        edits[key] = _FlowRewriter(model, key, plan, expected).rewrite()
    # glyphs in untouched streams are expected unchanged
    touched_gids = {e[1].gid for e in expected if e[0] == "orig"}
    for g in model.glyphs:
        if g.gid not in plan.remove and g.gid not in touched_gids:
            expected.append(("orig", g, 0.0))

    for b in plan.blocks:
        key, after_idx = b.after
        instrs = edits.get(key)
        if instrs is None:
            instrs = list(model.streams[key].instructions)
        # the index refers to the original list; map it through replacements by locating the ET
        new_block = _block_instructions(b, expected)
        pos = _locate_original(model.streams[key].instructions, instrs, after_idx)
        instrs[pos + 1:pos + 1] = new_block
        edits[key] = instrs

    committer = _Committer(doc, model)
    changed = committer.commit(edits)
    doc.invalidate(plan.page_index)
    _self_check(doc, plan.page_index, expected)
    new_glyphs = [NewGlyph(e[1], e[2], e[3], e[4], e[5]) for e in expected if e[0] == "new"]
    result = EditResult(new_glyphs, changed, committer.copied)
    doc.log.append({"page": plan.page_index + 1, "removed": len(plan.remove), "shifted": len(plan.shift),
                    "inserted": "".join(g.text for g in new_glyphs), "streams": changed})
    return result


def _locate_original(original: list, rewritten: list, index: int) -> int:
    """Position in ``rewritten`` of the instruction that was ``original[index]``."""
    target = original[index]
    for pos, ins in enumerate(rewritten):
        if ins is target:
            return pos
    # the instruction itself was replaced (should not happen for ET); fall back to the end
    raise PdfOpsError("could not locate the insertion point for the new text block")


def _self_check(doc: Document, page_index: int, expected: list) -> None:
    model = doc.model(page_index)
    want: dict[str, list] = {}
    for e in expected:
        if e[0] == "orig":
            g = e[1]
            want.setdefault(g.text, []).append((g.x + e[2], g.y))
        else:
            want.setdefault(e[1], []).append((e[2], e[3]))
    have: dict[str, list] = {}
    for g in model.glyphs:
        have.setdefault(g.text, []).append((g.x, g.y))
    n_want = sum(len(v) for v in want.values())
    if n_want != len(model.glyphs):
        raise SelfCheckError(f"edit produced {len(model.glyphs)} glyphs, expected {n_want}")
    for text, positions in want.items():
        pool = sorted(have.get(text, []))
        if len(pool) != len(positions):
            raise SelfCheckError(f"expected {len(positions)} glyph(s) {text!r}, found {len(pool)}")
        for wx, wy in sorted(positions):
            best = None
            for i, (hx, hy) in enumerate(pool):
                d = abs(hx - wx) + abs(hy - wy)
                if best is None or d < best[0]:
                    best = (d, i)
                if hx > wx + 1:
                    break
            if best is None or best[0] > 2 * POSITION_TOLERANCE:
                raise SelfCheckError(f"glyph {text!r} expected at ({wx:.3f}, {wy:.3f}) is missing or moved")
            pool.pop(best[1])
