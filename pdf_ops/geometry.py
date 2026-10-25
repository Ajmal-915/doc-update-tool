"""Small affine-matrix and rectangle helpers (PDF convention: [a b c d e f])."""

from __future__ import annotations

import math
from dataclasses import dataclass

Matrix = tuple  # (a, b, c, d, e, f) floats

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat(values) -> Matrix:
    a, b, c, d, e, f = (float(v) for v in values)
    return (a, b, c, d, e, f)


def mul(m1: Matrix, m2: Matrix) -> Matrix:
    """Return m1 x m2 (apply m1 first, then m2), as in the PDF specification."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def apply_vector(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, _, _ = m
    return (a * x + c * y, b * x + d * y)


def inverse(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise ZeroDivisionError("singular matrix")
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def translate(tx: float, ty: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))


def vertical_scale(m: Matrix) -> float:
    """Length of the transformed unit y vector: the effective size multiplier."""
    _, _, c, d, _, _ = m
    return math.hypot(c, d)


def horizontal_scale(m: Matrix) -> float:
    a, b, _, _, _, _ = m
    return math.hypot(a, b)


def is_axis_aligned_upright(m: Matrix, tol: float = 1e-6) -> bool:
    a, b, c, d, _, _ = m
    return abs(b) <= tol * max(1.0, abs(a)) and abs(c) <= tol * max(1.0, abs(d)) and a > 0 and d > 0


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @staticmethod
    def from_points(points) -> "Rect":
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def union(self, other: "Rect") -> "Rect":
        return Rect(min(self.x0, other.x0), min(self.y0, other.y0), max(self.x1, other.x1), max(self.y1, other.y1))

    def intersects(self, other: "Rect", tol: float = 0.0) -> bool:
        return not (
            self.x1 <= other.x0 + tol
            or other.x1 <= self.x0 + tol
            or self.y1 <= other.y0 + tol
            or other.y1 <= self.y0 + tol
        )

    def contains(self, other: "Rect", tol: float = 0.0) -> bool:
        return (
            other.x0 >= self.x0 - tol
            and other.y0 >= self.y0 - tol
            and other.x1 <= self.x1 + tol
            and other.y1 <= self.y1 + tol
        )

    def contains_point(self, x: float, y: float, tol: float = 0.0) -> bool:
        return self.x0 - tol <= x <= self.x1 + tol and self.y0 - tol <= y <= self.y1 + tol

    def expand(self, dx: float, dy: float | None = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def transform(self, m: Matrix) -> "Rect":
        pts = [apply(m, x, y) for x, y in ((self.x0, self.y0), (self.x1, self.y0), (self.x0, self.y1), (self.x1, self.y1))]
        return Rect.from_points(pts)

    def as_list(self, ndigits: int = 3) -> list[float]:
        return [round(self.x0, ndigits), round(self.y0, ndigits), round(self.x1, ndigits), round(self.y1, ndigits)]

    @staticmethod
    def from_list(values) -> "Rect":
        x0, y0, x1, y1 = (float(v) for v in values)
        return Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def union_all(rects) -> Rect | None:
    out = None
    for r in rects:
        out = r if out is None else out.union(r)
    return out
