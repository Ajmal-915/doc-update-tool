"""Step 8 checks: correct edits pass, deliberately broken edits are rejected."""

from __future__ import annotations

import pytest

import editkit as ek
from doc_update.checks import run_checks


def _failed(report) -> set[str]:
    return {r.name for r in report.results if not r.passed}


# ------------------------------------------------------------------ good edits
@pytest.mark.parametrize("name", ["report", "report_mac", "report_type0", "report_helvetica", "report_differences",
                                  "report_macroman", "multistream"])
def test_good_in_place_edit_passes(pdfs, work, name):
    out, spec, _ = ek.good_edit(pdfs[name], work / "out.pdf", "a", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs[name], out, spec, work / "img")
    assert report.passed, report.summary()
    assert (work / "img" / "difference.png").exists()


@pytest.mark.parametrize("name", ["report", "report_mac", "report_type0", "report_helvetica", "report_macroman"])
def test_good_horizontal_expansion_passes(pdfs, work, name):
    out, spec, _ = ek.good_edit(pdfs[name], work / "out.pdf", "b", "each month", "each calendar month")
    report = run_checks(pdfs[name], out, spec)
    assert report.passed, report.summary()


def test_good_expansion_mid_line_moves_rest_of_line(pdfs, work):
    out, spec, _ = ek.good_edit(pdfs["report"], work / "out.pdf", "b", "two working days", "three working days")
    report = run_checks(pdfs["report"], out, spec)
    assert report.passed, report.summary()


@pytest.mark.parametrize("name", ["report", "report_mac", "report_type0"])
def test_good_reduced_font_passes(pdfs, work, name):
    out, spec, result = ek.good_edit(pdfs[name], work / "out.pdf", "c", "Annually", "Annually,")
    assert result.new_font_size < result.font_size
    report = run_checks(pdfs[name], out, spec)
    assert report.passed, report.summary()


def test_good_table_cell_expansion_stays_in_cell(pdfs, work):
    out, spec, _ = ek.good_edit(pdfs["report"], work / "out.pdf", "b", "Annually", "Annually in May")
    report = run_checks(pdfs["report"], out, spec)
    assert report.passed, report.summary()


@pytest.mark.parametrize("name", ["report", "report_mac", "report_type0", "report_helvetica", "report_macroman"])
def test_good_paragraph_reflow_passes(pdfs, work, name):
    out, spec, result = ek.good_edit(pdfs[name], work / "out.pdf", "d", "within 30 days of joining",
                                     "within 30 calendar days of joining the organisation")
    assert result.fit["new_line_count"] == result.fit["original_line_count"] + 1
    report = run_checks(pdfs[name], out, spec)
    assert report.passed, report.summary()


def test_good_justified_reflow_passes(pdfs, work):
    out, spec, result = ek.good_edit(pdfs["justified"], work / "out.pdf", "d", "within 30 days of joining",
                                     "within 30 calendar days of joining")
    assert result.fit["alignment"] == "justify"
    report = run_checks(pdfs["justified"], out, spec)
    assert report.passed, report.summary()


# ---------------------------------------------------------------- broken edits
def test_rejects_reflow_running_into_next_paragraph(pdfs, work):
    out, spec = ek.broken_reflow_overrun(
        pdfs["report"], work / "bad.pdf", "within 30 days of joining",
        "within 30 days of joining, or within 10 working days for staff in high-risk roles such as procurement, "
        "sales and finance, with refresher training every year for everyone")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert {"text_outside_area_identical", "render_outside_area_identical", "no_overlap"} & _failed(report)

def test_rejects_shifted_neighbouring_words(pdfs, work):
    out, spec = ek.broken_shift_neighbour(pdfs["report"], work / "bad.pdf", "two working days", "two business days")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert {"text_outside_area_identical", "render_outside_area_identical"} & _failed(report)


def test_rejects_lost_space_after_target(pdfs, work):
    out, spec = ek.broken_lose_space_after(pdfs["report"], work / "bad.pdf", "two working days", "two working days")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "text_outside_area_identical" in _failed(report)


def test_rejects_squeezed_spaces_in_replacement(pdfs, work):
    out, spec = ek.broken_squeeze_spaces(pdfs["report"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "text_inside_area_expected" in _failed(report)


def test_rejects_deleted_line(pdfs, work):
    out, spec = ek.broken_delete_line(pdfs["report"], work / "bad.pdf", "within 30 days", "within 28 days")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "text_outside_area_identical" in _failed(report)
    assert "render_outside_area_identical" in _failed(report)


def test_rejects_white_box_cover_up(pdfs, work):
    out, spec = ek.broken_white_box(pdfs["report"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    failed = _failed(report)
    assert "no_new_or_changed_graphics" in failed
    assert "text_inside_area_expected" in failed or "old_text_removed" in failed


def test_rejects_text_running_into_next_column(pdfs, work):
    out, spec = ek.broken_into_next_column(pdfs["report"], work / "bad.pdf", "kept for six years",
                                            "kept for six full calendar years at least")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert {"text_inside_boundary", "no_overlap", "render_outside_area_identical", "text_outside_area_identical"} & _failed(report)


def test_rejects_other_form_field_changed(pdfs, work):
    out, spec = ek.broken_other_field(pdfs["form"], work / "bad.pdf", "PolicyName", "Anti-bribery policy 2024", "Owner")
    report = run_checks(pdfs["form"], out, spec)
    assert not report.passed
    assert "form_fields_unchanged" in _failed(report)


def test_rejects_edit_leaking_onto_page_sharing_stream(pdfs, work):
    out, spec = ek.broken_shared_stream_leak(pdfs["shared_stream"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["shared_stream"], out, spec)
    assert not report.passed
    assert "other_pages_text_identical" in _failed(report)
    assert "other_pages_render_identical" in _failed(report)


def test_rejects_new_font_object(pdfs, work):
    out, spec = ek.broken_new_font(pdfs["report"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "fonts_unchanged" in _failed(report)


def test_rejects_font_below_minimum_scale(pdfs, work):
    out, spec = ek.broken_tiny_font(pdfs["report"], work / "bad.pdf", "Quarterly", "Quarterly")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "font_size_within_limits" in _failed(report)


def test_rejects_missing_glyphs(pdfs, work):
    # 'Z' and 'X' are not in the embedded subset
    out, spec = ek.broken_missing_glyph(pdfs["report"], work / "bad.pdf", "Quarterly", "ZXZX")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert {"no_missing_glyphs", "new_text_visible"} & _failed(report)


def test_rejects_hidden_old_text(pdfs, work):
    out, spec = ek.broken_invisible_old_text(pdfs["report"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert {"text_inside_area_expected", "old_text_removed"} & _failed(report)


def test_rejects_removed_annotation(pdfs, work):
    out, spec = ek.broken_drop_annotation(pdfs["form"], work / "bad.pdf", "PolicyName", "Anti-bribery policy 2024")
    report = run_checks(pdfs["form"], out, spec)
    assert not report.passed
    assert {"annotations_and_links_unchanged", "form_fields_unchanged"} & _failed(report)


def test_identical_copy_is_not_an_edit(pdfs, work):
    """A candidate that changes nothing must fail (the target was not replaced)."""
    doc, target = ek.locate(pdfs["report"], "31 December 2023")
    spec = ek.spec_for(doc, target, "a", "31 December 2024")
    report = run_checks(pdfs["report"], pdfs["report"], spec)
    assert not report.passed
    assert "text_inside_area_expected" in _failed(report)


def test_rejects_added_document_javascript(pdfs, work):
    out, spec = ek.broken_add_javascript(pdfs["report"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "document_level_unchanged" in _failed(report)


def test_rejects_added_page_action(pdfs, work):
    out, spec = ek.broken_page_action(pdfs["report"], work / "bad.pdf", "31 December 2023", "31 December 2024")
    report = run_checks(pdfs["report"], out, spec)
    assert not report.passed
    assert "page_attributes_unchanged" in _failed(report)
