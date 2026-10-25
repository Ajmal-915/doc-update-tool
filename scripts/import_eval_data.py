"""Copy the evaluation data into tests/data (never modifying the originals).

    python scripts/import_eval_data.py
    python scripts/import_eval_data.py --eval-root "C:\\Users\\soley\\pdf-surgeon-eval"

1. Copies scenarios/year-end-test-pack (scenarios.json and the PDFs) to
   tests/data/year-end-test-pack.
2. Copies make_cases.py (only that file, plus the scenarios folder it may
   read) into a temporary folder, runs it there with this Python, and copies
   every PDF / JSON it creates into tests/data/generated-cases.

The pdf-surgeon and editPDFbyAI folders are never opened, read or copied.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL = Path(r"C:\Users\soley\pdf-surgeon-eval")
EXCLUDED = {"pdf-surgeon", "editpdfbyai"}


def _copy_tree_readonly_source(src: Path, dst: Path) -> list[str]:
    copied = []
    for p in sorted(src.rglob("*")):
        if any(part.lower() in EXCLUDED for part in p.relative_to(src).parts):
            continue
        if p.is_file():
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)  # copyfile: data only, the source is opened read-only
            copied.append(str(p.relative_to(src)))
    return copied


def import_pack(eval_root: Path) -> None:
    pack = eval_root / "scenarios" / "year-end-test-pack"
    if not (pack / "scenarios.json").exists():
        print(f"year-end test pack not found at {pack}")
        return
    dst = ROOT / "tests" / "data" / "year-end-test-pack"
    if dst.exists():
        shutil.rmtree(dst)
    files = _copy_tree_readonly_source(pack, dst)
    print(f"copied {len(files)} files to {dst}: {', '.join(files)}")


def run_make_cases(eval_root: Path, timeout: int) -> None:
    script = eval_root / "make_cases.py"
    if not script.exists():
        print(f"{script} not found; skipping generated cases")
        return
    text = script.read_text(encoding="utf-8", errors="replace")
    absolute = [m for m in re.findall(r"[A-Za-z]:\\\\[^'\"]+|[A-Za-z]:/[^'\"]+", text) if "pdf-surgeon-eval" in m.lower()]
    if absolute:
        print("make_cases.py writes to absolute paths inside the original folder; not running it, to keep the "
              f"originals untouched: {absolute[:3]}")
        return
    if re.search(r"^\s*(from|import)\s+(pdf_surgeon|editpdfbyai)", text, re.M | re.I):
        print("make_cases.py imports code from an excluded project; not running it")
        return
    with tempfile.TemporaryDirectory(prefix="make_cases_") as td:
        work = Path(td) / "pdf-surgeon-eval"
        work.mkdir()
        shutil.copyfile(script, work / "make_cases.py")
        if (eval_root / "scenarios").exists():
            _copy_tree_readonly_source(eval_root / "scenarios", work / "scenarios")
        before = {p for p in work.rglob("*") if p.is_file()}
        args = [sys.executable, "make_cases.py"]
        if re.search(r"--out(put)?(-dir)?\b", text):
            flag = re.search(r"(--out(?:put)?(?:-dir)?)\b", text).group(1)
            args += [flag, str(work / "cases_out")]
        print("running:", " ".join(args), f"(in {work})")
        proc = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=timeout)
        print(proc.stdout[-2000:])
        if proc.returncode != 0:
            print("make_cases.py failed:\n" + proc.stderr[-3000:])
            return
        new = [p for p in work.rglob("*") if p.is_file() and p not in before and p.suffix.lower() in (".pdf", ".json")]
        dst = ROOT / "tests" / "data" / "generated-cases"
        if dst.exists():
            shutil.rmtree(dst)
        for p in new:
            target = dst / p.relative_to(work)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
        print(f"copied {len(new)} generated files to {dst}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-root", default=str(DEFAULT_EVAL))
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    root = Path(args.eval_root)
    if not root.exists():
        print(f"evaluation folder not found: {root}")
        raise SystemExit(1)
    import_pack(root)
    run_make_cases(root, args.timeout)


if __name__ == "__main__":
    main()
