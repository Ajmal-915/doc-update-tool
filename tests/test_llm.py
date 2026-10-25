"""Step 4: Bedrock Converse integration logic (no network, no key)."""

from __future__ import annotations

import json

import pytest

from doc_update.config import load_config
from doc_update.llm import BudgetExceeded, LLMError, make_budget, make_llm
from doc_update.llm.base import Budget, prompt_hash
from doc_update.llm.bedrock import BedrockTransport, ConverseLLM, choose_model, resolve_models
from doc_update.llm.schemas import PLAN, validate


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def _resp(name, payload, text_only=False):
    content = [{"text": json.dumps(payload)}] if text_only else [{"toolUse": {"toolUseId": "x", "name": name, "input": payload}}]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use",
            "usage": {"inputTokens": 1000, "outputTokens": 100}, "metrics": {"latencyMs": 50}}


GOOD_PLAN = {"chosen_method": "original_box", "fallback_methods": [], "parameters": {}, "reason": "fits"}


class Scripted:
    def __init__(self, items):
        self.items = list(items)
        self.requests = []

    def converse(self, **req):
        self.requests.append(json.loads(json.dumps(req, default=str)))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _llm(items, **cfg_llm):
    cfg = load_config(None, {"llm": cfg_llm})
    events = []
    t = Scripted(items)
    llm = ConverseLLM(t, {"vision": "mv", "planner": "mp", "coder": "mc"}, cfg, make_budget(cfg),
                      audit=lambda **kw: events.append(kw))
    return llm, t, events


def test_temperature_rejected_is_retried_without_it():
    llm, t, events = _llm([ClientError("ValidationException", "temperature is not supported for this model"),
                           _resp("submit_plan", GOOD_PLAN)])
    assert llm.plan({"x": 1})["chosen_method"] == "original_box"
    assert t.requests[0]["inferenceConfig"]["temperature"] == 0
    assert "temperature" not in t.requests[1]["inferenceConfig"]
    assert any(e.get("event") == "llm_note" for e in events)
    # remembered for the next call
    t.items.append(_resp("submit_plan", GOOD_PLAN))
    llm.plan({"x": 2})
    assert "temperature" not in t.requests[2]["inferenceConfig"]


def test_missing_tool_call_gets_one_reminder():
    llm, t, _ = _llm([{"output": {"message": {"content": [{"text": "I think original_box."}]}}, "stopReason": "end_turn",
                       "usage": {"inputTokens": 5, "outputTokens": 5}},
                      _resp("submit_plan", GOOD_PLAN)])
    assert llm.plan({})["chosen_method"] == "original_box"
    assert "must answer by calling the tool" in json.dumps(t.requests[1]["messages"])


def test_json_in_text_is_accepted():
    llm, _, _ = _llm([_resp("submit_plan", GOOD_PLAN, text_only=True)])
    assert llm.plan({})["reason"] == "fits"


def test_schema_violation_twice_is_an_error():
    bad = {"chosen_method": "magic", "fallback_methods": [], "parameters": {}, "reason": "?"}
    llm, _, _ = _llm([_resp("submit_plan", bad), _resp("submit_plan", bad)])
    with pytest.raises(LLMError, match="does not match the schema"):
        llm.plan({})


def test_throttling_is_retried(monkeypatch):
    monkeypatch.setattr("doc_update.llm.bedrock.time.sleep", lambda s: None)
    llm, t, _ = _llm([ClientError("ThrottlingException", "slow down"), _resp("submit_plan", GOOD_PLAN)])
    assert llm.plan({})["chosen_method"] == "original_box"
    assert len(t.requests) == 2


def test_access_denied_is_reported_without_secrets(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ABSKsecretvalue")
    llm, _, _ = _llm([ClientError("AccessDeniedException", "bearer ABSKsecretvalue is not allowed")])
    with pytest.raises(LLMError) as exc:
        llm.plan({})
    assert "ABSKsecretvalue" not in str(exc.value)


def test_budget_limits_calls_and_cost():
    b = Budget(max_calls=2, max_cost=0.05, prices={"default": {"input": 3.0, "output": 15.0}})
    b.check("x")
    assert abs(b.add("m", 10_000, 1_000) - 0.045) < 1e-9
    b.check("x")
    b.add("m", 10_000, 1_000)
    with pytest.raises(BudgetExceeded):
        b.check("x")


def test_audit_records_hash_tokens_and_latency_but_no_text():
    llm, _, events = _llm([_resp("submit_plan", GOOD_PLAN)])
    llm.plan({"secret_pdf_text": "Confidential salary table"})
    call = [e for e in events if e.get("event") == "llm_call"][0]
    assert set(call) >= {"role", "model", "prompt_sha256", "input_tokens", "output_tokens", "latency_ms"}
    assert "Confidential" not in json.dumps(call)


def test_prompt_hash_ignores_image_bytes_but_changes_with_text():
    a = {"messages": [{"content": [{"image": {"source": {"bytes": b"\x89PNG..."}}}, {"text": "a"}]}]}
    b = {"messages": [{"content": [{"image": {"source": {"bytes": b"\x89PNG..."}}}, {"text": "b"}]}]}
    assert prompt_hash(a) != prompt_hash(b) and len(prompt_hash(a)) == 64


def test_schema_validator():
    assert validate(GOOD_PLAN, PLAN["schema"]) == []
    assert validate({"chosen_method": 3}, PLAN["schema"])


def test_transport_requires_key_and_region(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    with pytest.raises(LLMError, match="AWS_BEARER_TOKEN_BEDROCK"):
        BedrockTransport("eu-west-2")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "dummy")
    with pytest.raises(LLMError, match="AWS_REGION"):
        BedrockTransport(None)


def test_bedrock_client_prefers_bearer_token(monkeypatch):
    """With the key set, botocore must choose bearer auth even if other AWS credentials exist."""
    from botocore.handlers import _should_prefer_bearer_auth

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "dummy")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    assert _should_prefer_bearer_auth(False, "bedrock", "v4", ["smithy.api#httpBearerAuth", "aws.auth#sigv4"])
    t = BedrockTransport("eu-west-2")  # creates the client; no network call
    assert t.region == "eu-west-2"


def test_model_auto_selection_prefers_global_then_geo(tmp_path):
    models = [
        {"id": "eu.anthropic.claude-sonnet-5-5", "type": "inference_profile", "status": "ACTIVE"},
        {"id": "global.anthropic.claude-sonnet-5-5", "type": "inference_profile", "status": "ACTIVE"},
        {"id": "anthropic.claude-sonnet-5-5", "type": "foundation_model", "inference_types": ["INFERENCE_PROFILE"]},
    ]
    assert choose_model(models, "claude-sonnet-5-5", "eu-west-2") == "global.anthropic.claude-sonnet-5-5"
    assert choose_model(models[:1], "claude-sonnet-5-5", "eu-west-2") == "eu.anthropic.claude-sonnet-5-5"
    cfg = load_config(None, {"llm": {"vision_model": "x.v", "planner_model": "auto", "coder_model": "auto"}})
    (tmp_path / ".model_cache.json").write_text(json.dumps({"eu-west-2:claude-sonnet-5-5": "eu.anthropic.claude-sonnet-5-5"}))
    roles = resolve_models(cfg, "eu-west-2", tmp_path)
    assert roles == {"vision": "x.v", "planner": "eu.anthropic.claude-sonnet-5-5", "coder": "eu.anthropic.claude-sonnet-5-5"}


def test_make_llm_offline_and_unknown():
    cfg = load_config(None, {"llm": {"provider": "offline"}})
    assert make_llm(cfg).provider == "offline"
    with pytest.raises(LLMError):
        make_llm(load_config(None, {"llm": {"provider": "nonsense"}}))
