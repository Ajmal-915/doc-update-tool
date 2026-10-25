"""Recorded Bedrock responses, so the whole pipeline can run offline.

A recording is a JSON file: {"responses": [{"role": ..., "response": <Converse response>}, ...]}.
The replay transport hands them out in order, checking that each call is for
the role the recording expects.  Image bytes are not stored.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import LLMError


class ReplayTransport:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.responses = list(data["responses"])
        self.position = 0
        self.requests: list[dict] = []

    def converse(self, **request) -> dict:
        role = _role_of(request)
        if self.position >= len(self.responses):
            raise LLMError(f"recording {self.path.name} has no response left for a {role} call")
        item = self.responses[self.position]
        self.position += 1
        if item.get("role") and role and item["role"] != role:
            raise LLMError(f"recording {self.path.name} expected a {item['role']} call, got {role}")
        self.requests.append(request)
        return item["response"]


def _role_of(request: dict) -> str | None:
    tools = request.get("toolConfig", {}).get("tools", [])
    name = tools[0]["toolSpec"]["name"] if tools else ""
    return {"report_space_analysis": "vision", "submit_plan": "planner", "submit_script": "coder",
            "report_visual_review": "visual_review"}.get(name)


class Recorder:
    """Saves every live response so a run can be replayed offline later."""

    def __init__(self, directory: str | Path, name: str):
        self.path = Path(directory) / f"{name}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items: list[dict] = []

    def __call__(self, role: str, model: str, phash: str, response: dict) -> None:
        clean = {k: v for k, v in response.items() if k in ("output", "stopReason", "usage", "metrics")}
        self.items.append({"role": role, "model": model, "prompt_sha256": phash, "response": clean})
        self.path.write_text(json.dumps({"responses": self.items}, indent=1, default=str), encoding="utf-8")

