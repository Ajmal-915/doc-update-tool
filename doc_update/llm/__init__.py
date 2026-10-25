"""LLM providers: Amazon Bedrock (Converse), recorded responses, and an offline stand-in."""

from __future__ import annotations

from pathlib import Path

from .base import LLM, Budget, BudgetExceeded, LLMError


def make_budget(cfg: dict) -> Budget:
    llm = cfg["llm"]
    return Budget(int(llm.get("max_calls_per_change", 12)), float(llm.get("max_cost_per_change_usd", 1.0)),
                  llm.get("prices_per_million_tokens", {"default": {"input": 0, "output": 0}}))


def make_llm(cfg: dict, audit=None, record_name: str | None = None) -> LLM:
    provider = cfg["llm"].get("provider", "bedrock")
    budget = make_budget(cfg)
    if provider == "offline":
        from .offline import OfflineLLM

        return OfflineLLM(budget, audit)
    from .bedrock import BedrockTransport, ConverseLLM, region_from, resolve_models

    if provider == "manual":
        from .manual import ManualTransport

        directory = cfg["llm"].get("manual_dir")
        if not directory:
            raise LLMError("llm.manual_dir is not set")
        session = cfg["llm"].get("manual_session") or record_name or "session"
        models = {r: "manual" for r in ("vision", "planner", "coder")}
        return ConverseLLM(ManualTransport(directory, session), models, cfg, budget, audit)
    if provider == "replay":
        from .replay import ReplayTransport

        path = cfg["llm"].get("replay_file")
        if not path:
            raise LLMError("llm.replay_file is not set")
        transport = ReplayTransport(path)
        models = {r: (cfg["llm"].get(f"{r}_model") if cfg["llm"].get(f"{r}_model") not in (None, "auto")
                      else "replay." + cfg["llm"].get("auto_model_family", "claude")) for r in ("vision", "planner", "coder")}
        return ConverseLLM(transport, models, cfg, budget, audit)
    if provider != "bedrock":
        raise LLMError(f"unknown llm.provider {provider!r}")
    region = region_from(cfg)
    transport = BedrockTransport(region, float(cfg["llm"].get("timeout_seconds", 180)))
    models = resolve_models(cfg, region, Path(cfg["store"]["root"]))
    recorder = None
    if cfg["llm"].get("record_dir") and record_name:
        from .replay import Recorder

        recorder = Recorder(cfg["llm"]["record_dir"], record_name)
    return ConverseLLM(transport, models, cfg, budget, audit, recorder)


__all__ = ["LLM", "Budget", "BudgetExceeded", "LLMError", "make_budget", "make_llm"]
