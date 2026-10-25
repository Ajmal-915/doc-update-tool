"""Scenario runner: runs a pack of change requests and compares each result
with the scenario's "expected_safe_result".

    python -m doc_update.scenarios --pack tests/data/year-end-test-pack --provider offline
    python -m doc_update.scenarios --pack tests/data/year-end-test-pack --provider bedrock --results-md RESULTS.md

The loader accepts several layouts of scenarios.json (a list, or an object
with "scenarios" / "cases"; field names such as find / source_text /
original_text and replace / replacement / new_text, possibly nested; a
zero-based "page_index"), and reports clearly when it cannot map a scenario.

It also understands:
* chained scenarios whose file is "the output of scenario X": X is approved
  first (v001) and the scenario runs on that version (v002);
* scenarios marked "run": false (graphic recommendations), which are recorded
  as manual review without running;
* free-text expectations such as "PASS, ...", "REFUSE: ...", "PASS ... or
  clean refusal", and "Our requirement: ..." (which overrides what another
  tool was expected to do).
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import api

OUT_OF_SCOPE = {"D01", "D02"}  # graphic scenarios: always expected to go to manual review

KEYS = {
    "id": ["id", "scenario_id", "scenario", "code", "case_id", "key", "name"],
    "pdf": ["pdf", "pdf_file", "file", "document", "input_pdf", "source_pdf", "doc", "filename", "input", "path"],
    "page": ["page", "page_number", "pagenumber", "page_no", "pageno", "printed_page"],
    "page_index": ["page_index", "pageindex", "page_idx"],
    "find": ["find", "source_text", "original_text", "old_text", "current_text", "search", "from", "source", "text",
             "target_text", "existing_text"],
    "replace": ["replace", "replacement", "new_text", "replacement_text", "to", "proposed_text", "replacement_wording",
                "new_wording", "updated_text"],
    "expected": ["expected_safe_result", "expected", "expected_result", "expected_status", "expected_outcome"],
    "title": ["tests", "title", "description", "summary", "label", "category"],
    "recommendation": ["recommendation", "change", "instruction"],
}

REASON_HINTS = [
    (r"ambiguous|appears twice|more than once", "ambiguous"),
    (r"not in the (embedded )?font subset|not in font subset|missing (digit )?glyph", "missing_glyphs"),
    (r"no text layer|scanned|is an image", "scanned_page"),
    (r"run into|collide|cannot fit|would not fit|not enough space", "not_enough_space"),
    (r"not found", "not_found"),
]


@dataclass
class Scenario:
    id: str
    pdf: Path | None
    page: int | None
    find: str | None
    replace: str | None
    expected_raw: object
    expected: str  # "updated" | "manual_review" | "either" | "unknown"
    title: str = ""
    problems: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    depends_on: str | None = None
    run: bool = True
    recommendation: str | None = None
    expected_reason: str | None = None


def _flatten(d, prefix="", out=None) -> dict:
    out = {} if out is None else out
    if isinstance(d, dict):
        for k, v in d.items():
            key = str(k).lower().replace("-", "_").replace(" ", "_")
            if isinstance(v, dict) and key not in ("expected_safe_result",):
                _flatten(v, prefix + key + ".", out)
            out.setdefault(key, v)
            out.setdefault(prefix + key, v)
    return out


def _pick(flat: dict, names: list[str]):
    for n in names:
        if n in flat and flat[n] not in (None, ""):
            return flat[n]
    for n in names:
        for k, v in flat.items():
            if k.endswith("." + n) and v not in (None, ""):
                return v
    return None


def normalise_expected(value) -> str:
    """'updated', 'manual_review', 'either' (both acceptable) or 'unknown'."""
    if isinstance(value, dict):
        for k in ("status", "result", "outcome", "expected", "decision"):
            if k in value:
                return normalise_expected(value[k])
        text = json.dumps(value).lower()
    else:
        text = str(value).lower().strip()
    # "Our requirement: ..." / "Our tool needs / must ..." describe what THIS tool must do
    if re.search(r"our requirement|our tool (needs|must|should)", text):
        if re.search(r"our requirement[^.]*\b(refuse|reject)", text):
            return "manual_review"
        return "updated"
    if text.startswith("pass") and re.search(r"\bor (a )?(clean )?refus", text):
        return "either"
    if text.startswith("refuse") and re.search(r"needs reflow", text):
        # refused only because the tool it was written for cannot rewrap paragraphs:
        # a reflow that passes every check is also acceptable
        return "either"
    if text.startswith(("pass", "updated", "update", "apply", "applied")):
        return "updated"
    if text.startswith(("refuse", "reject", "not supported", "manual", "block")):
        return "manual_review"
    if "manual" in text or "review" in text or "reject" in text or "refuse" in text or "block" in text:
        return "manual_review"
    if any(w in text for w in ("updated", "update", "apply", "applied", "edit", "replace", "pass", "safe")):
        return "updated"
    return "unknown"


def expected_reason(value) -> str | None:
    text = json.dumps(value).lower() if isinstance(value, dict) else str(value).lower()
    for pattern, code in REASON_HINTS:
        if re.search(pattern, text):
            return code
    return None


def load_pack(pack: Path) -> list[Scenario]:
    path = pack / "scenarios.json" if pack.is_dir() else pack
    base = path.parent
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data
    if isinstance(data, dict):
        for k in ("scenarios", "cases", "items", "tests", "changes"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
        else:
            items = [dict(v, id=v.get("id", k)) if isinstance(v, dict) else {"id": k, "value": v} for k, v in data.items()]
    pdfs = sorted(base.rglob("*.pdf"))
    out = []
    for i, raw in enumerate(items):
        flat = _flatten(raw)
        sid = str(_pick(flat, KEYS["id"]) or f"S{i + 1:02d}")
        problems: list[str] = []
        run = not (isinstance(raw, dict) and raw.get("run") is False)
        pdf_name = _pick(flat, KEYS["pdf"])
        pdf, depends = None, None
        if pdf_name:
            m = re.search(r"output of scenario\s+([A-Za-z]+\d+)", str(pdf_name), re.I)
            if m:
                depends = m.group(1).upper()
            else:
                cand = base / str(pdf_name)
                if not cand.exists():
                    matches = [p for p in pdfs if p.name == Path(str(pdf_name)).name]
                    cand = matches[0] if matches else cand
                pdf = cand if cand.exists() else None
                if pdf is None:
                    problems.append(f"PDF {pdf_name!r} not found next to scenarios.json")
        elif len(pdfs) == 1:
            pdf = pdfs[0]
        else:
            problems.append("no PDF named in the scenario")
        page = None
        pidx = _pick(flat, KEYS["page_index"])
        if pidx is not None:
            try:
                page = int(pidx) + 1  # page_index is zero-based
            except (TypeError, ValueError):
                problems.append(f"page_index {pidx!r} is not a number")
        else:
            p = _pick(flat, KEYS["page"])
            try:
                page = int(p) if p is not None else None
            except (TypeError, ValueError):
                problems.append(f"page {p!r} is not a number")
        find = _pick(flat, KEYS["find"])
        replace = _pick(flat, KEYS["replace"])
        expected_raw = _pick(flat, KEYS["expected"])
        recommendation = _pick(flat, KEYS["recommendation"])
        if run:
            for name, value in (("page", page), ("source text", find), ("replacement", replace)):
                if value is None:
                    problems.append(f"no {name} field (keys present: {sorted(k for k in flat if '.' not in k)})")
        expected = normalise_expected(expected_raw) if expected_raw is not None else "unknown"
        reason = expected_reason(expected_raw) if expected in ("manual_review",) and expected_raw is not None else None
        if sid.upper() in OUT_OF_SCOPE or not run:
            expected = "manual_review"
            reason = "out_of_scope"
        out.append(Scenario(sid, pdf, page, str(find) if find is not None else None,
                            str(replace) if replace is not None else None, expected_raw, expected,
                            str(_pick(flat, KEYS["title"]) or ""), problems, raw, depends, run,
                            str(recommendation) if recommendation else None, reason))
    return out


def run_pack(pack: Path, provider: str = "offline", config_path: str | None = None, work: Path | None = None,
             only: list[str] | None = None, extra_overrides: dict | None = None) -> list[dict]:
    scenarios = load_pack(pack)
    tmp = Path(work or tempfile.mkdtemp(prefix="scenarios_"))
    results = []
    done: dict[str, dict] = {}
    wanted = set(only or [])
    if wanted:
        # also run the scenarios a requested scenario depends on
        for sc in scenarios:
            if sc.id in wanted and sc.depends_on:
                wanted.add(sc.depends_on)
    for sc in scenarios:
        if wanted and sc.id not in wanted:
            continue
        row = {"id": sc.id, "title": sc.title, "page": sc.page, "find": sc.find, "replace": sc.replace,
               "expected_safe_result": sc.expected_raw, "expected": sc.expected, "expected_reason": sc.expected_reason,
               "out_of_scope": sc.id.upper() in OUT_OF_SCOPE or not sc.run}
        if not sc.run:
            row.update({"status": "manual_review", "reason_code": "out_of_scope",
                        "reason": "graphic change requested (" + (sc.recommendation or "no text replacement given")
                                  + "); the tool only replaces text, so a person must make this change"})
            row["verdict"] = verdict(sc.expected, row["status"], sc.expected_reason, row["reason_code"])
            results.append(row)
            continue
        if sc.problems:
            row.update({"status": "error", "verdict": "ERROR", "reason": "; ".join(sc.problems)})
            results.append(row)
            continue
        ov = {"llm": {"provider": provider, "manual_session": sc.id}}
        if extra_overrides:
            for k, v in extra_overrides.items():
                ov.setdefault(k, {}).update(v)
        doc_id = "doc"
        if sc.depends_on:
            dep = done.get(sc.depends_on)
            if dep is None or dep.get("status") != "updated":
                row.update({"status": "error", "verdict": "BLOCKED",
                            "reason": f"depends on {sc.depends_on}, which did not produce an approved version"})
                results.append(row)
                continue
            ov["store"] = {"root": dep["store"]}
            appr = api.approve_change(doc_id, dep["change_id"], approver=f"scenario runner ({sc.id})",
                                      config_path=config_path, config_overrides=ov)
            if appr.get("status") != "approved":
                row.update({"status": "error", "verdict": "BLOCKED", "reason": f"could not approve {sc.depends_on}: {appr}"})
                results.append(row)
                continue
            row["based_on"] = f"{sc.depends_on} approved as {appr['version']}"
        else:
            store = tmp / "store" / sc.id
            ov["store"] = {"root": str(store)}
            ing = api.ingest_document(sc.pdf, doc_id, config_path, ov)
            if ing.get("status") != "ingested":
                row.update({"status": "error", "verdict": "ERROR", "reason": ing.get("error")})
                results.append(row)
                continue
        t0 = time.monotonic()
        res = api.update_document(doc_id, sc.find, sc.replace, sc.page, config_path, ov)
        row["seconds"] = round(time.monotonic() - t0, 1)
        row["status"] = res.get("status")
        row["method"] = res.get("method")
        row["change_id"] = res.get("change_id")
        row["reason_code"] = res.get("reason_code")
        row["reason"] = res.get("reason") or res.get("error")
        row["report_path"] = res.get("report_path")
        row["candidate_path"] = res.get("candidate_path")
        if res.get("report_path") and Path(res["report_path"]).exists():
            rep = json.loads(Path(res["report_path"]).read_text())
            row["font_size_change"] = rep.get("font_size_change")
            row["llm"] = rep.get("llm", {}).get("budget")
            row["warnings"] = rep.get("warnings", [])
            row["attempts"] = len(rep.get("attempts", []))
        row["verdict"] = verdict(sc.expected, row["status"], sc.expected_reason, row.get("reason_code"))
        done[sc.id] = {"status": row["status"], "store": ov["store"]["root"], "change_id": row.get("change_id")}
        if sc.depends_on and row["status"] == "updated":
            appr = api.approve_change(doc_id, row["change_id"], approver=f"scenario runner ({sc.id})",
                                      config_path=config_path, config_overrides=ov)
            row["approved_as"] = appr.get("version")
        results.append(row)
    return results


def verdict(expected: str, status: str | None, reason: str | None = None, got_reason: str | None = None) -> str:
    if status not in ("updated", "manual_review"):
        return "ERROR"
    if expected == "unknown":
        return "REPORTED"
    if expected == "either":
        return "PASS"
    if expected == status:
        if status == "manual_review" and reason and got_reason and reason != got_reason and reason != "out_of_scope":
            return "PASS (other reason)"
        return "PASS"
    if expected == "manual_review" and status == "updated":
        return "UNSAFE"  # an edit was produced where a person should decide
    return "MISSED"  # safe, but more cautious than expected


def markdown(results: list[dict], title: str) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    lines = [f"### {title}", "", "Verdicts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
             "| ID | Expected | Result | Method / reason | Verdict |", "|---|---|---|---|---|"]
    for r in results:
        detail = r.get("method") or (f"{r.get('reason_code') or ''}: {r.get('reason') or ''}")
        detail = str(detail).replace("|", "/").replace("\n", " ")[:220]
        exp = r["expected"] + (f" ({r['expected_reason']})" if r.get("expected_reason") else "")
        lines.append(f"| {r['id']} | {exp} | {r.get('status')} | {detail} | {r['verdict']} |")
    return "\n".join(lines) + "\n"


def write_results_md(path: Path, section: str, marker: str) -> None:
    begin, end = f"<!-- {marker}:BEGIN -->", f"<!-- {marker}:END -->"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f"{begin}\n{section}{end}"
    if begin in text and end in text:
        text = text[: text.index(begin)] + block + text[text.index(end) + len(end):]
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m doc_update.scenarios")
    p.add_argument("--pack", required=True, help="folder with scenarios.json and the PDFs")
    p.add_argument("--provider", default="offline", choices=["offline", "bedrock", "replay", "manual"])
    p.add_argument("--config")
    p.add_argument("--work", help="folder for the temporary stores (default: a temp folder)")
    p.add_argument("--only", nargs="*", help="scenario IDs to run")
    p.add_argument("--out", help="write the results as JSON here")
    p.add_argument("--results-md", help="update this Markdown file (e.g. RESULTS.md)")
    p.add_argument("--marker", default=None, help="section marker in the Markdown file")
    p.add_argument("--manual-dir", help="answers folder for --provider manual")
    args = p.parse_args(argv)
    pack = Path(args.pack)
    extra = {"llm": {"manual_dir": str(Path(args.manual_dir).resolve())}} if args.manual_dir else None
    results = run_pack(pack, args.provider, args.config, Path(args.work) if args.work else None, args.only, extra)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    title = f"{pack.name} ({args.provider})"
    md = markdown(results, title)
    print(md)
    if args.results_md:
        marker = args.marker or f"SCENARIOS-{pack.name}-{args.provider}".upper()
        write_results_md(Path(args.results_md), md, marker)
    return 1 if any(r["verdict"] in ("UNSAFE", "ERROR") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
