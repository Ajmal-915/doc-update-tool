"""Steps 1 and 3: analysis and measurement."""

from __future__ import annotations

import pdf_ops
from doc_update import matching, measure
from doc_update.analysis import analyse


def test_words_chars_fonts_and_render_details(pdfs):
    da, _ = analyse(pdfs["report"])
    p1 = da.pages[0]
    assert any(w["text"] == "Quarterly" for w in p1["words"])
    ch = p1["chars"][0]
    assert {"text", "font", "size", "effective_size", "bbox", "color"} <= set(ch)
    assert p1["structure"]["content_streams"] == 1
    font = next(iter(p1["fonts"].values()))
    assert font["embedded"] and font["subset"]
    assert any(g["kind"] == "line" for g in p1["graphics"])


def test_effective_size_from_text_matrix(pdfs):
    """Mac-style PDFs use '1 Tf' and put the size in the text matrix."""
    da, _ = analyse(pdfs["report_mac"])
    p1 = da.pages[0]
    assert p1["structure"]["mac_style_1_tf"]
    words = [w for w in p1["words"] if w["text"] == "report"]
    assert words and abs(words[0]["size"] - 11.0) < 0.01


def test_font_encodings_are_reported(pdfs):
    for name, expected in [("report", "WinAnsiEncoding"), ("report_macroman", "MacRomanEncoding"),
                           ("report_differences", "custom Differences"), ("report_type0", "Identity-H")]:
        da, _ = analyse(pdfs[name], pages=[0], include_chars=False)
        encodings = {f["encoding"] for f in da.pages[0]["fonts"].values()}
        assert expected in encodings, (name, encodings)


def test_signed_scanned_and_shared_detection(pdfs):
    assert analyse(pdfs["signed"])[0].signed
    scanned = analyse(pdfs["scanned"])[0].pages[0]
    assert scanned["scanned"] and scanned["ocr_layer"]
    shared = analyse(pdfs["shared_stream"])[0]
    assert shared.to_dict()["shared_content_streams"] == [{"pages": [1, 2]}]
    xo = analyse(pdfs["xobject"])[0].pages[0]["structure"]["form_xobjects"]
    assert len(xo) == 2 and all(x["uses_on_page"] == 2 for x in xo)


def test_form_fields_listed(pdfs):
    da, _ = analyse(pdfs["form"])
    names = {f["name"]: f["value"] for f in da.fields}
    assert names == {"PolicyName": "Anti-bribery policy 2023", "Owner": "Compliance team"}


def _measure(pdfs, name, find, replacement, page=1):
    doc = pdf_ops.open(pdfs[name])
    m = matching.find_target_text(doc, find, page)
    assert m.status == "found"
    return measure.measure(doc, m.best.target, replacement)


def test_measure_table_cell_boundary_stops_at_rule(pdfs):
    ms = _measure(pdfs, "report", "Quarterly", "Quarterly in March")
    assert ms.region_type == "table_cell"
    assert ms.boundary < 520  # the right-hand table rule is at x=520
    assert any(r["type"] == "table_line" for r in ms.risks)


def test_measure_two_columns_boundary_stops_before_next_column(pdfs):
    ms = _measure(pdfs, "report", "kept for six years", "kept for seven years")
    assert ms.boundary < 310  # right column starts at x=310


def test_measure_toc_line_flags_dot_leaders(pdfs):
    ms = _measure(pdfs, "report", "Section 4 Data protection", "Section 4 Privacy")
    assert ms.region_type == "table_of_contents_line"
    assert any(r["type"] == "dot_leader" for r in ms.risks)


def test_measure_free_space_and_missing_glyphs(pdfs):
    ms = _measure(pdfs, "report", "each month", "each quarter (Q)")
    assert ms.free_space_below > 0
    assert "Q" not in ms.missing_chars  # 'Q' is used in 'Quarterly'
    ms2 = _measure(pdfs, "report", "each month", "each Zebra")
    assert "Z" in ms2.missing_chars
