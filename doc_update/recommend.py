"""A recommendation for one page -> exact edits -> one candidate (steps R1-R4).

R1  The page is rendered and its text listed: lines (L1, L2, ... in reading order) and form fields (F1, ...).
R2  The editor model gets the screenshot, the listing and the recommendation and returns an edit plan:
    the exact current text of every change, its new text, and what it cannot do.
R3  Code verifies the plan.  Every quoted text must be on the page, word for word (spacing, punctuation and
    capitals aside); text that appears more than once must be singled out by the cited line ids.  Each edit is
    trimmed to the words that change (plus context until it is unique on the page), and edits may not overlap.
R4  The edits run one after another through the normal pipeline (steps 1-10), each on the previous edit's
    output.  All of them become one candidate and one approval; if any cannot be made safely, none is applied.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pdf_ops
from pdf_ops.forms import list_fields
from pdf_ops.target import BLOCK_SEPARATOR, page_sequence

from .analysis import analyse
from .llm import LLM, BudgetExceeded, LLMError, make_llm
from .matching import exact_wording, find_target_text
from .pipeline import Context, _pick_by_position, _png, _write_report, execute
from .render import diff_image, page_frames, render_pages
from .sandbox import Sandbox, make_sandbox
from .store import Store, now, sha256_file

CONTEXT_WORDS = 6  # most words of context added on each side to make an edit unique on the page


class RecommendationRefused(Exception):
    def __init__(self, code: str, reason: str, **details):
        super().__init__(reason)
        self.code, self.reason, self.details = code, reason, details


@dataclass
class PlannedEdit:
    number: int
    find: str  # what the pipeline matches: the changed words, with context until unique on the page
    replace: str
    current_text: str  # as the editor quoted it
    new_text: str
    line_ids: list
    reason: str
    location: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    near: list | None = None  # verified position, when the text alone is not unique on the page


# ------------------------------------------------------------------ R1
def page_listing(doc, page_index: int, frame) -> dict:
    """The page's lines in reading order and its text fields, with ids and boxes in image pixels."""
    layout = doc.layout(page_index)
    order: list[int] = []
    for item in page_sequence(layout):
        if item.line is not None and item.line not in order:
            order.append(item.line)
    order += [ln.index for ln in layout.lines if ln.index not in order]
    lines, id_of = [], {}
    for k, li in enumerate(order, 1):
        ln = layout.lines[li]
        if not ln.text.strip():
            continue
        id_of[li] = f"L{k}"
        lines.append({"id": f"L{k}", "text": ln.text, "box_px": list(frame.rect_to_px(ln.bbox))})
    fields, empty = [], 0
    for f in list_fields(doc):
        if f.field_type.lstrip("/") == "Tx" and any(w[0] == page_index for w in f.widgets):
            if f.value.strip():
                fields.append({"id": f"F{len(fields) + 1}", "name": f.name, "value": f.value})
            else:
                empty += 1  # nothing to replace; filling empty fields is not a text replacement
    rotated = sum(1 for g in doc.model(page_index).glyphs if g.visible and not g.upright and not g.text.isspace())
    return {"lines": lines, "form_fields": fields, "empty_form_fields": empty, "rotated_characters": rotated,
            "line_id_of": id_of}


# ------------------------------------------------------------------ R3
def verify_plan(doc, page_index: int, plan: dict, listing: dict, cfg: dict) -> tuple[list[PlannedEdit], list[str]]:
    """Check the editor's plan against the page; returns (edits ready for the pipeline, problems)."""
    min_score, margin = float(cfg["matching"]["min_score"]), float(cfg["matching"]["ambiguity_margin"])
    problems: list[str] = []
    edits: list[PlannedEdit] = []
    places: list[tuple[int, object]] = []
    field_of_id = {f["id"]: f["name"] for f in listing["form_fields"]}
    line_of_id = {v: k for k, v in listing["line_id_of"].items()}
    raw = plan.get("edits") or []
    if len(raw) > int(cfg.get("recommend", {}).get("max_edits", 10)):
        return [], [f"the plan has {len(raw)} edits; at most {cfg['recommend']['max_edits']} are allowed per page"]
    for n, e in enumerate(raw, 1):
        cur, new = " ".join(str(e.get("current_text", "")).split()), " ".join(str(e.get("new_text", "")).split())
        cited = [str(x) for x in e.get("line_ids") or []]
        if not cur:
            problems.append(f"edit {n}: no current text")
            continue
        if not new:
            problems.append(f"edit {n}: the new text is empty; deleting text is not supported")
            continue
        if cur == new:
            problems.append(f"edit {n}: the new text is the same as the current text")
            continue
        occ, why = _locate(doc, page_index, cur, cited, line_of_id, field_of_id, min_score, margin)
        if occ is None:
            problems.append(f"edit {n}: {why}")
            continue
        key = _place(occ)
        clash = next((m for m, k in places if _overlaps(k, key)), None)
        if clash is not None:
            problems.append(f"edits {clash} and {n} change overlapping text ({cur!r})")
            continue
        found = _minimal(doc, page_index, cur, new, occ, min_score, margin)
        if found is None:
            problems.append(f"edit {n}: {cur!r} could not be narrowed to text that is unique on the page")
            continue
        find, replace, notes, near = found
        if cited and occ.kind == "text":
            mine = {listing["line_id_of"].get(p.line) for p in occ.target.parts}
            if not mine & set(cited):
                notes.append(f"the plan cited {', '.join(cited)}; the text is on {', '.join(sorted(m for m in mine if m))}")
        places.append((n, key))
        edits.append(PlannedEdit(n, find, replace, cur, new, cited, str(e.get("reason", "")), _where(occ, listing), notes,
                                 near))
    return edits, problems


def _locate(doc, page_index, cur, cited, line_of_id, field_of_id, min_score, margin):
    """The one place on the page where ``cur`` is, or (None, why)."""
    m = find_target_text(doc, cur, page_index + 1, min_score, margin)
    cands = [c for c in ([m.best] if m.status == "found" else m.candidates)
             if c is not None and c.page_index == page_index and exact_wording(cur, c.text)]
    if m.status == "found" and cands:
        return cands[0], ""
    if len(cands) > 1:
        lines = {line_of_id[c] for c in cited if c in line_of_id}
        fields = {field_of_id[c] for c in cited if c in field_of_id}
        picked = [c for c in cands if (c.kind == "text" and {p.line for p in c.target.parts} & lines)
                  or (c.kind == "field" and c.field_name in fields)]
        if len(picked) == 1:
            return picked[0], ""
        return None, (f"{cur!r} appears {len(cands)} times on the page and the cited ids "
                      f"({', '.join(cited) or 'none'}) do not single one out; quote more of the surrounding text")
    if m.status == "found" and m.best is not None and m.best.page_index != page_index:
        return None, f"{cur!r} is not on page {page_index + 1} (it is on page {m.best.page_index + 1})"
    closest = f"; the closest text reads {m.candidates[0].text[:60]!r}" if m.candidates else ""
    return None, f"{cur!r} is not on page {page_index + 1} word for word{closest}"


def _place(c) -> tuple:
    if c.kind == "field":
        return ("field", c.field_name, c.span[0], c.span[1])
    return ("text", frozenset(c.target.glyphs))


def _overlaps(a: tuple, b: tuple) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "text":
        return bool(a[1] & b[1])
    return a[1] == b[1] and a[2] < b[3] and b[2] < a[3]


def _where(c, listing) -> dict:
    if c.kind == "field":
        return {"kind": "field", "field": c.field_name, "page": c.page_index + 1}
    return {"kind": "text", "page": c.page_index + 1, "bbox": c.bbox.as_list(1) if c.bbox else None,
            "lines": sorted({listing["line_id_of"].get(p.line) for p in c.target.parts} - {None})}


def _minimal(doc, page_index, cur, new, occ, min_score, margin):
    """The changed words of an edit, with as little context as makes them unique at ``occ``."""
    old_w, new_w = cur.split(), new.split()
    p = 0
    while p < min(len(old_w), len(new_w)) and old_w[p] == new_w[p]:
        p += 1
    s = 0
    while s < min(len(old_w), len(new_w)) - p and old_w[-1 - s] == new_w[-1 - s]:
        s += 1
    left, right = _outside_words(doc, page_index, occ)
    options = []
    for total in range(0, p + s + 2 * CONTEXT_WORDS + 1):
        for a in range(0, total + 1):
            b = total - a
            options.append((a, b))
    seen = set()
    by_position = None
    for a, b in options:  # a words of context on the left, b on the right (inside the quote first, then outside)
        in_a, in_b = min(a, p), min(b, s)
        out_a, out_b = a - in_a, b - in_b
        if (out_a and in_a < p) or (out_b and in_b < s) or out_a > len(left) or out_b > len(right):
            continue
        lw = left[len(left) - out_a:] if out_a else []
        rw = right[:out_b]
        f = lw + old_w[p - in_a:len(old_w) - s + in_b] + rw
        r = lw + new_w[p - in_a:len(new_w) - s + in_b] + rw
        if not f or not r or (tuple(f), tuple(r)) in seen:
            continue
        seen.add((tuple(f), tuple(r)))
        find, replace = " ".join(f), " ".join(r)
        m = find_target_text(doc, find, page_index + 1, min_score, margin)
        if m.status == "ambiguous" and by_position is None and occ.kind == "text":
            # the same words more than once (identical lines): the verified position will pick this one
            near = occ.bbox.as_list(3)
            picked = _pick_by_position(m, near, page_index)
            if picked.status == "found" and exact_wording(find, picked.best.text) and \
                    _overlaps(_place(picked.best), _place(occ)):
                by_position = (find, replace, [f"{find!r} appears {len(m.candidates)} times on the page; the edit is "
                                               "located by its verified position"], near)
            continue
        if m.status != "found" or m.best.page_index != page_index or not exact_wording(find, m.best.text):
            continue
        if not _overlaps(_place(m.best), _place(occ)):
            continue
        notes = []
        if (find, replace) != (cur, new):
            notes.append(f"narrowed to {find!r} -> {replace!r}")
        return find, replace, notes, None
    return by_position


def _outside_words(doc, page_index, occ) -> tuple[list[str], list[str]]:
    """Words just before and after a text occurrence, within its block (fields have none)."""
    if occ.kind != "text" or not occ.span:
        return [], []
    seq = page_sequence(doc.layout(page_index))
    s, e = occ.span
    before = "".join(it.text for it in seq[:s]).split(BLOCK_SEPARATOR)[-1]
    after = "".join(it.text for it in seq[e + 1:]).split(BLOCK_SEPARATOR)[0]
    # a word cut by the occurrence's edge is not usable context
    lw = before.split() if before[-1:].isspace() or not before else before.split()[:-1]
    rw = after.split() if after[:1].isspace() or not after else after.split()[1:]
    return lw[-CONTEXT_WORDS:], rw[:CONTEXT_WORDS]


# ------------------------------------------------------------------ R2 + R4
def run_recommendation(store: Store, cfg: dict, doc_id: str, page: int, recommendation: str,
                       llm: LLM | None = None, sandbox: Sandbox | None = None, plan_only: bool = False) -> dict:
    recommendation = " ".join(str(recommendation or "").split())
    if not recommendation:
        raise ValueError("the recommendation must not be empty")
    page = int(page)
    store.manifest(doc_id)
    store.verify_integrity(doc_id)
    base_path, base_label, base_sha = store.current(doc_id)
    change_id, folder = store.new_change(doc_id)
    ctx = Context(store, cfg, doc_id, change_id, folder, base_path, base_label, base_sha, "", "", page)
    rep = ctx.report = {
        "doc_id": doc_id, "change_id": change_id, "kind": "recommendation", "created": now(),
        "base_version": base_label, "base_sha256": base_sha,
        "request": {"recommendation": recommendation, "page": page, "plan_only": plan_only},
        "edits": [], "warnings": [],
    }
    ctx.audit("recommendation_started", base_version=base_label, page=page, plan_only=plan_only)
    result: dict
    try:
        if llm is None:
            llm = make_llm(cfg, audit=lambda **kw: ctx.audit(kw.pop("event", "llm_call"), **kw), record_name=change_id)
        else:
            llm.audit = lambda **kw: ctx.audit(kw.pop("event", "llm_call"), **kw)
        rep["llm"] = {"provider": llm.provider, "models": llm.models()}
        edits = _plan(ctx, llm)
        if plan_only:
            result = {"status": "planned", "doc_id": doc_id, "change_id": change_id,
                      "edits": [_edit_summary(e) for e in edits], "plan": rep["edit_plan"]}
            rep["status"] = "planned"
        else:
            result = _apply(ctx, llm, sandbox or make_sandbox(cfg), edits)
    except RecommendationRefused as exc:
        result = _refused(ctx, exc.code, exc.reason, **exc.details)
    except BudgetExceeded as exc:
        result = _refused(ctx, "llm_budget", f"LLM budget exceeded: {exc}")
    except LLMError as exc:
        result = _refused(ctx, "llm_error", f"the language model could not be used: {exc}")
    except Exception as exc:  # never leave a half-finished change behind
        import traceback

        rep["error_trace"] = traceback.format_exc()[-4000:]
        result = _refused(ctx, "internal_error", f"internal error: {type(exc).__name__}: {exc}")
    finally:
        if llm is not None:
            rep.setdefault("llm", {})["budget"] = llm.budget.to_dict()
    _write_report(ctx)
    if result["status"] == "updated":
        result["report_path"] = str(folder / "report.json")
        result["candidate_path"] = str(folder / "candidate.pdf")
    else:
        dest = store.finish_manual_review(doc_id, change_id)
        result["report_path"] = str(dest / "report.json")
    store.record_change(doc_id, change_id, {
        "status": result["status"], "created": rep["created"], "kind": "recommendation", "page": page,
        "recommendation": recommendation, "find": None, "replace": None,
        "edits": [{"find": e["find"], "replace": e["replace"], "method": e.get("method")} for e in rep["edits"]],
        "base_version": base_label, "base_sha256": base_sha, "method": rep.get("method"),
        "reason_code": rep.get("reason_code"), "candidate_sha256": rep.get("candidate_sha256"),
    })
    ctx.audit("recommendation_finished", status=result["status"], edits=len(rep["edits"]),
              reason_code=rep.get("reason_code"))
    return result


def _refused(ctx: Context, code: str, reason: str, **details) -> dict:
    ctx.report.update({"status": "manual_review", "reason_code": code, "reason": reason, **details})
    for f in ctx.folder.glob("candidate*.pdf"):
        f.unlink()
    return {"status": "manual_review", "doc_id": ctx.doc_id, "change_id": ctx.change_id, "reason_code": code,
            "reason": reason, **{k: v for k, v in details.items() if k in ("edits", "not_possible", "problems")}}


def _plan(ctx: Context, llm: LLM) -> list[PlannedEdit]:
    cfg, rep, page_index = ctx.cfg, ctx.report, ctx.page - 1
    count = pdf_ops.open(ctx.base_path).page_count
    if not 0 <= page_index < count:
        raise RecommendationRefused("not_found", f"page {ctx.page} does not exist; the document has {count} pages")
    da, doc = analyse(ctx.base_path, pages=[page_index], include_chars=False)
    rep["document"] = {"pages": da.page_count, "signed": da.signed, "encrypted": da.encrypted}
    if da.encrypted:
        raise RecommendationRefused("encrypted", "the PDF is encrypted; it cannot be edited safely")
    if da.signed:
        raise RecommendationRefused("signed_pdf", "the PDF is digitally signed; any edit would invalidate the signature")
    summ = da.pages.get(page_index) or {}
    if summ.get("scanned"):
        raise RecommendationRefused("scanned_page", f"page {ctx.page} is a scanned image; its text cannot be edited")

    # R1: screenshot and text listing
    dpi = int(cfg["checks"]["render_dpi"])
    frame = page_frames(ctx.base_path, dpi)[page_index]
    img = render_pages(ctx.base_path, dpi, [page_index])[page_index]
    (ctx.folder / "images").mkdir(exist_ok=True)
    from PIL import Image

    pil = Image.fromarray(img)
    pil.save(ctx.folder / "images" / f"page_{ctx.page}.png")
    listing = page_listing(doc, page_index, frame)
    with open(ctx.folder / "page_text.json", "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in listing.items() if k != "line_id_of"}, fh, indent=1, ensure_ascii=False)
    if not listing["lines"] and not listing["form_fields"]:
        raise RecommendationRefused("no_text", f"page {ctx.page} has no text that could be edited"
                                    + (" (its text is rotated; only horizontal text can be edited)"
                                       if listing["rotated_characters"] else ""))

    # R2: the edit plan
    png = _png(pil)
    info = {"width": pil.width, "height": pil.height, "dpi": dpi}
    plan = llm.plan_edits({"recommendation": rep["request"]["recommendation"], "page": ctx.page,
                           "page_count": da.page_count, "lines": listing["lines"],
                           "form_fields": listing["form_fields"], "empty_form_fields": listing["empty_form_fields"],
                           "rotated_characters": listing["rotated_characters"]},
                          png, info)
    rep["edit_plan"] = plan
    ctx.audit("edit_plan", edits=len(plan.get("edits") or []), not_possible=len(plan.get("not_possible") or []),
              clarification=bool(plan.get("needs_clarification")))
    if plan.get("needs_clarification"):
        raise RecommendationRefused("needs_clarification", "the recommendation is not clear enough to apply: "
                                    + str(plan["needs_clarification"]), not_possible=plan.get("not_possible") or [])
    if plan.get("not_possible") and not plan.get("edits"):
        raise RecommendationRefused(
            "not_possible", "the recommendation cannot be done by replacing text on this page: "
            + "; ".join(f"{x.get('description')}: {x.get('reason')}" for x in plan["not_possible"])
            + ". A person should make this change.", not_possible=plan["not_possible"])
    if plan.get("not_possible"):
        raise RecommendationRefused(
            "partly_not_possible", "part of the recommendation cannot be done by replacing text on this page: "
            + "; ".join(f"{x.get('description')}: {x.get('reason')}" for x in plan["not_possible"])
            + ". Nothing was changed; a person should make this change (or split the recommendation).",
            not_possible=plan["not_possible"])
    if not plan.get("edits"):
        raise RecommendationRefused("no_change", "the edit plan contains no edits for this page")

    # R3: verify
    edits, problems = verify_plan(doc, page_index, plan, listing, cfg)
    rep["plan_verification"] = {"edits": [asdict(e) for e in edits], "problems": problems}
    if problems:
        raise RecommendationRefused("plan_rejected", "the edit plan does not match the page: " + "; ".join(problems)
                                    + ". Nothing was changed.", problems=problems)
    return edits


def _edit_summary(e: PlannedEdit) -> dict:
    return {"number": e.number, "find": e.find, "replace": e.replace, "current_text": e.current_text,
            "new_text": e.new_text, "location": e.location, "notes": e.notes, "located_by_position": e.near is not None}


def _apply(ctx: Context, llm: LLM, sandbox: Sandbox, edits: list[PlannedEdit]) -> dict:
    """R4: every edit through steps 1-10, each on the previous one's output; all or nothing."""
    cfg, rep = ctx.cfg, ctx.report
    # the per-change LLM limits apply to each edit
    per_calls, per_cost = int(cfg["llm"].get("max_calls_per_change", 12)), float(cfg["llm"].get("max_cost_per_change_usd", 1.0))
    llm.budget.max_calls = llm.budget.calls + per_calls * len(edits)
    llm.budget.max_cost = llm.budget.cost + per_cost * len(edits)
    current, label = ctx.base_path, ctx.base_label
    for e in edits:
        sub = ctx.folder / "edits" / f"{e.number:02d}"
        sub.mkdir(parents=True)
        sctx = Context(ctx.store, cfg, ctx.doc_id, ctx.change_id, sub, current, label, sha256_file(current),
                       e.find, e.replace, ctx.page, near=e.near)
        sctx.report = {"doc_id": ctx.doc_id, "change_id": ctx.change_id, "edit": e.number, "created": now(),
                       "base_version": label, "base_sha256": sctx.base_sha,
                       "request": {"find": e.find, "replace": e.replace, "page": ctx.page}, "attempts": [],
                       "warnings": []}
        ctx.audit("edit_started", edit=e.number, base=label)
        res = execute(sctx, llm, sandbox)
        _write_report(sctx)
        summary = {**_edit_summary(e), "status": res["status"], "method": sctx.report.get("method"),
                   "font_size_change": sctx.report.get("font_size_change"), "warnings": sctx.report.get("warnings", []),
                   "report": f"edits/{e.number:02d}/report.json"}
        if res["status"] != "updated":
            summary.update(reason_code=res.get("reason_code"), reason=res.get("reason"))
            rep["edits"].append(summary)
            raise RecommendationRefused(
                res.get("reason_code") or "checks_failed",
                f"edit {e.number} of {len(edits)} ({e.find!r} -> {e.replace!r}) cannot be made safely: "
                f"{res.get('reason')}. Nothing was changed; a person should make this change.",
                edits=rep["edits"])
        rep["edits"].append(summary)
        rep["warnings"] += [f"edit {e.number}: {w}" for w in sctx.report.get("warnings", [])]
        current, label = sub / "candidate.pdf", f"{label}+edit{e.number}"
    final = ctx.folder / "candidate.pdf"
    shutil.copyfile(current, final)
    _final_images(ctx, final)
    methods = {e["method"] for e in rep["edits"]}
    rep.update({"status": "updated", "method": methods.pop() if len(methods) == 1 else "multiple",
                "candidate": "candidate.pdf", "candidate_sha256": sha256_file(final)})
    return {"status": "updated", "doc_id": ctx.doc_id, "change_id": ctx.change_id,
            "edits": [{k: e[k] for k in ("number", "find", "replace", "method", "warnings")} for e in rep["edits"]],
            "warnings": rep["warnings"]}


def _final_images(ctx: Context, final: Path) -> None:
    """Before / after / difference of the whole page for the reviewer."""
    dpi = int(ctx.cfg["checks"]["render_dpi"])
    p = ctx.page - 1
    before = render_pages(ctx.base_path, dpi, [p])[p]
    after = render_pages(final, dpi, [p])[p]
    from PIL import Image

    out = ctx.folder / "images"
    out.mkdir(exist_ok=True)
    Image.fromarray(before).save(out / "before.png")
    Image.fromarray(after).save(out / "after.png")
    diff_image(before, after).save(out / "difference.png")
    ctx.report["images"] = {k: f"images/{k}.png" for k in ("before", "after", "difference")}
