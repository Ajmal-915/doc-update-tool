"""JSON schemas for the structured LLM outputs (Bedrock tool use) and a small validator."""

from __future__ import annotations

METHODS = ["original_box", "horizontal_expansion", "reduced_font", "paragraph_reflow", "form_field_update"]
REGION_TYPES = ["paragraph", "table_cell", "heading", "table_of_contents_line", "form_field", "other"]
RISK_TYPES = ["dot_leader", "table_line", "adjacent_text", "margin", "image", "graphic", "other"]

_BOX = {
    "type": "object",
    "properties": {"x0": {"type": "number"}, "y0": {"type": "number"}, "x1": {"type": "number"}, "y1": {"type": "number"}},
    "required": ["x0", "y0", "x1", "y1"],
}

SPACE_ANALYSIS = {
    "name": "report_space_analysis",
    "description": "Report the visual analysis of the space around the target text.",
    "schema": {
        "type": "object",
        "properties": {
            "region_type": {"type": "string", "enum": REGION_TYPES},
            "boundary_px": dict(_BOX, description="Box the text must stay within, in image pixels (origin top-left)."),
            "risks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"type": {"type": "string", "enum": RISK_TYPES}, "description": {"type": "string"}},
                    "required": ["type", "description"],
                },
            },
            "recommended_method": {"type": "string", "enum": METHODS + ["manual_review"]},
            "confidence": {"type": "number"},
            "notes": {"type": "string"},
        },
        "required": ["region_type", "boundary_px", "risks", "recommended_method"],
    },
}

PLAN = {
    "name": "submit_plan",
    "description": "Submit the update plan.",
    "schema": {
        "type": "object",
        "properties": {
            "chosen_method": {"type": "string", "enum": METHODS + ["manual_review"]},
            "fallback_methods": {"type": "array", "items": {"type": "string", "enum": METHODS}},
            "parameters": {
                "type": "object",
                "properties": {
                    "boundary_x": {"type": "number", "description": "right boundary in PDF points (horizontal_expansion)"},
                    "font_scale": {"type": "number", "description": "scale of the original size (reduced_font)"},
                    "use_free_space_below": {"type": "boolean", "description": "paragraph_reflow may use space below"},
                },
            },
            "rejected_methods": {
                "type": "array",
                "items": {"type": "object", "properties": {"method": {"type": "string", "enum": METHODS},
                                                           "reason": {"type": "string"}},
                          "required": ["method", "reason"]},
            },
            "reason": {"type": "string"},
        },
        "required": ["chosen_method", "fallback_methods", "parameters", "reason"],
    },
}

SCRIPT = {
    "name": "submit_script",
    "description": "Submit the complete Python script.",
    "schema": {
        "type": "object",
        "properties": {"script": {"type": "string", "description": "complete Python source"},
                       "notes": {"type": "string"}},
        "required": ["script"],
    },
}

VISUAL_REVIEW = {
    "name": "report_visual_review",
    "description": "Report concerns after comparing the before and after crops.",
    "schema": {
        "type": "object",
        "properties": {
            "looks_correct": {"type": "boolean"},
            "concerns": {
                "type": "array",
                "items": {"type": "object", "properties": {"severity": {"type": "string", "enum": ["info", "warning"]},
                                                           "description": {"type": "string"}},
                          "required": ["severity", "description"]},
            },
        },
        "required": ["looks_correct", "concerns"],
    },
}


def tool_spec(tool: dict) -> dict:
    """Bedrock Converse toolSpec."""
    return {"toolSpec": {"name": tool["name"], "description": tool["description"], "inputSchema": {"json": tool["schema"]}}}


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Validate the subset of JSON Schema used above (type, enum, properties, required, items)."""
    errors: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path}: expected a number"]
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path}: expected an integer"]
    elif t in types and not isinstance(value, types[t]):
        return [f"{path}: expected {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if t == "object":
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing {req!r}")
        for k, sub in schema.get("properties", {}).items():
            if k in value and value[k] is not None:
                errors.extend(validate(value[k], sub, f"{path}.{k}"))
    if t == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors
