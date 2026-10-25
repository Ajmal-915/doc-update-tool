"""Text normalisation used for matching source text against PDF text.

Curly and straight quotes, the many dash characters, ligatures, soft hyphens,
whitespace and letter case are all folded so that "Year‑end ‘results’" in a
change request matches "year-end 'results'" in the PDF.  Every function that
builds a normalised string also returns, for each output character, the index
of the source character it came from, so a match can be mapped back to glyphs.
"""

from __future__ import annotations

import unicodedata

QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
}
DASHES = set("‐‑‒–—―−﹘﹣－")
SOFT_HYPHEN = "­"
ZERO_WIDTH = set("​‌‍⁠﻿")


def fold_char(ch: str) -> str:
    """Normalised form of one character ('' to drop it, ' ' for any whitespace)."""
    if ch == SOFT_HYPHEN or ch in ZERO_WIDTH:
        return ""
    if ch in QUOTES:
        return QUOTES[ch]
    if ch in DASHES:
        return "-"
    if ch.isspace() or ch == " ":
        return " "
    folded = unicodedata.normalize("NFKC", ch)
    out = []
    for c in folded:
        if c in QUOTES:
            out.append(QUOTES[c])
        elif c in DASHES:
            out.append("-")
        elif c.isspace():
            out.append(" ")
        else:
            out.append(c)
    return "".join(out).casefold()


def normalize(text: str) -> str:
    return normalize_with_map(list(text))[0]


def normalize_with_map(chars, sources=None) -> tuple[str, list]:
    """Normalise a sequence of characters (or short strings).

    ``sources`` gives, per input item, the value to report in the map (default:
    the item index).  Whitespace runs collapse to one space, leading and
    trailing whitespace is removed.
    """
    out: list[str] = []
    idx: list = []
    for i, item in enumerate(chars):
        src = i if sources is None else sources[i]
        for ch in item:
            f = fold_char(ch)
            for c in f:
                if c == " ":
                    if not out or out[-1] == " ":
                        continue
                out.append(c)
                idx.append(src)
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def is_line_end_hyphen(text: str) -> bool:
    return text in ("-", SOFT_HYPHEN, "‐", "‑")
