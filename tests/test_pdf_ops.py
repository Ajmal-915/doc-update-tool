"""Unit tests for the pdf_ops helper library."""

from __future__ import annotations

from decimal import Decimal

import pikepdf
import pytest

import editkit as ek
import pdf_ops
from doc_update.checks import run_checks
from pdf_ops.content import Interpreter
from pdf_ops.forms import find_field, list_fields, update_form_field
from pdfgen import Builder, paragraph_ops, single_line, wrap


# ----------------------------------------------------------------- fonts
@pytest.mark.parametrize("name", ["report", "report_type0", "report_differences", "report_macroman", "report_helvetica"])
def test_font_decoding_and_widths(pdfs, name):
    doc = pdf_ops.open(pdfs[name])
    model = doc.model(0)
    text = "".join(g.text for g in model.glyphs)
    assert "Annual Compliance Report" in text.replace(" ", " ")
    assert "31 December 2023" in text or "31December2023" in text
    for g in model.glyphs[:50]:
        assert g.font.width(g.code) > 0 or g.text.isspace()


def test_glyphs_available_uses_embedded_subset(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "31 December 2023")
    ok, missing = pdf_ops.glyphs_available(t.font, "31 December 2024")
    assert ok and missing == []
    ok, missing = pdf_ops.glyphs_available(t.font, "Zebra Xylophone")
    assert not ok and set(missing) == {"Z", "X"}


def test_type0_identity_h_reverse_mapping(pdfs):
    doc = pdf_ops.open(pdfs["report_type0"])
    t = pdf_ops.find_target(doc, 1, "31 December 2023")
    assert t.font.is_type0 and t.font.code_length == 2
    codes = t.font.encode("2024")
    assert all(isinstance(c, int) and c > 0 for c in codes)
    assert t.font.missing_chars("Z") == ["Z"]


def test_measure_text_matches_layout(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "Quarterly")
    width = pdf_ops.measure_text(t.font, "Quarterly", float(t.size))
    part = t.parts[0]
    assert abs(width - (part.x1 - part.x0)) < 0.01


def test_standard14_font_without_widths_uses_afm(pdfs):
    doc = pdf_ops.open(pdfs["report_helvetica"])
    t = pdf_ops.find_target(doc, 1, "Quarterly")
    assert not t.font.is_embedded
    assert abs(pdf_ops.measure_text(t.font, "Q", 10) - 7.78) < 0.01


# ------------------------------------------------------------ interpreter
def _one_page(content: bytes, chars: str = "ABCDEFGHIJ abcdefghij 0123456789.,"):
    b = Builder()
    f = b.font_truetype("F1", chars)
    b.page(content(f) if callable(content) else content, b.resources([f]))
    return b


def test_interpreter_positioning_operators(tmp_path):
    def content(f):
        return (b"BT /F1 10 Tf 12 TL 100 700 Td " + f.lit("abc") + b" Tj T* " + f.lit("def") + b" Tj 0 -20 TD "
                + f.lit("ghi") + b" Tj " + f.lit("ABC") + b" ' 1 0.5 " + f.lit("DEF") + b' " ET')
    b = _one_page(content)
    p = b.save(tmp_path / "ops.pdf")
    doc = pdf_ops.open(p)
    gl = doc.model(0).glyphs
    starts = {g.text: (round(g.x, 3), round(g.y, 3)) for g in gl}
    assert starts["a"] == (100, 700)
    assert starts["d"] == (100, 688)  # T* with TL 12
    assert starts["g"] == (100, 668)  # TD sets TL to 20
    assert starts["A"] == (100, 648)  # ' uses the new leading
    assert starts["D"] == (100, 628)
    assert gl[-1].tc == Decimal("0.5") and gl[-1].tw == Decimal(1)


def test_interpreter_tj_kerning_and_text_matrix_scale(tmp_path):
    def content(f):
        return b"BT /F1 1 Tf 12 0 0 12 50 500 Tm [" + f.lit("AB") + b" -1000 " + f.lit("C") + b"] TJ ET"
    p = _one_page(content).save(tmp_path / "k.pdf")
    gl = pdf_ops.open(p).model(0).glyphs
    a, b_, c = gl
    assert abs(a.effective_size - 12) < 1e-6
    assert abs(c.x - (b_.x_end + 12)) < 1e-6  # -1000 thousandths at 12 pt = 12 pt to the right


def test_xobject_text_positions_include_form_matrix(pdfs):
    doc = pdf_ops.open(pdfs["xobject"])
    model = doc.model(0)
    xo = [g for g in model.glyphs if len(g.stream) > 2]
    assert xo and abs(xo[0].x - 72) < 1e-6 and abs(xo[0].y - 760) < 1e-6


# ------------------------------------------------------------- edit engine
def test_remove_text_run_keeps_following_text_in_place(pdfs, work):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "two working days")
    after = [doc.model(0).glyphs[g] for g in t.parts[0].after]
    before_pos = [(g.text, round(g.x, 3)) for g in after]
    pdf_ops.remove_text_run(doc, t)
    text_after = [(g.text, round(g.x, 3)) for g in doc.model(0).glyphs if g.y == after[0].y]
    for item in before_pos:
        assert item in text_after


def test_write_text_uses_same_font_resource(pdfs, work):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "Page 1")
    pdf_ops.write_text(doc, t, "Page 1", 400, 40)
    new = [g for g in doc.model(0).glyphs if abs(g.y - 40) < 0.01 and g.x >= 399]
    assert "".join(g.text for g in new) == "Page 1"
    assert new[0].font_res == t.font_res and new[0].font is t.font


def test_shared_content_stream_is_copied_before_edit(pdfs, work):
    out, spec, _ = ek.good_edit(pdfs["shared_stream"], work / "o.pdf", "a", "31 December 2023", "31 December 2024")
    with pikepdf.open(out) as pdf:
        assert pdf.pages[0].obj.Contents.objgen != pdf.pages[1].obj.Contents.objgen
    report = run_checks(pdfs["shared_stream"], out, spec)
    assert report.passed, report.summary()
    doc = pdf_ops.open(out)
    assert "31 December 2023" in "".join(g.text for g in doc.model(1).glyphs)


def test_shared_xobject_drawn_twice_is_copied_and_renamed(pdfs, work):
    doc = pdf_ops.open(pdfs["xobject"])
    header = pdf_ops.find_target(doc, 1, "internal use only", near=[72, 755, 300, 775])
    assert header.bbox.y0 > 700
    result = pdf_ops.replace_in_place(doc, header, "internal use only")  # same text, still a real edit path
    result = pdf_ops.replace_in_place(doc, pdf_ops.find_target(doc, 1, "internal use only", near=[72, 755, 300, 775]),
                                      "internal use")
    assert result.edit["xobjects_copied"] >= 1
    pdf_ops.save(doc, work / "x.pdf")
    doc2 = pdf_ops.open(work / "x.pdf")
    p1 = doc2.model(0)
    texts = {}
    for g in p1.glyphs:
        texts.setdefault(round(g.y), []).append(g.text)
    assert "".join(texts[760]).endswith("internal use")
    assert "".join(texts[30]).endswith("internal use only")  # second use of the XObject untouched
    p2 = "".join(g.text for g in doc2.model(1).glyphs)
    assert "internal use only" in p2  # the other page keeps the original XObject


def test_xobject_edit_passes_checks(pdfs, work):
    doc = pdf_ops.open(pdfs["xobject"])
    t = pdf_ops.find_target(doc, 1, "internal use only", near=[72, 755, 300, 775])
    spec = ek.spec_for(doc, t, "a", "internal use")
    pdf_ops.replace_in_place(doc, t, "internal use")
    pdf_ops.save(doc, work / "x.pdf")
    report = run_checks(pdfs["xobject"], work / "x.pdf", spec)
    assert report.passed, report.summary()


def test_multiple_content_streams_only_one_rewritten(pdfs, work):
    with pikepdf.open(pdfs["multistream"]) as pdf:
        before = [s.objgen for s in pdf.pages[0].obj.Contents]
    out, spec, result = ek.good_edit(pdfs["multistream"], work / "m.pdf", "a", "31 December 2023", "31 December 2024")
    assert len(result.edit["streams_changed"]) == 1
    report = run_checks(pdfs["multistream"], out, spec)
    assert report.passed, report.summary()


def test_quote_operators_are_rewritten_equivalently(pdfs, work):
    # report_helvetica uses the ' operator for continuation lines
    out, spec, _ = ek.good_edit(pdfs["report_helvetica"], work / "q.pdf", "a", "record exceptions", "record exceptions")
    report = run_checks(pdfs["report_helvetica"], out, spec)
    # identical replacement: nothing visible changes, so the 'inside changed' check is skipped and all others pass
    assert report.passed, report.summary()


def test_missing_glyph_raises(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "Quarterly")
    with pytest.raises(pdf_ops.DoesNotFitError) as exc:
        pdf_ops.replace_in_place(doc, t, "Zonal")
    assert "Z" in exc.value.message


def test_does_not_fit_raises_with_measurements(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "Quarterly")
    fit = pdf_ops.fit_in_place(doc, t, "Quarterly and on request")
    assert not fit.feasible and fit.details["needed_width"] > fit.details["available_width"]
    with pytest.raises(pdf_ops.DoesNotFitError):
        pdf_ops.replace_in_place(doc, t, "Quarterly and on request")


def test_reduced_font_never_below_minimum(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "Annually")
    fit = pdf_ops.fit_scaled(doc, t, "Annually,", min_scale=0.9)
    assert fit.feasible and 0.9 <= fit.scale < 1
    fit2 = pdf_ops.fit_scaled(doc, t, "Annually, in May", min_scale=0.9)
    assert not fit2.feasible


# -------------------------------------------------------------- form fields
def test_list_and_update_form_field(pdfs, work):
    doc = pdf_ops.open(pdfs["form"])
    assert {f.name for f in list_fields(doc)} == {"PolicyName", "Owner"}
    info = find_field(doc, "PolicyName")
    res = update_form_field(doc, "PolicyName", "Anti-bribery policy 2024")
    assert res["new_value"] == "Anti-bribery policy 2024"
    pdf_ops.save(doc, work / "f.pdf")
    doc2 = pdf_ops.open(work / "f.pdf")
    assert find_field(doc2, "PolicyName").value == "Anti-bribery policy 2024"
    assert find_field(doc2, "Owner").value == "Compliance team"
    from doc_update import areas
    from pdf_ops.geometry import Rect

    spec = areas.check_spec("form_field_update", None, "Anti-bribery policy 2024", {}, 0.9, 0, field_name="PolicyName",
                            field_rect=Rect.from_list(info.widgets[0][2]), expected_field_value="Anti-bribery policy 2024",
                            original_size=10.0, target_text=info.value)
    report = run_checks(pdfs["form"], work / "f.pdf", spec)
    assert report.passed, report.summary()


def test_form_field_too_long_does_not_fit(pdfs):
    doc = pdf_ops.open(pdfs["form"])
    with pytest.raises(pdf_ops.DoesNotFitError):
        update_form_field(doc, "Owner", "Compliance team together with the internal audit function and the board")


# -------------------------------------------------------------- reflow (method d)
def test_reflow_uses_free_space_and_keeps_lines_above(pdfs, work):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "within 30 days of joining")
    layout = doc.layout(0)
    block = layout.blocks[t.block]
    first_line = layout.lines[block.lines[0]]
    above = [(doc.model(0).glyphs[g].text, round(doc.model(0).glyphs[g].x, 3)) for g in first_line.glyphs]
    res = pdf_ops.reflow_paragraph(doc, t, "within 30 calendar days of joining the organisation", max_bottom=600)
    assert res.fit["new_line_count"] == res.fit["original_line_count"] + 1
    model = doc.model(0)
    for item in above:
        assert item in [(g.text, round(g.x, 3)) for g in model.glyphs]


def test_reflow_refused_without_free_space(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    t = pdf_ops.find_target(doc, 1, "within 30 days of joining")
    long = "within 30 days of joining " + "and again every year " * 12
    fit = pdf_ops.fit_reflow(doc, t, long.strip(), max_bottom=660)
    assert not fit.feasible and "free space" in fit.reason


def test_reflow_refused_for_mixed_fonts(pdfs):
    doc = pdf_ops.open(pdfs["justified"])
    t = pdf_ops.find_target(doc, 1, "assessed before onboarding", near=[72, 550, 450, 575])
    fit = pdf_ops.fit_reflow(doc, t, "assessed and approved before onboarding", max_bottom=400)
    assert not fit.feasible and "mixes fonts" in fit.reason


def test_reflow_justified_lines_end_at_right_edge(pdfs, work):
    doc = pdf_ops.open(pdfs["justified"])
    t = pdf_ops.find_target(doc, 1, "within 30 days of joining")
    right = doc.layout(0).blocks[t.block].right
    pdf_ops.reflow_paragraph(doc, t, "within 30 calendar days of joining", max_bottom=600)
    lines = doc.layout(0).lines
    ends = sorted({round(ln.x1, 1) for ln in lines if ln.x0 < 80 and ln.bbox.y0 > 560 and ln.x1 > right - 40})
    assert ends and all(abs(e - right) < 0.6 for e in ends)
