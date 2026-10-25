"""The pipeline for one change request (steps 1-10)."""

from __future__ import annotations

import io
import json
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

import pdf_ops
from pdf_ops.forms import find_field
from pdf_ops.geometry import Rect

from . import areas
from .analysis import analyse, page_summary
from .checks import run_checks
from .llm import LLM, BudgetExceeded, LLMError, make_llm
from .llm.schemas import METHODS
from .matching import find_target_text
from .measure import measure
from .render import draw_boxes, page_frames, render_pages
from .sandbox import Sandbox, make_sandbox
from .store import Store, now, sha256_file

RISK_RANK = {"original_box": 0, "reduced_font": 1, "horizontal_expansion": 2, "paragraph_reflow": 3, "form_field_update": 0}


class ManualReview(Exception):
    def __init__(self, code: str, reason: str, **details):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = details


@dataclass
class Context:
    store: Store
    cfg: dict
    doc_id: str
    change_id: str
    folder: Path
    base_path: Path
    base_label: str
    base_sha: str
    find: str
    replace: str
    page: int
    report: dict = field(default_factory=dict)

    def audit(self, event: str, **details) -> None:
        self.store.audit(self.doc_id, event, change_id=self.change_id, **details)


# ------------------------------------------------------------------ helpers
def _png(img: Image.Image, max_side: int = 2400) -> bytes:
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def merge_advice(meas, advice: dict | None, frame, target_end: float, fits: dict | None = None) -> dict:
    """Combine measurements and the layout advice, always keeping the more cautious answer."""
    allowed = set(METHODS) - {"form_field_update"}
    notes: list[str] = []
    region_types = [meas.region_type]
    risks = [dict(r, source="measurement") for r in meas.risks]
    boundary = meas.boundary
    if advice:
        if advice.get("region_type") and advice["region_type"] != meas.region_type:
            region_types.append(advice["region_type"])
            notes.append(f"layout check sees a {advice['region_type']}, measurements a {meas.region_type}; "
                         "the restrictions of both apply")
        risks += [dict(r, source="layout_check") for r in advice.get("risks", [])]
        bpx = advice.get("boundary_px") or {}
        if "x1" in bpx:
            x1_pt = frame.crop.x0 + float(bpx["x1"]) / frame.scale
            if x1_pt < boundary - 0.5:
                if x1_pt <= target_end + 0.5:
                    allowed.discard("horizontal_expansion")
                    notes.append("layout check puts the boundary at the end of the target; no expansion")
                else:
                    notes.append(f"layout check boundary x={x1_pt:.1f} is tighter than measured x={boundary:.1f}; using it")
                    boundary = x1_pt
        rec = advice.get("recommended_method")
        if rec == "manual_review":
            allowed -= {"horizontal_expansion", "paragraph_reflow"}
            notes.append("layout check recommends manual review; only changes inside the original box are allowed")
        elif rec in RISK_RANK and fits is not None and not (fits.get(rec) and fits[rec].feasible):
            notes.append(f"layout check recommends {rec}, but the measurements show it cannot fit; "
                         "its boundary and risks still apply")
        elif rec in RISK_RANK:
            riskier = {m for m in allowed if RISK_RANK[m] > RISK_RANK[rec]}
            if riskier:
                allowed -= riskier
                notes.append(f"layout check recommends {rec}; riskier methods disabled: {sorted(riskier)}")
    for rt in region_types:
        if rt == "table_of_contents_line":
            allowed -= {"horizontal_expansion", "paragraph_reflow"}
        elif rt in ("heading", "table_cell"):
            # headings are single lines; table rows have fixed geometry (values aligned with the label)
            allowed.discard("paragraph_reflow")
        elif rt == "form_field":
            allowed -= {"horizontal_expansion", "paragraph_reflow"}
    if any(r.get("type") == "dot_leader" for r in risks):
        allowed -= {"horizontal_expansion", "paragraph_reflow"}
    return {"allowed_methods": [m for m in METHODS if m in allowed], "boundary": boundary, "region_types": region_types,
            "risks": risks, "notes": notes}


def _fit_all(doc, target, replacement, boundary: float, meas, min_scale: float) -> dict:
    fits = {
        "original_box": pdf_ops.fit_in_place(doc, target, replacement),
        "horizontal_expansion": pdf_ops.fit_horizontal(doc, target, replacement, boundary),
        "reduced_font": pdf_ops.fit_scaled(doc, target, replacement, min_scale),
    }
    fit_reflow = getattr(pdf_ops, "fit_reflow", None)
    if fit_reflow is not None:
        max_bottom = meas.paragraph["bbox"][1] - meas.free_space_below
        fits["paragraph_reflow"] = fit_reflow(doc, target, replacement, boundary=boundary, max_bottom=max_bottom)
    else:
        fits["paragraph_reflow"] = pdf_ops.Fit("paragraph_reflow", False, "paragraph reflow is not available")
    if fits["horizontal_expansion"].feasible and boundary <= max(p.x1 for p in target.parts) + 0.01 and \
            not fits["original_box"].feasible:
        fits["horizontal_expansion"].feasible = False
        fits["horizontal_expansion"].reason = "there is no room to the right of the target"
    return fits


def _not_enough_space_reason(fits: dict, allowed: list[str], merged: dict) -> str:
    parts = []
    for m in ("original_box", "horizontal_expansion", "reduced_font", "paragraph_reflow"):
        f = fits.get(m)
        if f is not None and not f.feasible:
            parts.append(f"{m}: {f.reason}")
        elif m not in allowed:
            why = "; ".join(merged["notes"]) or f"not used for a {'/'.join(merged['region_types'])}"
            if any(r.get("type") == "dot_leader" for r in merged["risks"]):
                why = "dot leaders follow the target"
            parts.append(f"{m}: not allowed here ({why})")
    return "not enough space for the replacement. " + " | ".join(parts)


def _validate_plan(plan: dict, fits: dict, allowed: list[str], merged: dict, min_scale: float) -> tuple[list[str], dict, list[str]]:
    """Methods to try, in the fixed order, and the clamped parameters."""
    notes = []
    feasible = [m for m in METHODS if m in allowed and fits.get(m) is not None and fits[m].feasible]
    proposed = [plan.get("chosen_method")] + list(plan.get("fallback_methods") or [])
    rejected = {r["method"] for r in plan.get("rejected_methods") or [] if isinstance(r, dict) and "method" in r}
    order = [m for m in METHODS if m in feasible and (m in proposed or m not in rejected)]
    if order and proposed and proposed[0] in METHODS and proposed[0] != order[0]:
        notes.append(f"planner chose {proposed[0]}; the first method that can work is {order[0]}, so it is tried first")
    for m in proposed:
        if m in METHODS and m not in feasible:
            notes.append(f"planner proposed {m}, which the measurements or layout check rule out; ignored")
    params = dict(plan.get("parameters") or {})
    boundary = merged["boundary"]
    bx = params.get("boundary_x")
    if isinstance(bx, (int, float)) and bx < boundary:
        notes.append(f"planner boundary x={bx:.1f} is tighter than the measured {boundary:.1f}; using it")
        boundary = float(bx)
    clamped = {"boundary": round(boundary, 3), "min_scale": min_scale}
    return order, clamped, notes


# ------------------------------------------------------------------ pipeline
def run_change(store: Store, cfg: dict, doc_id: str, find: str, replace: str, page: int,
               llm: LLM | None = None, sandbox: Sandbox | None = None) -> dict:
    if not find or not find.strip():
        raise ValueError("--find must not be empty")
    if replace is None or not replace.strip():
        raise ValueError("--replace must not be empty")
    store.manifest(doc_id)
    store.verify_integrity(doc_id)
    base_path, base_label, base_sha = store.current(doc_id)
    change_id, folder = store.new_change(doc_id)
    ctx = Context(store, cfg, doc_id, change_id, folder, base_path, base_label, base_sha, find, replace, int(page))
    ctx.report = {
        "doc_id": doc_id, "change_id": change_id, "created": now(), "base_version": base_label, "base_sha256": base_sha,
        "request": {"find": find, "replace": replace, "page": int(page)}, "attempts": [], "warnings": [],
    }
    ctx.audit("change_started", base_version=base_label, page=int(page), find_sha256=_h(find), replace_sha256=_h(replace))
    try:
        if llm is None:
            llm = make_llm(cfg, audit=lambda **kw: ctx.audit(kw.pop("event", "llm_call"), **kw), record_name=change_id)
        else:
            llm.audit = lambda **kw: ctx.audit(kw.pop("event", "llm_call"), **kw)
        ctx.report["llm"] = {"provider": llm.provider, "models": llm.models()}
        sandbox = sandbox or make_sandbox(cfg)
        result = _run(ctx, llm, sandbox)
    except ManualReview as mr:
        result = _manual(ctx, mr.code, mr.reason, **mr.details)
    except BudgetExceeded as exc:
        result = _manual(ctx, "llm_budget", f"LLM budget exceeded: {exc}")
    except LLMError as exc:
        result = _manual(ctx, "llm_error", f"the language model could not be used: {exc}")
    except Exception as exc:  # never leave a half-finished change behind
        ctx.report["error_trace"] = traceback.format_exc()[-4000:]
        result = _manual(ctx, "internal_error", f"internal error: {type(exc).__name__}: {exc}")
    finally:
        if llm is not None:
            ctx.report.setdefault("llm", {})["budget"] = llm.budget.to_dict()
    _write_report(ctx)
    if result["status"] == "manual_review":
        dest = store.finish_manual_review(doc_id, change_id)
        result["report_path"] = str(dest / "report.json")
    else:
        result["report_path"] = str(folder / "report.json")
        result["candidate_path"] = str(folder / "candidate.pdf")
    store.record_change(doc_id, change_id, {
        "status": result["status"], "created": ctx.report["created"], "find": find, "replace": replace, "page": int(page),
        "base_version": base_label, "base_sha256": base_sha, "method": ctx.report.get("method"),
        "reason_code": ctx.report.get("reason_code"), "candidate_sha256": ctx.report.get("candidate_sha256"),
    })
    ctx.audit("change_finished", status=result["status"], method=ctx.report.get("method"),
              reason_code=ctx.report.get("reason_code"))
    return result


def _h(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _manual(ctx: Context, code: str, reason: str, **details) -> dict:
    ctx.report.update({"status": "manual_review", "reason_code": code, "reason": reason, **details})
    for f in ctx.folder.glob("candidate*.pdf"):
        f.unlink()
    return {"status": "manual_review", "doc_id": ctx.doc_id, "change_id": ctx.change_id, "reason_code": code,
            "reason": reason, **{k: v for k, v in details.items() if k in ("candidates",)}}


def _write_report(ctx: Context) -> None:
    with open(ctx.folder / "report.json", "w", encoding="utf-8") as fh:
        json.dump(ctx.report, fh, indent=2, default=str)


def _run(ctx: Context, llm: LLM, sandbox: Sandbox) -> dict:
    cfg = ctx.cfg
    min_scale = float(cfg["editing"]["min_font_scale"])
    enabled = list(cfg["editing"].get("methods") or METHODS)
    rep = ctx.report

    # ---------------------------------------------------------- step 1
    page_index = ctx.page - 1
    da, doc = analyse(ctx.base_path, pages=[p for p in (page_index - 1, page_index, page_index + 1) if p >= 0],
                      include_chars=True)
    (ctx.folder / "logs").mkdir(exist_ok=True)
    with open(ctx.folder / "logs" / "analysis.json", "w", encoding="utf-8") as fh:
        json.dump(da.to_dict(), fh, default=str)
    rep["document"] = {"pages": da.page_count, "signed": da.signed, "encrypted": da.encrypted}
    if da.encrypted:
        raise ManualReview("encrypted", "the PDF is encrypted; it cannot be edited safely")
    if da.signed:
        raise ManualReview("signed_pdf", "the PDF is digitally signed; any edit would invalidate the signature. "
                                         "Ask the signer for an unsigned source document.",
                           signature_fields=da.signature_fields)
    if not 0 <= page_index < da.page_count:
        raise ManualReview("not_found", f"page {ctx.page} does not exist; the document has {da.page_count} pages")

    # ---------------------------------------------------------- step 2
    match = find_target_text(doc, ctx.find, ctx.page, float(cfg["matching"]["min_score"]),
                             float(cfg["matching"]["ambiguity_margin"]))
    rep["match"] = match.to_dict()
    ctx.audit("match", status=match.status, score=match.best.score if match.best else None)
    if match.status != "found":
        for p in match.searched_pages:
            summ = da.pages.get(p) or page_summary(doc, p, include_chars=False)
            if summ.get("scanned"):
                raise ManualReview("scanned_page", f"page {p + 1} is a scanned image"
                                   + (" with an invisible OCR text layer" if summ.get("ocr_layer") else "")
                                   + "; its visible text cannot be edited", candidates=[c.to_dict() for c in match.candidates])
        code = "ambiguous" if match.status == "ambiguous" else "not_found"
        reason = match.reason
        if code == "not_found" and _has_graphic_text(doc, match.searched_pages):
            reason += ("; parts of the page are drawn as images or vector graphics, so text that is visible on the "
                       "page may not exist as text (graphic text is out of scope)")
            code = "not_found_graphic"
        raise ManualReview(code, reason, candidates=[c.to_dict() for c in match.candidates])
    best = match.best
    tpage = best.page_index
    summ = da.pages.get(tpage) or page_summary(doc, tpage, include_chars=False)
    if summ.get("scanned"):
        raise ManualReview("scanned_page", f"page {tpage + 1} is a scanned image; the text found is an invisible OCR "
                                           "layer and changing it would not change what the page shows")
    if " ".join(best.text.split()) == " ".join(ctx.replace.split()):
        raise ManualReview("no_change", f"page {tpage + 1} already reads {best.text!r}; there is nothing to change",
                           candidates=[best.to_dict()])
    if best.kind == "field":
        return _run_field(ctx, llm, sandbox, doc, best, min_scale)
    target = best.target
    glyphs = [doc.model(tpage).glyphs[g] for g in target.glyphs]
    if any(not g.visible for g in glyphs if not g.text.isspace()):
        raise ManualReview("unsupported", "the target text is invisible (an OCR layer or hidden text)")
    if any(not g.upright for g in glyphs):
        raise ManualReview("unsupported", "the target text is rotated or skewed; only horizontal text is supported")
    if target.font.is_type3:
        raise ManualReview("unsupported", "the target uses a Type3 font, which cannot be edited safely")
    if not target.font.editable:
        raise ManualReview("unsupported", f"the font {target.font.base_font} cannot be written: "
                                          + "; ".join(target.font.problems))
    replacement, notes = adapt_quotes(" ".join(ctx.replace.split()), target.font)
    if notes:
        rep["replacement_adjusted"] = {"requested": " ".join(ctx.replace.split()), "written": replacement, "notes": notes}
        rep["warnings"].extend(notes)
    rep["target"] = target.to_dict()
    rep["target_location"] = {"page": tpage + 1, "bbox": target.bbox.as_list(2),
                              "lines": [p.bbox.as_list(2) for p in target.parts]}

    # ---------------------------------------------------------- step 3
    meas = measure(doc, target, replacement)
    rep["measurements"] = meas.to_dict()
    if meas.missing_chars:
        raise ManualReview("missing_glyphs", "the embedded font " + target.font.base_font + " does not contain: "
                           + ", ".join(repr(c) for c in meas.missing_chars)
                           + ". The font must not be switched, so a person has to make this change.",
                           missing_characters=meas.missing_chars)
    try:
        pdf_ops.methods.check_uniform_style(doc, target)
    except pdf_ops.UnsupportedError as exc:
        raise ManualReview("mixed_formatting", exc.message)

    # ---------------------------------------------------------- step 4
    frames = page_frames(ctx.base_path, int(cfg["checks"]["render_dpi"]))
    frame = frames[tpage]
    target_end = max(p.x1 for p in target.parts)
    advice = None
    if cfg["llm"].get("visual_check", True):
        page_img = render_pages(ctx.base_path, int(cfg["checks"]["render_dpi"]), [tpage])[tpage]
        (ctx.folder / "images").mkdir(exist_ok=True)
        Image.fromarray(page_img).save(ctx.folder / "images" / f"page_{tpage + 1}.png")
        boundary_box = Rect(min(p.x0 for p in target.parts), target.bbox.y0, meas.boundary, target.bbox.y1)
        img = draw_boxes(page_img, frame, [(p.bbox.expand(1), (220, 0, 0)) for p in target.parts]
                         + [(boundary_box.expand(1.5), (0, 90, 255))])
        (ctx.folder / "images").mkdir(exist_ok=True)
        img.save(ctx.folder / "images" / "layout_check_input.png")
        info = {"width": img.width, "height": img.height, "dpi": int(cfg["checks"]["render_dpi"]), "scale": frame.scale,
                "origin": [frame.crop.x0, frame.crop.y1]}
        pre_fits = _fit_all(doc, target, replacement, meas.boundary, meas, min_scale)
        advice = llm.analyse_space({"measurements": meas.to_dict(), "request": _request_data(ctx, best),
                                    "feasibility": {k: v.to_dict() for k, v in pre_fits.items()}}, _png(img), info)
        rep["layout_advice"] = advice
    merged = merge_advice(meas, advice, frame, target_end,
                          _fit_all(doc, target, replacement, meas.boundary, meas, min_scale))
    merged["allowed_methods"] = [m for m in merged["allowed_methods"] if m in enabled]
    rep["merged_advice"] = merged
    fits = _fit_all(doc, target, replacement, merged["boundary"], meas, min_scale)
    rep["feasibility"] = {k: v.to_dict() for k, v in fits.items()}

    # ---------------------------------------------------------- step 5
    feasible_allowed = [m for m in METHODS if m in merged["allowed_methods"] and fits.get(m) and fits[m].feasible]
    if not feasible_allowed:
        raise ManualReview("not_enough_space", _not_enough_space_reason(fits, merged["allowed_methods"], merged))
    plan_ctx = {
        "request": _request_data(ctx, best), "target": target.to_dict(), "measurements": meas.to_dict(),
        "feasibility": {k: v.to_dict() for k, v in fits.items()}, "allowed_methods": merged["allowed_methods"],
        "layout_notes": merged["notes"], "limits": {"boundary": merged["boundary"], "min_font_scale": min_scale},
    }
    plan = llm.plan(plan_ctx)
    rep["plan"] = plan
    order, params, notes = _validate_plan(plan, fits, merged["allowed_methods"], merged, min_scale)
    rep["plan_validation"] = {"order": order, "parameters": params, "notes": notes}
    ctx.audit("plan", chosen=plan.get("chosen_method"), order=order)
    if plan.get("chosen_method") == "manual_review":
        raise ManualReview("planner_manual_review", "the planner advised manual review: " + str(plan.get("reason", "")))
    if not order:
        raise ManualReview("not_enough_space", _not_enough_space_reason(fits, merged["allowed_methods"], merged))
    if params["boundary"] != merged["boundary"]:
        fits["horizontal_expansion"] = pdf_ops.fit_horizontal(doc, target, replacement, params["boundary"])

    # ---------------------------------------------------- steps 6 - 9
    retries = int(cfg["editing"].get("retries_per_method", 2))
    failures = []
    for method in order:
        fit = fits[method]
        if not fit.feasible:
            continue
        mparams = dict(params)
        if method == "reduced_font":
            mparams["scale"] = fit.scale
        if method == "paragraph_reflow":
            mparams["max_bottom"] = fit.details.get("max_bottom")
            mparams["area"] = fit.details.get("area")
        spec = areas.check_spec(method, target, replacement,
                                {"boundary": mparams["boundary"], "area": mparams.get("area")}, min_scale, tpage)
        previous = None
        for attempt in range(retries + 1):
            outcome = _attempt(ctx, llm, sandbox, method, attempt, mparams, spec, best, target, replacement, previous)
            rep["attempts"].append(outcome["record"])
            if outcome["passed"]:
                return _success(ctx, llm, method, fit, mparams, outcome, target, meas, spec)
            previous = outcome["feedback"]
            failures.append(f"{method} attempt {attempt + 1}: {outcome['summary']}")
    raise ManualReview("checks_failed", "every method that could work failed the safety checks or did not run: "
                       + " | ".join(failures[-6:]))


_STRAIGHT = {"'": ("\u2019", "\u2018"), '"': ("\u201d", "\u201c")}
_CURLY = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'}


def adapt_quotes(text: str, font) -> tuple[str, list[str]]:
    """Write quotes in the form the embedded font has.

    Matching already treats straight and curly quotes as equal, so a request
    typed with straight quotes for a document set with curly ones (or the
    reverse) is written in the document's form when the requested form is
    missing from the font.  Every substitution is reported to the reviewer.
    """
    avail = font.available_chars()
    out, notes = [], []
    for i, ch in enumerate(text):
        if ch in _STRAIGHT and ch not in avail:
            close, opening = _STRAIGHT[ch]
            prev = text[i - 1] if i else " "
            cand = opening if (prev.isspace() or prev in "([{") else close
            if cand in avail:
                out.append(cand)
                notes.append(f"replacement quote {ch!r} written as {cand!r}: the embedded font has no {ch!r}")
                continue
        if ch in _CURLY and ch not in avail and _CURLY[ch] in avail:
            out.append(_CURLY[ch])
            notes.append(f"replacement quote {ch!r} written as {_CURLY[ch]!r}: the embedded font has no {ch!r}")
            continue
        out.append(ch)
    return "".join(out), list(dict.fromkeys(notes))


def _request_data(ctx: Context, best) -> dict:
    written = ctx.report.get("replacement_adjusted", {}).get("written")
    return {"page": ctx.page, "found_on_page": best.page_index + 1, "source_text": ctx.find,
            "replacement": written or " ".join(ctx.replace.split()), "pdf_text": best.text}


def _has_graphic_text(doc, pages) -> bool:
    for p in pages:
        m = doc.model(p)
        if m.images or sum(1 for g in m.graphics if g.kind in ("curve", "polygon") and g.fill) >= 2:
            return True
    return False


def _attempt(ctx, llm, sandbox, method, attempt, params, spec, best, target, replacement, previous) -> dict:
    sdir = ctx.folder / "scripts"
    sdir.mkdir(exist_ok=True)
    coder_ctx = {
        "method": method, "page": best.page_index + 1, "target_text": best.text,
        "near": target.bbox.as_list(2) if target is not None else None, "replacement": replacement,
        "params": params, "previous_attempt": previous,
    }
    if method == "form_field_update":
        # the script receives the field, the matched span of its value and the replacement phrase;
        # the checks verify the complete new value independently
        coder_ctx.update({"field_name": best.field_name, "new_value": params["new_value"],
                          "old_value_span": list(best.span), "old_text": best.text,
                          "replacement": params["replacement"]})
    record = {"method": method, "attempt": attempt + 1}
    script = llm.write_script(coder_ctx)
    name = f"{method}_{attempt + 1}.py"
    (sdir / name).write_text(script, encoding="utf-8")
    record["script"] = f"scripts/{name}"
    candidate = ctx.folder / "candidate.pdf"
    if candidate.exists():
        candidate.unlink()
    res = sandbox.run(script, ctx.base_path, candidate, [coder_ctx["replacement"]],
                      log_dir=ctx.folder / "logs" / f"{method}_{attempt + 1}")
    record["sandbox"] = res.to_dict()
    ctx.audit("attempt", method=method, attempt=attempt + 1, sandbox_ok=res.ok, duration_s=round(res.duration, 2))
    if not res.ok:
        return {"passed": False, "record": record, "summary": res.error[:300],
                "feedback": {"script": script, "error": res.error[-3000:], "stderr_tail": res.stderr[-3000:]}}
    img_dir = ctx.folder / "images" / f"{method}_{attempt + 1}"
    report = run_checks(ctx.base_path, candidate, spec, img_dir)
    record["checks"] = report.to_dict()
    ctx.audit("checks", method=method, attempt=attempt + 1, passed=report.passed,
              failed=[r.name for r in report.failures])
    if not report.passed:
        candidate.unlink()
        return {"passed": False, "record": record, "summary": report.summary()[:300],
                "feedback": {"script": script, "check_failures": [r.to_dict() for r in report.failures][:8]}}
    return {"passed": True, "record": record, "report": report, "images": img_dir, "candidate": candidate,
            "result_json": res.result_json}


def _success(ctx, llm, method, fit, params, outcome, target, meas, spec) -> dict:
    rep = ctx.report
    report = outcome["report"]
    img_dir: Path = outcome["images"]
    final_imgs = ctx.folder / "images"
    for name in ("before.png", "after.png", "difference.png", "difference_page.png"):
        if (img_dir / name).exists():
            shutil.copyfile(img_dir / name, final_imgs / name)
    rep.update({
        "status": "updated",
        "method": method,
        "font": {"name": fit.to_dict().get("font", target.font.base_font if target else None),
                 "resource": target.font_res if target else None},
        "font_size_change": {"from": fit.font_size, "to": fit.new_font_size, "scale": round(fit.scale, 4),
                             "changed": abs(fit.font_size - fit.new_font_size) > 1e-6},
        "parameters": params,
        "allowed_area": spec.to_dict()["areas"],
        "checks": report.to_dict(),
        "images": {k: f"images/{k}.png" for k in ("before", "after", "difference", "difference_page")},
        "script_result": outcome.get("result_json"),
        "candidate": "candidate.pdf",
        "candidate_sha256": sha256_file(outcome["candidate"]),
    })
    if target is not None:
        rep["font"]["name"] = target.font.base_font
    if ctx.cfg["llm"].get("visual_review", True):
        try:
            before = (final_imgs / "before.png").read_bytes()
            after = (final_imgs / "after.png").read_bytes()
            review = llm.review_visual({"request": {"replacement": " ".join(ctx.replace.split())}, "method": method},
                                       before, after)
            rep["visual_review"] = review
            for c in review.get("concerns", []):
                rep["warnings"].append(f"visual review ({c.get('severity', 'info')}): {c.get('description', '')}")
            if review.get("looks_correct") is False:
                rep["warnings"].append("visual review: the reviewer model thinks the result may not look right")
        except (LLMError, OSError) as exc:
            rep["warnings"].append(f"visual review not available: {exc}")
    return {"status": "updated", "doc_id": ctx.doc_id, "change_id": ctx.change_id, "method": method,
            "font_size_change": rep["font_size_change"], "warnings": rep["warnings"]}


def _run_field(ctx, llm, sandbox, doc, best, min_scale) -> dict:
    rep = ctx.report
    enabled = list(ctx.cfg["editing"].get("methods") or METHODS)
    if "form_field_update" not in enabled:
        raise ManualReview("not_enough_space", "the target is a form-field value but form_field_update is disabled")
    info = find_field(doc, best.field_name)
    s, e = best.span
    new_value = info.value[:s] + " ".join(ctx.replace.split()) + info.value[e:]
    rect = Rect.from_list(info.widgets[0][2])
    page_index = info.widgets[0][0]
    rep["target"] = {"kind": "field", "field": info.name, "value": info.value, "page": page_index + 1,
                     "rect": rect.as_list(2)}
    rep["target_location"] = {"page": page_index + 1, "bbox": rect.as_list(2), "field": info.name}
    # dry run to measure (on a scratch copy in memory)
    scratch = pdf_ops.open(ctx.base_path)
    try:
        dry = pdf_ops.forms.update_form_field(scratch, info.name, new_value, min_scale=min_scale)
    except pdf_ops.MissingGlyphsError as exc:
        raise ManualReview("missing_glyphs", exc.message + ". The font must not be switched.", missing_characters=exc.missing)
    except pdf_ops.DoesNotFitError as exc:
        raise ManualReview("not_enough_space", "form field: " + exc.message)
    except pdf_ops.PdfOpsError as exc:
        raise ManualReview("unsupported", "form field: " + exc.message)
    rep["feasibility"] = {"form_field_update": {"feasible": True, **dry["widgets"][0]}}
    plan = llm.plan({"request": _request_data(ctx, best), "target": rep["target"],
                     "feasibility": {"form_field_update": {"feasible": True, "reason": "fits", **dry["widgets"][0]}},
                     "allowed_methods": ["form_field_update"], "limits": {"min_font_scale": min_scale}})
    rep["plan"] = plan
    if plan.get("chosen_method") == "manual_review":
        raise ManualReview("planner_manual_review", "the planner advised manual review: " + str(plan.get("reason", "")))
    w = dry["widgets"][0]
    fit = pdf_ops.Fit("form_field_update", True, "fits", scale=w["new_font_size"] / w["font_size"],
                      font_size=w["font_size"], new_font_size=w["new_font_size"])
    params = {"new_value": new_value, "replacement": " ".join(ctx.replace.split()), "min_scale": min_scale,
              "boundary": rect.x1}
    spec = areas.check_spec("form_field_update", None, new_value, {}, min_scale, page_index, field_name=info.name,
                            field_rect=rect, expected_field_value=new_value, original_size=w["font_size"],
                            target_text=info.value)
    retries = int(ctx.cfg["editing"].get("retries_per_method", 2))
    previous = None
    failures = []
    for attempt in range(retries + 1):
        outcome = _attempt(ctx, llm, sandbox, "form_field_update", attempt, params, spec, best, None, new_value, previous)
        rep["attempts"].append(outcome["record"])
        if outcome["passed"]:
            res = _success(ctx, llm, "form_field_update", fit, params, outcome, None, None, spec)
            rep["font"] = {"name": w["font"], "resource": None}
            return res
        previous = outcome["feedback"]
        failures.append(f"attempt {attempt + 1}: {outcome['summary']}")
    raise ManualReview("checks_failed", "the form-field update failed the safety checks: " + " | ".join(failures))
