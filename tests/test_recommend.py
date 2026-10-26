"""Recommendations: page + plain-words recommendation -> edit plan -> verified edits -> one candidate."""

from __future__ import annotations

import pypdfium2 as pdfium
import pytest

from doc_update import api, cli
from doc_update.llm import make_budget
from doc_update.llm.offline import OfflineLLM
from doc_update.sandbox.local import default_sandbox_python

needs_sandbox = pytest.mark.skipif(not default_sandbox_python().exists(), reason="sandbox venv not installed")


class ScriptedEditor(OfflineLLM):
    """The offline roles, with a fixed edit plan as the editor model would return it."""

    def __init__(self, plan: dict, seen: dict | None = None):
        super().__init__(make_budget({"llm": {"max_calls_per_change": 12, "max_cost_per_change_usd": 1.0}}))
        self.fixed = plan
        self.seen = seen if seen is not None else {}

    def plan_edits(self, context, image_png, image_info):
        self._count("editor")
        self.seen.update(context=context, image=image_png, info=image_info)
        return self.fixed


def _plan(*edits, **extra) -> dict:
    return {"understanding": "test", "not_possible": [],
            "edits": [{"line_ids": ids, "current_text": cur, "new_text": new, "reason": "test"} for ids, cur, new in edits],
            **extra}


def _setup(tmp_path, pdfs, name="report") -> dict:
    ov = {"store": {"root": str(tmp_path / "store")}, "llm": {"provider": "offline"}}
    assert api.ingest_document(pdfs[name], "d", None, ov)["status"] == "ingested"
    return ov


def _line_id(seen: dict, text: str) -> str:
    return next(ln["id"] for ln in seen["context"]["lines"] if text in ln["text"])


def _page_text(path, page=0) -> str:
    pdf = pdfium.PdfDocument(str(path))
    text = " ".join(pdf[page].get_textpage().get_text_range().split())
    pdf.close()
    return text


def test_editor_gets_the_page_screenshot_and_numbered_lines(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    seen: dict = {}
    llm = ScriptedEditor(_plan((["L1"], "31 December 2023", "31 December 2024")), seen)
    r = api.recommend_change("d", 1, "Move the year end to 2024", None, ov, plan_only=True, llm=llm)
    assert r["status"] == "planned", r
    assert seen["image"][:8] == b"\x89PNG\r\n\x1a\n" and seen["info"]["width"] > 500
    assert seen["context"]["recommendation"] == "Move the year end to 2024"
    texts = [ln["text"] for ln in seen["context"]["lines"]]
    assert any("31 December 2023" in t for t in texts) and seen["context"]["lines"][0]["id"] == "L1"
    assert r["edits"][0]["find"] == "2023" and r["edits"][0]["replace"] == "2024"  # only the changed word
    assert api.document_status("d", None, ov)["pending"] == []  # nothing was changed


@needs_sandbox
def test_several_edits_become_one_candidate_and_one_approval(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    llm = ScriptedEditor(_plan(([], "31 December 2023", "31 December 2024"),
                               ([], "resolved within eight weeks", "resolved within six weeks")))
    r = api.recommend_change("d", 1, "Year end 2024; complaints resolved within six weeks", None, ov, llm=llm)
    assert r["status"] == "updated", r
    assert [e["find"] for e in r["edits"]] == ["2023", "eight"]
    text = _page_text(r["candidate_path"])
    assert "31 December 2024" in text and "resolved within six weeks" in text
    pending = api.document_status("d", None, ov)["pending"]
    assert len(pending) == 1 and pending[0]["kind"] == "recommendation" and len(pending[0]["edits"]) == 2
    assert api.approve_change("d", r["change_id"], "reviewer", None, None, ov)["status"] == "approved"
    assert api.document_status("d", None, ov)["current_version"] == "v001"


def test_a_quote_that_is_not_on_the_page_rejects_the_plan(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    llm = ScriptedEditor(_plan(([], "31 December 2023", "31 December 2024"),
                               ([], "within 45 days of joining", "within 30 days of joining")))  # invented
    r = api.recommend_change("d", 1, "Training within 30 days", None, ov, llm=llm)
    assert r["status"] == "manual_review" and r["reason_code"] == "plan_rejected"
    assert "edit 2" in r["reason"] and "not on page 1" in r["reason"]
    assert api.document_status("d", None, ov)["pending"] == []


def test_repeated_text_must_be_singled_out(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    # page 2: "The policy applies to all staff. The policy applies to all contractors" (both on one line)
    llm = ScriptedEditor(_plan((["L1"], "The policy applies to", "The rule applies to")))
    r = api.recommend_change("d", 2, "Say rule instead of policy for contractors", None, ov, plan_only=True, llm=llm)
    assert r["status"] == "manual_review" and "appears 2 times" in r["reason"] and "quote more" in r["reason"]
    llm = ScriptedEditor(_plan((["L1", "L2"], "The policy applies to all contractors", "The rule applies to all contractors")))
    r = api.recommend_change("d", 2, "Say rule instead of policy for contractors", None, ov, plan_only=True, llm=llm)
    assert r["status"] == "planned", r
    edit = r["edits"][0]
    assert edit["find"] != "policy" and edit["find"].replace("policy", "rule") == edit["replace"]
    assert edit["find"].count("policy") == 1 and not edit["located_by_position"]  # context makes it unique


def test_repeated_text_on_different_lines_is_picked_by_line_id(tmp_path):
    from pdfgen import Builder, single_line
    from samples import _all_text

    b = Builder()
    f1 = b.font_truetype("F1", _all_text())
    b.page(single_line(f1, "Review the register every quarter.", 72, 700, 11) + b"\n"
           + single_line(f1, "Review the register every quarter.", 72, 600, 11), b.resources([f1]))
    ov = {"store": {"root": str(tmp_path / "store")}, "llm": {"provider": "offline"}}
    api.ingest_document(b.save(tmp_path / "twice.pdf"), "d", None, ov)
    seen: dict = {}
    llm = ScriptedEditor(_plan(([], "every quarter", "every month")), seen)
    r = api.recommend_change("d", 1, "The second review is monthly", None, ov, plan_only=True, llm=llm)
    assert r["status"] == "manual_review" and "appears 2 times" in r["reason"]
    second = seen["context"]["lines"][1]
    llm = ScriptedEditor(_plan(([second["id"]], "every quarter", "every month")))
    r = api.recommend_change("d", 1, "The second review is monthly", None, ov, plan_only=True, llm=llm)
    assert r["status"] == "planned", r
    assert r["edits"][0]["location"]["lines"] == [second["id"]] and r["edits"][0]["located_by_position"]
    assert r["edits"][0]["location"]["bbox"][1] < 650  # the lower line
    if default_sandbox_python().exists():
        llm = ScriptedEditor(_plan(([second["id"]], "every quarter", "every month")))
        r = api.recommend_change("d", 1, "The second review is monthly", None, ov, llm=llm)
        assert r["status"] == "updated", r
        assert _page_text(r["candidate_path"]) == ("Review the register every quarter. "
                                                   "Review the register every month.")


def test_overlapping_edits_reject_the_plan(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    llm = ScriptedEditor(_plan(([], "within two working days", "within one working day"),
                               ([], "two working days and resolved", "three working days and resolved")))
    r = api.recommend_change("d", 1, "Faster logging", None, ov, plan_only=True, llm=llm)
    assert r["status"] == "manual_review" and "overlapping" in r["reason"]


@needs_sandbox
def test_one_edit_that_cannot_be_made_means_no_change_at_all(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    llm = ScriptedEditor(_plan(([], "31 December 2023", "31 December 2024"),
                               ([], "Quarterly", "Quarterly €")))  # the euro sign is not in the font subset
    r = api.recommend_change("d", 1, "Two changes", None, ov, llm=llm)
    assert r["status"] == "manual_review" and r["reason_code"] == "missing_glyphs"
    assert r["reason"].startswith("edit 2 of 2") and "Nothing was changed" in r["reason"]
    assert [e["status"] for e in r["edits"]] == ["updated", "manual_review"]
    status = api.document_status("d", None, ov)
    assert status["pending"] == [] and status["current_version"] == "original"


def test_parts_that_are_not_text_replacements_go_to_a_person(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    llm = ScriptedEditor(_plan(([], "31 December 2023", "31 December 2024"),
                               not_possible=[{"description": "add a new paragraph", "reason": "not a replacement"}]))
    r = api.recommend_change("d", 1, "Change the year and add a paragraph", None, ov, llm=llm)
    assert r["status"] == "manual_review" and r["reason_code"] == "partly_not_possible"
    llm = ScriptedEditor(_plan(needs_clarification="which date is meant?"))
    r = api.recommend_change("d", 1, "Fix the date", None, ov, llm=llm)
    assert r["status"] == "manual_review" and r["reason_code"] == "needs_clarification"


@needs_sandbox
def test_form_field_value_in_a_recommendation(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs, "form")
    seen: dict = {}
    llm = ScriptedEditor(_plan((["F1"], "Anti-bribery policy 2023", "Anti-bribery policy 2024")), seen)
    r = api.recommend_change("d", 1, "The policy name should say 2024", None, ov, llm=llm)
    assert [f["name"] for f in seen["context"]["form_fields"]] == ["PolicyName", "Owner"]
    assert r["status"] == "updated", r
    assert r["edits"][0]["method"] == "form_field_update"


@needs_sandbox
def test_offline_editor_understands_plain_instructions(pdfs, tmp_path):
    ov = _setup(tmp_path, pdfs)
    r = api.recommend_change("d", 1, "Change 'Quarterly' to 'Monthly'.", None, ov)
    assert r["status"] == "updated", r
    assert "Monthly" in _page_text(r["candidate_path"])
    r = api.recommend_change("d", 1, "Make the access review sound more modern", None, ov)
    assert r["status"] == "manual_review" and r["reason_code"] == "needs_clarification"


def test_recommend_input_errors_and_cli(pdfs, tmp_path, capsys):
    ov = _setup(tmp_path, pdfs)
    assert api.recommend_change("d", 1, "   ", None, ov)["status"] == "error"
    assert api.recommend_change("d", 9, "Change 'a' to 'b'", None, ov)["reason_code"] == "not_found"
    code = cli.main(["--store", str(tmp_path / "store"), "recommend", "--doc-id", "d", "--page", "1", "--provider",
                     "offline", "--text", "Replace \"Quarterly\" with \"Monthly\"", "--plan-only"])
    assert code == 0 and '"status": "planned"' in capsys.readouterr().out


@pytest.mark.scenarios
@needs_sandbox
def test_year_end_recommendations_with_hand_written_edit_plans(tmp_path):
    """13 plain-words recommendations on the year-end pack; the edit plans were written by hand (manual
    provider) from the page screenshots and line lists, the other roles run offline."""
    import shutil
    from pathlib import Path

    from doc_update.recommend_pack import run_pack

    data = Path(__file__).parent / "data"
    if not (data / "year-end-test-pack" / "scenarios.json").exists():
        pytest.skip("year-end pack not imported: run scripts/import_eval_data.py")
    answers = tmp_path / "answers"
    shutil.copytree(data / "manual-answers" / "recommendations", answers)
    rows = run_pack(data / "recommendations" / "year-end.json",
                    {"llm": {"provider": "offline", "editor_provider": "manual", "manual_dir": str(answers)}},
                    tmp_path / "work")
    assert len(rows) == 13
    assert all(r["verdict"] == "PASS" for r in rows), [(r["id"], r["status"], r.get("reason")) for r in rows
                                                       if r["verdict"] != "PASS"]
