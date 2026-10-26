"""Content-stream interpreter.

Walks every content stream of a page (including several /Contents streams and
nested form XObjects) and records:

* every glyph, with the exact instruction / operand / byte position it came
  from, its font, size, text state, colour and position on the page;
* vector graphics (lines, rectangles, curves), images and shadings;
* the instruction lists themselves, so that the edit engine can rewrite them.

Stream keys identify *where* an instruction lives:
``("p", k)`` is the k-th page content stream, and
``parent_key + ("x", op_index, name)`` is the content of a form XObject drawn
by the ``Do`` instruction at ``op_index`` of the parent stream.  The same
XObject drawn twice therefore has two keys, which lets the editor copy it
before changing only one of the uses.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from decimal import Decimal

import pikepdf

from . import geometry as G
from .fonts import FontCache, PdfFont

SHOW_OPS = {"Tj", "TJ", "'", '"'}
FLOW_BREAK_OPS = {"BT", "Td", "TD", "Tm", "T*", "'", '"'}


def D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(str(float(value))) if not isinstance(value, pikepdf.Object) else Decimal(str(value))
    except Exception:
        return Decimal(0)


@dataclass
class TextState:
    font: PdfFont | None = None
    font_res: str | None = None
    size: Decimal = Decimal(0)
    tc: Decimal = Decimal(0)
    tw: Decimal = Decimal(0)
    th: Decimal = Decimal(1)
    tl: Decimal = Decimal(0)
    ts: Decimal = Decimal(0)
    tr: int = 0

    def copy(self) -> "TextState":
        return TextState(self.font, self.font_res, self.size, self.tc, self.tw, self.th, self.tl, self.ts, self.tr)


@dataclass
class GraphicsState:
    ctm: tuple = G.IDENTITY
    text: TextState = field(default_factory=TextState)
    fill_cs: str = "DeviceGray"
    fill: tuple = (0.0,)
    stroke_cs: str = "DeviceGray"
    stroke: tuple = (0.0,)
    line_width: float = 1.0
    gs_names: tuple = ()
    fill_ops: tuple = ()  # instructions that re-create the fill colour

    def copy(self) -> "GraphicsState":
        return GraphicsState(self.ctm, self.text.copy(), self.fill_cs, self.fill, self.stroke_cs, self.stroke,
                             self.line_width, self.gs_names, self.fill_ops)


@dataclass
class Glyph:
    gid: int
    stream: tuple
    op_index: int
    elem_index: int
    byte_offset: int
    byte_len: int
    code: int
    text: str
    font: PdfFont
    font_res: str
    size: Decimal
    tc: Decimal
    tw: Decimal
    th: Decimal
    ts: Decimal
    tr: int
    advance: Decimal  # text-space advance including Tc / Tw, times Th
    tm: tuple  # text matrix at the glyph origin (text space -> user space before CTM)
    ctm: tuple
    flow: tuple  # (stream key, flow number)
    bt: tuple  # (stream key, op index of BT)
    x: float  # origin on the page (user space)
    y: float
    x_end: float  # origin + advance on the page
    bbox: G.Rect
    effective_size: float
    fill_cs: str
    fill: tuple
    fill_ops: tuple
    gs_names: tuple
    upright: bool

    @property
    def user_matrix(self) -> tuple:
        return G.mul(self.tm, self.ctm)

    @property
    def x_scale(self) -> float:
        return self.user_matrix[0]

    @property
    def visible(self) -> bool:
        return self.tr not in (3, 7)

    def to_dict(self) -> dict:
        return {
            "gid": self.gid,
            "text": self.text,
            "font": self.font.base_font,
            "font_res": self.font_res,
            "size": float(self.size),
            "effective_size": round(self.effective_size, 3),
            "bbox": self.bbox.as_list(),
            "origin": [round(self.x, 3), round(self.y, 3)],
            "color": [self.fill_cs, list(self.fill)],
            "render_mode": self.tr,
        }


@dataclass
class Graphic:
    kind: str  # "line", "rect", "curve", "shading"
    bbox: G.Rect
    stroke: bool
    fill: bool
    fill_color: tuple
    stroke_color: tuple
    line_width: float
    stream: tuple
    op_index: int

    def to_dict(self) -> dict:
        return {"kind": self.kind, "bbox": self.bbox.as_list(), "stroke": self.stroke, "fill": self.fill,
                "fill_color": list(self.fill_color), "stroke_color": list(self.stroke_color)}


@dataclass
class ImagePlacement:
    name: str | None
    bbox: G.Rect
    stream: tuple
    op_index: int
    width_px: int = 0
    height_px: int = 0


@dataclass
class StreamInfo:
    key: tuple
    obj: pikepdf.Object | None  # the stream object (None for an inline-concatenated stream)
    instructions: list
    resources: pikepdf.Object | None
    parent: tuple | None = None
    do_index: int | None = None
    name: str | None = None
    invocations: int = 1
    show_state: dict = field(default_factory=dict)  # op index -> TextState at that show op
    show_tlm: dict = field(default_factory=dict)  # op index -> text line matrix at that show op
    concatenated: bool = False  # all of the page's /Contents streams read as one (see interpret_page)


@dataclass
class BTBlock:
    stream: tuple
    bt_index: int
    et_index: int | None
    ctm: tuple
    ctm_changed_inside: bool = False


class PageModel:
    """Everything the interpreter learned about one page."""

    def __init__(self, page_index: int, page: pikepdf.Page):
        self.page_index = page_index
        self.page = page
        self.streams: dict[tuple, StreamInfo] = {}
        self.stream_order: list[tuple] = []
        self.glyphs: list[Glyph] = []
        self.graphics: list[Graphic] = []
        self.images: list[ImagePlacement] = []
        self.bt_blocks: dict[tuple, BTBlock] = {}
        self.xobject_uses: dict[tuple, int] = {}  # (resources objgen/id, name) -> count
        self.warnings: list[str] = []
        self.unsupported: list[str] = []

    @property
    def text(self) -> str:
        return "".join(g.text for g in self.glyphs)

    def glyph(self, gid: int) -> Glyph:
        return self.glyphs[gid]


def _resource_key(res) -> object:
    if res is None:
        return None
    return res.objgen if res.is_indirect else id(res)


def page_resources(page_obj: pikepdf.Object):
    node = page_obj
    seen = 0
    while node is not None and seen < 64:
        if "/Resources" in node:
            return node.Resources
        node = node.get("/Parent")
        seen += 1
    return None


def _parse(stream_obj) -> list:
    try:
        return list(pikepdf.parse_content_stream(stream_obj))
    except Exception:
        return []


def _parse_checked(stream_or_page) -> tuple[list, bool]:
    """Parse; the flag is False when the parser complained (e.g. "Unexpected end of stream")."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            return list(pikepdf.parse_content_stream(stream_or_page)), not caught
        except Exception:
            return [], False


class Interpreter:
    def __init__(self, pdf: pikepdf.Pdf, fonts: FontCache | None = None, max_depth: int = 12):
        self.pdf = pdf
        self.fonts = fonts or FontCache()
        self.max_depth = max_depth

    # ----------------------------------------------------------------- API
    def interpret_page(self, page_index: int) -> PageModel:
        page = self.pdf.pages[page_index]
        model = PageModel(page_index, page)
        resources = page_resources(page.obj)
        contents = page.obj.get("/Contents")
        streams = []
        if isinstance(contents, pikepdf.Array):
            streams = [s for s in contents if isinstance(s, pikepdf.Stream)]
        elif isinstance(contents, pikepdf.Stream):
            streams = [contents]
        gs = GraphicsState()
        self._stack: list[GraphicsState] = []
        self._flow_counter = 0
        parsed, clean = [], True
        for s in streams:
            ins, ok = _parse_checked(s)
            parsed.append(ins)
            clean = clean and ok
        if len(streams) > 1 and not clean:
            # the streams are one sequence and may be split anywhere between tokens, even between an
            # operator and its operands (Acrobat Distiller does this): read them as one stream
            ins, _ = _parse_checked(page.obj)
            info = StreamInfo(("p", 0), None, ins, resources, concatenated=True)
            model.streams[info.key] = info
            model.stream_order.append(info.key)
            gs = self._run(model, info, gs, depth=0)
            streams = []
        for k, s in enumerate(streams):
            key = ("p", k)
            info = StreamInfo(key, s, parsed[k], resources)
            model.streams[key] = info
            model.stream_order.append(key)
            gs = self._run(model, info, gs, depth=0)
        self._count_xobject_uses(model)
        return model

    def interpret_form(self, xobj: pikepdf.Object, key: tuple = ("ap", 0)) -> PageModel:
        """Interpret a standalone form XObject (e.g. an annotation appearance)."""
        model = PageModel(-1, None)
        res = xobj.get("/Resources")
        info = StreamInfo(key, xobj, _parse(xobj), res)
        model.streams[key] = info
        model.stream_order.append(key)
        gs = GraphicsState()
        m = xobj.get("/Matrix")
        if m is not None:
            gs.ctm = G.mat(m)
        self._stack = []
        self._flow_counter = 0
        self._run(model, info, gs, depth=0)
        return model

    # ------------------------------------------------------------ helpers
    def _count_xobject_uses(self, model: PageModel) -> None:
        counts: dict[tuple, int] = {}
        for info in model.streams.values():
            if info.parent is not None:
                parent = model.streams[info.parent]
                k = (_resource_key(parent.resources), info.name)
                counts[k] = counts.get(k, 0) + 1
        model.xobject_uses = counts
        for info in model.streams.values():
            if info.parent is not None:
                parent = model.streams[info.parent]
                info.invocations = counts[(_resource_key(parent.resources), info.name)]

    def _font(self, resources, name: str) -> PdfFont | None:
        if resources is None:
            return None
        fonts = resources.get("/Font")
        if fonts is None:
            return None
        obj = fonts.get("/" + name if not name.startswith("/") else name)
        if obj is None:
            return None
        return self.fonts.get(obj)

    def _run(self, model: PageModel, info: StreamInfo, gs: GraphicsState, depth: int) -> GraphicsState:
        tm = G.IDENTITY
        tlm = G.IDENTITY
        in_bt = False
        bt_key = None
        flow = None
        path_points: list = []
        subpaths: list = []
        current_subpath: list = []
        rects: list = []
        res = info.resources

        def flush_path(stroke: bool, fill: bool, op_index: int):
            nonlocal subpaths, current_subpath, rects
            if current_subpath:
                subpaths.append(current_subpath)
            for r in rects:
                bbox = G.Rect.from_points([G.apply(gs.ctm, x, y) for x, y in r])
                model.graphics.append(Graphic("rect", bbox, stroke, fill, gs.fill, gs.stroke, gs.line_width, info.key, op_index))
            for sp in subpaths:
                pts = [G.apply(gs.ctm, x, y) for (x, y, _) in sp]
                if not pts:
                    continue
                curved = any(kind == "c" for (_, _, kind) in sp)
                if not curved and len(pts) == 2:
                    model.graphics.append(Graphic("line", G.Rect.from_points(pts), stroke, fill, gs.fill, gs.stroke, gs.line_width, info.key, op_index))
                elif not curved and len(pts) >= 2 and not fill:
                    for a, b in zip(pts, pts[1:]):
                        model.graphics.append(Graphic("line", G.Rect.from_points([a, b]), stroke, fill, gs.fill, gs.stroke, gs.line_width, info.key, op_index))
                else:
                    model.graphics.append(Graphic("curve" if curved else "polygon", G.Rect.from_points(pts), stroke, fill, gs.fill, gs.stroke, gs.line_width, info.key, op_index))
            subpaths = []
            current_subpath = []
            rects = []

        for idx, ins in enumerate(info.instructions):
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                bbox = G.Rect(0, 0, 1, 1).transform(gs.ctm)
                model.images.append(ImagePlacement(None, bbox, info.key, idx))
                continue
            op = str(ins.operator)
            ops = ins.operands
            try:
                if op == "q":
                    self._stack.append(gs.copy())
                elif op == "Q":
                    if self._stack:
                        gs = self._stack.pop()
                elif op == "cm":
                    gs.ctm = G.mul(G.mat(ops), gs.ctm)
                    if in_bt and bt_key in model.bt_blocks:
                        model.bt_blocks[bt_key].ctm_changed_inside = True
                elif op == "w":
                    gs.line_width = float(ops[0])
                elif op == "gs":
                    gs.gs_names = gs.gs_names + (str(ops[0]),)
                    self._apply_extgstate(res, ops[0], gs)
                elif op in ("g", "rg", "k"):
                    gs.fill_cs = {"g": "DeviceGray", "rg": "DeviceRGB", "k": "DeviceCMYK"}[op]
                    gs.fill = tuple(float(v) for v in ops)
                    gs.fill_ops = ((tuple(ops), op),)
                elif op in ("G", "RG", "K"):
                    gs.stroke_cs = {"G": "DeviceGray", "RG": "DeviceRGB", "K": "DeviceCMYK"}[op]
                    gs.stroke = tuple(float(v) for v in ops)
                elif op == "cs":
                    gs.fill_cs = str(ops[0])[1:]
                    gs.fill = (0.0,)
                    gs.fill_ops = ((tuple(ops), "cs"),)
                elif op == "CS":
                    gs.stroke_cs = str(ops[0])[1:]
                elif op in ("sc", "scn"):
                    gs.fill = tuple(float(v) if not isinstance(v, pikepdf.Name) else str(v) for v in ops)
                    base = tuple(x for x in gs.fill_ops if x[1] == "cs")
                    gs.fill_ops = base + ((tuple(ops), op),)
                elif op in ("SC", "SCN"):
                    gs.stroke = tuple(float(v) if not isinstance(v, pikepdf.Name) else str(v) for v in ops)
                # ---------------------------------------------------- paths
                elif op == "m":
                    if current_subpath:
                        subpaths.append(current_subpath)
                    current_subpath = [(float(ops[0]), float(ops[1]), "m")]
                elif op == "l":
                    current_subpath.append((float(ops[0]), float(ops[1]), "l"))
                elif op in ("c", "v", "y"):
                    current_subpath.append((float(ops[-2]), float(ops[-1]), "c"))
                elif op == "h":
                    if current_subpath:
                        current_subpath.append((current_subpath[0][0], current_subpath[0][1], "l"))
                elif op == "re":
                    x, y, w, h = (float(v) for v in ops)
                    rects.append([(x, y), (x + w, y), (x, y + h), (x + w, y + h)])
                elif op in ("S", "s"):
                    flush_path(True, False, idx)
                elif op in ("f", "F", "f*"):
                    flush_path(False, True, idx)
                elif op in ("B", "B*", "b", "b*"):
                    flush_path(True, True, idx)
                elif op == "n":
                    subpaths, current_subpath, rects = [], [], []
                elif op == "sh":
                    model.graphics.append(Graphic("shading", G.Rect(-1e6, -1e6, 1e6, 1e6), False, True, gs.fill, gs.stroke, 0, info.key, idx))
                # ----------------------------------------------------- text
                elif op == "BT":
                    in_bt = True
                    tm = G.IDENTITY
                    tlm = G.IDENTITY
                    bt_key = (info.key, idx)
                    model.bt_blocks[bt_key] = BTBlock(info.key, idx, None, gs.ctm)
                    flow = self._new_flow(info.key)
                elif op == "ET":
                    in_bt = False
                    if bt_key in model.bt_blocks:
                        model.bt_blocks[bt_key].et_index = idx
                elif op == "Tf":
                    gs.text.font_res = str(ops[0])[1:]
                    gs.text.font = self._font(res, gs.text.font_res)
                    gs.text.size = D(ops[1])
                    if gs.text.font is None:
                        model.warnings.append(f"font /{gs.text.font_res} not found in resources")
                elif op == "Tc":
                    gs.text.tc = D(ops[0])
                elif op == "Tw":
                    gs.text.tw = D(ops[0])
                elif op == "Tz":
                    gs.text.th = D(ops[0]) / 100
                elif op == "TL":
                    gs.text.tl = D(ops[0])
                elif op == "Ts":
                    gs.text.ts = D(ops[0])
                elif op == "Tr":
                    gs.text.tr = int(ops[0])
                elif op == "Td":
                    tlm = G.mul(G.translate(float(ops[0]), float(ops[1])), tlm)
                    tm = tlm
                    flow = self._new_flow(info.key)
                elif op == "TD":
                    gs.text.tl = -D(ops[1])
                    tlm = G.mul(G.translate(float(ops[0]), float(ops[1])), tlm)
                    tm = tlm
                    flow = self._new_flow(info.key)
                elif op == "Tm":
                    tlm = G.mat(ops)
                    tm = tlm
                    flow = self._new_flow(info.key)
                elif op == "T*":
                    tlm = G.mul(G.translate(0, -float(gs.text.tl)), tlm)
                    tm = tlm
                    flow = self._new_flow(info.key)
                elif op in SHOW_OPS:
                    if op == "'":
                        tlm = G.mul(G.translate(0, -float(gs.text.tl)), tlm)
                        tm = tlm
                        flow = self._new_flow(info.key)
                        items = [ops[0]]
                    elif op == '"':
                        gs.text.tw = D(ops[0])
                        gs.text.tc = D(ops[1])
                        tlm = G.mul(G.translate(0, -float(gs.text.tl)), tlm)
                        tm = tlm
                        flow = self._new_flow(info.key)
                        items = [ops[2]]
                    elif op == "TJ":
                        items = list(ops[0])
                    else:
                        items = [ops[0]]
                    info.show_state[idx] = gs.text.copy()
                    info.show_tlm[idx] = tlm
                    tm = self._show(model, info, gs, tm, items, idx, op == "TJ", flow, bt_key)
                elif op == "Do":
                    self._do(model, info, gs, ops[0], idx, depth)
                elif op in ("BI", "ID", "EI"):
                    pass
            except (ValueError, TypeError, IndexError, ZeroDivisionError) as exc:
                model.warnings.append(f"{info.key} op {idx} {op}: {exc}")
        return gs

    def _new_flow(self, key: tuple) -> tuple:
        self._flow_counter += 1
        return (key, self._flow_counter)

    def _apply_extgstate(self, res, name, gs: GraphicsState) -> None:
        if res is None:
            return
        egs = res.get("/ExtGState")
        if egs is None:
            return
        d = egs.get(str(name))
        if d is None:
            return
        if "/Font" in d:
            f = d.Font
            gs.text.font = self.fonts.get(f[0])
            gs.text.size = D(f[1])
            gs.text.font_res = None
        if "/LW" in d:
            gs.line_width = float(d.LW)

    def _show(self, model, info, gs: GraphicsState, tm, items, op_index, is_tj, flow, bt_key):
        ts = gs.text
        font = ts.font
        if font is None:
            model.warnings.append(f"text shown without a font at {info.key} op {op_index}")
            return tm
        if font.is_type3:
            model.unsupported.append("Type3 font")
        size, tc, tw, th, rise = ts.size, ts.tc, ts.tw, ts.th, ts.ts
        sizef, thf, risef = float(size), float(th), float(rise)
        for elem_index, item in enumerate(items):
            if isinstance(item, pikepdf.String):
                data = bytes(item)
                for code, offset, nbytes in font.split_codes(data):
                    font.used_codes[code] = font.used_codes.get(code, 0) + 1
                    w0 = font.width(code)
                    adv = w0 * size + tc
                    if font.is_word_space(code, nbytes):
                        adv += tw
                    adv *= th
                    user = G.mul(tm, gs.ctm)
                    trm = G.mul((sizef * thf, 0.0, 0.0, sizef, 0.0, risef), user)
                    x0, y0 = G.apply(trm, 0, 0)
                    w0f = float(w0)
                    corners = [G.apply(trm, 0, font.descent), G.apply(trm, w0f, font.descent),
                               G.apply(trm, 0, font.ascent), G.apply(trm, w0f, font.ascent)]
                    bbox = G.Rect.from_points(corners)
                    advf = float(adv)
                    xe, ye = G.apply(user, advf, risef)
                    text = font.to_unicode(code)
                    if text is None:
                        text = "�"
                    glyph = Glyph(
                        gid=len(model.glyphs), stream=info.key, op_index=op_index,
                        elem_index=elem_index if is_tj else 0, byte_offset=offset, byte_len=nbytes,
                        code=code, text=text, font=font, font_res=ts.font_res or "", size=size, tc=tc, tw=tw,
                        th=th, ts=rise, tr=ts.tr, advance=adv, tm=tm, ctm=gs.ctm, flow=flow, bt=bt_key,
                        x=x0, y=y0, x_end=xe, bbox=bbox, effective_size=sizef * G.vertical_scale(user),
                        fill_cs=gs.fill_cs, fill=gs.fill, fill_ops=gs.fill_ops, gs_names=gs.gs_names,
                        upright=G.is_axis_aligned_upright(user),
                    )
                    model.glyphs.append(glyph)
                    tm = G.mul(G.translate(advf, 0), tm)
            else:
                n = float(item)
                tx = -n / 1000.0 * sizef * thf
                tm = G.mul(G.translate(tx, 0), tm)
        return tm

    def _do(self, model: PageModel, info: StreamInfo, gs: GraphicsState, name, op_index: int, depth: int) -> None:
        res = info.resources
        xobjs = res.get("/XObject") if res is not None else None
        xobj = xobjs.get(str(name)) if xobjs is not None else None
        if xobj is None:
            model.warnings.append(f"XObject {name} not found")
            return
        sub = str(xobj.get("/Subtype", "/"))
        if sub == "/Image":
            bbox = G.Rect(0, 0, 1, 1).transform(gs.ctm)
            model.images.append(ImagePlacement(str(name)[1:], bbox, info.key, op_index,
                                               int(xobj.get("/Width", 0)), int(xobj.get("/Height", 0))))
            return
        if sub != "/Form":
            return
        if depth >= self.max_depth:
            model.unsupported.append("XObject nesting too deep")
            return
        key = info.key + ("x", op_index, str(name)[1:])
        child_res = xobj.get("/Resources", res)
        child = StreamInfo(key, xobj, _parse(xobj), child_res, parent=info.key, do_index=op_index, name=str(name)[1:])
        model.streams[key] = child
        model.stream_order.append(key)
        outer_stack = self._stack
        self._stack = []
        inner = gs.copy()
        m = xobj.get("/Matrix")
        if m is not None:
            inner.ctm = G.mul(G.mat(m), inner.ctm)
        try:
            self._run(model, child, inner, depth + 1)
        finally:
            self._stack = outer_stack
