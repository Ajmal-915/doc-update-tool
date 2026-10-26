"""Configuration loading (config.yaml) with defaults."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT / "config.yaml"

DEFAULTS: dict = {
    "store": {"root": "docs"},
    "matching": {"min_score": 90, "ambiguity_margin": 5},
    "editing": {
        "min_font_scale": 0.90,
        "retries_per_method": 2,
        "methods": ["original_box", "horizontal_expansion", "reduced_font", "paragraph_reflow", "form_field_update"],
    },
    "checks": {"render_dpi": 150, "pixel_margin": 2},
    "recommend": {"max_edits": 10},
    "llm": {
        "provider": "bedrock", "region": None, "vision_model": "auto", "planner_model": "auto", "coder_model": "auto",
        "auto_model_family": "claude-sonnet-5-5", "temperature": 0, "max_tokens": 8000, "tool_choice": "auto",
        "max_calls_per_change": 12, "max_cost_per_change_usd": 1.0,
        "prices_per_million_tokens": {"default": {"input": 2.0, "output": 10.0}},
        "timeout_seconds": 180, "visual_check": True, "visual_review": True, "replay_file": None, "record_dir": None,
        "manual_dir": None, "manual_session": None, "editor_provider": None,
    },
    "sandbox": {"kind": "local", "python": None, "lib_dir": None, "timeout_seconds": 60, "memory_mb": 1536},
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    p = Path(path) if path else Path(os.environ.get("DOC_UPDATE_CONFIG", DEFAULT_PATH))
    data = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, data)
    cfg = _merge(cfg, overrides or {})
    base = p.parent if p.exists() else PROJECT
    root = Path(os.path.expanduser(str(cfg["store"]["root"])))
    cfg["store"]["root"] = str(root if root.is_absolute() else (base / root).resolve())
    for key in ("replay_file", "record_dir", "manual_dir"):
        v = cfg["llm"].get(key)
        if v:
            q = Path(os.path.expanduser(str(v)))
            cfg["llm"][key] = str(q if q.is_absolute() else (base / q).resolve())
    scale = float(cfg["editing"]["min_font_scale"])
    if not 0.5 <= scale <= 1.0:
        raise ValueError("editing.min_font_scale must be between 0.5 and 1.0")
    cfg["_path"] = str(p)
    return cfg
