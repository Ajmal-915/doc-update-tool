"""Step 2: find the target text (plain code, approximate matching).

The source text in a change request may not match the PDF exactly, so both
sides are normalised (quotes, dashes, ligatures, soft hyphens, hyphenation
across lines, whitespace, case) and compared with rapidfuzz.  The given page
is searched first (page text and form-field values), then the pages either
side.  A match is accepted only if it scores at least ``min_score`` and
clearly beats the next best candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

import pdf_ops
from pdf_ops.forms import list_fields
from pdf_ops.geometry import Rect
from pdf_ops.target import BLOCK_SEPARATOR, Target, normalized_sequence, page_sequence, target_from_span
from pdf_ops.textnorm import normalize, normalize_with_map

MASK = "\x01"


@dataclass
class Candidate:
    kind: str  # "text" | "field"
    page_index: int
    score: float
    text: str
    bbox: Rect | None
    span: tuple = ()  # (seq_start, seq_end) for text; (value_start, value_end) for fields
    field_name: str | None = None
    field_value: str | None = None
    target: Target | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "page": self.page_index + 1,
            "score": round(self.score, 1),
            "text": self.text,
            "bbox": self.bbox.as_list(1) if self.bbox else None,
            "field": self.field_name,
        }


@dataclass
class MatchResult:
    status: str  # "found" | "not_found" | "ambiguous"
    best: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""
    searched_pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "best": self.best.to_dict() if self.best else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "searched_pages": [p + 1 for p in self.searched_pages],
        }


def _word_starts(hay: str) -> list[int]:
    return [i for i in range(len(hay)) if hay[i] not in (" ", BLOCK_SEPARATOR, MASK) and (i == 0 or hay[i - 1] in (" ", BLOCK_SEPARATOR))]


def _word_ends(hay: str) -> list[int]:
    n = len(hay)
    return [i + 1 for i in range(n) if hay[i] not in (" ", BLOCK_SEPARATOR, MASK) and (i == n - 1 or hay[i + 1] in (" ", BLOCK_SEPARATOR))]


def _refine(needle: str, hay: str, ds: int, de: int, starts: list[int], ends: list[int]) -> tuple[float, int, int]:
    """Best (score, start, end) near an alignment, preferring whole words."""
    import bisect

    def around(sorted_list, pos):
        i = bisect.bisect_left(sorted_list, pos)
        return {sorted_list[j] for j in range(max(0, i - 2), min(len(sorted_list), i + 2))}

    cand_s = {ds} | around(starts, ds)
    cand_e = {de} | around(ends, de)
    best = None
    for s in cand_s:
        for e in cand_e:
            if e <= s or e - s > 2 * len(needle) + 10:
                continue
            piece = hay[s:e]
            if BLOCK_SEPARATOR in piece or MASK in piece:
                continue
            key = (fuzz.ratio(needle, piece), (s in starts) + (e in ends), -(e - s))
            if best is None or key > best[0]:
                best = (key, s, e)
    if best is None:
        return (-1.0, ds, de)
    return (float(best[0][0]), best[1], best[2])


def text_candidates(doc: pdf_ops.Document, page_index: int, source: str, max_candidates: int = 6) -> list[Candidate]:
    layout = doc.layout(page_index)
    seq = page_sequence(layout)
    hay, idx = normalized_sequence(seq)
    needle = normalize(source)
    if not needle or not hay:
        return []
    out: list[Candidate] = []
    spans: list[tuple[int, int, float]] = []
    pos = hay.find(needle)
    while pos >= 0:
        spans.append((pos, pos + len(needle), 100.0))
        pos = hay.find(needle, pos + 1)
    if not spans:
        starts, ends = _word_starts(hay), _word_ends(hay)
        work = hay
        for _ in range(max_candidates):
            if len(needle) <= len(work):
                al = fuzz.partial_ratio_alignment(needle, work)
                ds, de = al.dest_start, al.dest_end
            else:
                al = fuzz.partial_ratio_alignment(work, needle)
                ds, de = al.src_start, al.src_end
            if al is None or al.score < 60:
                break
            score, s, e = _refine(needle, hay, ds, de, starts, ends)
            if score < 0:
                break
            spans.append((s, e, score))
            work = work[:ds] + MASK * (de - ds) + work[de:]
    seen: list[tuple[int, int]] = []
    for s, e, score in sorted(spans, key=lambda t: -t[2]):
        if any(not (e <= a or s >= b) for a, b in seen):
            continue
        seen.append((s, e))
        seq_s, seq_e = idx[s], idx[e - 1]
        try:
            t = target_from_span(doc, page_index, seq, seq_s, seq_e)
        except pdf_ops.PdfOpsError:
            continue
        out.append(Candidate("text", page_index, score, t.text, t.bbox, (seq_s, seq_e), target=t))
    return out


def field_candidates(doc: pdf_ops.Document, page_index: int, source: str) -> list[Candidate]:
    needle = normalize(source)
    out = []
    try:
        fields = list_fields(doc)
    except Exception:
        return out
    for f in fields:
        if f.field_type != "Tx" or not f.value:
            continue
        on_page = [w for w in f.widgets if w[0] == page_index]
        if not on_page:
            continue
        hay, idx = normalize_with_map(list(f.value))
        if not hay:
            continue
        pos = hay.find(needle)
        if pos >= 0:
            score, s, e = 100.0, pos, pos + len(needle)
        else:
            if len(needle) <= len(hay):
                al = fuzz.partial_ratio_alignment(needle, hay)
                s, e = al.dest_start, al.dest_end
            else:
                al = fuzz.partial_ratio_alignment(hay, needle)
                s, e = 0, len(hay)
            score = fuzz.ratio(needle, hay[s:e])
        if score < 60:
            continue
        vs, ve = idx[s], idx[e - 1] + 1
        rect = Rect.from_list(on_page[0][2])
        out.append(Candidate("field", page_index, score, f.value[vs:ve], rect, (vs, ve), f.name, f.value))
    return out


def _light(text: str) -> str:
    """Normalised like the matcher, but keeping letter case."""
    import unicodedata

    from pdf_ops.textnorm import DASHES, QUOTES

    out = []
    for ch in unicodedata.normalize("NFKC", text):
        ch = QUOTES.get(ch, ch)
        out.append("-" if ch in DASHES else ch)
    return " ".join("".join(out).replace("\u00ad", "").split())


def _decide(cands: list[Candidate], min_score: float, margin: float, source: str = ""):
    cands = sorted(cands, key=lambda c: -c.score)
    if not cands or cands[0].score < min_score:
        return None, cands
    close = [c for c in cands if c.score >= min_score and c.score > cands[0].score - margin]
    if len(close) > 1 and source:
        # several places match once case is ignored: one that also matches the letter case wins
        exact_case = [c for c in close if _light(c.text) == _light(source)]
        if len(exact_case) == 1:
            winner = exact_case[0]
            return "found", [winner] + [c for c in cands if c is not winner]
    if len(close) > 1:
        return "ambiguous", close
    return "found", cands


def find_target_text(doc: pdf_ops.Document, source: str, page_number: int, min_score: float = 90.0,
                     margin: float = 5.0) -> MatchResult:
    page_index = page_number - 1
    if not 0 <= page_index < doc.page_count:
        return MatchResult("not_found", None, [], f"page {page_number} does not exist (the document has {doc.page_count} pages)")
    stages = [[page_index], [p for p in (page_index - 1, page_index + 1) if 0 <= p < doc.page_count]]
    searched: list[int] = []
    all_cands: list[Candidate] = []
    for stage in stages:
        cands: list[Candidate] = []
        for p in stage:
            searched.append(p)
            cands += text_candidates(doc, p, source)
            cands += field_candidates(doc, p, source)
        all_cands += cands
        verdict, ranked = _decide(cands, min_score, margin, source)
        if verdict == "found":
            best = ranked[0]
            reason = f"matched with score {best.score:.0f} on page {best.page_index + 1}"
            if best.page_index != page_index:
                reason += f" (the request said page {page_number})"
            return MatchResult("found", best, ranked[:5], reason, searched)
        if verdict == "ambiguous":
            return MatchResult(
                "ambiguous", None, ranked,
                f"{len(ranked)} places match equally well; the reviewer must say which one is meant", searched)
    ranked = sorted(all_cands, key=lambda c: -c.score)[:3]
    if ranked:
        reason = (f"no match scored {min_score:.0f} or more on page {page_number} or the pages either side; "
                  f"closest was {ranked[0].score:.0f} on page {ranked[0].page_index + 1}: {ranked[0].text[:80]!r}")
    else:
        reason = f"the text was not found on page {page_number} or the pages either side"
    return MatchResult("not_found", None, ranked, reason, searched)
