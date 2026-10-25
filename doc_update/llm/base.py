"""LLM interface, budget and audit helpers shared by all providers."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Budget:
    max_calls: int
    max_cost: float
    prices: dict
    calls: int = 0
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    records: list = field(default_factory=list)

    def check(self, role: str) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"the limit of {self.max_calls} LLM calls per change was reached (before the {role} call)")
        if self.cost >= self.max_cost:
            raise BudgetExceeded(f"the estimated cost limit of ${self.max_cost:.2f} per change was reached (before the {role} call)")

    def price(self, model: str) -> dict:
        for key, value in self.prices.items():
            if key != "default" and key in model:
                return value
        return self.prices.get("default", {"input": 0.0, "output": 0.0})

    def add(self, model: str, tokens_in: int, tokens_out: int) -> float:
        p = self.price(model)
        cost = tokens_in / 1e6 * float(p.get("input", 0)) + tokens_out / 1e6 * float(p.get("output", 0))
        self.calls += 1
        self.cost += cost
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        return cost

    def to_dict(self) -> dict:
        return {"calls": self.calls, "estimated_cost_usd": round(self.cost, 5), "input_tokens": self.tokens_in,
                "output_tokens": self.tokens_out, "max_calls": self.max_calls, "max_cost_usd": self.max_cost}


def prompt_hash(request: dict) -> str:
    """SHA-256 of the request with image bytes replaced by their own hashes (never logged in full)."""

    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        if isinstance(obj, (bytes, bytearray)):
            return "sha256:" + hashlib.sha256(obj).hexdigest()
        return obj

    return hashlib.sha256(json.dumps(scrub(request), sort_keys=True, default=str).encode()).hexdigest()


class LLM(ABC):
    """The four roles the pipeline needs."""

    provider = "abstract"

    def __init__(self, budget: Budget, audit=None):
        self.budget = budget
        self.audit = audit or (lambda **kw: None)

    @abstractmethod
    def analyse_space(self, context: dict, image_png: bytes, image_info: dict) -> dict: ...

    @abstractmethod
    def plan(self, context: dict) -> dict: ...

    @abstractmethod
    def write_script(self, context: dict) -> str: ...

    @abstractmethod
    def review_visual(self, context: dict, before_png: bytes, after_png: bytes) -> dict: ...

    def models(self) -> dict:
        return {}
