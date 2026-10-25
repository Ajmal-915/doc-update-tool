"""Static validation of a generated script before it is run.

The script is parsed with the ``ast`` module and rejected unless it

* imports only from the allowlist,
* uses no exec / eval / compile / __import__ (or other reflection built-ins),
* touches no dunder or private attributes,
* passes only literal, plain file names (no directories, no '..') to
  anything that opens or saves a file, and
* uses no network, subprocess or OS modules (they are not on the allowlist).
"""

from __future__ import annotations

import ast
import re

ALLOWED_MODULES = {"pdf_ops", "pikepdf", "fontTools", "pypdfium2", "pdfplumber", "math", "json", "re", "decimal"}

BANNED_NAMES = {
    "exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "input", "breakpoint", "help", "exit", "quit", "memoryview", "__builtins__", "__loader__", "__spec__",
    "chr", "ord", "bytearray", "classmethod", "staticmethod", "property", "super",
}
BANNED_ATTRIBUTES = {
    "Job", "raw", "internal", "main", "system", "popen", "startfile", "unlink", "rmtree", "remove", "rmdir",
    "chmod", "chown", "symlink", "link", "mkfifo", "fork", "spawn", "kill", "load_module", "exec_module",
    "fromhex", "decode_bytes",
}
# calls that open or save files, with the positions of their path arguments
PATH_ARGS = {
    ("pdf_ops", "save"): [1],
    ("pdf_ops", "open"): [0],
    (None, "open"): [0],
    (None, "save"): [0],
    (None, "PdfDocument"): [0],
    (None, "TTFont"): [0],
    (None, "saveXML"): [0],
    (None, "importXML"): [0],
    (None, "save_as"): [0],
    (None, "to_file"): [0],
    (None, "from_file"): [0],
}
PATH_KEYWORDS = {"path", "filename", "file", "fp", "filename_or_stream", "filepath", "dest", "output", "file_name"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\- ]{0,63}$")
LOOKS_LIKE_PATH = re.compile(r"(^[A-Za-z]:[\\/])|(^[\\/])|(^~)|(\.\.[\\/])|([\\/]\.\.)|(^\\\\)")


class ScriptRejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _safe_filename(value: str) -> bool:
    return bool(SAFE_NAME.match(value)) and ".." not in value and "/" not in value and "\\" not in value


class _Validator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.constants: dict[str, object] = {}

    def fail(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", "?")
        self.problems.append(f"line {line}: {message}")

    # ---- imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_MODULES:
                self.fail(node, f"import of {alias.name!r} is not allowed (allowed: {', '.join(sorted(ALLOWED_MODULES))})")
            if any(part.startswith("_") for part in alias.name.split(".")[1:]) or ".raw" in alias.name or ".internal" in alias.name:
                self.fail(node, f"import of private module {alias.name!r} is not allowed")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.fail(node, "relative imports are not allowed")
            return
        mod = node.module or ""
        root = mod.split(".")[0]
        if root not in ALLOWED_MODULES:
            self.fail(node, f"import from {mod!r} is not allowed (allowed: {', '.join(sorted(ALLOWED_MODULES))})")
        if any(part.startswith("_") for part in mod.split(".")[1:]) or mod.endswith(".raw") or ".internal" in mod:
            self.fail(node, f"import from private module {mod!r} is not allowed")
        for alias in node.names:
            if alias.name == "*":
                self.fail(node, "'import *' is not allowed")
            elif alias.name.startswith("_") or alias.name in BANNED_ATTRIBUTES:
                self.fail(node, f"importing {alias.name!r} is not allowed")

    # ---- names and attributes
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self.fail(node, f"use of {node.id!r} is not allowed")
        elif node.id.startswith("__") and node.id not in ("__name__",):
            self.fail(node, f"use of {node.id!r} is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self.fail(node, f"access to private attribute {node.attr!r} is not allowed")
        elif node.attr in BANNED_ATTRIBUTES:
            self.fail(node, f"access to attribute {node.attr!r} is not allowed")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("__"):
            self.fail(node, f"defining {node.name!r} is not allowed")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.fail(node, "class definitions are not allowed in change scripts")

    def visit_Global(self, node: ast.Global) -> None:
        self.fail(node, "'global' is not allowed")

    def visit_Await(self, node) -> None:
        self.fail(node, "async code is not allowed")

    # ---- constants
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and LOOKS_LIKE_PATH.search(node.value):
            self.fail(node, f"string {node.value[:40]!r} looks like a path outside the working folder")
        if isinstance(node.value, bytes) and LOOKS_LIKE_PATH.search(node.value.decode("latin-1")):
            self.fail(node, "bytes constant looks like a path outside the working folder")

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self.generic_visit(node)

    # ---- file access
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            key_mod, name = None, func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            key_mod = func.value.id if isinstance(func.value, ast.Name) else None
        else:
            key_mod, name = None, None
        positions = PATH_ARGS.get((key_mod, name))
        if positions is None and name is not None:
            positions = PATH_ARGS.get((None, name))
        if positions is not None:
            for pos in positions:
                if pos < len(node.args):
                    self._check_path(node.args[pos], name)
            for kw in node.keywords:
                if kw.arg in PATH_KEYWORDS:
                    self._check_path(kw.value, name)
                if kw.arg is None:
                    self.fail(node, "'**' arguments are not allowed in calls that open or save files")
            if any(isinstance(a, ast.Starred) for a in node.args):
                self.fail(node, "'*' arguments are not allowed in calls that open or save files")
        self.generic_visit(node)

    def _check_path(self, arg: ast.AST, func_name: str) -> None:
        value = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            value = arg.value
        elif isinstance(arg, ast.Name) and isinstance(self.constants.get(arg.id), str):
            value = self.constants[arg.id]
        else:
            self.fail(arg, f"{func_name}() must be given a literal file name such as 'input.pdf' or 'output.pdf'")
            return
        if not _safe_filename(value):
            self.fail(arg, f"file name {value!r} is not a plain name inside the working folder")


def _collect_constants(tree: ast.Module) -> dict[str, object]:
    consts: dict[str, object] = {}
    reassigned: set[str] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.comprehension)):
            targets = [node.target]
        elif isinstance(node, (ast.With,)):
            targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    if n.id in consts or n.id in reassigned:
                        reassigned.add(n.id)
                    elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node in tree.body:
                        consts[n.id] = node.value.value
                    else:
                        reassigned.add(n.id)
    return {k: v for k, v in consts.items() if k not in reassigned}


def validate(source: str, required_strings: list[str] | None = None, max_bytes: int = 64_000) -> list[str]:
    """Return a list of problems (empty when the script may run)."""
    problems: list[str] = []
    if len(source.encode("utf-8")) > max_bytes:
        return [f"script is larger than {max_bytes} bytes"]
    try:
        tree = ast.parse(source, filename="script.py", mode="exec")
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]
    v = _Validator()
    v.constants = _collect_constants(tree)
    v.visit(tree)
    problems.extend(v.problems)
    literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    for name in ("input.pdf", "output.pdf"):
        if name not in literals:
            problems.append(f"the script must use the file name {name!r}")
    for req in required_strings or []:
        if not any(req in lit for lit in literals):
            problems.append(f"the script must contain the exact replacement text {req!r} in a string literal")
    return problems


def check(source: str, required_strings: list[str] | None = None) -> None:
    problems = validate(source, required_strings)
    if problems:
        raise ScriptRejected(problems)
