"""Python functions for the application: each returns a JSON-serialisable dict."""

from __future__ import annotations

from pathlib import Path

from .config import load_config
from .pipeline import run_change
from .recommend import run_recommendation
from .store import Store, StoreError


def _store(cfg: dict) -> Store:
    return Store(cfg["store"]["root"])


def ingest_document(pdf_path: str | Path, doc_id: str, config_path: str | Path | None = None,
                    config_overrides: dict | None = None) -> dict:
    cfg = load_config(config_path, config_overrides)
    try:
        return _store(cfg).ingest(pdf_path, doc_id)
    except StoreError as exc:
        return {"status": "error", "error": str(exc)}


def update_document(doc_id: str, find: str, replace: str, page: int, config_path: str | Path | None = None,
                    config_overrides: dict | None = None, llm=None, sandbox=None) -> dict:
    """Run one change request.

    Returns {"status": "updated", "change_id", "candidate_path", "report_path", ...} or
    {"status": "manual_review", "reason_code", "reason", "report_path", ...}.
    """
    cfg = load_config(config_path, config_overrides)
    try:
        return run_change(_store(cfg), cfg, doc_id, find, replace, int(page), llm=llm, sandbox=sandbox)
    except (StoreError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}


def recommend_change(doc_id: str, page: int, recommendation: str, config_path: str | Path | None = None,
                     config_overrides: dict | None = None, plan_only: bool = False, llm=None, sandbox=None) -> dict:
    """Carry out a recommendation, written in plain words, on one page.

    The page is rendered and its text listed; the editor model turns the recommendation into exact edits;
    code verifies them against the page; then every edit runs through the normal steps, each on the previous
    one's output, into ONE candidate (or none).  Returns {"status": "updated", "change_id", "edits",
    "candidate_path", "report_path", ...}, {"status": "manual_review", "reason_code", "reason", ...} or, with
    plan_only, {"status": "planned", "edits", "plan", ...} without changing anything.
    """
    cfg = load_config(config_path, config_overrides)
    try:
        return run_recommendation(_store(cfg), cfg, doc_id, int(page), recommendation, llm=llm, sandbox=sandbox,
                                  plan_only=plan_only)
    except (StoreError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}


def approve_change(doc_id: str, change_id: str, approver: str | None = None, note: str | None = None,
                   config_path: str | Path | None = None, config_overrides: dict | None = None) -> dict:
    cfg = load_config(config_path, config_overrides)
    try:
        return _store(cfg).approve(doc_id, change_id, approver, note)
    except StoreError as exc:
        return {"status": "error", "error": str(exc)}


def reject_change(doc_id: str, change_id: str, approver: str | None = None, note: str | None = None,
                  config_path: str | Path | None = None, config_overrides: dict | None = None) -> dict:
    cfg = load_config(config_path, config_overrides)
    try:
        return _store(cfg).reject(doc_id, change_id, approver, note)
    except StoreError as exc:
        return {"status": "error", "error": str(exc)}


def document_status(doc_id: str, config_path: str | Path | None = None, config_overrides: dict | None = None) -> dict:
    cfg = load_config(config_path, config_overrides)
    try:
        return {"status": "ok", **_store(cfg).status(doc_id)}
    except StoreError as exc:
        return {"status": "error", "error": str(exc)}
