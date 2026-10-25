"""Code that runs first inside the sandbox process (``python -I bootstrap.py LIB SCRIPT``).

It puts the pdf_ops copy on sys.path, imports the allowed libraries, then
installs an audit hook that refuses file access outside the working folder
and any process, network or native-library activity, and finally runs the
script.  This is a second line of defence behind the static check, not a
security boundary.
"""

BOOTSTRAP = r'''
import sys, os
LIB, SCRIPT = sys.argv[1], sys.argv[2]
WORK = os.path.realpath(os.getcwd())
sys.path.insert(0, LIB)
sys.dont_write_bytecode = True
with open(SCRIPT, "r", encoding="utf-8") as _fh:
    _SOURCE = _fh.read()
_CODE = compile(_SOURCE, "script.py", "exec")
import json, math, re, decimal
import pikepdf, fontTools, fontTools.ttLib, fontTools.cffLib, pdfminer, pdfplumber, pypdfium2
import pdf_ops
_READ_OK = []
for _p in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, LIB, WORK} | set(sys.path):
    if _p and os.path.isabs(_p):
        _READ_OK.append(os.path.realpath(_p))
_SEP = os.sep
_BLOCK_PREFIXES = ("subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty",
                   "os.kill", "os.startfile", "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace",
                   "os.chmod", "os.chown", "os.symlink", "os.link", "os.truncate", "shutil.", "socket.",
                   "urllib.", "http.", "ftplib.", "smtplib.", "ctypes.dlopen", "ctypes.dlsym", "winreg.",
                   "_winapi.", "msvcrt.", "webbrowser.", "sys.settrace", "sys.setprofile", "sys.addaudithook")
def _inside(path, roots):
    path = os.path.normcase(path)
    return any(path == os.path.normcase(r) or path.startswith(os.path.normcase(r).rstrip(_SEP) + _SEP) for r in roots)
def _hook(event, args):
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if path is None or isinstance(path, int):
            return
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        real = os.path.realpath(os.fspath(path))
        writing = bool(mode and any(c in str(mode) for c in "wax+")) or bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if writing:
            if not _inside(real, [WORK]):
                raise PermissionError("sandbox: writing outside the working folder is not allowed: " + real)
        elif not _inside(real, _READ_OK):
            raise PermissionError("sandbox: reading outside the working folder is not allowed: " + real)
        return
    if event.startswith(_BLOCK_PREFIXES):
        raise PermissionError("sandbox: operation not allowed: " + event)
sys.addaudithook(_hook)
del _fh
exec(_CODE, {"__name__": "__main__", "__file__": "script.py", "__builtins__": __builtins__})
'''
