"""Run a set of plain-words recommendations and compare each result with the expected one.

    python -m doc_update.recommend_pack --pack tests/data/recommendations/year-end.json [--provider bedrock]
        [--editor-provider manual --manual-dir DIR] [--work DIR] [--only R01 R02] [--out results.json]

The pack is JSON: {"pack": folder with the PDFs (relative to the file), "recommendations": [{"id", "file",
"page", "text", "expected": "updated" | "manual_review" | "either", "reason_code": optional, "edits": optional
number of edits the plan must have}]}.  Every recommendation runs on a fresh copy of its PDF in its own store;
one whose PDF is missing is skipped.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from . import api


def run_pack(pack_file: Path, overrides: dict | None = None, work: Path | None = None,
             only: list[str] | None = None, config_path: str | None = None) -> list[dict]:
    pack = json.loads(Path(pack_file).read_text(encoding="utf-8"))
    pdf_dir = (Path(pack_file).parent / pack.get("pack", ".")).resolve()
    work = Path(work or tempfile.mkdtemp(prefix="recommend_"))
    rows = []
    for rec in pack["recommendations"]:
        if only and rec["id"] not in only:
            continue
        ov = json.loads(json.dumps(overrides or {}))
        ov.setdefault("store", {})["root"] = str(work / rec["id"])
        ov.setdefault("llm", {})["manual_session"] = rec["id"]
        row = {"id": rec["id"], "page": rec["page"], "text": rec["text"], "expected": rec["expected"],
               "expected_reason": rec.get("reason_code"), "file": rec["file"]}
        if not (pdf_dir / rec["file"]).exists():
            rows.append({**row, "status": "skipped", "reason": f"{rec['file']} not found", "verdict": "SKIPPED"})
            continue
        ing = api.ingest_document(pdf_dir / rec["file"], "doc", config_path, ov)
        if ing.get("status") != "ingested":
            row.update(status="error", reason=ing.get("error"))
        else:
            res = api.recommend_change("doc", rec["page"], rec["text"], config_path, ov)
            row.update(status=res.get("status"), reason_code=res.get("reason_code"), reason=res.get("reason"),
                       edits=[{k: e.get(k) for k in ("find", "replace", "method")} for e in res.get("edits") or []],
                       candidate=res.get("candidate_path"), report=res.get("report_path"))
            plan = (json.loads(Path(res["report_path"]).read_text(encoding="utf-8")).get("edit_plan")
                    if res.get("report_path") and Path(res["report_path"]).exists() else None)
            row["plan_edits"] = len(plan.get("edits") or []) if plan else None
        expected = ("updated", "manual_review") if rec["expected"] == "either" else (rec["expected"],)
        ok = row["status"] in expected and (not rec.get("reason_code") or row.get("reason_code") == rec["reason_code"])
        ok = ok and row.get("reason_code") != "llm_error"  # the model gave no answer: nothing was tested
        if ok and "edits" in rec:  # the number of edits the editor planned
            ok = row.get("plan_edits") == rec["edits"]
        row["verdict"] = "PASS" if ok else ("UNSAFE" if row["status"] == "updated" else "FAIL")
        rows.append(row)
    return rows


def markdown(rows: list[dict]) -> str:
    out = ["| ID | Page | Expected | Result | Edits / reason | Verdict |", "|---|---|---|---|---|---|"]
    for r in rows:
        exp = r["expected"] + (f" ({r['expected_reason']})" if r.get("expected_reason") else "")
        if r.get("edits") and r["status"] == "updated":
            what = "; ".join(f"{e['find']!r} → {e['replace']!r} ({e['method']})" for e in r["edits"])
        else:
            what = f"{r.get('reason_code') or ''}: {(r.get('reason') or '')[:160]}"
        out.append(f"| {r['id']} | {r['page']} | {exp} | {r['status']} | {what.replace('|', '/')} | {r['verdict']} |")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m doc_update.recommend_pack")
    p.add_argument("--pack", required=True)
    p.add_argument("--config")
    p.add_argument("--provider", default="offline", choices=["offline", "bedrock", "replay", "manual"])
    p.add_argument("--editor-provider", choices=["offline", "bedrock", "replay", "manual"])
    p.add_argument("--manual-dir")
    p.add_argument("--work")
    p.add_argument("--only", nargs="*")
    p.add_argument("--out")
    a = p.parse_args(argv)
    llm = {"provider": a.provider}
    if a.editor_provider:
        llm["editor_provider"] = a.editor_provider
    if a.manual_dir:
        llm["manual_dir"] = str(Path(a.manual_dir).resolve())
    rows = run_pack(Path(a.pack), {"llm": llm}, Path(a.work) if a.work else None, a.only, a.config)
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(markdown(rows))
    return 1 if any(r["verdict"] in ("UNSAFE", "FAIL") for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
