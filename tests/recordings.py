"""Builds the recorded Bedrock Converse responses in tests/data/recorded/.

The responses have exactly the shape bedrock-runtime returns
(output.message.content[].toolUse, stopReason, usage, metrics), so replaying
them exercises the same parsing, validation, budgeting and audit code as a
live run.  The scripts are written the way a model writes them (not the
offline template), including deliberately bad first attempts.

Run ``python tests/recordings.py`` to regenerate.  Live runs can be recorded
with llm.record_dir in config.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "data" / "recorded"


def tool_response(name: str, payload: dict, tin: int = 2500, tout: int = 400, thinking: bool = False) -> dict:
    content = []
    if thinking:
        content.append({"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}})
    content.append({"text": "Here is my answer."})
    content.append({"toolUse": {"toolUseId": f"tooluse_{name}", "name": name, "input": payload}})
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use",
            "usage": {"inputTokens": tin, "outputTokens": tout, "totalTokens": tin + tout}, "metrics": {"latencyMs": 1800}}


def vision(region="paragraph", x1_px=1160.0, method="original_box", risks=None) -> dict:
    return {"role": "vision", "response": tool_response("report_space_analysis", {
        "region_type": region, "boundary_px": {"x0": 140.0, "y0": 160.0, "x1": x1_px, "y1": 230.0},
        "risks": risks or [], "recommended_method": method, "confidence": 0.8,
        "notes": "Body paragraph, ragged right edge, nothing to the right of the line."}, tin=3900, tout=220)}


def plan(chosen, fallbacks=(), params=None, reason="fits in the original box") -> dict:
    return {"role": "planner", "response": tool_response("submit_plan", {
        "chosen_method": chosen, "fallback_methods": list(fallbacks), "parameters": params or {},
        "rejected_methods": [], "reason": reason}, tin=5200, tout=300, thinking=True)}


def script(src: str) -> dict:
    return {"role": "coder", "response": tool_response("submit_script", {"script": src, "notes": ""}, tin=4800, tout=600)}


def review(ok=True, concerns=()) -> dict:
    return {"role": "visual_review", "response": tool_response("report_visual_review", {
        "looks_correct": ok, "concerns": [{"severity": s, "description": d} for s, d in concerns]}, tin=3000, tout=120)}


GOOD_A = '''import json
import pdf_ops

PAGE = 1
TARGET_TEXT = "financial year ending 31 December 2023"
REPLACEMENT = "financial year ending 31 December 2024"

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, PAGE, TARGET_TEXT, near=[150.0, 690.0, 450.0, 715.0])
ok, missing = pdf_ops.glyphs_available(target.font, REPLACEMENT)
if not ok:
    raise pdf_ops.MissingGlyphsError("missing glyphs", missing)
result = pdf_ops.replace_in_place(doc, target, REPLACEMENT)
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''

WHITE_BOX = '''import json
import pikepdf
import pdf_ops

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, 1, "two working days")
box = target.bbox
page = doc.pdf.pages[0]
cover = "q 1 g %.2f %.2f %.2f %.2f re f Q" % (box.x0, box.y0, box.width, box.height)
page.contents_add(doc.pdf.make_stream(cover.encode("ascii")), prepend=False)
pdf_ops.write_text(doc, target, "three working days", box.x0, box.y0 + 2.4)
pdf_ops.save(doc, "output.pdf")
print(json.dumps({"method": "horizontal_expansion", "note": "covered and redrawn"}))
'''

GOOD_B = '''import json
import pdf_ops

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, 1, "two working days")
result = pdf_ops.expand_horizontal(doc, target, "three working days", boundary={boundary})
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''

CRASH = '''import json
import pdf_ops

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, 1, "financial year ending 31 December 2023")
result = pdf_ops.replace_in_place(doc, targte, "financial year ending 31 December 2024")
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''

EXTRA_TEXT = '''import json
import pdf_ops

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, 1, "financial year ending 31 December 2023")
result = pdf_ops.{call}
footer = pdf_ops.find_target(doc, 1, "Page 1")
pdf_ops.write_text(doc, footer, "financial year ending 31 December 2024", 300, 60)
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''

FIELD = '''import json
import pdf_ops

result = None
doc = pdf_ops.open("input.pdf")
result = pdf_ops.update_form_field(doc, "PolicyName", "Anti-bribery policy 2024", min_scale=0.9)
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result))
'''


def build() -> dict[str, dict]:
    rec = {
        # 1. straightforward in-place change
        "report_in_place": [vision(), plan("original_box", ["horizontal_expansion"]), script(GOOD_A), review()],
        # 2. first script covers the old text with a white box -> rejected -> corrected script passes
        "report_retry_white_box": [
            vision(method="horizontal_expansion"),
            plan("horizontal_expansion", ["paragraph_reflow"], {"boundary_x": 540.0}, "the line has room on the right"),
            script(WHITE_BOX),
            script(GOOD_B.replace("{boundary}", "529.9")),
            review(concerns=[("info", "The following words moved slightly to the right; spacing looks even.")]),
        ],
        # 3. first script crashes (typo) -> corrected script passes
        "report_crash_then_fix": [vision(), plan("original_box"), script(CRASH), script(GOOD_A), review()],
        # 4. planner picks a later method although original_box works -> code enforces the order
        "report_planner_wrong_order": [vision(), plan("reduced_font", ["original_box"], {"font_scale": 0.95},
                                                      "shrinking is safest"), script(GOOD_A), review()],
        # 5. every attempt changes text outside the area -> manual review after all retries
        "report_all_attempts_fail": [vision(method="horizontal_expansion"), plan("original_box", ["horizontal_expansion"])]
        + [script(EXTRA_TEXT.replace("{call}", 'replace_in_place(doc, target, "financial year ending 31 December 2024")'))] * 3
        + [script(EXTRA_TEXT.replace("{call}", 'expand_horizontal(doc, target, "financial year ending 31 December 2024", boundary=529.9)'))] * 3,
        # 6. form field
        "form_field": [plan("form_field_update", [], {}, "the target is a form-field value"), script(FIELD), review()],
        # 7. layout check warns about a table cell and recommends shrinking; expansion must then be disabled
        "report_vision_cautious": [vision(region="table_cell", method="reduced_font",
                                          risks=[{"type": "table_line", "description": "cell border right of the text"}]),
                                   plan("reduced_font", [], {"font_scale": 0.92}),
                                   script('''import json
import pdf_ops

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, 1, "Annually")
result = pdf_ops.replace_with_scale(doc, target, "Annually,", min_scale=0.9)
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''), review()],
    }
    return {k: {"note": "hand-built Converse responses for offline end-to-end tests", "responses": v} for k, v in rec.items()}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"wrote {len(build())} recordings to {OUT}")


if __name__ == "__main__":
    main()
