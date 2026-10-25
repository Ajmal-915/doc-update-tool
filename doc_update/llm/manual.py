"""Manual provider: a person (or another tool) answers each LLM call.

Every call the pipeline would send to Bedrock is written to
``<manual_dir>/<session>/NN_<role>.request.md`` (system prompt, user text,
tool name) with its images as ``NN_<role>_imageK.png``.  The run then stops
with a manual-review result saying which answer is missing.  The answer is a
JSON file ``NN_<role>.json`` holding exactly the tool input the model would
have produced.  Running the same change again replays the answers so far and
asks for the next one.

The answers go through the same Converse response parsing, schema
validation, budget and audit code as live Bedrock responses.  Useful for
offline evaluation, and for testing prompts before spending money.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import LLMError

ROLE_OF_TOOL = {"report_space_analysis": "vision", "submit_plan": "planner", "submit_script": "coder",
                "report_visual_review": "visual_review"}


class AwaitingAnswer(LLMError):
    pass


class ManualTransport:
    def __init__(self, directory: str | Path, session: str):
        self.dir = Path(directory) / session
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def converse(self, **request) -> dict:
        tool = request["toolConfig"]["tools"][0]["toolSpec"]["name"]
        role = ROLE_OF_TOOL.get(tool, tool)
        stem = f"{self.n:02d}_{role}"
        self.n += 1
        answer = self.dir / f"{stem}.json"
        texts, images = [], []
        for block in request["messages"][0]["content"]:
            if "text" in block:
                texts.append(block["text"])
            elif "image" in block:
                images.append(block["image"]["source"]["bytes"])
        if not answer.exists():
            for old in self.dir.glob(f"{self.n - 1:02d}_*"):
                if old.name.endswith(".json"):
                    raise LLMError(f"answer {old.name} exists but the pipeline now asks for {stem}; delete stale answers")
            body = [f"# {stem}", "", f"tool: {tool}", "", "## system", "", request["system"][0]["text"], "",
                    "## user", "", "\n\n".join(texts)]
            for k, data in enumerate(images):
                img = self.dir / f"{stem}_image{k}.png"
                img.write_bytes(data)
                body.append(f"\n[image {k}: {img.name}]")
            (self.dir / f"{stem}.request.md").write_text("\n".join(body), encoding="utf-8")
            raise AwaitingAnswer(f"waiting for a manual answer: {answer}")
        payload = json.loads(answer.read_text(encoding="utf-8"))
        tin = sum(len(t) for t in texts) // 4 + len(request["system"][0]["text"]) // 4 + 1600 * len(images)
        tout = len(json.dumps(payload)) // 4
        return {"output": {"message": {"role": "assistant",
                                       "content": [{"toolUse": {"toolUseId": f"manual-{stem}", "name": tool, "input": payload}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": tin, "outputTokens": tout, "totalTokens": tin + tout},
                "metrics": {"latencyMs": 0}}
