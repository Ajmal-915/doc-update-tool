"""Page rendering with pypdfium2 and conversions between PDF points and pixels."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from pdf_ops.geometry import Rect

DEFAULT_DPI = 150


@dataclass
class PageFrame:
    """How page coordinates map to rendered pixels (CropBox and /Rotate aware)."""

    crop: Rect
    rotate: int
    scale: float  # pixels per point

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        c = self.crop
        s = self.scale
        r = self.rotate % 360
        if r == 0:
            return (x - c.x0) * s, (c.y1 - y) * s
        if r == 90:
            return (y - c.y0) * s, (x - c.x0) * s
        if r == 180:
            return (c.x1 - x) * s, (y - c.y0) * s
        return (c.y1 - y) * s, (c.x1 - x) * s

    def rect_to_px(self, rect: Rect) -> tuple[int, int, int, int]:
        pts = [self.to_px(rect.x0, rect.y0), self.to_px(rect.x1, rect.y1)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return int(np.floor(min(xs))), int(np.floor(min(ys))), int(np.ceil(max(xs))), int(np.ceil(max(ys)))


def page_frames(path: str | Path, dpi: int = DEFAULT_DPI) -> list[PageFrame]:
    out = []
    with pikepdf.open(str(path)) as pdf:
        for page in pdf.pages:
            box = page.obj.get("/CropBox") or _inherited(page.obj, "/MediaBox")
            rect = Rect.from_list([float(v) for v in box])
            rot = int(_inherited(page.obj, "/Rotate") or 0)
            out.append(PageFrame(rect, rot, dpi / 72.0))
    return out


def _inherited(obj, key):
    node = obj
    for _ in range(64):
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def render_pages(path: str | Path, dpi: int = DEFAULT_DPI, pages: list[int] | None = None) -> dict[int, np.ndarray]:
    """Render pages (0-based indices) to RGB arrays, annotations included."""
    doc = pdfium.PdfDocument(str(path))
    try:
        doc.init_forms()  # widget appearance streams are only drawn with a form environment
        out = {}
        indices = range(len(doc)) if pages is None else pages
        for i in indices:
            page = doc[i]
            bitmap = page.render(scale=dpi / 72.0, draw_annots=True, may_draw_forms=True, rev_byteorder=True)
            arr = bitmap.to_numpy().copy()
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, axis=-1)
            out[i] = arr[:, :, :3]
            page.close()
        return out
    finally:
        doc.close()


def save_png(arr: np.ndarray, path: str | Path) -> None:
    Image.fromarray(arr.astype(np.uint8)).save(str(path))


def draw_boxes(arr: np.ndarray, frame: PageFrame, boxes: list[tuple[Rect, tuple]], width: int = 2) -> Image.Image:
    img = Image.fromarray(arr.astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(img)
    for rect, color in boxes:
        x0, y0, x1, y1 = frame.rect_to_px(rect)
        d.rectangle([x0, y0, x1, y1], outline=color, width=width)
    return img


def diff_image(before: np.ndarray, after: np.ndarray) -> Image.Image:
    """Grey copy of 'after' with changed pixels in red."""
    h = max(before.shape[0], after.shape[0])
    w = max(before.shape[1], after.shape[1])
    b = np.full((h, w, 3), 255, np.uint8)
    a = np.full((h, w, 3), 255, np.uint8)
    b[: before.shape[0], : before.shape[1]] = before
    a[: after.shape[0], : after.shape[1]] = after
    changed = (a != b).any(axis=2)
    grey = (0.3 * a[:, :, 0] + 0.59 * a[:, :, 1] + 0.11 * a[:, :, 2]).astype(np.uint8)
    out = np.stack([grey] * 3, axis=-1)
    out = (out * 0.5 + 127).astype(np.uint8)
    out[changed] = [220, 0, 0]
    return Image.fromarray(out)


def crop(img: Image.Image, frame: PageFrame, rect: Rect, margin_pt: float = 24) -> Image.Image:
    x0, y0, x1, y1 = frame.rect_to_px(rect.expand(margin_pt * 2.5, margin_pt))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return img
    return img.crop((x0, y0, x1, y1))
