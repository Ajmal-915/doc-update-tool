"""Sandbox interface.

The pipeline only talks to :class:`Sandbox`.  :class:`LocalSandbox` runs the
script in a separate, minimal Python environment on this machine; a container
or virtual-machine sandbox can be added later by implementing ``run``.

The local sandbox is a safety layer for internal use, not a hard security
boundary: the static check, ``python -I``, an empty environment, the audit
hook, a timeout and a memory limit make accidents and simple misuse
unlikely, but they do not contain a determined attacker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SandboxResult:
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    output_pdf: Path | None
    duration: float
    timed_out: bool = False
    memory_exceeded: bool = False
    rejected: list[str] = field(default_factory=list)
    result_json: dict | None = None
    peak_memory_mb: float | None = None

    @property
    def error(self) -> str:
        if self.rejected:
            return "script rejected before running: " + "; ".join(self.rejected)
        if self.timed_out:
            return "script exceeded the time limit"
        if self.memory_exceeded:
            return "script exceeded the memory limit"
        if self.returncode not in (0, None):
            tail = self.stderr.strip().splitlines()[-12:]
            return f"script exited with code {self.returncode}: " + "\n".join(tail)
        if self.output_pdf is None:
            return "script did not write output.pdf"
        return ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "returncode": self.returncode, "duration_s": round(self.duration, 3),
            "timed_out": self.timed_out, "memory_exceeded": self.memory_exceeded, "rejected": self.rejected,
            "result_json": self.result_json, "error": self.error, "peak_memory_mb": self.peak_memory_mb,
        }


class Sandbox(ABC):
    name = "abstract"

    @abstractmethod
    def run(self, script: str, input_pdf: Path, output_pdf: Path, required_strings: list[str] | None = None,
            log_dir: Path | None = None) -> SandboxResult:
        """Validate and run ``script`` on a copy of ``input_pdf``; copy its output.pdf to ``output_pdf``."""


class ContainerSandbox(Sandbox):
    """Placeholder for a container / VM implementation of the same interface."""

    name = "container"

    def run(self, script, input_pdf, output_pdf, required_strings=None, log_dir=None) -> SandboxResult:
        raise NotImplementedError("a container sandbox is not configured on this machine")


def make_sandbox(config) -> Sandbox:
    kind = config.get("sandbox", {}).get("kind", "local")
    if kind == "local":
        from .local import LocalSandbox

        return LocalSandbox.from_config(config)
    if kind == "container":
        return ContainerSandbox()
    raise ValueError(f"unknown sandbox kind {kind!r}")
