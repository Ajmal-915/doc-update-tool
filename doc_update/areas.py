"""The allowed change area for each method (used by the step 8 checks).

The area is computed here from the measurements and the method, never taken
from what a generated script claims it did.
"""

from __future__ import annotations

from pdf_ops.geometry import Rect
from pdf_ops.target import Target

from .checks import CheckSpec

PAD = 0.25


def target_boxes(target: Target) -> list[Rect]:
    return [Rect(p.x0, p.bbox.y0, p.x1, p.bbox.y1).expand(0.3) for p in target.parts]


def change_area(method: str, target: Target | None, params: dict, field_rect: Rect | None = None) -> tuple[list[Rect], list[Rect]]:
    """(areas, boundary) for a method.

    params: for horizontal_expansion ``boundary`` (x); for paragraph_reflow
    ``area`` ([x0, y0, x1, y1]).
    """
    if method == "form_field_update":
        r = field_rect.expand(PAD)
        return [r], [r]
    assert target is not None
    if method in ("original_box", "reduced_font"):
        rects = [Rect(p.x0, p.bbox.y0, p.x1, p.bbox.y1).expand(PAD) for p in target.parts]
        return rects, rects
    if method == "horizontal_expansion":
        bx = float(params["boundary"])
        rects = []
        for p in target.parts:
            ink = p.bbox
            if p.after:
                # the rest of the line moves with the replacement: its old and new places are both inside
                ink = ink.union(Rect(p.x0, p.bbox.y0, max(p.line_x1, p.x1), p.bbox.y1))
            rects.append(Rect(p.x0 - PAD, ink.y0 - PAD, max(bx, p.x1, p.line_x1 if p.after else p.x1) + PAD,
                              ink.y1 + PAD))
        return rects, rects
    if method == "paragraph_reflow":
        r = Rect.from_list(params["area"])
        return [r], [r]
    raise ValueError(f"unknown method {method}")


def check_spec(method: str, target: Target | None, replacement: str, params: dict, min_font_scale: float,
               page_index: int, field_name: str | None = None, field_rect: Rect | None = None,
               expected_field_value: str | None = None, original_size: float | None = None,
               target_text: str | None = None) -> CheckSpec:
    areas, boundary = change_area(method, target, params, field_rect)
    return CheckSpec(
        page_index=page_index,
        method=method,
        areas=areas,
        boundary=boundary,
        target_boxes=target_boxes(target) if target is not None else [field_rect] if field_rect else [],
        target_text=target_text if target_text is not None else (target.text if target else ""),
        replacement=" ".join(replacement.split()),
        original_size=original_size if original_size is not None else (target.effective_size if target else 0.0),
        min_font_scale=min_font_scale,
        field_name=field_name,
        expected_field_value=expected_field_value,
    )
