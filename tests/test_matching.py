"""Step 2: normalisation and approximate matching."""

from __future__ import annotations

import pdf_ops
from doc_update import matching
from pdf_ops.textnorm import normalize


def test_normalisation_folds_quotes_dashes_ligatures_case_and_spaces():
    assert normalize("“Year‑end”  RESULTS — ﬁnal") == normalize('"year-end" results - final')
    assert normalize("soft­hyphen") == "softhyphen"
    assert normalize("  a \n\t b ") == "a b"


def test_exact_match_on_given_page(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "financial year ending 31 December 2023", 1)
    assert m.status == "found"
    assert m.best.score == 100
    assert m.best.page_index == 0
    assert len(m.best.target.parts) == 1


def test_match_with_different_quotes_dashes_and_case(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "ALL STAFF MUST COMPLETE THE ANTI–BRIBERY TRAINING", 1)
    assert m.status == "found"
    assert "anti-bribery" in m.best.target.text


def test_phrase_across_line_break(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "the end of the relationship", 1)
    assert m.status == "found"
    assert len(m.best.target.parts) == 2
    assert m.best.target.text == "the end of the relationship"


def test_approximate_match_tolerates_small_differences(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "Customer complaints are logged within 2 working days", 1)
    assert m.status == "found", m.reason
    assert 90 <= m.best.score < 100
    assert m.best.target.text.startswith("Customer complaints are logged within")


def test_not_found_reports_closest_candidate(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "This sentence is nowhere in the document at all", 1)
    assert m.status == "not_found"
    assert "page 1" in m.reason


def test_ambiguous_lists_candidates_with_positions(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "The policy applies to all", 2)
    assert m.status == "ambiguous"
    assert len(m.candidates) == 2
    d = m.to_dict()
    assert all(c["page"] == 2 and c["bbox"] for c in d["candidates"])


def test_falls_back_to_neighbouring_page(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "Revenue grew by 12%", 1)
    assert m.status == "found"
    assert m.best.page_index == 1
    assert "request said page 1" in m.reason


def test_form_field_values_are_searched(pdfs):
    doc = pdf_ops.open(pdfs["form"])
    m = matching.find_target_text(doc, "anti-bribery policy 2023", 1)
    assert m.status == "found"
    assert m.best.kind == "field"
    assert m.best.field_name == "PolicyName"


def test_page_out_of_range(pdfs):
    doc = pdf_ops.open(pdfs["report"])
    m = matching.find_target_text(doc, "anything", 9)
    assert m.status == "not_found"
    assert "does not exist" in m.reason
