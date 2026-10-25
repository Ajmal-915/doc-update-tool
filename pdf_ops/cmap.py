"""A small parser for ToUnicode CMaps (bfchar / bfrange / codespacerange)."""

from __future__ import annotations

import re

_TOKEN = re.compile(rb"<[0-9A-Fa-f\s]*>|\[|\]|/[^\s<>\[\]()/%]+|[^\s<>\[\]()/%]+|\((?:\\.|[^\\)])*\)")


def _hex_bytes(token: bytes) -> bytes:
    digits = re.sub(rb"\s", b"", token[1:-1])
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii"))


def _utf16(data: bytes) -> str:
    if len(data) % 2:
        data = b"\x00" + data
    return data.decode("utf-16-be", errors="replace")


class ToUnicodeCMap:
    """Maps character codes (as integers of a given byte length) to Unicode text."""

    def __init__(self) -> None:
        self.map: dict[tuple[int, int], str] = {}  # (nbytes, code) -> text
        self.codespace: list[tuple[int, int, int]] = []  # (nbytes, lo, hi)

    @classmethod
    def parse(cls, data: bytes) -> "ToUnicodeCMap":
        cm = cls()
        tokens = _TOKEN.findall(data)
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            if tok == b"begincodespacerange":
                i += 1
                while i + 1 < n and tokens[i] != b"endcodespacerange":
                    lo, hi = tokens[i], tokens[i + 1]
                    if lo.startswith(b"<") and hi.startswith(b"<"):
                        lob, hib = _hex_bytes(lo), _hex_bytes(hi)
                        cm.codespace.append((len(lob), int.from_bytes(lob, "big"), int.from_bytes(hib, "big")))
                    i += 2
            elif tok == b"beginbfchar":
                i += 1
                while i + 1 < n and tokens[i] != b"endbfchar":
                    src, dst = tokens[i], tokens[i + 1]
                    if src.startswith(b"<"):
                        srcb = _hex_bytes(src)
                        cm.map[(len(srcb), int.from_bytes(srcb, "big"))] = _dst_text(dst)
                    i += 2
            elif tok == b"beginbfrange":
                i += 1
                while i + 2 < n and tokens[i] != b"endbfrange":
                    lo, hi = tokens[i], tokens[i + 1]
                    if not (lo.startswith(b"<") and hi.startswith(b"<")):
                        i += 1
                        continue
                    lob, hib = _hex_bytes(lo), _hex_bytes(hi)
                    nbytes = len(lob)
                    start, end = int.from_bytes(lob, "big"), int.from_bytes(hib, "big")
                    if tokens[i + 2] == b"[":
                        j = i + 3
                        code = start
                        while j < n and tokens[j] != b"]":
                            if code <= end:
                                cm.map[(nbytes, code)] = _dst_text(tokens[j])
                            code += 1
                            j += 1
                        i = j + 1
                    else:
                        dst = tokens[i + 2]
                        if dst.startswith(b"<"):
                            base = _hex_bytes(dst)
                            if end - start <= 65535:
                                for offset in range(end - start + 1):
                                    value = int.from_bytes(base, "big") + offset
                                    raw = value.to_bytes(max(len(base), 2), "big")
                                    cm.map[(nbytes, start + offset)] = _utf16(raw)
                        i += 3
            else:
                i += 1
        return cm

    def lookup(self, nbytes: int, code: int) -> str | None:
        return self.map.get((nbytes, code))

    def code_lengths(self) -> list[int]:
        return sorted({cs[0] for cs in self.codespace}) or sorted({k[0] for k in self.map}) or [1]

    def reverse(self, nbytes: int) -> dict[str, int]:
        """Unicode text -> code, for codes of the given byte length."""
        out: dict[str, int] = {}
        for (nb, code), text in sorted(self.map.items()):
            if nb == nbytes and text and text not in out:
                out[text] = code
        return out


def _dst_text(token: bytes) -> str:
    if token.startswith(b"<"):
        return _utf16(_hex_bytes(token))
    if token.startswith(b"/"):
        from .encodings import glyph_to_unicode

        return glyph_to_unicode(token[1:].decode("latin-1"))
    return ""
