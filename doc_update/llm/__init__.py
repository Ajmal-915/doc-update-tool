"""LLM providers: Amazon Bedrock (Converse), recorded responses, and an offline stand-in."""

from __future__ import annotations

from pathlib import Path

from .base import LLM, Budget, BudgetExceeded, LLMError


def make_budget(cfg: dict) -> Budget:
    llm = cfg["llm"]
    return Budget(int(llm.get("max_calls_per_change", 12)), float(llm.get("max_cost_per_change_usd", 1.0)),
                  llm.get("prices_per_million_tokens", {"default": {"input": 0, "output": 0}}))


class SplitLLM(LLM):
    """The editor role (recommendation -> edit plan) from one provider, the other roles from another."""

    def __init__(self, editor: LLM, rest: LLM):
        super().__init__(rest.budget, rest.audit)
        self.editor, self.rest = editor, rest
        self.provider = f"{rest.provider}+editor:{editor.provider}"

    @property
    def audit(self):
        return self._audit

    @audit.setter
    def audit(self, fn):
        self._audit = fn
        for part in ("editor", "rest"):
            if hasattr(self, part):
                getattr(self, part).audit = fn

    def models(self) -> dict:
        return {**self.rest.models(), "editor": self.editor.models().get("vision", self.editor.provider)}

    def analyse_space(self, context, image_png, image_info):
        return self.rest.analyse_space(context, image_png, image_info)

    def plan(self, context):
        return self.rest.plan(context)

    def write_script(self, context):
        return self.rest.write_script(context)

    def review_visual(self, context, before_png, after_png):
        return self.rest.review_visual(context, before_png, after_png)

    def plan_edits(self, context, image_png, image_info):
        return self.editor.plan_edits(context, image_png, image_info)


def make_llm(cfg: dict, audit=None, record_name: str | None = None) -> LLM:
    editor = cfg["llm"].get("editor_provider")
    if editor and editor != cfg["llm"].get("provider", "bedrock"):
        import copy

        other = copy.deepcopy(cfg)
        other["llm"]["provider"] = editor
        other["llm"]["editor_provider"] = None
        return SplitLLM(make_llm(other, audit, record_name), make_llm(dict(cfg, llm=dict(cfg["llm"], editor_provider=None)),
                                                                       audit, record_name))
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


__all__ = ["LLM", "Budget", "BudgetExceeded", "LLMError", "SplitLLM", "make_budget", "make_llm"]
