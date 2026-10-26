"""The safety gate: decides whether a candidate PDF is an acceptable edit.

This code is separate from both the generated script and the LLM.  It reads
the original and the candidate with its own engines (pdfminer for text,
pdfplumber for graphics, pypdfium2 for pixels, pikepdf for structure) and
rejects the candidate unless every check passes.
"""

from __future__ import annotations

import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pikepdf

from pdf_ops.geometry import Rect

from ..render import diff_image, draw_boxes, page_frames, render_pages, crop
from .extract import Extraction, GlyphChecker, XChar, extract
from .structure import Snapshot, graphics_rects, image_rects, page_hashes, snapshot

POS_TOL = 0.05  # points: positions of unchanged characters
SIZE_TOL = 0.01


@dataclass
class CheckSpec:
    page_index: int
    method: str
    areas: list  # list[Rect], allowed change area
    boundary: list  # list[Rect], new / moved text must stay inside
    target_boxes: list  # list[Rect], boxes of the original target glyphs
    target_text: str
    replacement: str
    original_size: float
    min_font_scale: float = 0.9
    field_name: str | None = None
    expected_field_value: str | None = None
    dpi: int = 150
    pixel_margin: int = 2

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("areas", "boundary", "target_boxes"):
            d[k] = [r.as_list() if isinstance(r, Rect) else list(r) for r in getattr(self, k)]
        return d


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "message": self.message, "details": self.details}


@dataclass
class CheckReport:
    passed: bool
    results: list[CheckResult]
    images: dict = field(default_factory=dict)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        if self.passed:
            return "all checks passed"
        return "; ".join(f"{r.name}: {r.message}" for r in self.failures)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "summary": self.summary(), "results": [r.to_dict() for r in self.results],
                "images": self.images}


# ------------------------------------------------------------------ helpers
def _in_any(ch: XChar, rects: list[Rect], tol: float = 0.0) -> bool:
    return any(r.contains_point(ch.cx, ch.oy + 0.3 * ch.size, tol) for r in rects)


POSITION_TOLERANCE = 0.005  # points; far below anything visible
RECHECK_PIXELS = 400  # outside-area pixel changes up to this many are re-checked at a second resolution


def _same_chars(before: list[XChar], after: list[XChar], tol: float = POSITION_TOLERANCE) -> bool:
    """Same glyphs (text, font, size, render mode, colour) at the same positions, within ``tol``.

    Rounded keys alone are not enough: a 0.0002 pt difference can straddle a rounding boundary.
    """
    def groups(chars):
        out: dict = {}
        for c in chars:
            k = c.key()
            out.setdefault((k[0],) + k[3:], []).append((c.oy, c.ox))
        return out

    gb, ga = groups(before), groups(after)
    if gb.keys() != ga.keys():
        return False
    for k, pb in gb.items():
        pa = ga[k]
        if len(pa) != len(pb):
            return False
        for (yb, xb), (ya, xa) in zip(sorted(pb), sorted(pa)):
            if abs(yb - ya) > tol or abs(xb - xa) > tol:
                return False
    return True


def _diff_keys(before: list[XChar], after: list[XChar], limit: int = 12) -> dict:
    from collections import Counter

    cb = Counter(c.key() for c in before)
    ca = Counter(c.key() for c in after)
    missing = list((cb - ca).elements())
    extra = list((ca - cb).elements())
    return {
        "missing_or_changed": [_fmt_key(k) for k in sorted(missing, key=lambda k: (-k[2], k[1]))[:limit]],
        "new_or_moved": [_fmt_key(k) for k in sorted(extra, key=lambda k: (-k[2], k[1]))[:limit]],
        "counts": [len(missing), len(extra)],
    }


def _fmt_key(k) -> str:
    return f"{k[0]!r} at ({k[1]}, {k[2]}) {k[3]} {k[4]}pt"


def reading_text(chars: list[XChar]) -> tuple[str, list[int]]:
    """Text of characters in reading order, with per-output-char source index."""
    items = [(i, c) for i, c in enumerate(chars)]
    items.sort(key=lambda t: (-t[1].oy, t[1].ox))
    lines: list[list[tuple[int, XChar]]] = []
    for i, c in items:
        if lines and abs(lines[-1][0][1].oy - c.oy) <= 0.3 * max(c.size, 1):
            lines[-1].append((i, c))
        else:
            lines.append([(i, c)])
    out: list[str] = []
    idx: list[int] = []
    for li, line in enumerate(lines):
        line.sort(key=lambda t: t[1].ox)
        if li > 0:
            prev = out[-1] if out else ""
            first = line[0][1].text
            if prev in ("-", "­") and first[:1].islower():
                out.pop()
                idx.pop()
            else:
                out.append(" ")
                idx.append(-1)
        last = None
        for i, c in line:
            if last is not None and not c.text.isspace() and not last.text.isspace():
                if c.x0 - last.x1 > 0.15 * max(c.size, 1):
                    out.append(" ")
                    idx.append(-1)
            out.append(c.text)
            idx.append(i)
            last = c
    return "".join(out), idx


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _overlap_area(a: tuple, b: tuple) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _overlap(a: tuple, b: tuple, tol: float = 0.1) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > tol and min(a[3], b[3]) - max(a[1], b[1]) > tol


# ------------------------------------------------------------------- checks
class _Run:
    def __init__(self, before: Path, after: Path, spec: CheckSpec, out_dir: Path | None):
        self.before, self.after, self.spec, self.out_dir = before, after, spec, out_dir
        self.results: list[CheckResult] = []
        self.images: dict = {}

    def add(self, name: str, passed: bool, message: str = "", **details):
        self.results.append(CheckResult(name, bool(passed), message if not passed or message else "ok", details))

    def run(self) -> CheckReport:
        spec = self.spec
        try:
            hb, ha = page_hashes(self.before), page_hashes(self.after)
            # pages whose whole object graph is unchanged are identical by construction;
            # everything else (and always the target page) is extracted and rendered
            self.pages = {i for i in range(max(len(hb), len(ha))) if i >= len(hb) or i >= len(ha) or hb[i] != ha[i]}
            self.pages.add(spec.page_index)
            self.pages = {i for i in self.pages if i < min(len(hb), len(ha))}
            self.unchanged_pages = min(len(hb), len(ha)) - len(self.pages)
            self.ex_b, self.ex_a = extract(self.before, self.pages), extract(self.after, self.pages)
            self.snap_b, self.snap_a = snapshot(self.before, self.pages), snapshot(self.after, self.pages)
        except Exception as exc:  # unreadable candidate
            self.add("readable", False, f"the candidate PDF cannot be read: {exc}")
            return CheckReport(False, self.results)
        self.add("readable", True)
        if not self._page_structure():
            return CheckReport(False, self.results)
        self.frames = page_frames(self.before, spec.dpi)
        self.r_b = render_pages(self.before, spec.dpi, sorted(self.pages))
        self.r_a = render_pages(self.after, spec.dpi, sorted(self.pages))
        self._other_pages()
        self._text_outside()
        self._render_outside()
        self._graphics_images()
        self._document_level()
        self._fonts()
        self._annotations_fields()
        if spec.method == "form_field_update":
            self._field_appearance()
        else:
            self._text_inside()
        self._write_images()
        passed = all(r.passed for r in self.results)
        return CheckReport(passed, self.results, self.images)

    # -- structure
    def _page_structure(self) -> bool:
        b, a = self.snap_b, self.snap_a
        if len(b.page_boxes) != len(a.page_boxes):
            self.add("page_count", False, f"page count changed from {len(b.page_boxes)} to {len(a.page_boxes)}")
            return False
        self.add("page_count", True)
        changed = [i + 1 for i, (x, y) in enumerate(zip(b.page_boxes, a.page_boxes)) if x != y]
        self.add("page_sizes", not changed, f"page size, crop box or rotation changed on page(s) {changed}" if changed else "")
        if not 0 <= self.spec.page_index < len(b.page_boxes):
            self.add("target_page", False, "target page does not exist")
            return False
        return True

    def _other_pages(self):
        bad_text, bad_px = [], []
        for i in sorted(self.pages):
            if i == self.spec.page_index:
                continue
            if not _same_chars(self.ex_b.pages.get(i, []), self.ex_a.pages.get(i, [])):
                bad_text.append(i + 1)
            rb, ra = self.r_b[i], self.r_a[i]
            if rb.shape != ra.shape or not np.array_equal(rb, ra):
                bad_px.append(i + 1)
        self.add("other_pages_text_identical", not bad_text, f"text changed on other page(s) {bad_text}" if bad_text else "",
                 pages_unchanged_by_hash=self.unchanged_pages, pages_compared=len(self.pages) - 1)
        self.add("other_pages_render_identical", not bad_px, f"rendering changed on other page(s) {bad_px}" if bad_px else "")

    def _text_outside(self):
        p = self.spec.page_index
        areas = self.spec.areas
        before = [c for c in self.ex_b.pages.get(p, []) if not _in_any(c, areas)]
        after = [c for c in self.ex_a.pages.get(p, []) if not _in_any(c, areas)]
        same = _same_chars(before, after)
        self.add("text_outside_area_identical", same,
                 "text outside the allowed area changed (words moved, spacing changed, lines lost or added)" if not same else "",
                 **({} if same else _diff_keys(before, after)))

    def _mask(self, page: int, shape, frame=None) -> np.ndarray:
        """True where pixels are allowed to change."""
        mask = np.zeros(shape[:2], bool)
        m = self.spec.pixel_margin
        frame = frame or self.frames[page]
        for r in self.spec.areas:
            x0, y0, x1, y1 = frame.rect_to_px(r)
            mask[max(0, y0 - m):max(0, y1 + m), max(0, x0 - m):max(0, x1 + m)] = True
        return mask

    def _render_outside(self):
        p = self.spec.page_index
        rb, ra = self.r_b[p], self.r_a[p]
        if rb.shape != ra.shape:
            self.add("render_outside_area_identical", False, "rendered page size changed")
            return
        changed = (rb != ra).any(axis=2)
        mask = self._mask(p, rb.shape)
        outside = changed & ~mask
        n = int(outside.sum())
        details = {}
        if 0 < n <= RECHECK_PIXELS and self._outside_clean_at(p, round(self.spec.dpi * 1.37)):
            # a glyph that sits almost exactly on a pixel-rounding boundary can flip its anti-aliasing when
            # the content stream around it is rewritten (positions equal to ~1e-5 pt); a real change shows
            # at any resolution, a rounding flip does not
            self.add("render_outside_area_identical", True, "", antialiasing_only_pixels=n,
                     clean_at_dpi=round(self.spec.dpi * 1.37))
            n = -1
        if n > 0:
            ys, xs = np.nonzero(outside)
            frame = self.frames[p]
            details = {"changed_pixels": n, "pixel_box": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
                       "approx_points": [round(frame.crop.x0 + xs.min() / frame.scale, 1), round(frame.crop.y1 - ys.max() / frame.scale, 1),
                                         round(frame.crop.x0 + xs.max() / frame.scale, 1), round(frame.crop.y1 - ys.min() / frame.scale, 1)]}
        if n >= 0:
            self.add("render_outside_area_identical", n == 0,
                     f"{n} pixels changed outside the allowed area" if n else "", **details)
        inside_changed = int((changed & mask).sum())
        if self.spec.replacement != self.spec.target_text:
            self.add("render_inside_area_changed", inside_changed > 0,
                     "nothing visible changed inside the area although the text should have changed" if not inside_changed else "",
                     changed_pixels=inside_changed)

    def _outside_clean_at(self, p: int, dpi: int) -> bool:
        rb = render_pages(self.before, dpi, [p])[p]
        ra = render_pages(self.after, dpi, [p])[p]
        if rb.shape != ra.shape:
            return False
        frame = page_frames(self.before, dpi)[p]
        return not ((rb != ra).any(axis=2) & ~self._mask(p, rb.shape, frame)).any()

    def _graphics_images(self):
        diff_g = [i + 1 for i, (x, y) in enumerate(zip(self.snap_b.graphics_per_page, self.snap_a.graphics_per_page)) if x != y]
        self.add("no_new_or_changed_graphics", not diff_g,
                 f"vector graphics (boxes, lines, fills) were added or changed on page(s) {diff_g}; covering text is not allowed" if diff_g else "")
        diff_i = [i + 1 for i, (x, y) in enumerate(zip(self.snap_b.images_per_page, self.snap_a.images_per_page)) if x != y]
        self.add("images_unchanged", not diff_i, f"images were added, removed or moved on page(s) {diff_i}" if diff_i else "")

    def _document_level(self):
        b, a = self.snap_b.document_level, self.snap_a.document_level
        changed = sorted(k for k in set(b) | set(a) if b.get(k) != a.get(k))
        self.add("document_level_unchanged", not changed,
                 "document-level objects changed (actions, outlines, metadata, attachments or form settings): "
                 + ", ".join(changed[:8]) if changed else "")
        pages = [i + 1 for i, (x, y) in enumerate(zip(self.snap_b.page_attributes, self.snap_a.page_attributes)) if x != y]
        self.add("page_attributes_unchanged", not pages,
                 f"page attributes (actions, boxes, transparency group, ...) changed on page(s) {pages}" if pages else "")

    def _fonts(self):
        b, a = self.snap_b, self.snap_a
        new = a.all_fonts - b.all_fonts
        per_page = [i + 1 for i, (x, y) in enumerate(zip(b.fonts_per_page, a.fonts_per_page)) if not y <= x]
        ok = not new and not per_page and a.font_objects <= b.font_objects
        self.add("fonts_unchanged", ok,
                 ("new or modified font objects: " + f"{len(new)} new font(s), pages {per_page}, "
                  f"{b.font_objects}->{a.font_objects} font objects") if not ok else "")

    def _annotations_fields(self):
        spec = self.spec
        fb, fa = dict(self.snap_b.fields), dict(self.snap_a.fields)
        target = spec.field_name if spec.method == "form_field_update" else None
        problems = []
        if set(fb) != set(fa):
            problems.append(f"fields added/removed: {sorted(set(fb) ^ set(fa))}")
        for name in sorted(set(fb) & set(fa)):
            x, y = fb[name], fa[name]
            if name == target:
                if (x["type"], x["flags"], x["rects"]) != (y["type"], y["flags"], y["rects"]):
                    problems.append(f"target field {name!r} changed type, flags or position")
                continue
            if x != y:
                problems.append(f"field {name!r} changed")
        self.add("form_fields_unchanged", not problems, "; ".join(problems))
        ab, aa = self.snap_b.annots_per_page, self.snap_a.annots_per_page
        bad = []
        for i, (x, y) in enumerate(zip(ab, aa)):
            if target:
                x = [dict(r, ap="", value="") if r["name"] == target else r for r in x]
                y = [dict(r, ap="", value="") if r["name"] == target else r for r in y]
            if x != y:
                bad.append(i + 1)
        self.add("annotations_and_links_unchanged", not bad, f"annotations or links changed on page(s) {bad}" if bad else "")
        if target:
            got = fa.get(target, {}).get("value")
            want = spec.expected_field_value
            self.add("field_value_updated", got == want, f"field value is {got!r}, expected {want!r}" if got != want else "")

    # -- the area
    def _text_inside(self):
        spec = self.spec
        p = spec.page_index
        areas = spec.areas
        before = [c for c in self.ex_b.pages.get(p, []) if _in_any(c, areas)]
        after = [c for c in self.ex_a.pages.get(p, []) if _in_any(c, areas)]
        btext, bidx = reading_text(before)
        atext, aidx = reading_text(after)
        tpos = [k for k, i in enumerate(bidx) if i >= 0 and any(r.contains_point(before[i].cx, before[i].oy + 0.3 * before[i].size, 0.3)
                                                                 for r in spec.target_boxes)]
        if not tpos:
            self.add("text_inside_area_expected", False, "the original target text was not found inside the area")
            return
        s, e = tpos[0], tpos[-1] + 1
        target_style = before[bidx[s]]
        expected = btext[:s] + spec.replacement + btext[e:]
        # expected per-character style: original chars keep theirs, replacement chars use the target's
        exp_styles = [before[i] if i >= 0 else None for i in bidx[:s]] + [target_style if not ch.isspace() else None for ch in spec.replacement] \
            + [before[i] if i >= 0 else None for i in bidx[e:]]
        ok = _norm(expected) == _norm(atext)
        self.add("text_inside_area_expected", ok,
                 "inside the area the text does not read as the original with only the target replaced" if not ok else "",
                 expected=_norm(expected)[:400], found=_norm(atext)[:400], original=_norm(btext)[:400])
        nrep = _norm(spec.replacement)
        cnt_a, cnt_e = _norm(atext).count(nrep), _norm(expected).count(nrep)
        self.add("replacement_appears_once", ok and cnt_a == cnt_e and cnt_a >= 1,
                 f"replacement found {cnt_a} time(s) inside the area, expected {cnt_e}" if not (cnt_a == cnt_e and cnt_a >= 1) else "")
        nold = _norm(spec.target_text)
        if nold and nold not in nrep:
            cnt_old_a, cnt_old_e = _norm(atext).count(nold), _norm(expected).count(nold)
            self.add("old_text_removed", cnt_old_a == cnt_old_e,
                     "the old text is still in the content stream (hidden or covered text is not allowed)" if cnt_old_a != cnt_old_e else "")
        # ----- style, size, glyphs, render mode, colour
        a_chars = [after[i] for i in aidx if i >= 0 and not after[i].text.isspace()]
        e_styles = [st for st, ch in zip(exp_styles, _expected_chars(btext, s, e, spec.replacement)) if not ch.isspace()]
        problems = []
        size_problems = []
        if ok and len(a_chars) == len(e_styles):
            for got, want in zip(a_chars, e_styles):
                if want is None:
                    continue
                if got.fontname != want.fontname:
                    problems.append(f"{got.text!r} uses font {got.fontname}, expected {want.fontname}")
                if got.render != want.render or got.render in (3, 7):
                    problems.append(f"{got.text!r} has text render mode {got.render} (expected {want.render})")
                if _color(got.color) != _color(want.color):
                    problems.append(f"{got.text!r} colour {got.color} differs from {want.color}")
                is_new = want is target_style
                if got.size < want.size * spec.min_font_scale - SIZE_TOL:
                    size_problems.append(f"{got.text!r} at {got.size:.2f}pt is below {spec.min_font_scale:.0%} of {want.size:.2f}pt")
                elif got.size > want.size + SIZE_TOL:
                    size_problems.append(f"{got.text!r} grew from {want.size:.2f}pt to {got.size:.2f}pt")
                elif abs(got.size - want.size) > SIZE_TOL and (not is_new or spec.method != "reduced_font"):
                    size_problems.append(f"{got.text!r} changed size from {want.size:.2f}pt to {got.size:.2f}pt, "
                                         f"which method {spec.method} does not allow")
                if not got.upright:
                    problems.append(f"{got.text!r} is rotated or skewed")
        elif ok:
            problems.append("could not align the new characters with the expected text")
        self.add("new_text_same_font_colour_visible", not problems, "; ".join(dict.fromkeys(problems))[:600])
        self.add("font_size_within_limits", not size_problems, "; ".join(dict.fromkeys(size_problems))[:600])
        checker = GlyphChecker(self.ex_a)
        missing = []
        for c in a_chars:
            good, why = checker.check(c)
            if not good:
                missing.append(why)
        self.add("no_missing_glyphs", not missing, "; ".join(dict.fromkeys(missing))[:600])
        self._boundary_overlap(after, before)
        self._ink(a_chars)

    def _boundary_overlap(self, after_inside: list[XChar], before_inside: list[XChar]):
        spec = self.spec
        p = spec.page_index
        bnd = spec.boundary
        outside_b = [c for c in after_inside if not c.text.isspace() and
                     not any(r.contains(Rect(c.x0, c.core()[1], c.x1, c.core()[3]), tol=0.5) for r in bnd)]
        self.add("text_inside_boundary", not outside_b,
                 f"{len(outside_b)} character(s) run outside the allowed boundary, e.g. "
                 + ", ".join(f"{c.text!r} at x={c.x0:.1f}-{c.x1:.1f}" for c in outside_b[:5]) if outside_b else "")
        others = [c for c in self.ex_a.pages.get(p, []) if not _in_any(c, spec.areas) and not c.text.isspace()]
        inside = [c for c in after_inside if not c.text.isspace()]
        before_cores = [c.core() for c in before_inside if not c.text.isspace()]
        hits = []
        for c in inside:
            cc = c.core()
            for o in others:
                oc = o.core()
                if _overlap(cc, oc):
                    # kerned pairs at the target's edge ("y." in italics) overlapped in the original too
                    prior = max((_overlap_area(b, oc) for b in before_cores), default=0.0)
                    if _overlap_area(cc, oc) <= prior * 1.05 + 0.01:
                        continue
                    hits.append(f"{c.text!r} overlaps {o.text!r} at ({o.x0:.1f},{o.oy:.1f})")
                    break
        for i, c in enumerate(inside):
            for d in inside[i + 1:]:
                same_line = abs(c.oy - d.oy) < 0.3 * max(c.size, 1)
                if same_line:
                    ov = min(c.x1, d.x1) - max(c.x0, d.x0)
                    if ov > 0.35 * min(c.x1 - c.x0, d.x1 - d.x0) and ov > 0.3:
                        hits.append(f"{c.text!r} and {d.text!r} overlap at x={max(c.x0, d.x0):.1f}")
                elif _overlap(c.core(), d.core(), 0.3):
                    hits.append(f"{c.text!r} and {d.text!r} overlap between lines")
        # graphics that the original target did not already touch
        target_zone = [r for r in spec.target_boxes]
        for kind, rect, fill, stroke in graphics_rects(self.snap_a, p):
            touched_before = any(rect.intersects(t, tol=-0.5) for t in target_zone)
            for c in inside:
                box = Rect(c.x0, c.core()[1], c.x1, c.core()[3])
                if not box.intersects(rect.expand(0.25), tol=0.05):
                    continue
                if fill and not stroke and rect.contains(box, tol=0.1) and any(rect.contains(t, tol=0.5) for t in target_zone):
                    continue  # the same background the original text sat on
                if touched_before and kind in ("line", "rect") and rect.contains(box, tol=0.1):
                    continue
                if touched_before and kind == "line" and min(rect.width, rect.height) <= 2.5:
                    continue  # a rule the original text already sat on or crossed (forms, underlines)
                hits.append(f"{c.text!r} touches a {kind} at {rect.as_list(1)}")
                break
        for rect in image_rects(self.snap_a, p):
            if any(rect.contains(t, tol=0.5) for t in target_zone):
                continue
            for c in inside:
                if Rect(c.x0, c.core()[1], c.x1, c.core()[3]).intersects(rect):
                    hits.append(f"{c.text!r} overlaps an image")
                    break
        hits = list(dict.fromkeys(hits))
        self.add("no_overlap", not hits, "; ".join(hits[:6]))

    def _ink(self, chars: list[XChar]):
        p = self.spec.page_index
        img = self.r_a[p].astype(int)
        frame = self.frames[p]
        blank = []
        for c in chars:
            x0, y0, x1, y1 = frame.rect_to_px(Rect(c.x0, c.core()[1], c.x1, c.core()[3]))
            x0, y0 = max(0, x0), max(0, y0)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            region = img[y0:y1, x0:x1]
            border = np.concatenate([img[max(0, y0 - 2), x0:x1], img[min(img.shape[0] - 1, y1 + 1), x0:x1]]) if x1 > x0 else region.reshape(-1, 3)
            bg = np.median(border.reshape(-1, 3), axis=0) if border.size else np.array([255, 255, 255])
            if not (np.abs(region - bg).max(axis=2) > 40).any():
                blank.append(c.text)
        self.add("new_text_visible", not blank,
                 f"no ink rendered for {len(blank)} character(s): {''.join(blank)[:40]!r} (invisible, white or missing glyphs)" if blank else "")

    def _field_appearance(self):
        """Method e: read the regenerated appearance stream with pdfminer."""
        spec = self.spec
        with pikepdf.open(str(self.after)) as pdf:
            widget_aps = []
            for page in pdf.pages:
                for a in page.obj.get("/Annots", []) or []:
                    n = a
                    name_parts = []
                    for _ in range(32):
                        if n is None:
                            break
                        if "/T" in n:
                            name_parts.append(str(n.T))
                        n = n.get("/Parent")
                    if ".".join(reversed(name_parts)) == spec.field_name and a.get("/AP") is not None:
                        widget_aps.append((a.AP.N, [float(v) for v in a.Rect]))
            if not widget_aps:
                self.add("field_appearance", False, "the target field has no appearance stream")
                return
            problems = []
            for ap, rect in widget_aps:
                tmp = pikepdf.new()
                bbox = [float(v) for v in ap.get("/BBox", [0, 0, rect[2] - rect[0], rect[3] - rect[1]])]
                tmp.add_blank_page(page_size=(max(bbox[2], 1), max(bbox[3], 1)))
                pg = tmp.pages[0]
                pg.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(AP=tmp.copy_foreign(ap)))
                pg.obj.Contents = tmp.make_stream(b"/AP Do")
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "ap.pdf"
                    tmp.save(path)
                    ex = extract(path)
                chars = ex.pages.get(0, [])
                text = _norm("".join(reading_text(chars)[0]))
                if text != _norm(spec.expected_field_value or ""):
                    problems.append(f"appearance shows {text!r}, expected {spec.expected_field_value!r}")
                checker = GlyphChecker(ex)
                for c in chars:
                    good, why = checker.check(c)
                    if not good:
                        problems.append(why)
                    if c.x1 > bbox[2] + 0.5 or c.x0 < bbox[0] - 0.5:
                        problems.append(f"{c.text!r} runs outside the field")
                    if c.size < spec.original_size * spec.min_font_scale - SIZE_TOL:
                        problems.append(f"font size {c.size:.2f} below the minimum")
            self.add("field_appearance", not problems, "; ".join(dict.fromkeys(problems))[:600])

    # -- images for the reviewer
    def _write_images(self):
        if self.out_dir is None:
            return
        p = self.spec.page_index
        frame = self.frames[p]
        out = Path(self.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        area = self.spec.areas[0]
        for r in self.spec.areas[1:]:
            area = area.union(r)
        before_img = draw_boxes(self.r_b[p], frame, [(r, (0, 90, 255)) for r in self.spec.areas])
        after_img = draw_boxes(self.r_a[p], frame, [(r, (0, 90, 255)) for r in self.spec.areas])
        diff = diff_image(self.r_b[p], self.r_a[p])
        crop(before_img, frame, area).save(out / "before.png")
        crop(after_img, frame, area).save(out / "after.png")
        crop(diff, frame, area).save(out / "difference.png")
        diff.save(out / "difference_page.png")
        self.images = {"before": "before.png", "after": "after.png", "difference": "difference.png",
                       "difference_page": "difference_page.png"}


def _expected_chars(btext: str, s: int, e: int, replacement: str) -> str:
    return btext[:s] + replacement + btext[e:]


def _color(c):
    if c is None:
        return ()
    if isinstance(c, (int, float)):
        return (round(float(c), 3),)
    try:
        return tuple(round(float(v), 3) for v in c)
    except (TypeError, ValueError):
        return (str(c),)


def run_checks(before: str | Path, after: str | Path, spec: CheckSpec, out_dir: str | Path | None = None) -> CheckReport:
    return _Run(Path(before), Path(after), spec, Path(out_dir) if out_dir else None).run()
