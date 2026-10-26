"""Step 5: versioning, approve / reject, manifest and audit log, CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from doc_update import api
from doc_update.sandbox.local import default_sandbox_python
from doc_update.store import Store, StoreError, sha256_file

ROOT = Path(__file__).resolve().parent.parent
needs_sandbox = pytest.mark.skipif(not default_sandbox_python().exists(), reason="sandbox venv not installed")


def _ov(tmp_path):
    return {"store": {"root": str(tmp_path / "store")}, "llm": {"provider": "offline"}}


def test_ingest_sets_original_read_only_with_hash(pdfs, tmp_path):
    res = api.ingest_document(pdfs["report"], "rep", config_overrides=_ov(tmp_path))
    orig = tmp_path / "store" / "rep" / "original.pdf"
    assert res["sha256"] == sha256_file(orig) == sha256_file(pdfs["report"])
    assert not os.stat(orig).st_mode & stat.S_IWUSR
    m = json.loads((tmp_path / "store" / "rep" / "manifest.json").read_text())
    assert m["original"]["sha256"] == res["sha256"] and m["versions"] == []
    assert "ingest" in (tmp_path / "store" / "rep" / "audit.log").read_text()
    again = api.ingest_document(pdfs["report"], "rep", config_overrides=_ov(tmp_path))
    assert again["status"] == "error"


def test_invalid_ids_are_refused(tmp_path, pdfs):
    assert api.ingest_document(pdfs["report"], "../escape", config_overrides=_ov(tmp_path))["status"] == "error"
    store = Store(tmp_path / "s")
    with pytest.raises(StoreError):
        store.doc_dir("a/b")


@needs_sandbox
def test_update_approve_then_second_change_builds_on_v001(pdfs, tmp_path):
    ov = _ov(tmp_path)
    api.ingest_document(pdfs["report"], "rep", config_overrides=ov)
    r1 = api.update_document("rep", "31 December 2023", "31 December 2024", 1, config_overrides=ov)
    assert r1["status"] == "updated"
    status = api.document_status("rep", config_overrides=ov)
    assert status["current_version"] == "original" and len(status["pending"]) == 1
    a1 = api.approve_change("rep", r1["change_id"], approver="Reviewer A", config_overrides=ov)
    assert a1["status"] == "approved" and a1["version"] == "v001"
    v1 = tmp_path / "store" / "rep" / "versions" / "v001.pdf"
    assert v1.exists() and not os.stat(v1).st_mode & stat.S_IWUSR
    assert not (tmp_path / "store" / "rep" / "pending" / r1["change_id"]).exists()
    assert (tmp_path / "store" / "rep" / "history" / r1["change_id"] / "report.json").exists()
    # the original is untouched
    m = json.loads((tmp_path / "store" / "rep" / "manifest.json").read_text())
    assert sha256_file(tmp_path / "store" / "rep" / "original.pdf") == m["original"]["sha256"]
    # the next change works on v001
    r2 = api.update_document("rep", "within 30 days", "within 28 days", 1, config_overrides=ov)
    assert r2["status"] == "updated"
    rep2 = json.loads(Path(r2["report_path"]).read_text())
    assert rep2["base_version"] == "v001"
    a2 = api.approve_change("rep", r2["change_id"], config_overrides=ov)
    assert a2["version"] == "v002"
    import pdf_ops

    text = "".join(g.text for g in pdf_ops.open(tmp_path / "store" / "rep" / "versions" / "v002.pdf").model(0).glyphs)
    assert "31 December 2024" in text and "within 28 days" in text
    audit = (tmp_path / "store" / "rep" / "audit.log").read_text()
    assert audit.count('"approve"') == 2


@needs_sandbox
def test_reject_deletes_candidate(pdfs, tmp_path):
    ov = _ov(tmp_path)
    api.ingest_document(pdfs["report"], "rep", config_overrides=ov)
    r = api.update_document("rep", "31 December 2023", "31 December 2024", 1, config_overrides=ov)
    res = api.reject_change("rep", r["change_id"], approver="Reviewer B", note="wrong year", config_overrides=ov)
    assert res["status"] == "rejected"
    hist = tmp_path / "store" / "rep" / "history" / r["change_id"]
    assert hist.exists() and not (hist / "candidate.pdf").exists()
    assert api.document_status("rep", config_overrides=ov)["current_version"] == "original"
    again = api.approve_change("rep", r["change_id"], config_overrides=ov)
    assert again["status"] == "error"


@needs_sandbox
def test_stale_candidate_cannot_be_approved(pdfs, tmp_path):
    ov = _ov(tmp_path)
    api.ingest_document(pdfs["report"], "rep", config_overrides=ov)
    r1 = api.update_document("rep", "31 December 2023", "31 December 2024", 1, config_overrides=ov)
    r2 = api.update_document("rep", "within 30 days", "within 28 days", 1, config_overrides=ov)
    assert api.approve_change("rep", r1["change_id"], config_overrides=ov)["status"] == "approved"
    res = api.approve_change("rep", r2["change_id"], config_overrides=ov)
    assert res["status"] == "error" and "latest version is v001" in res["error"]


@needs_sandbox
def test_tampered_candidate_or_original_is_refused(pdfs, tmp_path):
    ov = _ov(tmp_path)
    api.ingest_document(pdfs["report"], "rep", config_overrides=ov)
    r = api.update_document("rep", "31 December 2023", "31 December 2024", 1, config_overrides=ov)
    cand = Path(r["candidate_path"])
    with open(cand, "ab") as fh:
        fh.write(b"\n% tampered\n")
    res = api.approve_change("rep", r["change_id"], config_overrides=ov)
    assert res["status"] == "error" and "modified" in res["error"]
    orig = tmp_path / "store" / "rep" / "original.pdf"
    os.chmod(orig, stat.S_IREAD | stat.S_IWRITE)
    with open(orig, "ab") as fh:
        fh.write(b"\n% tampered\n")
    res = api.update_document("rep", "within 30 days", "within 28 days", 1, config_overrides=ov)
    assert res["status"] == "error" and "SHA-256" in res["error"]


def test_manual_review_is_recorded_without_pdf(pdfs, tmp_path):
    ov = _ov(tmp_path)
    api.ingest_document(pdfs["signed"], "sig", config_overrides=ov)
    r = api.update_document("sig", "31 December 2023", "31 December 2024", 1, config_overrides=ov)
    assert r["status"] == "manual_review"
    m = json.loads((tmp_path / "store" / "sig" / "manifest.json").read_text())
    assert m["changes"][r["change_id"]]["status"] == "manual_review"
    assert not list((tmp_path / "store" / "sig").rglob("candidate.pdf"))


@needs_sandbox
def test_cli_round_trip(pdfs, tmp_path):
    store = str(tmp_path / "store")
    py = sys.executable

    def cli(*args):
        p = subprocess.run([py, "-m", "doc_update", "--store", store, *args], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
        return p.returncode, json.loads(p.stdout) if p.stdout.strip().startswith("{") else p.stdout + p.stderr

    rc, out = cli("ingest", "--pdf", str(pdfs["report"]), "--doc-id", "cli")
    assert rc == 0 and out["status"] == "ingested"
    rc, out = cli("update", "--doc-id", "cli", "--find", "31 December 2023", "--replace", "31 December 2024",
                  "--page", "1", "--provider", "offline")
    assert rc == 0 and out["status"] == "updated", out
    change = out["change_id"]
    rc, out = cli("status", "--doc-id", "cli")
    assert rc == 0 and out["pending"][0]["change_id"] == change
    rc, out = cli("approve", "--doc-id", "cli", "--change", change, "--by", "QA")
    assert rc == 0 and out["version"] == "v001"
    rc, out = cli("update", "--doc-id", "cli", "--find", "no such text at all here", "--replace", "x", "--page", "1",
                  "--provider", "offline")
    assert rc == 2 and out["status"] == "manual_review"
    rc, out = cli("approve", "--doc-id", "cli", "--change", "c20990101T000000Z-abcdef")
    assert rc == 1 and out["status"] == "error"


def test_password_protected_or_broken_pdf_is_refused_cleanly(pdfs, tmp_path):
    # corpus test: these raised pikepdf exceptions out of the API and left a half-made document behind
    import pikepdf

    locked = tmp_path / "locked.pdf"
    with pikepdf.open(pdfs["report"]) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="secret", owner="secret"))
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n%%EOF\n")
    ov = {"store": {"root": str(tmp_path / "store")}}
    r = api.ingest_document(locked, "locked", None, ov)
    assert r["status"] == "error" and "password-protected" in r["error"]
    r = api.ingest_document(broken, "broken", None, ov)
    assert r["status"] == "error" and "not a readable PDF" in r["error"]
    assert not (tmp_path / "store").exists() or not any((tmp_path / "store").iterdir())
