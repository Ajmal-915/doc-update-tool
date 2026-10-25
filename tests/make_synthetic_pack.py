"""Builds tests/data/synthetic-pack: synthetic PDFs plus scenarios.json.

It mirrors the shape of the year-end test pack (one scenarios.json with an
expected_safe_result per scenario next to the PDFs), so the scenario runner
can be exercised without the real pack.  D01 and D02 are the graphic
scenarios (text drawn as vector shapes, a scanned page).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import samples  # noqa: E402

OUT = Path(__file__).parent / "data" / "synthetic-pack"

SCENARIOS = [
    ("A01", "report.pdf", 1, "financial year ending 31 December 2023", "financial year ending 31 December 2024", "updated",
     "Date change inside a paragraph (same width)"),
    ("A02", "report_mac.pdf", 1, "financial year ending 31 December 2023", "financial year ending 31 December 2024",
     "updated", "macOS-style PDF: 1 Tf with the size in the text matrix, TJ kerning"),
    ("A03", "report_type0.pdf", 1, "within 30 days", "within 28 days", "updated", "Type0 / Identity-H font"),
    ("B01", "report.pdf", 1, "each month", "each calendar month", "updated", "Line can grow into the ragged right edge"),
    ("B02", "report.pdf", 1, "Annually", "Annually in May", "updated", "Table cell with room before the rule"),
    ("C01", "report_type0.pdf", 1, "Section 4 Data protection", "Section 4 Data protections", "updated",
     "Table-of-contents line with dot leaders: only a slightly smaller size is possible"),
    ("C02", "report.pdf", 1, "Section 4 Data protection", "Section 4 Data protection and information security management",
     "manual_review", "Table-of-contents line, replacement far too long"),
    ("D01", "graphic_text.pdf", 1, "Total 2023", "Total 2024", "manual_review", "Text drawn as vector graphics"),
    ("D02", "scanned.pdf", 1, "31 December 2023", "31 December 2024", "manual_review", "Scanned page with an OCR layer"),
    ("E01", "form.pdf", 1, "Compliance team", "Compliance and Risk team", "updated", "Form-field value"),
    ("F01", "signed.pdf", 1, "31 December 2023", "31 December 2024", "manual_review", "Digitally signed PDF"),
    ("G01", "report.pdf", 2, "The policy applies to all", "The policy applies to every", "manual_review",
     "Phrase occurs twice on the page"),
    ("H01", "report.pdf", 1, "The annual bonus pool is approved by the remuneration committee", "x", "manual_review",
     "Source text not in the document"),
    ("H02", "report.pdf", 1, "31 December 2023", "31 Dezember 2024", "manual_review",
     "Replacement needs a letter the embedded subset font does not contain"),
    ("R01", "report.pdf", 1, "within 30 days of joining", "within 30 calendar days of joining the organisation", "updated",
     "Paragraph reflow using free space below"),
    ("R02", "report.pdf", 1, "within 30 days of joining",
     "within 30 days of joining, or within 10 working days for staff in high-risk roles such as procurement, sales "
     "and finance, with refresher training every year for everyone and a record kept in the training system",
     "manual_review", "Paragraph would need more space than is free"),
    ("X01", "report.pdf", 1, "ALL STAFF MUST COMPLETE THE ANTI–BRIBERY TRAINING",
     "All staff must complete the anti-corruption training", "updated", "Source text differs in case and dash"),
    ("X02", "report.pdf", 1, "Revenue grew by 12%", "Revenue grew by 14%", "updated", "Text is on the next page"),
    ("S01", "shared_stream.pdf", 1, "31 December 2023", "31 December 2024", "updated",
     "Two pages share one content stream"),
    ("S02", "xobject.pdf", 1, "internal use only", "internal use", "manual_review",
     "Header XObject drawn twice on the page: ambiguous"),
]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    built = samples.build_all(OUT / "_build")
    used = sorted({s[1] for s in SCENARIOS})
    for name in used:
        shutil.copyfile(built[name[:-4]], OUT / name)
    shutil.rmtree(OUT / "_build")
    data = {"pack": "synthetic year-end style pack", "scenarios": [
        {"id": sid, "title": title, "pdf": pdf, "page": page, "source_text": find, "replacement": rep,
         "expected_safe_result": {"status": exp}}
        for sid, pdf, page, find, rep, exp, title in SCENARIOS]}
    (OUT / "scenarios.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(SCENARIOS)} scenarios and {len(used)} PDFs to {OUT}")


if __name__ == "__main__":
    main()
