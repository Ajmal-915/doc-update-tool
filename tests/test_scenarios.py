"""Scenario packs: the synthetic pack (always) and the real year-end pack (when imported)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from doc_update.sandbox.local import default_sandbox_python
from doc_update.scenarios import load_pack, normalise_expected, run_pack

DATA = Path(__file__).parent / "data"
SYNTHETIC = DATA / "synthetic-pack"
YEAR_END = DATA / "year-end-test-pack"
GENERATED = DATA / "generated-cases"
RESULTS = DATA / "scenario_results"

needs_sandbox = pytest.mark.skipif(not default_sandbox_python().exists(), reason="sandbox venv not installed")


def test_expected_result_normalisation():
    assert normalise_expected("manual_review") == "manual_review"
    assert normalise_expected({"status": "updated"}) == "updated"
    assert normalise_expected("Should go to manual review: graphic") == "manual_review"
    assert normalise_expected("apply the edit") == "updated"


def test_loader_accepts_other_layouts(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "scenarios.json").write_text(json.dumps([
        {"scenario_id": "A01", "document": "a.pdf", "change": {"page_number": 2, "original_text": "x", "new_text": "y"},
         "expected_safe_result": "updated"},
        {"id": "D01", "file": "a.pdf", "page": 1, "find": "x", "replace": "y", "expected_safe_result": "updated"},
        {"id": "Z9", "pdf": "missing.pdf"},
    ]))
    sc = {s.id: s for s in load_pack(tmp_path)}
    assert sc["A01"].page == 2 and sc["A01"].find == "x" and sc["A01"].replace == "y" and not sc["A01"].problems
    assert sc["D01"].expected == "manual_review"  # graphic scenarios are always out of scope
    assert sc["Z9"].problems


def _check(results: list[dict], name: str):
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    unsafe = [r for r in results if r["verdict"] == "UNSAFE"]
    errors = [r for r in results if r["verdict"] == "ERROR"]
    assert not unsafe, f"edits produced where manual review was expected: {[r['id'] for r in unsafe]}"
    assert not errors, [(r["id"], r.get("reason")) for r in errors]
    for r in results:
        assert r["status"] in ("updated", "manual_review")
        if r["status"] == "manual_review":
            assert r["reason"], r["id"]
        if r["out_of_scope"]:
            assert r["status"] == "manual_review" and r["reason"], r


@needs_sandbox
def test_synthetic_pack_offline(tmp_path):
    if not (SYNTHETIC / "scenarios.json").exists():
        pytest.skip("run tests/make_synthetic_pack.py first")
    results = run_pack(SYNTHETIC, "offline", work=tmp_path)
    _check(results, "synthetic-offline")
    assert all(r["verdict"] == "PASS" for r in results), [(r["id"], r["status"], r.get("reason")) for r in results
                                                        if r["verdict"] != "PASS"]


@pytest.mark.scenarios
@needs_sandbox
def test_year_end_pack_offline(tmp_path):
    if not (YEAR_END / "scenarios.json").exists():
        pytest.skip("year-end pack not imported: run scripts/import_eval_data.py")
    _check(run_pack(YEAR_END, "offline", work=tmp_path), "year-end-offline")


@pytest.mark.scenarios
@needs_sandbox
def test_year_end_pack_manual_answers(tmp_path):
    """Replays hand-written LLM answers (vision, plan, script, review) through the whole pipeline."""
    answers = DATA / "manual-answers" / "year-end"
    if not (YEAR_END / "scenarios.json").exists() or not answers.exists():
        pytest.skip("year-end pack or its manual answers not present")
    copy = tmp_path / "answers"
    shutil.copytree(answers, copy)  # missing answers write request files; keep them out of tests/data
    results = run_pack(YEAR_END, "manual", work=tmp_path / "work",
                       extra_overrides={"llm": {"manual_dir": str(copy)}})
    _check(results, "year-end-manual")
    assert all(r["verdict"].startswith("PASS") for r in results), [
        (r["id"], r["verdict"], r["status"], r.get("reason")) for r in results if not r["verdict"].startswith("PASS")]


@pytest.mark.scenarios
@needs_sandbox
def test_generated_cases_offline(tmp_path):
    packs = sorted(p.parent for p in GENERATED.rglob("*.json")) if GENERATED.exists() else []
    packs = [p for p in dict.fromkeys(packs) if (p / "scenarios.json").exists() or any(p.glob("*.json"))]
    if not packs:
        pytest.skip("no generated cases: run scripts/import_eval_data.py")
    for i, pack in enumerate(packs):
        target = pack / "scenarios.json" if (pack / "scenarios.json").exists() else sorted(pack.glob("*.json"))[0]
        _check(run_pack(target, "offline", work=tmp_path / str(i)), f"generated-{i}-offline")
