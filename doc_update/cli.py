"""Command line: python -m doc_update <command> ...

Exit codes: 0 = done (ingested / updated / approved / rejected / status),
2 = manual review needed, 1 = error.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import api
from .config import load_config


def _print(result: dict) -> int:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    status = result.get("status")
    if status == "manual_review":
        return 2
    if status in (None, "error"):
        return 1
    return 0  # also "planned"


def _overrides(args) -> dict:
    o: dict = {}
    if getattr(args, "provider", None):
        o.setdefault("llm", {})["provider"] = args.provider
    if getattr(args, "replay", None):
        o.setdefault("llm", {})["replay_file"] = args.replay
    if getattr(args, "store", None):
        o.setdefault("store", {})["root"] = args.store
    return o


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m doc_update", description="Document Update Tool")
    parser.add_argument("--config", help="path to config.yaml (default: the one next to the program)")
    parser.add_argument("--store", help="override store.root")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="add a PDF to the store")
    p.add_argument("--pdf", required=True)
    p.add_argument("--doc-id", required=True)

    p = sub.add_parser("update", help="run one change request")
    p.add_argument("--doc-id", required=True)
    p.add_argument("--find", required=True, help="source text (may differ slightly from the PDF)")
    p.add_argument("--replace", required=True, help="replacement wording")
    p.add_argument("--page", required=True, type=int, help="page number, from 1")
    p.add_argument("--provider", choices=["bedrock", "offline", "replay", "manual"], help="override llm.provider")
    p.add_argument("--replay", help="recorded responses (with --provider replay)")

    p = sub.add_parser("recommend", help="carry out a recommendation written in plain words on one page")
    p.add_argument("--doc-id", required=True)
    p.add_argument("--page", required=True, type=int, help="page number, from 1")
    p.add_argument("--text", required=True, help="the approved recommendation, e.g. \"Rename Gross Margin to Gross Profit\"")
    p.add_argument("--plan-only", action="store_true", help="only show the verified edit plan; change nothing")
    p.add_argument("--provider", choices=["bedrock", "offline", "replay", "manual"], help="override llm.provider")
    p.add_argument("--replay", help="recorded responses (with --provider replay)")

    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending change")
        p.add_argument("--doc-id", required=True)
        p.add_argument("--change", required=True, help="change ID printed by 'update'")
        p.add_argument("--by", help="name of the reviewer")
        p.add_argument("--note")

    p = sub.add_parser("status", help="show versions and pending changes")
    p.add_argument("--doc-id", required=True)

    sub.add_parser("models", help="list the Bedrock models and inference profiles the key can use")
    sub.add_parser("check-setup", help="check the virtual environments, sandbox and Bedrock settings")

    args = parser.parse_args(argv)
    ov = _overrides(args)
    if args.command == "ingest":
        return _print(api.ingest_document(args.pdf, args.doc_id, args.config, ov))
    if args.command == "update":
        return _print(api.update_document(args.doc_id, args.find, args.replace, args.page, args.config, ov))
    if args.command == "recommend":
        return _print(api.recommend_change(args.doc_id, args.page, args.text, args.config, ov, plan_only=args.plan_only))
    if args.command == "approve":
        return _print(api.approve_change(args.doc_id, args.change, args.by, args.note, args.config, ov))
    if args.command == "reject":
        return _print(api.reject_change(args.doc_id, args.change, args.by, args.note, args.config, ov))
    if args.command == "status":
        return _print(api.document_status(args.doc_id, args.config, ov))
    if args.command == "models":
        return _models(load_config(args.config, ov))
    if args.command == "check-setup":
        return _check_setup(load_config(args.config, ov))
    return 1


def _models(cfg: dict) -> int:
    from .llm.bedrock import choose_model, list_models, region_from

    region = region_from(cfg)
    if not region:
        return _print({"status": "error", "error": "set AWS_REGION first"})
    try:
        models = list_models(region)
    except Exception as exc:
        return _print({"status": "error", "error": str(exc)})
    claude = [m for m in models if m.get("id") and "claude" in m["id"]]
    family = cfg["llm"].get("auto_model_family", "claude-sonnet-5-5")
    return _print({"status": "ok", "region": region, "auto_choice": choose_model(models, family, region),
                   "claude_models": claude})


def _check_setup(cfg: dict) -> int:
    from .llm.bedrock import KEY_VAR, key_present, region_from
    from .sandbox.local import LocalSandbox

    out = {"status": "ok", "python": sys.version.split()[0], "store": cfg["store"]["root"]}
    sb = LocalSandbox.from_config(cfg)
    out["sandbox_python"] = str(sb.python)
    problem = sb.check_ready()
    out["sandbox_ready"] = problem is None
    if problem:
        out["status"] = "error"
        out["sandbox_problem"] = problem
    out["bedrock_key_set"] = key_present()  # the value itself is never shown
    out["aws_region"] = region_from(cfg)
    out["llm_provider"] = cfg["llm"]["provider"]
    if cfg["llm"]["provider"] == "bedrock" and not (key_present() and out["aws_region"]):
        out["status"] = "error"
        out["bedrock_problem"] = f"set {KEY_VAR} and AWS_REGION to use Bedrock"
    if sys.version_info < (3, 11):
        out["status"] = "error"
        out["python_problem"] = "Python 3.11 or later is required"
    return _print(out)
