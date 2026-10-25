"""Opening, caching and saving documents."""

from __future__ import annotations

from pathlib import Path

import pikepdf

from .content import Interpreter, PageModel
from .fonts import FontCache
from .layout import PageLayout, build_layout


class Document:
    """A pikepdf document plus cached page interpretations.

    Generated scripts receive a Document from :func:`open`, pass it to the
    edit functions and finally call :func:`save`.
    """

    def __init__(self, pdf: pikepdf.Pdf, path: str | None = None):
        self.pdf = pdf
        self.path = path
        self.fonts = FontCache()
        self._models: dict[int, PageModel] = {}
        self._layouts: dict[int, PageLayout] = {}
        self.log: list[dict] = []

    @property
    def page_count(self) -> int:
        return len(self.pdf.pages)

    def check_page(self, page_index: int) -> None:
        if not 0 <= page_index < self.page_count:
            raise IndexError(f"page {page_index + 1} does not exist (document has {self.page_count} pages)")

    def model(self, page_index: int) -> PageModel:
        self.check_page(page_index)
        if page_index not in self._models:
            self._models[page_index] = Interpreter(self.pdf, self.fonts).interpret_page(page_index)
        return self._models[page_index]

    def layout(self, page_index: int) -> PageLayout:
        if page_index not in self._layouts:
            self._layouts[page_index] = build_layout(self.model(page_index))
        return self._layouts[page_index]

    def invalidate(self, page_index: int | None = None) -> None:
        if page_index is None:
            self._models.clear()
            self._layouts.clear()
        else:
            self._models.pop(page_index, None)
            self._layouts.pop(page_index, None)
        for f in self.fonts.all():
            f.used_codes.clear()
            f.invalidate()


def open(path: str | Path) -> Document:  # noqa: A001 - mirrors the public API name
    """Open a PDF for editing (the file itself is never modified)."""
    pdf = pikepdf.open(str(path))
    return Document(pdf, str(path))


def save(doc: Document, path: str | Path) -> None:
    """Write the edited document to ``path``."""
    target = Path(path)
    if doc.path is not None and target.resolve() == Path(doc.path).resolve():
        raise ValueError("refusing to overwrite the input PDF")
    # fix_metadata_version=False: never rewrite the document's XMP metadata
    doc.pdf.save(str(target), compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve,
                 fix_metadata_version=False, preserve_pdfa=True)

