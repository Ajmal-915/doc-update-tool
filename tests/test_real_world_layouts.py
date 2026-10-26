"""Layouts found in real PDFs (corpus test) that the synthetic samples did not cover.

Each test builds a small PDF with the pattern, makes a correct edit with pdf_ops and
runs the step-8 checks on it.
"""

from __future__ import annotations

import pypdfium2 as pdfium

import editkit as ek
import pdf_ops
from doc_update import matching, measure
from doc_update.checks import run_checks
from pdfgen import Builder, wrap
from samples import PARA_A, PARA_B, _all_text


def _text(path, page=0) -> str:
    pdf = pdfium.PdfDocument(str(path))
    tp = pdf[page].get_textpage()
    text = " ".join(tp.get_text_range().split())
    pdf.close()
    return text


def _one_text_object(b: Builder, f1, size=11, leading=14, gap=50) -> bytes:
    """Two paragraphs in ONE BT/ET text object, positioned with relative Td (Acrobat Distiller style)."""
    la, lb = wrap(f1, PARA_A, size, 460), wrap(f1, PARA_B, size, 460)
    ops = [b"BT /F1 11 Tf 72 700 Td", f1.lit(la[0]) + b" Tj"]
    for line in la[1:]:
        ops += [b"0 -14 Td", f1.lit(line) + b" Tj"]
    ops += [b"0 -%d Td" % gap, f1.lit(lb[0]) + b" Tj"]
    for line in lb[1:]:
        ops += [b"0 -14 Td", f1.lit(line) + b" Tj"]
    ops.append(b"ET")
    return b"\n".join(ops)


def test_reflow_keeps_reading_order_when_the_column_is_one_text_object(work):
    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    b.page(_one_text_object(b, f1), b.resources([f1]))
    src = b.save(work / "one_bt.pdf")
    out, spec, _ = ek.good_edit(src, work / "out.pdf", "d", "within 30 days of joining",
                                "within 30 calendar days of joining the organisation")
    report = run_checks(src, out, spec)
    assert report.passed, report.summary()
    text = _text(out)
    # the rewrapped paragraph is still read before the next paragraph (copy/paste, search, screen readers)
    assert text.index("joining the organisation") < text.index("Customer complaints")


def test_operator_split_from_its_operands_across_content_streams(work):
    # the streams of /Contents are one sequence; Distiller splits them every few KB, even between an
    # operator and its operands
    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    content = _one_text_object(b, f1)
    cut = content.index(b" Tj", content.index(b"0 -14 Td")) + 1  # "(...) " | "Tj ..."
    b.page([content[:cut], content[cut:]], b.resources([f1]))
    src = b.save(work / "split.pdf")
    doc = pdf_ops.open(src)
    assert "".join(g.text for g in doc.model(0).glyphs).count("Customer complaints") == 1  # nothing lost
    out, spec, _ = ek.good_edit(src, work / "out.pdf", "a", "31 December 2023", "31 December 2024")
    report = run_checks(src, out, spec)
    assert report.passed, report.summary()
    assert "31 December 2024" in _text(out) and "Customer complaints" in _text(out)


def test_spaced_out_glyphs_shorter_replacement_fills_the_original_box(work):
    # OpenOffice and others position every glyph; same letters at natural spacing come out shorter
    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    words = "Records are kept for review and audit purposes".split(" ")
    arr = []
    for i, w in enumerate(words):
        for ch in w:
            arr.append(f1.lit(ch) + b" -25")
        if i < len(words) - 1:
            arr.append(f1.lit(" ") + b" -60")
    b.page(b"BT /F1 11 Tf 72 700 Td [" + b" ".join(arr) + b"] TJ ET", b.resources([f1]))
    src = b.save(work / "spaced.pdf")
    doc, target = ek.locate(src, "review and audit")
    fit = pdf_ops.fit_in_place(doc, target, "audit and review")
    assert fit.feasible, fit.reason
    out, spec, _ = ek.good_edit(src, work / "out.pdf", "a", "review and audit", "audit and review")
    report = run_checks(src, out, spec)
    assert report.passed, report.summary()
    assert "kept for audit and review purposes" in _text(out)


def test_reduced_font_inside_text_with_character_spacing(work):
    # a size change splits the TJ; text after it must not appear to move (Tc after the last glyph of a TJ)
    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    b.page(b"BT /F1 11 Tf 0.35 Tc 72 700 Td [" + f1.lit("Reviewed Annually by the board") + b"] TJ ET",
           b.resources([f1]))
    src = b.save(work / "tc.pdf")
    out, spec, result = ek.good_edit(src, work / "out.pdf", "c", "Annually", "Annually,")
    assert result.new_font_size < result.font_size
    report = run_checks(src, out, spec)
    assert report.passed, report.summary()


def test_horizontal_boundary_is_never_left_of_the_line_end(work):
    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    lines = wrap(f1, PARA_A, 11, 400)
    ops = [b"BT /F1 11 Tf 72 700 Td"]
    for i, line in enumerate(lines):
        if i:
            ops.append(b"0 -14 Td")
        ops.append(f1.lit(line + (" (see note)" if i == 1 else "")) + b" Tj")  # one line runs past the others
    ops.append(b"ET")
    b.page(b"\n".join(ops), b.resources([f1]))
    src = b.save(work / "long_line.pdf")
    doc = pdf_ops.open(src)
    word = lines[1].split(" ")[1]
    m = matching.find_target_text(doc, word, 1)
    assert m.status == "found", m.reason
    target = m.best.target
    meas = measure.measure(doc, target, word)
    line_end = max(p.line_x1 for p in target.parts)
    assert meas.boundary >= line_end - 0.01
    assert pdf_ops.fit_horizontal(doc, target, word, meas.boundary).feasible


def _report_with_box(work, name, media_box=None, rotate=None):
    import pikepdf

    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    content = (_one_text_object(b, f1) + b"\n0.5 w 72 696.5 m 300 696.5 l S")  # a form rule through the first line
    b.page(content, b.resources([f1]))
    src = b.save(work / f"{name}_plain.pdf")
    with pikepdf.open(src) as pdf:
        page = pdf.pages[0].obj
        if media_box:
            page.MediaBox = pikepdf.Array(media_box)
        if rotate:
            page.Rotate = rotate
        pdf.save(work / f"{name}.pdf")
    return work / f"{name}.pdf"


def test_media_box_not_at_the_origin(work):
    # IRS forms and cropped scans: the media box starts at (-34, -32); the checks must use PDF coordinates
    src = _report_with_box(work, "offset", media_box=[-34.5, -32, 577.5, 760])
    out, spec, _ = ek.good_edit(src, work / "out.pdf", "a", "31 December 2023", "31 December 2024")
    report = run_checks(src, out, spec)
    assert report.passed, report.summary()


def test_rotated_page(work):
    src = _report_with_box(work, "rotated", rotate=90)
    out, spec, _ = ek.good_edit(src, work / "out.pdf", "a", "31 December 2023", "31 December 2024")
    report = run_checks(src, out, spec)
    assert report.passed, report.summary()


def test_columns_aligned_with_runs_of_spaces_are_separate_cells(work):
    # corpus test (a police property report): columns aligned with runs of spaces were read as one line,
    # so a longer value reflowed the whole row into running text
    b = Builder()
    f1 = b.font_truetype("F1", _all_text() + "PISTOLEMAUCRWHKGJ#:-0123456789")
    row1 = "PISTOL        SEMI-AUTOMATIC        CARL WALTHER        PPK"
    row2 = "832229        Tag#:SJ4664-1         GO SJ 2012-122960284"
    b.page(b"BT /F1 9 Tf 40 700 Td " + f1.lit(row1) + b" Tj 0 -11 Td " + f1.lit(row2) + b" Tj ET", b.resources([f1]))
    src = b.save(work / "report_rows.pdf")
    doc = pdf_ops.open(src)
    texts = [ln.text for ln in doc.layout(0).lines]
    assert "CARL WALTHER" in texts and "PPK" in texts and "SEMI-AUTOMATIC" in texts, texts
    target = matching.find_target_text(doc, "CARL WALTHER", 1).best.target
    assert not target.parts[0].has_text_after  # "PPK" is the next column, not text that may be pushed along
    assert not pdf_ops.fit_reflow(doc, target, "CARL WALTHER GMBH AND COMPANY KG").feasible
    for p in (" ", "  "):  # a sentence gap is not a column gap
        assert matching.find_target_text(doc, "CARL" + p + "WALTHER", 1).status == "found"
