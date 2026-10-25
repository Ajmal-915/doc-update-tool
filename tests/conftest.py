from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import samples  # noqa: E402


@pytest.fixture(scope="session")
def pdfs(tmp_path_factory) -> dict[str, Path]:
    return samples.build_all(tmp_path_factory.mktemp("samples"))


@pytest.fixture
def work(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def copy_pdf(pdfs, tmp_path):
    def _copy(name: str) -> Path:
        dst = tmp_path / f"{name}.pdf"
        shutil.copy(pdfs[name], dst)
        return dst

    return _copy


@pytest.fixture(autouse=True)
def _no_bedrock_key(monkeypatch, request):
    """Offline tests must never see a Bedrock key."""
    if "live" not in request.keywords:
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
