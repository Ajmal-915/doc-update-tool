"""The pdf_ops reference given to the script-writing model."""

PDF_OPS_REFERENCE = '''
pdf_ops - helper library for minimal, checked text edits (import pdf_ops)

  doc = pdf_ops.open("input.pdf")                      -> Document
  pdf_ops.save(doc, "output.pdf")                      write the result (never overwrite input.pdf)

  target = pdf_ops.find_target(doc, page, text, near=[x0, y0, x1, y1])
      page: page number from 1.  text: the exact target text as it appears in the PDF.
      near: box in PDF points (origin bottom-left) used to pick the right occurrence.
      Raises pdf_ops.TargetNotFoundError / pdf_ops.AmbiguousTargetError.

  Methods (each returns a MethodResult; call result.to_dict() for the JSON line):
  a) pdf_ops.replace_in_place(doc, target, replacement)
        same font and size, inside the original box; following text keeps its exact position
  b) pdf_ops.expand_horizontal(doc, target, replacement, boundary=X)
        the line may grow up to x = X (points); text after the target on the same line moves with it
  c) pdf_ops.replace_with_scale(doc, target, replacement, min_scale=S, scale=None)
        smaller size for the replaced text only; never below min_scale; scale=None picks the largest that fits
  d) pdf_ops.reflow_paragraph(doc, target, replacement, boundary=X, max_bottom=Y)
        rewraps the paragraph from the target line on, same font, size and line spacing, within the
        paragraph's left edge and X; it may use space down to y = Y; never moves other content
  e) pdf_ops.update_form_field(doc, field_name, new_value, min_scale=S)
        sets a text field's value and regenerates its appearance; returns a dict.
        pdf_ops.find_field(doc, field_name).value is the current value; build the new value by
        replacing the given old_value_span [start, end] with the replacement

  Measuring:
     pdf_ops.fit_in_place(doc, target, replacement) / fit_horizontal(doc, target, replacement, boundary)
     pdf_ops.fit_scaled(doc, target, replacement, min_scale) -> Fit(feasible, reason, ...)
     pdf_ops.measure_text(target.font, text, size) -> width in points at that size (text space)
     pdf_ops.glyphs_available(target.font, text) -> (ok, missing_characters)

  Lower level (only when the methods above do not cover the case):
     pdf_ops.remove_text_run(doc, target)              remove the target glyphs, keep everything else in place
     pdf_ops.write_text(doc, like_target, text, x, y)  draw text in the same font resource and colour

  Errors: pdf_ops.PdfOpsError and subclasses DoesNotFitError, MissingGlyphsError, UnsupportedError,
  TargetNotFoundError, AmbiguousTargetError, SelfCheckError (all have .code and .message).

Rules for the script:
  * read ./input.pdf, write ./output.pdf, print exactly one JSON line describing what was done
  * allowed imports: pdf_ops, pikepdf, fontTools, pypdfium2, pdfplumber, math, json, re, decimal
  * no exec/eval/compile/__import__/getattr, no private or dunder attributes, no other files
  * file names must be literal strings ("input.pdf", "output.pdf")
  * the replacement text must appear in the script exactly as given, as a string literal
  * never draw boxes or shapes, never add fonts, never change other text
  * if the method cannot work, let the pdf_ops exception propagate (do not catch and hide it)
'''

TEMPLATE = '''import json
import pdf_ops

PAGE = {page}
TARGET_TEXT = {target_text}
NEAR = {near}
REPLACEMENT = {replacement}

doc = pdf_ops.open("input.pdf")
target = pdf_ops.find_target(doc, PAGE, TARGET_TEXT, near=NEAR)
result = {call}
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result.to_dict()))
'''

FIELD_TEMPLATE = '''import json
import pdf_ops

FIELD_NAME = {field}
OLD_TEXT = {old}
START, END = {span}
REPLACEMENT = {replacement}

doc = pdf_ops.open("input.pdf")
value = pdf_ops.find_field(doc, FIELD_NAME).value
if value[START:END] != OLD_TEXT:
    raise pdf_ops.TargetNotFoundError("the field value changed")
NEW_VALUE = value[:START] + REPLACEMENT + value[END:]
result = pdf_ops.update_form_field(doc, FIELD_NAME, NEW_VALUE, min_scale={min_scale})
pdf_ops.save(doc, "output.pdf")
print(json.dumps(result))
'''


def method_call(method: str, params: dict) -> str:
    if method == "original_box":
        return "pdf_ops.replace_in_place(doc, target, REPLACEMENT)"
    if method == "horizontal_expansion":
        return f"pdf_ops.expand_horizontal(doc, target, REPLACEMENT, boundary={params['boundary']!r})"
    if method == "reduced_font":
        scale = params.get("scale")
        return f"pdf_ops.replace_with_scale(doc, target, REPLACEMENT, min_scale={params['min_scale']!r}, scale={scale!r})"
    if method == "paragraph_reflow":
        return (f"pdf_ops.reflow_paragraph(doc, target, REPLACEMENT, boundary={params['boundary']!r}, "
                f"max_bottom={params['max_bottom']!r})")
    raise ValueError(method)


def render_template(method: str, ctx: dict) -> str:
    if method == "form_field_update":
        return FIELD_TEMPLATE.format(field=repr(ctx["field_name"]), old=repr(ctx["old_text"]),
                                     span=tuple(ctx["old_value_span"]), replacement=repr(ctx["replacement"]),
                                     min_scale=repr(ctx["params"]["min_scale"]))
    return TEMPLATE.format(page=ctx["page"], target_text=repr(ctx["target_text"]), near=repr(ctx["near"]),
                           replacement=repr(ctx["replacement"]), call=method_call(method, ctx["params"]))
