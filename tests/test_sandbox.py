"""Step 3: static script checks and the local sandbox."""

from __future__ import annotations

import os
import sys

import pytest

from doc_update.sandbox.local import LocalSandbox, default_sandbox_python, minimal_env
from doc_update.sandbox.static_check import validate

GOOD = '''
import json
import pdf_ops

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, 1, "31 December 2023")
result = pdf_ops.replace_in_place(doc, target, "31 December 2024")
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''

needs_sandbox = pytest.mark.skipif(not default_sandbox_python().exists(), reason="sandbox venv not installed")


def test_good_script_passes_static_check():
    assert validate(GOOD, ["31 December 2024"]) == []


@pytest.mark.parametrize("snippet, fragment", [
    ("import os", "import of 'os'"),
    ("import subprocess", "import of 'subprocess'"),
    ("from socket import socket", "import from 'socket'"),
    ("import urllib.request", "import of 'urllib.request'"),
    ("import pypdfium2.raw", "private module"),
    ("exec('1')", "'exec'"),
    ("eval('1')", "'eval'"),
    ("compile('1', 'x', 'exec')", "'compile'"),
    ("__import__('os')", "'__import__'"),
    ("getattr(pdf_ops, 'open')", "'getattr'"),
    ("x = ().__class__.__bases__", "private attribute"),
    ("open('/etc/passwd')", "looks like a path"),
    ("open('..\\\\secret.txt')", "looks like a path"),
    ("open('C:\\\\Users\\\\x.txt')", "looks like a path"),
    ("p = 'in' + 'put.pdf'\npdf_ops.open(p)", "literal file name"),
    ("pdf_ops.save(doc, name)", "literal file name"),
    ("import pikepdf\npikepdf.Job(['--help'])", "'Job'"),
    ("class X: pass", "class definitions"),
])
def test_static_check_rejects(snippet, fragment):
    src = GOOD + "\n" + snippet + "\n"
    problems = validate(src, ["31 December 2024"])
    assert problems, snippet
    assert any(fragment in p for p in problems), problems


def test_static_check_requires_replacement_and_file_names():
    problems = validate("import pdf_ops\nprint(1)\n", ["new text"])
    assert any("input.pdf" in p for p in problems)
    assert any("output.pdf" in p for p in problems)
    assert any("new text" in p for p in problems)


def test_minimal_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "secret-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    env = minimal_env()
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert set(env) <= {"SYSTEMROOT"}


@pytest.fixture
def sandbox(tmp_path):
    return LocalSandbox(timeout=60, memory_mb=1536)


@needs_sandbox
def test_sandbox_runs_script_and_returns_output(sandbox, pdfs, tmp_path):
    res = sandbox.run(GOOD, pdfs["report"], tmp_path / "candidate.pdf", ["31 December 2024"], log_dir=tmp_path / "logs")
    assert res.ok, res.error + res.stderr
    assert res.output_pdf.exists()
    assert res.result_json["method"] == "original_box"
    assert (tmp_path / "logs" / "stdout.txt").exists()


@needs_sandbox
def test_sandbox_environment_is_empty(sandbox, pdfs, tmp_path, monkeypatch):
    """Runtime proof (static check bypassed): the child sees no inherited variables."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "must-not-leak")
    script = "import os, json\nopen('output.pdf', 'wb').write(open('input.pdf', 'rb').read())\n" \
             "print(json.dumps({'env': sorted(os.environ)}))\n"
    res = sandbox._run_prepared(script, pdfs["report"], tmp_path / "c.pdf")
    assert res.ok, res.error
    assert "must-not-leak" not in res.stdout + res.stderr
    assert set(res.result_json["env"]) <= {"SYSTEMROOT", "LC_CTYPE", "PYTHONUTF8"}


@needs_sandbox
@pytest.mark.parametrize("attack, fragment", [
    ("open({secret!r}).read()", "reading outside the working folder"),
    ("open({outside!r}, 'w').write('x')", "writing outside the working folder"),
    ("import subprocess; subprocess.run(['echo', 'hi'])", "operation not allowed"),
    ("import socket; socket.create_connection(('example.com', 80))", "operation not allowed"),
    ("import os; os.system('echo hi')", "operation not allowed"),
    ("import os; os.remove('input.pdf')", "operation not allowed"),
])
def test_sandbox_audit_hook_blocks_at_runtime(sandbox, pdfs, tmp_path, attack, fragment):
    """Second line of defence: even a script that slipped past the static check is stopped."""
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    script = attack.format(secret=str(secret), outside=str(tmp_path / "written.txt"))
    res = sandbox._run_prepared(script, pdfs["report"], tmp_path / "c.pdf")
    assert not res.ok
    assert fragment in res.stderr, res.stderr[-500:]
    assert "top secret" not in res.stdout
    assert not (tmp_path / "written.txt").exists()


@needs_sandbox
def test_sandbox_timeout(pdfs, tmp_path):
    sb = LocalSandbox(timeout=3, memory_mb=1536)
    script = GOOD + "\nwhile True:\n    pass\n"
    res = sb.run(script, pdfs["report"], tmp_path / "c.pdf", ["31 December 2024"])
    assert res.timed_out and not res.ok
    assert "time limit" in res.error


@needs_sandbox
def test_sandbox_memory_limit(pdfs, tmp_path):
    sb = LocalSandbox(timeout=30, memory_mb=600)
    script = GOOD + "\nblocks = []\nfor i in range(100):\n    blocks.append(bytes(50 * 1024 * 1024))\n"
    res = sb.run(script, pdfs["report"], tmp_path / "c.pdf", ["31 December 2024"])
    assert not res.ok
    assert res.memory_exceeded or res.returncode != 0


@needs_sandbox
def test_sandbox_script_error_is_reported(sandbox, pdfs, tmp_path):
    script = GOOD.replace('"31 December 2023"', '"text that is not there"')
    res = sandbox.run(script, pdfs["report"], tmp_path / "c.pdf", ["31 December 2024"])
    assert not res.ok
    assert "TargetNotFoundError" in res.stderr or "not found" in res.error


@needs_sandbox
def test_sandbox_rejects_before_running(sandbox, pdfs, tmp_path):
    res = sandbox.run("import os\nos.system('echo hi')\n", pdfs["report"], tmp_path / "c.pdf")
    assert not res.ok and res.rejected and res.returncode is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_windows_job_object_limits(pdfs, tmp_path):
    sb = LocalSandbox(timeout=30, memory_mb=400)
    script = GOOD + "\nblocks = []\nfor i in range(100):\n    blocks.append(bytes(50 * 1024 * 1024))\n"
    res = sb.run(script, pdfs["report"], tmp_path / "c.pdf", ["31 December 2024"])
    assert not res.ok and (res.memory_exceeded or res.returncode != 0)
