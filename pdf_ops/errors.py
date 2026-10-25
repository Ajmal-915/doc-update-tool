"""Exceptions raised by pdf_ops.

Every error carries a short machine-readable ``code`` so that callers (the
pipeline, generated scripts) can turn it into a manual-review reason without
parsing the message text.
"""

from __future__ import annotations


class PdfOpsError(Exception):
    code = "pdf_ops_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": _jsonable(self.details)}


class TargetNotFoundError(PdfOpsError):
    code = "not_found"


class AmbiguousTargetError(PdfOpsError):
    code = "ambiguous"


class MissingGlyphsError(PdfOpsError):
    code = "missing_glyphs"

    def __init__(self, message: str, missing: list[str], **details):
        super().__init__(message, missing=missing, **details)
        self.missing = missing


class DoesNotFitError(PdfOpsError):
    code = "does_not_fit"


class UnsupportedError(PdfOpsError):
    code = "unsupported"


class SelfCheckError(PdfOpsError):
    """The edit engine re-read its own output and it did not match the plan."""

    code = "self_check_failed"


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
