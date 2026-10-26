"""The document store.

docs/<doc_id>/original.pdf            read-only, SHA-256 recorded in the manifest
docs/<doc_id>/versions/v001.pdf ...    approved versions (read-only)
docs/<doc_id>/pending/<change_id>/     candidate.pdf, report.json, images, scripts, logs
docs/<doc_id>/history/<change_id>/     reports of approved, rejected and manual-review changes
docs/<doc_id>/manifest.json            versions, hashes, changes and approvals
docs/<doc_id>/audit.log                one JSON object per line
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path

DOC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
CHANGE_ID = re.compile(r"^c\d{8}T\d{6}Z-[0-9a-f]{6}$")


class StoreError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _writable(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- paths
    def doc_dir(self, doc_id: str) -> Path:
        if not DOC_ID.match(doc_id or ""):
            raise StoreError(f"invalid document ID {doc_id!r} (use letters, digits, '.', '_' and '-')")
        return self.root / doc_id

    def _check_change_id(self, change_id: str) -> None:
        if not CHANGE_ID.match(change_id or ""):
            raise StoreError(f"invalid change ID {change_id!r}")

    def manifest_path(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "manifest.json"

    def exists(self, doc_id: str) -> bool:
        return self.manifest_path(doc_id).exists()

    # --------------------------------------------------------------- locking
    @contextmanager
    def lock(self, doc_id: str, timeout: float = 30.0):
        path = self.doc_dir(doc_id) / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > 600:  # stale lock from a crashed run
                        path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise StoreError(f"document {doc_id} is locked by another operation")
                time.sleep(0.2)
        try:
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # -------------------------------------------------------------- manifest
    def manifest(self, doc_id: str) -> dict:
        p = self.manifest_path(doc_id)
        if not p.exists():
            raise StoreError(f"document {doc_id!r} has not been ingested")
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save_manifest(self, doc_id: str, manifest: dict) -> None:
        p = self.manifest_path(doc_id)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(tmp, p)

    def audit(self, doc_id: str, event: str, **details) -> None:
        line = {"time": now(), "event": event, **details}
        with open(self.doc_dir(doc_id) / "audit.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")

    # ---------------------------------------------------------------- ingest
    def ingest(self, pdf: str | Path, doc_id: str) -> dict:
        src = Path(pdf)
        if not src.is_file():
            raise StoreError(f"PDF not found: {src}")
        with open(src, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise StoreError(f"{src} is not a PDF file")
        import pikepdf

        try:  # before anything is written to the store
            with pikepdf.open(src) as pdf_doc:
                pages = len(pdf_doc.pages)
        except pikepdf.PasswordError:
            raise StoreError(f"{src.name} is password-protected; supply an unencrypted copy") from None
        except Exception as exc:  # pikepdf.PdfError and friends
            raise StoreError(f"{src.name} is not a readable PDF: {exc}") from None
        d = self.doc_dir(doc_id)
        if self.exists(doc_id):
            raise StoreError(f"document {doc_id!r} already exists")
        (d / "versions").mkdir(parents=True, exist_ok=True)
        (d / "pending").mkdir(exist_ok=True)
        (d / "history").mkdir(exist_ok=True)
        with self.lock(doc_id):
            orig = d / "original.pdf"
            shutil.copyfile(src, orig)
            _read_only(orig)
            digest = sha256_file(orig)
            manifest = {
                "doc_id": doc_id,
                "created": now(),
                "original": {"file": "original.pdf", "sha256": digest, "pages": pages, "source_name": src.name},
                "versions": [],
                "changes": {},
            }
            self._save_manifest(doc_id, manifest)
            self.audit(doc_id, "ingest", sha256=digest, pages=pages, source_name=src.name)
        return {"status": "ingested", "doc_id": doc_id, "sha256": digest, "pages": pages}

    # ------------------------------------------------------------- versions
    def verify_integrity(self, doc_id: str, manifest: dict | None = None) -> None:
        m = manifest or self.manifest(doc_id)
        d = self.doc_dir(doc_id)
        if sha256_file(d / "original.pdf") != m["original"]["sha256"]:
            raise StoreError("original.pdf no longer matches its recorded SHA-256; refusing to continue")
        for v in m["versions"]:
            if sha256_file(d / "versions" / v["file"]) != v["sha256"]:
                raise StoreError(f"{v['file']} no longer matches its recorded SHA-256; refusing to continue")

    def current(self, doc_id: str) -> tuple[Path, str, str]:
        """(path, label, sha256) of the latest approved version, or the original."""
        m = self.manifest(doc_id)
        d = self.doc_dir(doc_id)
        if m["versions"]:
            v = m["versions"][-1]
            return d / "versions" / v["file"], v["version"], v["sha256"]
        return d / "original.pdf", "original", m["original"]["sha256"]

    def new_change(self, doc_id: str) -> tuple[str, Path]:
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        change_id = f"c{stamp}-{secrets.token_hex(3)}"
        path = self.doc_dir(doc_id) / "pending" / change_id
        path.mkdir(parents=True)
        return change_id, path

    def record_change(self, doc_id: str, change_id: str, entry: dict) -> None:
        with self.lock(doc_id):
            m = self.manifest(doc_id)
            m["changes"][change_id] = entry
            self._save_manifest(doc_id, m)

    def finish_manual_review(self, doc_id: str, change_id: str) -> Path:
        """Move a change that produced no candidate from pending/ to history/."""
        src = self.doc_dir(doc_id) / "pending" / change_id
        dst = self.doc_dir(doc_id) / "history" / change_id
        for f in src.glob("candidate*.pdf"):
            f.unlink()
        shutil.move(str(src), str(dst))
        return dst

    # ------------------------------------------------------- approve/reject
    def _pending(self, doc_id: str, change_id: str) -> tuple[dict, dict, Path]:
        self._check_change_id(change_id)
        m = self.manifest(doc_id)
        entry = m["changes"].get(change_id)
        if entry is None:
            raise StoreError(f"change {change_id} not found for document {doc_id}")
        if entry.get("status") != "updated" or entry.get("decision"):
            raise StoreError(f"change {change_id} is not waiting for review (status: {entry.get('decision') or entry.get('status')})")
        path = self.doc_dir(doc_id) / "pending" / change_id
        if not (path / "candidate.pdf").exists():
            raise StoreError(f"candidate PDF for change {change_id} is missing")
        return m, entry, path

    def approve(self, doc_id: str, change_id: str, approver: str | None = None, note: str | None = None) -> dict:
        with self.lock(doc_id):
            m, entry, path = self._pending(doc_id, change_id)
            self.verify_integrity(doc_id, m)
            _, label, base_sha = self.current(doc_id)
            if entry.get("base_sha256") != base_sha:
                raise StoreError(
                    f"change {change_id} was made on {entry.get('base_version')} but the latest version is {label}; "
                    "reject it and run the change again on the latest version")
            cand = path / "candidate.pdf"
            if sha256_file(cand) != entry.get("candidate_sha256"):
                raise StoreError("the candidate PDF was modified after it was checked; reject it and run the change again")
            number = len(m["versions"]) + 1
            name = f"v{number:03d}.pdf"
            dst = self.doc_dir(doc_id) / "versions" / name
            shutil.move(str(cand), str(dst))
            _read_only(dst)
            digest = sha256_file(dst)
            version = {"version": f"v{number:03d}", "file": name, "sha256": digest, "created": now(),
                       "change_id": change_id, "based_on": entry.get("base_version"), "approved_by": approver,
                       "note": note}
            m["versions"].append(version)
            entry.update({"decision": "approved", "decided": now(), "decided_by": approver, "version": version["version"]})
            hist = self.doc_dir(doc_id) / "history" / change_id
            shutil.move(str(path), str(hist))
            m["changes"][change_id] = entry
            self._save_manifest(doc_id, m)
            self.audit(doc_id, "approve", change_id=change_id, version=version["version"], sha256=digest, approver=approver)
        return {"status": "approved", "doc_id": doc_id, "change_id": change_id, "version": version["version"],
                "path": str(dst), "sha256": digest}

    def reject(self, doc_id: str, change_id: str, approver: str | None = None, note: str | None = None) -> dict:
        with self.lock(doc_id):
            m, entry, path = self._pending(doc_id, change_id)
            (path / "candidate.pdf").unlink()
            entry.update({"decision": "rejected", "decided": now(), "decided_by": approver, "note": note})
            hist = self.doc_dir(doc_id) / "history" / change_id
            shutil.move(str(path), str(hist))
            m["changes"][change_id] = entry
            self._save_manifest(doc_id, m)
            self.audit(doc_id, "reject", change_id=change_id, approver=approver, note=note)
        return {"status": "rejected", "doc_id": doc_id, "change_id": change_id}

    def status(self, doc_id: str) -> dict:
        m = self.manifest(doc_id)
        path, label, sha = self.current(doc_id)
        keys = ("status", "created", "find", "replace", "method", "kind", "page", "recommendation", "edits")
        pending = [dict(change_id=k, **{x: v[x] for x in keys if v.get(x) is not None})
                   for k, v in m["changes"].items() if v.get("status") == "updated" and not v.get("decision")]
        return {
            "doc_id": doc_id,
            "current_version": label,
            "current_path": str(path),
            "current_sha256": sha,
            "original_sha256": m["original"]["sha256"],
            "versions": m["versions"],
            "pending": pending,
            "changes": len(m["changes"]),
        }
