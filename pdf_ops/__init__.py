"""pdf_ops: helper library for safe, minimal text edits inside existing PDFs.

Generated change scripts import this module.  Typical use::

    import json
    import pdf_ops

    doc = pdf_ops.open("input.pdf")
    target = pdf_ops.find_target(doc, 2, "31 December 2023", near=[72, 600, 300, 615])
    result = pdf_ops.replace_in_place(doc, target, "31 December 2024")
    pdf_ops.save(doc, "output.pdf")
    print(json.dumps(result.to_dict()))
"""

from .document import Document, open, save  # noqa: A004
from .errors import (
    AmbiguousTargetError,
    DoesNotFitError,
    MissingGlyphsError,
    PdfOpsError,
    SelfCheckError,
    TargetNotFoundError,
    UnsupportedError,
)
from .fonts import PdfFont, glyphs_available, measure_text
from .forms import find_field, list_fields, update_form_field
from .methods import (
    Fit,
    MethodResult,
    expand_horizontal,
    fit_horizontal,
    fit_in_place,
    fit_scaled,
    remove_text_run,
    replace_in_place,
    replace_with_scale,
    write_text,
)
from .reflow import fit_reflow, reflow_paragraph
from .target import Target, find_target

__version__ = "1.0.0"

__all__ = [
    "AmbiguousTargetError", "Document", "DoesNotFitError", "Fit", "MethodResult", "MissingGlyphsError",
    "PdfFont", "PdfOpsError", "SelfCheckError", "Target", "TargetNotFoundError", "UnsupportedError",
    "expand_horizontal", "find_target", "fit_horizontal", "fit_in_place", "fit_scaled", "glyphs_available",
    "measure_text", "open", "remove_text_run", "replace_in_place", "replace_with_scale", "save", "write_text",
    "find_field", "list_fields", "update_form_field", "fit_reflow", "reflow_paragraph",
]
