"""Offline provider: plain-code stand-ins for the LLM roles.

Used when no Bedrock key is available (tests, offline scenario runs).  It
reproduces what a careful model would do from the measurements alone: the
layout advice repeats the measured boundary, the plan picks the first
feasible method, and the script comes from a fixed template.
"""

from __future__ import annotations

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
