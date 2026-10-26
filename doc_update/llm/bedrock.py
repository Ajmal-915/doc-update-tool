"""Amazon Bedrock (Converse API) provider.

The Bedrock API key is read by boto3 itself from AWS_BEARER_TOKEN_BEDROCK;
this module only checks that it is present and never reads, prints, logs or
stores its value.  The region comes from AWS_REGION (or config llm.region).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import prompts, schemas
from .base import LLM, Budget, LLMError, prompt_hash

KEY_VAR = "AWS_BEARER_TOKEN_BEDROCK"
RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException", "InternalServerException",
             "ModelTimeoutException"}


def key_present() -> bool:
    return bool(os.environ.get(KEY_VAR))


def region_from(cfg: dict) -> str | None:
    return cfg.get("llm", {}).get("region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


class BedrockTransport:
    """Thin wrapper around boto3's bedrock-runtime client."""

    def __init__(self, region: str, timeout: float = 180):
        if not key_present():
            raise LLMError(f"the Bedrock API key is not set: set the {KEY_VAR} environment variable")
        if not region:
            raise LLMError("no AWS region: set the AWS_REGION environment variable (or llm.region in config.yaml)")
        import boto3
        from botocore.config import Config

        cfg = Config(read_timeout=timeout, connect_timeout=20, retries={"max_attempts": 1, "mode": "standard"})
        # No credentials or signature version are given in code, so botocore prefers the bearer token.
        self.client = boto3.client("bedrock-runtime", region_name=region, config=cfg)
        self.region = region

    def converse(self, **request) -> dict:
        return self.client.converse(**request)


def _geo(region: str) -> str:
    if region.startswith("us-gov"):
        return "us-gov"
    if region.startswith("us-"):
        return "us"
    if region.startswith("eu-"):
        return "eu"
    if region.startswith(("ap-", "me-")):
        return "apac"
    if region.startswith("ca-"):
        return "ca"
    if region.startswith("sa-"):
        return "sa"
    return region.split("-")[0]


def list_models(region: str) -> list[dict]:
    """Inference profiles and Anthropic foundation models the key can see."""
    if not key_present():
        raise LLMError(f"the Bedrock API key is not set: set the {KEY_VAR} environment variable")
    import boto3

    client = boto3.client("bedrock", region_name=region)
    out = []
    token = None
    while True:
        kw = {"maxResults": 100}
        if token:
            kw["nextToken"] = token
        resp = client.list_inference_profiles(**kw)
        for p in resp.get("inferenceProfileSummaries", []):
            out.append({"id": p.get("inferenceProfileId"), "name": p.get("inferenceProfileName"),
                        "status": p.get("status"), "type": "inference_profile"})
        token = resp.get("nextToken")
        if not token:
            break
    try:
        resp = client.list_foundation_models(byProvider="Anthropic")
        for m in resp.get("modelSummaries", []):
            out.append({"id": m.get("modelId"), "name": m.get("modelName"), "status": m.get("modelLifecycle", {}).get("status"),
                        "type": "foundation_model", "inference_types": m.get("inferenceTypesSupported", [])})
    except Exception:
        pass
    return out


def choose_model(models: list[dict], family: str, region: str) -> str | None:
    geo = _geo(region)
    profiles = [m["id"] for m in models if m["type"] == "inference_profile" and m.get("status", "ACTIVE") == "ACTIVE"
                and m["id"] and family in m["id"]]
    for prefix in ("global.", geo + "."):
        for pid in profiles:
            if pid.startswith(prefix):
                return pid
    if profiles:
        return profiles[0]
    sonnets = sorted({m["id"] for m in models if m["id"] and "claude-sonnet" in m["id"] and m["type"] == "inference_profile"
                      and (m["id"].startswith("global.") or m["id"].startswith(geo + "."))}, reverse=True)
    if sonnets:
        return sonnets[0]
    direct = [m["id"] for m in models if m["type"] == "foundation_model" and m["id"] and family in m["id"]
              and "ON_DEMAND" in (m.get("inference_types") or [])]
    return direct[0] if direct else None


def resolve_models(cfg: dict, region: str, cache_dir: Path) -> dict:
    llm = cfg["llm"]
    roles = {r: llm.get(f"{r}_model", "auto") for r in ("vision", "planner", "coder")}
    if all(v and v != "auto" for v in roles.values()):
        return roles
    family = llm.get("auto_model_family", "claude-sonnet-5-5")
    cache = cache_dir / ".model_cache.json"
    data = {}
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
        except ValueError:
            data = {}
    key = f"{region}:{family}"
    chosen = data.get(key)
    if not chosen:
        try:
            chosen = choose_model(list_models(region), family, region)
        except Exception:
            chosen = None
        if not chosen:
            chosen = f"global.anthropic.{family}"
        data[key] = chosen
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, indent=2))
        except OSError:
            pass
    return {r: (v if v and v != "auto" else chosen) for r, v in roles.items()}


class ConverseLLM(LLM):
    provider = "bedrock"

    def __init__(self, transport, models: dict, cfg: dict, budget: Budget, audit=None, recorder=None):
        super().__init__(budget, audit)
        self.transport = transport
        self._models = models
        self.cfg = cfg["llm"]
        self.recorder = recorder
        self.no_temperature: set[str] = set()

    def models(self) -> dict:
        return dict(self._models)

    # ---------------------------------------------------------------- core
    def _request(self, role: str, system: str, content: list, tool: dict, temperature: bool) -> dict:
        req = {
            "modelId": self._models[role],
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": {"maxTokens": int(self.cfg.get("max_tokens", 8000))},
            "toolConfig": {"tools": [schemas.tool_spec(tool)],
                           "toolChoice": {"tool": {"name": tool["name"]}} if self.cfg.get("tool_choice") == "tool" else {"auto": {}}},
        }
        if temperature and self.cfg.get("temperature") is not None:
            req["inferenceConfig"]["temperature"] = float(self.cfg["temperature"])
        return req

    def _send(self, role: str, req: dict) -> dict:
        delay = 2.0
        for attempt in range(4):
            try:
                return self.transport.converse(**req)
            except LLMError:
                raise
            except Exception as exc:  # botocore.exceptions.ClientError and friends
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
                message = str(exc)
                if code == "ValidationException" and "temperature" in message.lower() and "temperature" in req.get("inferenceConfig", {}):
                    self.no_temperature.add(req["modelId"])
                    req = dict(req, inferenceConfig={k: v for k, v in req["inferenceConfig"].items() if k != "temperature"})
                    self.audit(event="llm_note", role=role, model=req["modelId"],
                               note="model accepts only its default temperature; retried without temperature")
                    continue
                if code in RETRYABLE and attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Bedrock call failed ({code or type(exc).__name__}): {_safe(message)}") from None
        raise LLMError("Bedrock call failed after retries")

    def _call(self, role: str, system: str, content: list, tool: dict, reminder: bool = False,
              model_role: str | None = None) -> dict:
        self.budget.check(role)
        model = self._models[model_role or role]
        req = self._request(model_role or role, system, content, tool, temperature=model not in self.no_temperature)
        if reminder:
            req["messages"][0]["content"] = content + [{"text": f"You must answer by calling the tool {tool['name']}."}]
        phash = prompt_hash(req)
        t0 = time.monotonic()
        resp = self._send(role, req)
        latency = time.monotonic() - t0
        usage = resp.get("usage", {}) or {}
        tin, tout = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
        cost = self.budget.add(model, tin, tout)
        self.audit(event="llm_call", role=role, model=model, prompt_sha256=phash, input_tokens=tin, output_tokens=tout,
                   latency_ms=int(resp.get("metrics", {}).get("latencyMs", latency * 1000)), estimated_cost_usd=round(cost, 5),
                   stop_reason=resp.get("stopReason"))
        if self.recorder is not None:
            self.recorder(role, model, phash, resp)
        value = _tool_input(resp, tool["name"])
        if value is None:
            if not reminder:
                return self._call(role, system, content, tool, reminder=True, model_role=model_role)
            raise LLMError(f"the {role} model did not call {tool['name']} (stop reason {resp.get('stopReason')})")
        errors = schemas.validate(value, tool["schema"])
        if errors:
            if not reminder:
                return self._call(role, system, content + [{"text": "Your previous answer did not match the schema: "
                                                             + "; ".join(errors[:5])}], tool, reminder=True,
                                  model_role=model_role)
            raise LLMError(f"the {role} model's answer does not match the schema: " + "; ".join(errors[:5]))
        return value

    # --------------------------------------------------------------- roles
    def analyse_space(self, context: dict, image_png: bytes, image_info: dict) -> dict:
        content = [{"image": {"format": "png", "source": {"bytes": image_png}}},
                   {"text": prompts.vision_user(context["measurements"], image_info, context["request"])}]
        return self._call("vision", prompts.VISION_SYSTEM, content, schemas.SPACE_ANALYSIS)

    def plan(self, context: dict) -> dict:
        return self._call("planner", prompts.PLANNER_SYSTEM, [{"text": prompts.planner_user(context)}], schemas.PLAN)

    def write_script(self, context: dict) -> str:
        value = self._call("coder", prompts.CODER_SYSTEM, [{"text": prompts.coder_user(context)}], schemas.SCRIPT)
        return _strip_fences(value["script"])

    def review_visual(self, context: dict, before_png: bytes, after_png: bytes) -> dict:
        content = [{"image": {"format": "png", "source": {"bytes": before_png}}},
                   {"image": {"format": "png", "source": {"bytes": after_png}}},
                   {"text": prompts.review_user(context)}]
        return self._call("visual_review", prompts.REVIEW_SYSTEM, content, schemas.VISUAL_REVIEW, model_role="vision")


    def plan_edits(self, context: dict, image_png: bytes, image_info: dict) -> dict:
        content = [{"image": {"format": "png", "source": {"bytes": image_png}}},
                   {"text": prompts.editor_user(context, image_info)}]
        return self._call("editor", prompts.EDITOR_SYSTEM, content, schemas.EDIT_PLAN, model_role="vision")


def _tool_input(resp: dict, name: str):
    content = (resp.get("output", {}) or {}).get("message", {}).get("content", []) or []
    for block in content:
        tu = block.get("toolUse") if isinstance(block, dict) else None
        if tu and tu.get("name") == name:
            value = tu.get("input")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    return None
            return value if isinstance(value, dict) else None
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if text:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    value = json.loads(m.group(0))
                    return value if isinstance(value, dict) else None
                except ValueError:
                    continue
    return None


def _strip_fences(script: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", script, re.S)
    return m.group(1) if m else script


def _safe(message: str) -> str:
    """Error text without anything that looks like a credential."""
    return re.sub(r"(?i)(bearer\s+|token[=:]\s*|key[=:]\s*)\S+", r"\1***", message)[:500]
