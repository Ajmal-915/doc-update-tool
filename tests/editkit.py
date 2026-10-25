"""Helpers that perform correct and deliberately broken edits for the tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pikepdf

import pdf_ops
from doc_update import areas, matching, measure
from pdf_ops.edit import EditPlan, Insertion, apply_plan
from pdf_ops.geometry import Rect

METHOD_NAMES = {"a": "original_box", "b": "horizontal_expansion", "c": "reduced_font", "d": "paragraph_reflow",
                "e": "form_field_update"}


def locate(src: Path, find: str, page: int = 1):
    doc = pdf_ops.open(src)
    m = matching.find_target_text(doc, find, page)
    assert m.status == "found", m.reason
    return doc, m.best.target


def spec_for(doc, target, method: str, replacement: str, params: dict | None = None, min_scale: float = 0.9):
    return areas.check_spec(METHOD_NAMES.get(method, method), target, replacement, params or {}, min_scale, target.page_index)


def good_edit(src: Path, out: Path, method: str, find: str, replacement: str, page: int = 1, **kw):
    """Perform a correct edit with pdf_ops; returns (out, spec, result)."""
    doc, target = locate(src, find, page)
    params = {}
    if method == "a":
        result = pdf_ops.replace_in_place(doc, target, replacement)
    elif method == "b":
        meas = measure.measure(doc, target, replacement)
        params = {"boundary": kw.get("boundary", meas.boundary)}
        result = pdf_ops.expand_horizontal(doc, target, replacement, params["boundary"])
    elif method == "c":
        result = pdf_ops.replace_with_scale(doc, target, replacement, min_scale=kw.get("min_scale", 0.9))
    elif method == "d":
        meas = measure.measure(doc, target, replacement)
        result = pdf_ops.reflow_paragraph(doc, target, replacement, max_bottom=meas.paragraph["bbox"][1] - meas.free_space_below)
        params = {"area": result.fit["area"]}
    else:
        raise ValueError(method)
    pdf_ops.save(doc, out)
    return out, spec_for(doc, target, method, replacement, params), result


def _neighbour_after(doc, target):
    layout = doc.layout(target.page_index)
    model = doc.model(target.page_index)
    part = target.parts[-1]
    after = [g for g in part.after if not model.glyphs[g].text.isspace()]
    return after


# ------------------------------------------------------------- broken edits
def broken_shift_neighbour(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Correct replacement, but the next word on the line moves by 1.5 pt."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    first = doc.model(target.page_index).glyphs[target.glyphs[0]]
    codes = first.font.encode(replacement)
    plan = EditPlan(target.page_index, remove=set(target.glyphs))
    plan.inserts.append(Insertion(target.glyphs[0], codes))
    for gid in _neighbour_after(doc, target)[:6]:
        plan.shift[gid] = 1.5
    apply_plan(doc, plan)
    pdf_ops.save(doc, out)
    return out, spec


def broken_lose_space_after(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Correct replacement, and the space glyph after the target is deleted."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    model = doc.model(target.page_index)
    first = model.glyphs[target.glyphs[0]]
    space = [g for g in target.parts[-1].after if model.glyphs[g].text == " "][:1]
    assert space, "test needs a space glyph after the target"
    plan = EditPlan(target.page_index, remove=set(target.glyphs) | set(space))
    plan.inserts.append(Insertion(target.glyphs[0], first.font.encode(replacement)))
    apply_plan(doc, plan)
    pdf_ops.save(doc, out)
    return out, spec


def broken_squeeze_spaces(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """The replacement is written without its spaces."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    first = doc.model(target.page_index).glyphs[target.glyphs[0]]
    plan = EditPlan(target.page_index, remove=set(target.glyphs))
    plan.inserts.append(Insertion(target.glyphs[0], first.font.encode(replacement.replace(" ", ""))))
    apply_plan(doc, plan)
    pdf_ops.save(doc, out)
    return out, spec


def broken_delete_line(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Correct replacement, plus the following line of the paragraph disappears."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    layout = doc.layout(target.page_index)
    model = doc.model(target.page_index)
    block = layout.blocks[target.block]
    li = target.parts[-1].line
    nxt = block.lines[block.lines.index(li) + 1]
    first = model.glyphs[target.glyphs[0]]
    plan = EditPlan(target.page_index, remove=set(target.glyphs) | set(layout.lines[nxt].glyphs))
    plan.inserts.append(Insertion(target.glyphs[0], first.font.encode(replacement)))
    apply_plan(doc, plan)
    pdf_ops.save(doc, out)
    return out, spec


def broken_white_box(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """The old text is covered by a white rectangle and the new text drawn on top."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    pdf = doc.pdf
    pg = pdf.pages[target.page_index]
    b = target.bbox
    first = doc.model(target.page_index).glyphs[target.glyphs[0]]
    cover = f"q 1 g {b.x0:.2f} {b.y0:.2f} {b.width:.2f} {b.height:.2f} re f Q".encode()
    text = f"BT /{first.font_res} {float(first.size)} Tf {first.x:.3f} {first.y:.3f} Td ".encode() + \
        pikepdf.unparse_content_stream([([pikepdf.String(bytes(first.font.encode(replacement)))], pikepdf.Operator("Tj"))]) + b" ET"
    pg.contents_add(pdf.make_stream(cover + b"\n" + text), prepend=False)
    pdf.save(out)
    return out, spec


def broken_into_next_column(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Horizontal expansion that ignores the measured boundary and runs into the next column."""
    doc, target = locate(src, find, page)
    meas = measure.measure(doc, target, replacement)
    spec = spec_for(doc, target, "b", replacement, {"boundary": meas.boundary})
    pdf_ops.expand_horizontal(doc, target, replacement, boundary=600)
    pdf_ops.save(doc, out)
    return out, spec


def broken_other_field(src: Path, out: Path, field: str, new_value: str, other: str):
    """The requested field is updated correctly, but another field changes too."""
    from pdf_ops.forms import find_field, update_form_field

    doc = pdf_ops.open(src)
    info = find_field(doc, field)
    rect = Rect.from_list(info.widgets[0][2])
    update_form_field(doc, field, new_value)
    find_field(doc, other).obj.V = pikepdf.String("Somebody else")
    pdf_ops.save(doc, out)
    spec = areas.check_spec("form_field_update", None, new_value, {}, 0.9, info.widgets[0][0], field_name=field,
                            field_rect=rect, expected_field_value=new_value, original_size=10.0, target_text=info.value)
    return out, spec


def broken_shared_stream_leak(src: Path, out: Path, find: str, replacement: str):
    """Edits the content stream object shared by pages 1 and 2 in place."""
    doc, target = locate(src, find, 1)
    spec = spec_for(doc, target, "a", replacement)
    pdf = doc.pdf
    stream = pdf.pages[0].obj.Contents
    old = bytes(target.font.encode(find))
    new = bytes(target.font.encode(replacement))
    instrs = pikepdf.parse_content_stream(stream)
    rebuilt = []
    for ins in instrs:
        if str(ins.operator) == "Tj":
            s = bytes(ins.operands[0])
            if old in s:
                s = s.replace(old, new)
            rebuilt.append(([pikepdf.String(s)], ins.operator))
        else:
            rebuilt.append((list(ins.operands), ins.operator))
    stream.write(pikepdf.unparse_content_stream(rebuilt))
    pdf.save(out)
    return out, spec


def broken_new_font(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Old text removed correctly, new text drawn with a newly added Helvetica font."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    pdf_ops.remove_text_run(doc, target)
    pdf = doc.pdf
    pg = pdf.pages[target.page_index]
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    res = pikepdf.Dictionary({k: pg.obj.Resources[k] for k in pg.obj.Resources.keys()})
    res.Font = pikepdf.Dictionary({k: res.Font[k] for k in res.Font.keys()})
    res.Font.FNew = font
    pg.obj.Resources = res
    p = target.parts[0]
    first_y = p.bbox.y0 - target.font.descent * target.effective_size
    text = f"BT /FNew {float(target.size)} Tf {p.x0:.3f} {first_y:.3f} Td ({replacement}) Tj ET".encode()
    pg.contents_add(pdf.make_stream(text), prepend=False)
    pdf_ops.save(doc, out)
    return out, spec


def broken_tiny_font(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Replacement drawn at half size (below min_font_scale)."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "c", replacement)
    first = doc.model(target.page_index).glyphs[target.glyphs[0]]
    plan = EditPlan(target.page_index, remove=set(target.glyphs))
    plan.inserts.append(Insertion(target.glyphs[0], first.font.encode(replacement), size=first.size / 2))
    apply_plan(doc, plan)
    pdf_ops.save(doc, out)
    return out, spec


def broken_missing_glyph(src: Path, out: Path, find: str, replacement_with_missing: str, page: int = 1):
    """Writes WinAnsi codes for characters whose glyphs are not in the subset font."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement_with_missing)
    first = doc.model(target.page_index).glyphs[target.glyphs[0]]
    codes = [ord(ch) for ch in replacement_with_missing.encode("cp1252").decode("latin-1")]
    plan = EditPlan(target.page_index, remove=set(target.glyphs))
    plan.inserts.append(Insertion(target.glyphs[0], codes))
    apply_plan(doc, plan)
    pdf_ops.save(doc, out)
    return out, spec


def broken_invisible_old_text(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Old text kept but switched to invisible render mode; new text drawn over it."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    pdf = doc.pdf
    pg = pdf.pages[target.page_index]
    model = doc.model(target.page_index)
    first = model.glyphs[target.glyphs[0]]
    instrs = list(model.streams[first.stream].instructions)
    idx = first.op_index
    instrs.insert(idx, pikepdf.ContentStreamInstruction([3], pikepdf.Operator("Tr")))
    instrs.insert(idx + 2, pikepdf.ContentStreamInstruction([0], pikepdf.Operator("Tr")))
    new_text = f"BT /{first.font_res} {float(first.size)} Tf {first.x:.3f} {first.y:.3f} Td ".encode() + \
        pikepdf.unparse_content_stream([([pikepdf.String(bytes(first.font.encode(replacement)))], pikepdf.Operator("Tj"))]) + b" ET"
    pg.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instrs) + b"\n" + new_text)
    pdf.save(out)
    return out, spec


def locate_doc(doc, find, page):
    m = matching.find_target_text(doc, find, page)
    assert m.status == "found", m.reason
    return doc, m.best.target


def broken_drop_annotation(src: Path, out: Path, field: str, new_value: str):
    """Correct field update but another widget annotation is removed from the page."""
    from pdf_ops.forms import find_field, update_form_field

    doc = pdf_ops.open(src)
    info = find_field(doc, field)
    rect = Rect.from_list(info.widgets[0][2])
    update_form_field(doc, field, new_value)
    pg = doc.pdf.pages[0]
    annots = [a for a in pg.obj.Annots if str(a.get("/T", "")) == field]
    pg.obj.Annots = pikepdf.Array(annots)
    pdf_ops.save(doc, out)
    spec = areas.check_spec("form_field_update", None, new_value, {}, 0.9, 0, field_name=field, field_rect=rect,
                            expected_field_value=new_value, original_size=10.0, target_text=info.value)
    return out, spec


def broken_reflow_overrun(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Reflow that ignores the free space below and runs into the next paragraph."""
    doc, target = locate(src, find, page)
    meas = measure.measure(doc, target, replacement)
    layout = doc.layout(target.page_index)
    block = layout.blocks[target.block]
    allowed_bottom = block.bbox.y0 - meas.free_space_below
    first = layout.lines[target.parts[0].line]
    area = Rect(block.left, allowed_bottom, meas.boundary, first.bbox.y1).expand(0.25)
    spec = spec_for(doc, target, "d", replacement, {"area": area.as_list()})
    pdf_ops.reflow_paragraph(doc, target, replacement, max_bottom=0)
    pdf_ops.save(doc, out)
    return out, spec


def broken_add_javascript(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    """Correct edit, plus an /OpenAction with JavaScript and a page-open action."""
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    pdf_ops.replace_in_place(doc, target, replacement)
    pdf = doc.pdf
    pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('hi')"))
    pdf_ops.save(doc, out)
    return out, spec


def broken_page_action(src: Path, out: Path, find: str, replacement: str, page: int = 1):
    doc, target = locate(src, find, page)
    spec = spec_for(doc, target, "a", replacement)
    pdf_ops.replace_in_place(doc, target, replacement)
    doc.pdf.pages[0].obj.AA = pikepdf.Dictionary(O=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("http://x")))
    pdf_ops.save(doc, out)
    return out, spec
