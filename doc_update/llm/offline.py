"""Offline provider: plain-code stand-ins for the LLM roles.

Used when no Bedrock key is available (tests, offline scenario runs).  It
reproduces what a careful model would do from the measurements alone: the
layout advice repeats the measured boundary, the plan picks the first
feasible method, and the script comes from a fixed template.  The edit plan
for a recommendation understands only plain instructions ("change X to Y",
"replace 'X' with 'Y'", several of them separated by ';', "throughout"); a
real model is needed for free wording.
"""

from __future__ import annotations

import re

from .apidoc import render_template
from .base import LLM, LLMError
from .schemas import METHODS


class OfflineLLM(LLM):
    provider = "offline"

    def models(self) -> dict:
        return {"vision": "offline", "planner": "offline", "coder": "offline"}

    def _count(self, role: str) -> None:
        self.budget.check(role)
        self.budget.add("offline", 0, 0)
        self.audit(event="llm_call", role=role, model="offline", prompt_sha256="-", input_tokens=0, output_tokens=0,
                   latency_ms=0, estimated_cost_usd=0.0, stop_reason="offline")

    def analyse_space(self, context: dict, image_png: bytes, image_info: dict) -> dict:
        self._count("vision")
        m = context["measurements"]
        scale = image_info["scale"]
        x0, y_top = image_info["origin"]
        lines = m.get("lines") or []
        tx0 = min(ln["target_x0"] for ln in lines) if lines else m["target"]["bbox"][0]
        tb = m["target"]["bbox"]
        box = {"x0": (tx0 - x0) * scale, "x1": (m["horizontal_boundary"] - x0) * scale,
               "y0": (y_top - tb[3]) * scale, "y1": (y_top - tb[1]) * scale}
        feasible = [k for k in METHODS if context.get("feasibility", {}).get(k, {}).get("feasible")]
        return {
            "region_type": m["region_type"], "boundary_px": box,
            "risks": [{"type": r["type"], "description": r["description"]} for r in m.get("risks", [])],
            "recommended_method": feasible[0] if feasible else "manual_review",
            "confidence": 0.5, "notes": "offline provider: derived from the measurements, no image analysis",
        }

    def plan(self, context: dict) -> dict:
        self._count("planner")
        feas = context["feasibility"]
        allowed = context["allowed_methods"]
        usable = [k for k in METHODS if k in allowed and feas.get(k, {}).get("feasible")]
        rejected = [{"method": k, "reason": feas.get(k, {}).get("reason", "not allowed by the layout check")}
                    for k in METHODS if k not in usable and k in feas]
        if not usable:
            return {"chosen_method": "manual_review", "fallback_methods": [], "parameters": {}, "rejected_methods": rejected,
                    "reason": "no method can work: " + "; ".join(f"{r['method']}: {r['reason']}" for r in rejected)}
        params = {}
        if "horizontal_expansion" in usable:
            params["boundary_x"] = context["limits"]["boundary"]
        if "reduced_font" in usable:
            params["font_scale"] = feas["reduced_font"].get("scale")
        return {"chosen_method": usable[0], "fallback_methods": usable[1:], "parameters": params,
                "rejected_methods": rejected, "reason": f"{usable[0]} is the first method that fits"}

    def write_script(self, context: dict) -> str:
        self._count("coder")
        method = context["method"]
        if method not in METHODS:
            raise LLMError(f"unknown method {method}")
        return render_template(method, context)

    def review_visual(self, context: dict, before_png: bytes, after_png: bytes) -> dict:
        self._count("visual_review")
        return {"looks_correct": True,
                "concerns": [{"severity": "info", "description": "offline provider: no visual review was performed"}]}


    def plan_edits(self, context: dict, image_png: bytes, image_info: dict) -> dict:
        self._count("editor")
        rec = " ".join(str(context["recommendation"]).split())
        everywhere = bool(re.search(r"\b(throughout|everywhere|all occurrences|every occurrence|each occurrence|"
                                    r"wherever)\b", rec, re.I))
        pairs = _instruction_pairs(rec)
        plan = {"understanding": f"offline provider: {len(pairs)} plain instruction(s) recognised",
                "edits": [], "not_possible": []}
        if not pairs:
            plan["needs_clarification"] = ("offline provider: no plain instruction such as \"change X to Y\" was found; "
                                           "a language model is needed for this wording")
            return plan
        sources = [(ln["id"], ln["text"]) for ln in context["lines"]] + \
                  [(f["id"], f["value"]) for f in context["form_fields"] if f.get("value")]
        for old, new in pairs:
            hits = _occurrences(old, sources)
            if not hits:
                plan["not_possible"].append({"description": f"change {old!r} to {new!r}",
                                             "reason": "this text is not on the page"})
                continue
            if len(hits) > 1 and not everywhere:
                plan["edits"] = []
                plan["needs_clarification"] = (f"{old!r} appears {len(hits)} times on the page "
                                               f"({', '.join(h[0] for h in hits)}); say which one, or 'throughout'")
                return plan
            for lid, found in hits:
                plan["edits"].append({"line_ids": [lid], "current_text": found, "new_text": _same_case(found, new),
                                      "reason": f"change {old!r} to {new!r}"})
        return plan


# a quoted phrase: "..." or \u201c...\u201d, or '...' / \u2018...\u2019 whose quotes are not apostrophes inside a word
_Q = "(?:\"([^\"]+)\"|\u201c([^\u201d]+)\u201d|(?<!\\w)['\u2018](.+?)['\u2019](?!\\w))"
_PAIR = re.compile(_Q + r"\s*(?:->|\u2192|\bto\b|\bwith\b|\bby\b|\binto\b)\s*" + _Q, re.I)
_VERB = re.compile(r"^(?:please\s+)?(?:change|replace|update|amend|correct|rename|reword)\s+(?:the\s+)?"
                   r"(?:(?:text|word|words|phrase|wording|heading|label|title|line)\s+)?(.+?)\s+"
                   r"(?:to|with|by|into)\s+(.+?)$", re.I)
_TAIL = re.compile(r"\s*,?\s*\b(throughout|everywhere|on this page|wherever it appears|in all occurrences)\b.*$", re.I)


def _instruction_pairs(rec: str) -> list[tuple[str, str]]:
    out = []
    for clause in re.split(r"[;\n]|(?<=[.!?])\s+(?=[A-Z])", rec):
        quoted = [("".join(g[:3]).strip(), "".join(g[3:]).strip()) for g in _PAIR.findall(clause)]
        if quoted:
            out += quoted
            continue
        m = _VERB.match(_TAIL.sub("", clause.strip().rstrip(".!?")))
        if m:
            out.append((m.group(1).strip(" ,"), m.group(2).strip(" ,")))
    return out


def _occurrences(old: str, sources: list[tuple[str, str]]) -> list[tuple[str, str]]:
    want = " ".join(old.split())
    exact = [(lid, want) for lid, text in sources for _ in re.finditer(re.escape(want), text)]
    if exact:
        return exact
    return [(lid, m.group(0)) for lid, text in sources for m in re.finditer(re.escape(want), text, re.I)]


def _same_case(found: str, new: str) -> str:
    return new.upper() if found.isupper() and not new.isupper() else new
