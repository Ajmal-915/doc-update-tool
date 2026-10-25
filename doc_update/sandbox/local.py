"""Local sandbox: a separate Python environment on this machine (no Docker)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import Sandbox, SandboxResult
from .bootstrap import BOOTSTRAP
from .static_check import validate

PROJECT = Path(__file__).resolve().parents[2]
PDF_OPS_SRC = PROJECT / "pdf_ops"
MAX_OUTPUT = 64_000


def default_sandbox_python() -> Path:
    base = Path.home() / ".venvs" / "doc-update-sandbox"
    return base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def minimal_env() -> dict:
    """The child gets no inherited variables: never the Bedrock key or any other secret.

    On Windows, SYSTEMROOT is required for the interpreter to start (it holds
    no secret); nothing else is passed.
    """
    env: dict[str, str] = {}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


class LocalSandbox(Sandbox):
    name = "local"

    def __init__(self, python: Path | None = None, timeout: float = 60.0, memory_mb: int = 1536,
                 lib_dir: Path | None = None):
        self.python = Path(python) if python else default_sandbox_python()
        self.timeout = timeout
        self.memory_mb = memory_mb
        self.lib_dir = Path(lib_dir) if lib_dir else self.python.parent.parent / "pdf_ops_lib"

    @classmethod
    def from_config(cls, config: dict) -> "LocalSandbox":
        sb = config.get("sandbox", {})
        py = sb.get("python") or None
        lib = sb.get("lib_dir") or None
        return cls(Path(os.path.expanduser(py)) if py else None, float(sb.get("timeout_seconds", 60)),
                   int(sb.get("memory_mb", 1536)), Path(os.path.expanduser(lib)) if lib else None)

    # ------------------------------------------------------------- set-up
    def sync_library(self) -> Path:
        """Copy the current pdf_ops into the sandbox's library folder when it changed."""
        want = _tree_hash(PDF_OPS_SRC)
        stamp = self.lib_dir / "pdf_ops.sha256"
        if stamp.exists() and stamp.read_text().strip() == want and (self.lib_dir / "pdf_ops" / "__init__.py").exists():
            return self.lib_dir
        self.lib_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="pdf_ops_", dir=self.lib_dir))
        shutil.copytree(PDF_OPS_SRC, tmp / "pdf_ops", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target = self.lib_dir / "pdf_ops"
        if target.exists():
            shutil.rmtree(target)
        os.replace(tmp / "pdf_ops", target)
        shutil.rmtree(tmp, ignore_errors=True)
        stamp.write_text(want)
        return self.lib_dir

    def check_ready(self) -> str | None:
        if not self.python.exists():
            return (f"sandbox Python not found at {self.python}; create it with "
                    f"'python -m venv \"{self.python.parent.parent}\"' and install requirements-sandbox.txt")
        return None

    # ---------------------------------------------------------------- run
    def run(self, script: str, input_pdf: Path, output_pdf: Path, required_strings: list[str] | None = None,
            log_dir: Path | None = None) -> SandboxResult:
        problems = validate(script, required_strings)
        if problems:
            return SandboxResult(False, None, "", "", None, 0.0, rejected=problems)
        return self._run_prepared(script, input_pdf, output_pdf, log_dir)

    def _run_prepared(self, script: str, input_pdf: Path, output_pdf: Path, log_dir: Path | None = None) -> SandboxResult:
        """Run a script that has already passed the static check."""
        not_ready = self.check_ready()
        if not_ready:
            return SandboxResult(False, None, "", not_ready, None, 0.0, rejected=[not_ready])
        lib = self.sync_library()
        root = Path(tempfile.mkdtemp(prefix="docupd_"))
        try:
            work = root / "work"
            code = root / "code"
            work.mkdir()
            code.mkdir()
            shutil.copyfile(input_pdf, work / "input.pdf")
            script_path = code / "script.py"
            script_path.write_text(script, encoding="utf-8")
            boot = code / "bootstrap.py"
            boot.write_text(BOOTSTRAP, encoding="utf-8")
            args = [str(self.python), "-I", "-X", "utf8", "-B", str(boot), str(lib), str(script_path)]
            start = time.monotonic()
            stdout, stderr, rc, timed_out, peak = self._execute(args, work)
            duration = time.monotonic() - start
            stdout = stdout[-MAX_OUTPUT:]
            stderr = stderr[-MAX_OUTPUT:]
            mem_exceeded = (peak is not None and peak >= self.memory_mb * 1024 * 1024 * 0.97 and rc != 0) or \
                ("MemoryError" in stderr)
            out = work / "output.pdf"
            produced = None
            if rc == 0 and not timed_out and out.exists() and out.stat().st_size > 0:
                output_pdf.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(out, output_pdf)
                produced = output_pdf
            result_json = _last_json_line(stdout)
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
                (log_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
            return SandboxResult(
                ok=produced is not None, returncode=rc, stdout=stdout, stderr=stderr, output_pdf=produced,
                duration=duration, timed_out=timed_out, memory_exceeded=mem_exceeded, result_json=result_json,
                peak_memory_mb=round(peak / 1048576, 1) if peak else None,
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _execute(self, args: list[str], work: Path):
        env = minimal_env()
        if os.name == "nt":
            from .winjob import JobObject

            job = JobObject(self.memory_mb)
            try:
                proc = job.start(args, str(work), env)
                try:
                    out, err = proc.communicate(timeout=self.timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    job.terminate()
                    out, err = proc.communicate()
                    timed_out = True
                peak = job.peak_memory()
            finally:
                job.close()
            return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode, timed_out, peak

        import resource
        import signal

        limit = self.memory_mb * 1024 * 1024

        def limits():  # runs in the child before exec
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))

        proc = subprocess.Popen(args, cwd=str(work), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, preexec_fn=limits, start_new_session=True, close_fds=True)
        try:
            out, err = proc.communicate(timeout=self.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            timed_out = True
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode, timed_out, None


def _last_json_line(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None
