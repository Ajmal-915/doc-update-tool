r"""Corpus test: many generated change requests on a folder of real PDFs, each result verified.

    python scripts\corpus_test.py --corpus "C:\path\to\pdfs" --work "C:\path\to\corpus-run" [--workers 4]
    python scripts\corpus_test.py --work "C:\path\to\corpus-run" --report-only

For every PDF, change requests are generated from pdfium's text layer (not from the
tool's own text reading): same-letter and same-digit swaps, reversed phrases and
lines, shorter and longer replacements, two-line targets, fuzzy find text, the
wrong page, characters missing from the font, text that is not there, ambiguous
words, empty and unchanged replacements, and form-field values.  Each request
goes through the public API (ingest, update, approve) with the offline LLM
stand-in unless --provider says otherwise.

Every 'updated' result is checked by an oracle that shares no code with the
tool's own checks: the pdfium text of every changed page must equal the original
with exactly the requested substitution; rendered pixels may only change in the
target's line band (the paragraph below it for reflow, the widget for form
fields); no other page may change; no new fonts; no new qpdf problems; the input
PDF stays untouched.  Results: <work>/results.jsonl and <work>/report.md; the
store folders of oracle failures are kept under <work>/docs/<pdf>/failures.

--provider bedrock sends every request to Bedrock (costs money: about 3-6 calls per
request); --max-docs limits the number of PDFs.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import numpy as np  # noqa: E402
import pikepdf  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402

LIGATURE = re.compile(r"(fi|fl|ff|ffi|ffl|ft|st)|[ﬀ-ﬆ]")
SNOWMAN = "☃"
MUST_UPDATE, MUST_REFUSE, EITHER = "must_update", "must_refuse", "either"
SCALE = 1.5  # render scale for the pixel oracle (108 dpi)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------ pdfium text
def page_text(page):
    tp = page.get_textpage()
    n = tp.count_chars()
    text = tp.get_text_range() if n else ""
    if len(text) != n:
        text = "".join((tp.get_text_range(i, 1) or "\x00")[:1] for i in range(n))
    boxes = [tp.get_charbox(i, loose=True) for i in range(n)]
    tp.close()
    return text, boxes


def normalise(text: str):
    """Collapse whitespace runs to one space; return (norm, map norm index -> original index)."""
    out, idx = [], []
    space = False
    for i, ch in enumerate(text):
        if ch.isspace() or ch == "\x00":
            if not space and out:
                out.append(" ")
                idx.append(i)
            space = True
            continue
        space = False
        out.append(ch)
        idx.append(i)
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def lines_of(text: str, boxes):
    """Lines as lists of (start, end) char ranges, split at pdfium's generated line breaks."""
    lines, start = [], 0
    for m in re.finditer(r"\r\n|\n|\r", text):
        lines.append((start, m.start()))
        start = m.end()
    lines.append((start, len(text)))
    return [ln for ln in lines if ln[1] > ln[0]]


def tokens_in(text: str, a: int, b: int):
    return [(m.start() + a, m.end() + a, m.group()) for m in re.finditer(r"\S+", text[a:b])]


def count_ci(hay: str, needle: str) -> int:
    return len(re.findall(re.escape(needle.lower()), hay.lower()))


def permute(word: str) -> str | None:
    if len(word) < 4:
        return None
    inner = word[1:-1]
    cand = word[0] + inner[::-1] + word[-1]
    if cand == word:
        cand = word[0] + inner[1:] + inner[0] + word[-1]
    if cand == word:
        return None
    return cand


def permute_digits(tok: str) -> str | None:
    digits = [c for c in tok if c.isdigit()]
    if len(set(digits)) < 2:
        return None
    rot = digits[1:] + digits[:1]
    it = iter(rot)
    out = "".join(next(it) if c.isdigit() else c for c in tok)
    return out if out != tok else None


# ------------------------------------------------------------------ generation
def gen_doc(path: Path, rng: random.Random) -> list[dict]:
    name = path.name
    base = {"pdf": name}
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:
        return [{**base, "id": f"{name}#open", "kind": "unreadable", "page": 1, "find": "the", "replace": "a",
                 "expect": MUST_REFUSE, "note": f"pdfium cannot open: {type(exc).__name__}"}]
    encrypted = False
    try:
        with pikepdf.open(path) as pk:
            encrypted = pk.is_encrypted
            fields = _fields(pk)
    except pikepdf.PasswordError:
        return [{**base, "id": f"{name}#pw", "kind": "password", "page": 1, "find": "the", "replace": "a",
                 "expect": MUST_REFUSE}]
    except Exception:
        fields = []
    n = len(pdf)
    texts = {}
    for i in range(min(n, 400)):
        try:
            texts[i] = page_text(pdf[i])
        except Exception:
            texts[i] = ("", [])
    good = [i for i, (t, _) in texts.items() if len(t.strip()) >= 150] or \
        [i for i, (t, _) in texts.items() if len(t.strip()) >= 10]
    if encrypted:
        p = good[0] if good else 0
        word = next((w for _, _, w in tokens_in(texts[p][0], 0, len(texts[p][0])) if w.isalpha() and len(w) > 4), "the")
        return [{**base, "id": f"{name}#enc", "kind": "encrypted", "page": p + 1, "find": word,
                 "replace": permute(word) or "x", "expect": MUST_REFUSE}]
    if not good:
        out = [{**base, "id": f"{name}#notext", "kind": "no_text", "page": 1, "find": "Total", "replace": "Sum",
                "expect": MUST_REFUSE}]
        out += _field_scenarios(base, fields, rng)
        return out
    pages = [good[0]]
    others = [g for g in good[1:]]
    if others:
        pages.append(rng.choice(others))
    out = []
    for p in pages:
        out += gen_page(base, texts, p, n, rng)
    out += _field_scenarios(base, fields, rng)
    for k, sc in enumerate(out):
        sc["id"] = f"{name}#{k:02d}-{sc['kind']}"
    return out


def _fields(pk) -> list[dict]:
    out = []
    af = pk.Root.get("/AcroForm")
    if af is None:
        return out
    pages = {p.objgen: i for i, p in enumerate(pk.pages)}

    def walk(f, prefix=""):
        t = str(f.get("/T", "")) if "/T" in f else ""
        full = f"{prefix}.{t}" if prefix and t else (t or prefix)
        kids = f.get("/Kids")
        if kids is not None and any("/T" in k for k in kids):
            for k in kids:
                walk(k, full)
            return
        ft = f.get("/FT") or (f.get("/Parent") or {}).get("/FT") if "/Parent" in f else f.get("/FT")
        v = f.get("/V")
        if str(ft) == "/Tx" and v is not None:
            try:
                val = str(v)
            except Exception:
                return
            widget = (kids[0] if kids is not None and len(kids) else f)
            pg = widget.get("/P")
            page = pages.get(pg.objgen, 0) if pg is not None else 0
            out.append({"name": full, "value": val, "page": page})

    for f in af.get("/Fields", []):
        try:
            walk(f)
        except Exception:
            continue
    return out


def _field_scenarios(base, fields, rng) -> list[dict]:
    out = []
    usable = [f for f in fields if len(f["value"].strip()) >= 4]
    rng.shuffle(usable)
    for k, f in enumerate(usable[:10]):
        words = [w for w in f["value"].split() if len(w) >= 4]
        if not words:
            continue
        w = rng.choice(words)
        if k % 3 == 2:
            rep, kind = w + " Ltd", "form_field_longer"
        elif re.fullmatch(r"(19|20)\d\d", w):
            rep, kind = str(int(w) + 1), "form_field_year"
        else:
            rep = permute_digits(w) if any(c.isdigit() for c in w) else permute(w)
            kind = "form_field"
        if not rep:
            continue
        out.append({**base, "kind": kind, "page": f["page"] + 1, "find": w, "replace": rep,
                    "expect": EITHER, "field": f["name"]})
    return out


def gen_page(base, texts, p, npages, rng) -> list[dict]:
    text, boxes = texts[p]
    norm, _ = normalise(text)
    lines = lines_of(text, boxes)
    toks = []
    for a, b in lines:
        toks.append(tokens_in(text, a, b))
    flat = [t for ln in toks for t in ln]

    def clean(w):
        return w.strip(".,;:()[]{}\"'!?")

    def unique(s):
        return count_ci(norm, s) == 1

    words = []
    for s, e, w in flat:
        c = clean(w)
        if 5 <= len(c) <= 14 and c.isascii() and c.isalpha() and not LIGATURE.search(c.lower()) and unique(c):
            words.append(c)
    words = list(dict.fromkeys(words))
    rng.shuffle(words)
    nums = []
    for s, e, w in flat:
        c = clean(w)
        if re.fullmatch(r"\d{2,7}|\d{1,3}(,\d{3})+|\d+\.\d+", c) and unique(c):
            nums.append(c)
    nums = list(dict.fromkeys(nums))
    rng.shuffle(nums)
    out = []
    pg = p + 1

    def add(kind, find, replace, expect, **kw):
        out.append({**base, "kind": kind, "page": pg, "find": find, "replace": replace, "expect": expect, **kw})

    for w in words[:3]:
        r = permute(w)
        if r:
            add("swap_letters", w, r, MUST_UPDATE)
    for w in nums[:2]:
        r = permute_digits(w)
        if r:
            add("swap_digits", w, r, MUST_UPDATE)
    years = [w for w in nums if re.fullmatch(r"(19|20)\d\d", w)]
    if years:
        add("year_plus_one", years[0], str(int(years[0]) + 1), EITHER)
    long_words = [w for w in words if len(w) >= 7]
    for w in long_words[3:5]:
        add("shorter", w, w[:-2], EITHER)
    if len(words) > 5:
        w = words[5]
        add("longer_small", w, w + w[-3:], EITHER)
    phrases, lines_ok = [], []
    for ln in toks:
        if 3 <= len(ln) <= 12:
            seg = text[ln[0][0]:ln[-1][1]]
            if len(seg) <= 80 and seg.isascii() and not LIGATURE.search(seg.lower()) and unique(" ".join(seg.split())):
                lines_ok.append(ln)
        for k in range(len(ln) - 2):
            trio = ln[k:k + 3]
            seg = " ".join(t[2] for t in trio)
            if seg.isascii() and not LIGATURE.search(seg.lower()) and any(c.isalpha() for c in seg) and unique(seg) \
                    and len(seg) >= 10:
                phrases.append(trio)
    rng.shuffle(phrases)
    for trio in phrases[:2]:
        seg = [t[2] for t in trio]
        add("phrase_reversed", " ".join(seg), " ".join(seg[::-1]), MUST_UPDATE)
    if phrases:
        seg = [t[2] for t in phrases[0]]
        big = " ".join(seg)
        add("longer_big", big, " ".join([big] * 3), EITHER)
        fuzzy = [s.lower() if s[:1].isupper() else s.capitalize() for s in seg]
        add("fuzzy_find", "  ".join(fuzzy), " ".join(seg[::-1]), EITHER)
    if lines_ok:
        ln = rng.choice(lines_ok)
        seg = [t[2] for t in ln]
        add("line_reversed", " ".join(seg), " ".join(seg[::-1]), MUST_UPDATE)
    # two words at the end of a line + two at the start of the next one (same column)
    pairs = []
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if len(a) >= 3 and len(b) >= 3:
            ax, bx = boxes[a[0][0]][0], boxes[b[0][0]][0]
            ay, by = boxes[a[0][0]][1], boxes[b[0][0]][1]
            h = boxes[a[0][0]][3] - boxes[a[0][0]][1]
            if abs(ax - bx) < 4 and 0 < ay - by < 2.2 * max(h, 1) and not a[-1][2].endswith("-"):
                seg = [a[-2][2], a[-1][2], b[0][2], b[1][2]]
                s = " ".join(seg)
                if s.isascii() and not LIGATURE.search(s.lower()) and unique(s):
                    pairs.append(seg)
    if pairs:
        seg = rng.choice(pairs)
        add("cross_line", " ".join(seg), " ".join(seg[2:] + seg[:2]), EITHER)
    if words:
        w = words[0]
        add("missing_glyph", w, w[:2] + SNOWMAN + w[2:], MUST_REFUSE)
        add("no_change", w, w, MUST_REFUSE)
        add("empty_replacement", w, "", MUST_REFUSE)
        add("percent_sign", w, w + " 100%", EITHER)
    add("not_found", "Qwzxv Plorkt Jumbix", "Something else", MUST_REFUSE)
    dup = Counter(clean(t[2]) for t in flat)
    dups = [w for w, c in dup.items() if c >= 2 and len(w) >= 5 and w.isalpha() and w.isascii()
            and count_ci(norm, w) == len(re.findall(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", norm))
            and len(re.findall(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", norm)) >= 2]
    if dups:
        w = rng.choice(dups)
        add("ambiguous", w, permute(w) or w + "x", MUST_REFUSE)
    # a word unique on this page and absent from the neighbour page, requested on the neighbour
    nb = p + 1 if p + 1 < npages else p - 1
    if nb >= 0 and nb != p and nb in texts:
        nb_norm = normalise(texts[nb][0])[0]
        for w in words[6:]:
            if count_ci(nb_norm, w) == 0 and permute(w):
                out.append({**base, "kind": "wrong_page", "page": nb + 1, "find": w, "replace": permute(w),
                            "expect": EITHER, "true_page": pg})
                break
    return out


def cmd_gen(corpus: Path, out: Path, seed: int = 20261025, max_docs: int | None = None):
    rng = random.Random(seed)
    allsc = []
    for f in sorted(corpus.glob("*.pdf"))[:max_docs]:
        try:
            allsc += gen_doc(f, rng)
        except Exception as exc:
            allsc.append({"pdf": f.name, "id": f"{f.name}#generror", "kind": "gen_error", "page": 1, "find": "the",
                          "replace": "a", "expect": EITHER, "note": repr(exc)[:200]})
    out.write_text(json.dumps(allsc, indent=1), encoding="utf-8")
    print(len(allsc), "scenarios", Counter(s["kind"] for s in allsc))


# ------------------------------------------------------------------ oracle
def deep_hash(pk, obj, seen=None) -> str:
    h = hashlib.sha256()
    seen = set() if seen is None else seen

    def feed(o, depth=0):
        if depth > 60:
            return
        if isinstance(o, pikepdf.Object) and o.is_indirect:
            og = o.objgen
            if og in seen:
                h.update(f"ref{og}".encode())
                return
            seen.add(og)
        if isinstance(o, pikepdf.Stream):
            try:
                h.update(o.read_raw_bytes())
            except Exception:
                h.update(b"?")
            feed(dict(o.stream_dict.items()), depth + 1)
        elif isinstance(o, (pikepdf.Dictionary, dict)):
            for k in sorted(o.keys()):
                if k in ("/Parent", "/P", "/StructParents", "/StructParent"):
                    continue
                h.update(k.encode())
                feed(o[k], depth + 1)
        elif isinstance(o, pikepdf.Array):
            for x in o:
                feed(x, depth + 1)
        else:
            try:
                h.update(repr(o).encode())
            except Exception:
                h.update(b"?")

    feed(obj)
    return h.hexdigest()


def page_hashes(path: Path) -> list[str]:
    with pikepdf.open(path) as pk:
        return [deep_hash(pk, p.obj) for p in pk.pages]


def fonts_used(path: Path) -> list[set]:
    out = []
    with pikepdf.open(path) as pk:
        for p in pk.pages:
            names = set()
            stack, seen = [p.obj.get("/Resources")], set()
            while stack:
                r = stack.pop()
                if r is None or not isinstance(r, pikepdf.Dictionary):
                    continue
                if r.is_indirect:
                    if r.objgen in seen:
                        continue
                    seen.add(r.objgen)
                for _, f in (r.get("/Font") or {}).items():
                    names.add(str(f.get("/BaseFont", "?")))
                for _, x in (r.get("/XObject") or {}).items():
                    if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == "/Form":
                        stack.append(x.get("/Resources"))
            out.append(names)
    return out


def qpdf_problems(path: Path) -> set:
    try:
        with pikepdf.open(path) as pk:
            return {re.sub(r"\d+", "N", str(x)) for x in pk.check()}
    except Exception as exc:
        return {f"open failed: {type(exc).__name__}"}


def render(pdf, i, scale=SCALE):
    """RGB array and the page -> bitmap position converter (crop box and /Rotate included)."""
    page = pdf[i]
    bmp = page.render(scale=scale, draw_annots=True, may_draw_forms=True)
    arr = bmp.to_numpy().copy()
    return arr[..., :3].astype(np.int16), bmp.get_posconv(page)


def field_values(path: Path) -> dict:
    with pikepdf.open(path) as pk:
        return {f["name"]: f["value"] for f in _fields(pk)}


def widget_rects(path: Path, names: set, page_index: int) -> list:
    rects = []
    with pikepdf.open(path) as pk:
        page = pk.pages[page_index]
        for a in page.obj.get("/Annots") or []:
            if a.get("/Subtype") != "/Widget":
                continue
            t, node = [], a
            while node is not None:
                if "/T" in node:
                    t.append(str(node["/T"]))
                node = node.get("/Parent") if "/Parent" in node else None
            if ".".join(reversed(t)) in names:
                rects.append([float(v) for v in a["/Rect"]])
    return rects


def oracle(orig: Path, cand: Path, sc: dict, result: dict, cache: dict) -> dict:
    """Return {'ok': bool, 'problems': [...], 'notes': [...]} for an 'updated' result."""
    problems, notes = [], []
    method = result.get("method")
    try:
        po, pc = pdfium.PdfDocument(str(orig)), pdfium.PdfDocument(str(cand))
        for d in (po, pc):
            d.init_forms()  # widget appearances are only drawn with a form environment
    except Exception as exc:
        return {"ok": False, "problems": [f"candidate does not open in pdfium: {exc}"], "notes": []}
    if len(po) != len(pc):
        return {"ok": False, "problems": [f"page count {len(po)} -> {len(pc)}"], "notes": []}
    try:
        with pikepdf.open(cand) as pk:
            pass
    except Exception as exc:
        problems.append(f"candidate does not open in pikepdf: {exc}")
    try:
        import pdfplumber
        with pdfplumber.open(str(cand)) as pl:
            pl.pages[0].extract_text()
    except Exception as exc:
        problems.append(f"candidate does not open in pdfplumber: {type(exc).__name__}: {exc}")
    key = str(orig)
    if key not in cache:
        cache[key] = {"hashes": page_hashes(orig), "fonts": fonts_used(orig), "qpdf": qpdf_problems(orig),
                      "fields": field_values(orig)}
    oc = cache[key]
    ch = page_hashes(cand)
    changed = [i for i, (a, b) in enumerate(zip(oc["hashes"], ch)) if a != b]
    new_q = qpdf_problems(cand) - oc["qpdf"]
    if new_q:
        problems.append(f"new qpdf problems: {sorted(new_q)[:3]}")
    cf = fonts_used(cand)
    for i in changed:
        extra = cf[i] - oc["fonts"][i]
        if extra:
            problems.append(f"page {i + 1}: new fonts {sorted(extra)}")
    diff: set = set()
    if method == "form_field_update":
        fv = field_values(cand)
        diff = {k for k in set(fv) | set(oc["fields"]) if fv.get(k) != oc["fields"].get(k)}
        if len(diff) != 1:
            problems.append(f"{len(diff)} field values changed: {sorted(diff)[:4]}")
        for k in diff:
            old, new = oc["fields"].get(k, ""), fv.get(k, "")
            ok = any(old[:m.start()] + sc["replace"] + old[m.end():] == new
                     for m in re.finditer(re.escape(sc["find"]), old))
            if not ok:
                problems.append(f"field {k!r}: {old[:60]!r} -> {new[:60]!r} is not the requested substitution")
    hit_pages = []
    for i in changed:
        to_raw, ob = page_text(po[i])
        tc_raw, _ = page_text(pc[i])
        to, omap = normalise(to_raw)
        tc, _ = normalise(tc_raw)
        (img_o, conv), (img_c, _) = render(po, i), render(pc, i)
        if img_o.shape != img_c.shape:
            problems.append(f"page {i + 1}: render size changed")
            continue
        mask = (np.abs(img_o - img_c) > 40).any(axis=2)
        if to == tc and not mask.any():
            notes.append(f"page {i + 1}: objects changed but text and pixels are identical")
            continue
        if method == "form_field_update":
            if to != tc:
                problems.append(f"page {i + 1}: page text changed by a form-field update")
            rects = widget_rects(cand, diff, i)
            allowed = _rect_mask(mask.shape, conv, [_pad(r, 1.5) for r in rects])
            _pixels(mask, allowed, i, problems)
            hit_pages.append(i)
            continue
        pos = _substitution(to, tc, sc["find"], sc["replace"])
        if pos is None:
            weak = re.sub(r"\s", "", to), re.sub(r"\s", "", tc)
            pos2 = _substitution(weak[0], weak[1], re.sub(r"\s", "", sc["find"]), re.sub(r"\s", "", sc["replace"]))
            if pos2 is not None:
                problems.append(f"page {i + 1}: text matches only when spaces are ignored (spacing changed): "
                                + _diff_snippet(to, tc))
            else:
                problems.append(f"page {i + 1}: text is not the requested substitution: " + _diff_snippet(to, tc))
            continue
        hit_pages.append(i)
        start, length = pos
        idxs = [omap[k] for k in range(start, start + length) if k < len(omap)]
        tbox = [ob[k] for k in idxs if ob[k][2] > ob[k][0]]
        if not tbox:
            notes.append(f"page {i + 1}: no boxes for the target")
            continue
        allowed = _allowed_mask(mask.shape, po[i], conv, tbox, ob, to_raw, method)

        def recheck(i=i, tbox=tbox, ob=ob, to_raw=to_raw):
            (a2, conv2), (c2, _) = render(po, i, 2.07), render(pc, i, 2.07)
            if a2.shape != c2.shape:
                return False
            m2 = (np.abs(a2 - c2) > 40).any(axis=2)
            return not (m2 & ~_allowed_mask(m2.shape, po[i], conv2, tbox, ob, to_raw, method)).any()

        _pixels(mask, allowed, i, problems, recheck)
    if not hit_pages and not problems:
        problems.append("no page shows the requested change")
    if len(hit_pages) > 1:
        problems.append(f"change applied on {len(hit_pages)} pages")
    want = sc.get("true_page", sc["page"]) - 1
    if hit_pages and sc["kind"] != "wrong_page" and hit_pages[0] != want and method != "form_field_update":
        problems.append(f"changed page {hit_pages[0] + 1}, requested page {want + 1}")
    if sc["kind"] == "wrong_page" and hit_pages and hit_pages[0] != want:
        problems.append(f"changed page {hit_pages[0] + 1}, the text is on page {want + 1}")
    po.close()
    pc.close()
    return {"ok": not problems, "problems": problems, "notes": notes, "changed_pages": [i + 1 for i in changed]}


def _pad(r, d):
    return [min(r[0], r[2]) - d, min(r[1], r[3]) - d, max(r[0], r[2]) + d, max(r[1], r[3]) + d]


def _substitution(to: str, tc: str, find: str, repl: str):
    fn = " ".join(find.split())
    rn = " ".join(repl.split())
    for flags in (0, re.I):  # case-insensitive only matters for the fuzzy_find scenarios
        for m in re.finditer(re.escape(fn).replace(r"\ ", " "), to, flags):
            if to[:m.start()] + rn + to[m.end():] == tc:
                return m.start(), m.end() - m.start()
    return None


def _diff_snippet(a: str, b: str) -> str:
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = [o for o in sm.get_opcodes() if o[0] != "equal"][:3]
    return "; ".join(f"{o[0]} {a[o[1]:o[2]][:50]!r} -> {b[o[3]:o[4]][:50]!r}" for o in ops)


def _to_px(conv, box):
    x0, y0, x1, y1 = box
    pts = [conv.to_bitmap(x, y) for x in (x0, x1) for y in (y0, y1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return int(min(xs)) - 1, int(min(ys)) - 1, int(max(xs)) + 2, int(max(ys)) + 2


def _rect_mask(shape, conv, rects):
    m = np.zeros(shape, bool)
    for r in rects:
        px0, py0, px1, py1 = _to_px(conv, r)
        m[max(py0, 0):max(py1, 0), max(px0, 0):max(px1, 0)] = True
    return m


def _allowed_mask(shape, page, conv, tbox, all_boxes, text, method):
    """Pixels the change may touch: the target's line bands (to the right edge for b/d; paragraph for d)."""
    _, bottom, w, _ = page.get_cropbox()
    lines = []
    for b in sorted(tbox, key=lambda b: -b[1]):
        for ln in lines:
            if abs(ln[1] - b[1]) < 2 or (b[1] < ln[3] and b[3] > ln[1] and min(b[3], ln[3]) - max(b[1], ln[1]) > 0.5 * (b[3] - b[1])):
                ln[0], ln[1], ln[2], ln[3] = min(ln[0], b[0]), min(ln[1], b[1]), max(ln[2], b[2]), max(ln[3], b[3])
                break
        else:
            lines.append(list(b))
    rects = []
    pad = 1.5
    for x0, y0, x1, y1 in lines:
        hgt = y1 - y0
        if method in ("original_box", "reduced_font"):
            rects.append([x0 - pad - 0.3 * hgt, y0 - pad, x1 + pad + 0.3 * hgt, y1 + pad])
        else:
            rects.append([x0 - pad - 0.3 * hgt, y0 - pad, w, y1 + pad])
    if method == "paragraph_reflow":
        # the paragraph is rewrapped from the target line on: everything below the target line's top, from
        # the paragraph's left margin (where the target line and the lines below it in this column start)
        top = max(r[3] for r in rects)
        left = min(r[0] for r in rects)
        first_line = max(lines, key=lambda ln: ln[3])
        line_starts = []  # (x0 of the line's first glyph, top of the line)
        for la, lb in lines_of(text, all_boxes):
            real = [all_boxes[k] for k in range(la, lb) if all_boxes[k][2] > all_boxes[k][0]]
            if real:
                line_starts.append((real[0][0], max(r[3] for r in real), min(r[1] for r in real)))
        # the target line's own start, then lines below it that start near there (same column)
        own = [x for x, t, bt in line_starts if bt < first_line[3] and t > first_line[1] and x <= left + 0.5]
        if own:
            left = min(left, min(own))
        below = [x for x, t, bt in line_starts if t <= top + 2 and left - 40 <= x <= left + 1]
        left = min(below + [left])
        rects.append([left - 2, bottom, w, top])
    if len(lines) > 1 and method in ("original_box", "reduced_font"):
        # a multi-line target may move words between its lines: allow the full width between its lines
        left = min(r[0] for r in rects)
        rects = [[left, r[1], max(rr[2] for rr in rects), r[3]] for r in rects]
    return _rect_mask(shape, conv, rects)


def _pixels(mask, allowed, i, problems, recheck=None):
    outside = mask & ~allowed
    n = int(outside.sum())
    if 3 < n <= 60 and recheck is not None and recheck():
        return  # an anti-aliasing flip at this resolution only (a real change shows at any resolution)
    if n > 3:
        ys, xs = np.nonzero(outside)
        problems.append(f"page {i + 1}: {n} pixels changed outside the allowed area "
                        f"(x {xs.min() / SCALE:.0f}-{xs.max() / SCALE:.0f} pt, y-from-top {ys.min() / SCALE:.0f}-{ys.max() / SCALE:.0f} pt)")


# ------------------------------------------------------------------ runner
def cmd_run(scen_path: Path, pdf_name: str, work: Path, out_path: Path, corpus: Path, provider: str = "offline"):
    from doc_update import api

    scen = [s for s in json.loads(scen_path.read_text()) if s["pdf"] == pdf_name]
    src = corpus / pdf_name
    work.mkdir(parents=True, exist_ok=True)
    store = work / "store"
    ov = {"store": {"root": str(store)}, "llm": {"provider": provider}}
    doc_id = re.sub(r"[^A-Za-z0-9_-]", "_", pdf_name)[:60]
    h0 = sha256(src)
    fails = work / "failures"
    cache: dict = {}
    out = out_path.open("a", encoding="utf-8")

    def emit(rec):
        out.write(json.dumps(rec, default=str) + "\n")
        out.flush()

    t0 = time.time()
    ing = api.ingest_document(str(src), doc_id, None, ov)
    emit({"pdf": pdf_name, "id": f"{pdf_name}#ingest", "kind": "ingest", "status": ing.get("status"),
          "error": ing.get("error"), "seconds": round(time.time() - t0, 2)})
    orig = None
    if ing.get("status") == "ingested":
        orig = next(store.rglob("original.pdf"), None)
    first_updated = None
    for sc in scen:
        rec = {"pdf": pdf_name, "id": sc["id"], "kind": sc["kind"], "expect": sc["expect"], "page": sc["page"],
               "find": sc["find"], "replace": sc["replace"]}
        t1 = time.time()
        try:
            if ing.get("status") != "ingested":
                rec.update({"status": "not_ingested", "reason_code": "ingest", "reason": ing.get("error")})
            else:
                res = api.update_document(doc_id, sc["find"], sc["replace"], sc["page"], None, ov)
                rec.update({k: res.get(k) for k in ("status", "method", "reason_code", "change_id", "error")})
                rec["reason"] = (res.get("reason") or "")[:400]
                rec["warnings"] = res.get("warnings")
                folder = Path(res["report_path"]).parent if res.get("report_path") else None
                if res.get("status") == "updated":
                    cand = Path(res.get("candidate_path") or folder / "candidate.pdf")
                    rec["oracle"] = oracle(orig, cand, sc, res, cache)
                    if not rec["oracle"]["ok"]:
                        dest = fails / sc["id"].replace("#", "_")
                        shutil.copytree(folder, dest, dirs_exist_ok=True)
                    elif first_updated is None and sc["kind"] in ("swap_letters", "swap_digits", "phrase_reversed"):
                        first_updated = (sc, res)
                elif res.get("status") == "manual_review":
                    if folder and (folder / "candidate.pdf").exists():
                        rec["problem"] = "candidate.pdf left behind for a manual-review result"
                    if not res.get("reason_code") or not res.get("reason"):
                        rec["problem"] = "manual review without a reason"
        except Exception as exc:
            rec.update({"status": "harness_exception", "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc()[-1500:]})
        rec["seconds"] = round(time.time() - t1, 2)
        emit(rec)
    # version chain: approve one good change, edit again on top of it, check versions and the original
    if first_updated is not None:
        rec = {"pdf": pdf_name, "id": f"{pdf_name}#chain", "kind": "chain", "expect": EITHER}
        try:
            sc0, res0 = first_updated
            ap = api.approve_change(doc_id, res0["change_id"], "corpus-test", None, None, ov)
            rec["approve"] = ap.get("status")
            v1 = next(store.rglob("v001.pdf"), None)
            more = [s for s in scen if s["kind"] == "swap_letters" and s["id"] != sc0["id"] and s["page"] == sc0["page"]
                    and s["find"] != sc0["find"]]
            if v1 is None:
                rec["problem"] = "approved but no v001.pdf"
            elif more:
                sc1 = more[0]
                res1 = api.update_document(doc_id, sc1["find"], sc1["replace"], sc1["page"], None, ov)
                rec.update({"status": res1.get("status"), "method": res1.get("method"),
                            "reason_code": res1.get("reason_code"), "reason": (res1.get("reason") or "")[:300]})
                if res1.get("status") == "updated":
                    cand = Path(res1["candidate_path"])
                    rec["oracle"] = oracle(v1, cand, sc1, res1, cache)
                    ap2 = api.approve_change(doc_id, res1["change_id"], "corpus-test", None, None, ov)
                    rec["approve2"] = ap2.get("status")
                    v2 = next(store.rglob("v002.pdf"), None)
                    if v2 is None:
                        rec["problem"] = "second approval produced no v002.pdf"
            st = api.document_status(doc_id, None, ov)
            rec["versions"] = len(st.get("versions") or [])
        except Exception as exc:
            rec.update({"status": "harness_exception", "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc()[-1500:]})
        emit(rec)
    if orig is not None and sha256(orig) != h0:
        emit({"pdf": pdf_name, "id": f"{pdf_name}#original", "kind": "original", "problem": "store original changed"})
    if sha256(src) != h0:
        emit({"pdf": pdf_name, "id": f"{pdf_name}#source", "kind": "source", "problem": "corpus file changed"})
    out.close()
    shutil.rmtree(store, ignore_errors=True)


def cmd_all(scen_path: Path, work: Path, out_path: Path, corpus: Path, workers: int, provider: str = "offline"):
    scen = json.loads(scen_path.read_text())
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            r = json.loads(line)
            if r["kind"] == "ingest":
                done.add(r["pdf"])
    pdfs = [p for p in dict.fromkeys(s["pdf"] for s in scen) if p not in done]
    # big documents first so the pool finishes evenly
    pdfs.sort(key=lambda p: -(corpus / p).stat().st_size)
    running: list = []
    log = work / "runner.log"
    work.mkdir(parents=True, exist_ok=True)
    while pdfs or running:
        while pdfs and len(running) < workers:
            p = pdfs.pop(0)
            part = work / "parts" / (p + ".jsonl")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.unlink(missing_ok=True)
            cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "run-doc", str(scen_path), p,
                   str(work / "docs" / p), str(part), str(corpus), provider]
            proc = subprocess.Popen(cmd, stdout=open(log, "a"), stderr=subprocess.STDOUT)
            running.append((p, proc, time.time(), part))
        time.sleep(1)
        for item in list(running):
            p, proc, t0, part = item
            timeout = 2400
            if proc.poll() is not None or time.time() - t0 > timeout:
                if proc.poll() is None:
                    proc.kill()
                    with part.open("a") as fh:
                        fh.write(json.dumps({"pdf": p, "id": f"{p}#timeout", "kind": "timeout",
                                             "problem": f"document run exceeded {timeout} s"}) + "\n")
                elif proc.returncode != 0:
                    with part.open("a") as fh:
                        fh.write(json.dumps({"pdf": p, "id": f"{p}#crash", "kind": "crash",
                                             "problem": f"runner exited with {proc.returncode}"}) + "\n")
                with out_path.open("a") as out:
                    if part.exists():
                        out.write(part.read_text())
                running.remove(item)
                print(time.strftime("%H:%M:%S"), "done", p, f"{time.time() - t0:.0f}s", flush=True)


def classify(r: dict) -> str:
    if r.get("problem"):
        return "PROBLEM"
    st = r.get("status")
    if st in ("harness_exception",):
        return "HARNESS"
    if st == "error" and r.get("kind") == "empty_replacement" and "must not be empty" in (r.get("error") or ""):
        return "OK_REFUSED"  # input rejected with a clear message
    if st == "error" or r.get("reason_code") == "internal_error":
        return "TOOL_ERROR"
    if st == "updated":
        if r.get("oracle") and not r["oracle"]["ok"]:
            return "UNSAFE"
        if r.get("expect") == MUST_REFUSE:
            return "UNSAFE_EXPECTED_REFUSAL"
        return "OK_UPDATED"
    if st in ("manual_review", "not_ingested"):
        if r.get("reason_code") in ("signed_pdf", "encrypted", "scanned_page"):
            return "OK_REFUSED"  # the whole document must not be edited
        return "REFUSED_EXPECTED_UPDATE" if r.get("expect") == MUST_UPDATE else "OK_REFUSED"
    return f"OTHER_{st}"


VERDICT_HELP = {
    "OK_UPDATED": "edited, and the oracle confirmed the edit",
    "OK_REFUSED": "manual review (or input rejected) where that is acceptable",
    "REFUSED_EXPECTED_UPDATE": "manual review where an edit was expected (safe, but worth a look)",
    "UNSAFE": "edited, but the oracle found damage",
    "UNSAFE_EXPECTED_REFUSAL": "edited where manual review was required",
    "TOOL_ERROR": "the tool failed with an error",
    "HARNESS": "the test harness failed",
    "PROBLEM": "store problem (original changed, candidate left behind, missing reason, timeout)",
}


def cmd_report(scen_path: Path, res_path: Path) -> str:
    rows = [json.loads(line) for line in res_path.read_text().splitlines() if line.strip()] if res_path.exists() else []
    docs = {r["pdf"] for r in rows}
    rows = [r for r in rows if r["kind"] != "ingest"]
    c = Counter(classify(r) for r in rows)
    out = ["# Corpus test", "", f"{len(docs)} PDFs, {len(rows)} change requests.", "",
           "| Verdict | Count | Meaning |", "|---|---|---|"]
    for k, help_ in VERDICT_HELP.items():
        if c.get(k):
            out.append(f"| {k} | {c[k]} | {help_} |")
    out += ["", "## By kind of request", "", "| Kind | Expected | Runs | Updated (verified) | Manual review | Unsafe | Errors |",
            "|---|---|---|---|---|---|---|"]
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    for k, rs in sorted(by_kind.items()):
        cc = Counter(classify(r) for r in rs)
        exp = Counter(r.get("expect", "-") for r in rs).most_common(1)[0][0]
        out.append(f"| {k} | {exp} | {len(rs)} | {cc['OK_UPDATED']} | {cc['OK_REFUSED'] + cc['REFUSED_EXPECTED_UPDATE']} | "
                   f"{cc['UNSAFE'] + cc['UNSAFE_EXPECTED_REFUSAL']} | {cc['TOOL_ERROR'] + cc['HARNESS'] + cc['PROBLEM']} |")
    meth = Counter(r.get("method") for r in rows if r.get("status") == "updated")
    out += ["", "## Methods used for verified edits", "", ", ".join(f"{m}: {n}" for m, n in meth.most_common()) or "-"]
    reasons = Counter((r.get("reason_code") or ("input_rejected" if r.get("status") == "error" else "-"))
                      for r in rows if r.get("status") != "updated")
    out += ["", "## Manual-review reasons", "", ", ".join(f"{m}: {n}" for m, n in reasons.most_common()) or "-"]
    secs = [r.get("seconds", 0) for r in rows if r.get("seconds")]
    if secs:
        secs.sort()
        out += ["", f"Time per request: median {secs[len(secs) // 2]:.1f} s, 95th percentile {secs[int(len(secs) * 0.95)]:.1f} s."]
    bad = [r for r in rows if classify(r) in ("UNSAFE", "UNSAFE_EXPECTED_REFUSAL", "TOOL_ERROR", "HARNESS", "PROBLEM")]
    out += ["", "## Unsafe results and errors", ""]
    if not bad:
        out.append("None.")
    for r in bad:
        detail = (r.get("oracle") or {}).get("problems") or r.get("problem") or r.get("error") or r.get("reason")
        out.append(f"- {classify(r)} `{r['id']}` ({r.get('method') or r.get('reason_code')}): {str(detail)[:300]}")
    refused = [r for r in rows if classify(r) == "REFUSED_EXPECTED_UPDATE"]
    out += ["", "## Edits expected but sent to manual review", ""]
    if not refused:
        out.append("None.")
    for r in refused:
        out.append(f"- `{r['id']}` {r['find']!r} -> {r['replace']!r}: {r.get('reason_code')}: "
                   f"{(r.get('reason') or '').splitlines()[0][:220] if r.get('reason') else ''}")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    import argparse

    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "run-doc":  # one document, in a worker process
        a = sys.argv[2:]
        cmd_run(Path(a[0]), a[1], Path(a[2]), Path(a[3]), Path(a[4]), a[5])
        return 0
    ap = argparse.ArgumentParser(description="Generated change requests on a folder of PDFs, every result verified")
    ap.add_argument("--corpus", help="folder with the PDFs (never modified)")
    ap.add_argument("--work", required=True, help="output folder (scenarios, results, report, failures)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--provider", default="offline", choices=["offline", "bedrock"])
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--max-docs", type=int)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    scen, res = work / "scenarios.json", work / "results.jsonl"
    if not args.report_only:
        if not args.corpus:
            ap.error("--corpus is required")
        corpus = Path(args.corpus).resolve()
        if not scen.exists():
            cmd_gen(corpus, scen, args.seed, args.max_docs)
        cmd_all(scen, work, res, corpus, args.workers, args.provider)
    text = cmd_report(scen, res)
    (work / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
