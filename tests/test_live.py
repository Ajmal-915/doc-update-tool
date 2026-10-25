"""Live mode: pytest -m live (needs AWS_BEARER_TOKEN_BEDROCK and AWS_REGION).

Runs the year-end scenarios (or, if that pack has not been imported, the
synthetic pack) through Amazon Bedrock and records the responses so they can
be replayed offline later.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from doc_update.scenarios import run_pack

DATA = Path(__file__).parent / "data"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or not os.environ.get("AWS_REGION"),
                       reason="set AWS_BEARER_TOKEN_BEDROCK and AWS_REGION for live tests"),
]


def test_scenarios_live(tmp_path):
    from test_scenarios import _check

    pack = DATA / "year-end-test-pack"
    if not (pack / "scenarios.json").exists():
        pack = DATA / "synthetic-pack"
    results = run_pack(pack, "bedrock", work=tmp_path,
                       extra_overrides={"llm": {"record_dir": str(DATA / "recorded" / "live")}})
    _check(results, f"{pack.name}-live")
