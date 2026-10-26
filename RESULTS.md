# Results

Build date: 24 October 2026; year-end pack tested 25 October 2026. Built and tested in a
Linux container (Python 3.11.17; the last full test run used 3.13.17), not on the
target Windows machine; see "What was not run here" below.

## Test suite

`python -m pytest` (offline, no Bedrock key): **all tests pass** — 200 passed,
2 skipped (Windows Job Object test, generated cases not imported), 1 deselected (live).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_checks.py` | 43 | Step 8 gate. Correct edits pass for every font/operator style (WinAnsi, MacRoman, custom Differences, Type0 Identity-H, non-embedded Helvetica, "1 Tf" + text-matrix size, TJ kerning, Td/TD/T*/'/", multiple content streams). Deliberately broken edits are **all rejected**: neighbouring words shifted, space lost, spaces squeezed out of the replacement, line deleted, white box over the old text, text running into the next column, reflow running into the next paragraph, another form field changed, edit leaking onto a second page that shares the content stream, new font object, font below the minimum scale, glyphs missing from the subset, old text hidden with an invisible render mode, annotation removed, added document JavaScript, added page action, and an unchanged copy. |
| `tests/test_pdf_ops.py` | 28 | Fonts (encodings, widths, real subset coverage, AFM metrics), interpreter (all positioning operators, kerning, text-matrix size, form XObject matrix), edit engine (following text stays in place, same font resource, shared content stream copied, XObject drawn twice copied and renamed, other page keeps the original), methods a–e, reflow (free space, justified, mixed fonts refused). |
| `tests/test_sandbox.py` | 34 | Static rejection of imports, exec/eval/compile/__import__/getattr, dunder access, absolute and parent paths, computed file names, `pikepdf.Job`, classes; empty environment (proved at runtime); audit hook blocks reads/writes outside the folder, subprocess, sockets, `os.system`, file deletion; timeout; memory limit; errors reported. |
| `tests/test_pipeline_e2e.py` | 24 | Whole pipeline offline with **recorded Bedrock Converse responses**: in-place change, white-box script rejected then corrected, crashing script retried, planner's wrong order overridden, all attempts failing → manual review, form field, cautious layout advice disabling expansion, LLM call budget, missing key; PDF text is passed only inside the untrusted `<document_data>` block; manual-review reasons (not found, ambiguous, signed, scanned, missing glyphs, not enough space, graphic text, no change). |
| `tests/test_llm.py` | 16 | Converse logic without network: temperature refused → retried without it, missing tool call → one reminder, schema validation, throttling retry, secrets scrubbed from errors, budget, audit fields, prompt hash, key/region required, bearer token preferred by botocore, model auto-selection. |
| `tests/test_store.py` | 9 | Read-only original with SHA-256, v001/v002 chain, reject deletes the candidate, stale candidate refused, tampered candidate/original refused, manual review recorded without PDF, CLI round trip and exit codes. |
| `tests/test_matching.py` | 13 | Normalisation, exact/approximate/cross-line matches, ambiguity with positions, neighbouring page, form-field values; a missing number, word, accent or end mark is not a match; a close match does not beat the exact text on the next page. |
| `tests/test_analysis.py` | 9 | Words/chars, effective size from the text matrix, encodings, signed/scanned/shared detection, fields, measurements (table cell, two columns, TOC dot leaders, free space, missing glyphs). |
| `tests/test_real_world_layouts.py` | 8 | Layouts found in the corpus test: one text object per column (reflow keeps the reading order), an instruction split across content streams, individually positioned glyphs, Tc with a size change, a line longer than its column, a media box not at the origin, a rotated page, columns aligned with runs of spaces. |
| `tests/test_recommend.py` | 12 | Recommendations: screenshot + numbered lines to the editor, several edits in one candidate and one approval, invented quotes, repeated text (by context, by line, by position), overlapping edits, all-or-nothing when one edit cannot be made, not possible / needs clarification, form fields, offline instructions, CLI, and the 13 year-end recommendations with hand-written plans. |
| `tests/test_scenarios.py` | 6 | Scenario loader (several layouts), synthetic pack end to end, year-end pack with the offline stand-in and with the hand-written LLM answers (all 28 PASS); generated cases (skipped here — not available). |

A 60-page PDF goes through the whole offline pipeline in about 2.5 s: the checks
skip pages whose complete object graph is unchanged (identical by construction)
and render/extract only the pages that changed.

## Scenario results

### Year-end test pack (manual provider: every LLM answer written by hand)

The pack (`year-end-test-pack.zip`: `report_original.pdf`, `report_flattened.pdf`,
`report_scanned_p7.pdf`, 28 scenarios) was copied into `tests/data/year-end-test-pack`
and the copies made read-only. No Bedrock key was available, so the LLM's part was
played by hand with `--provider manual`: for every call the pipeline made (vision
layout check, plan, script, visual review), the request and its images were read
and the tool input a model would return was written into
`tests/data/manual-answers/year-end/<ID>/NN_<role>.json`. Those answers then went
through the same parsing, schema checks, static script checks, sandbox and
step-8 checks as Bedrock answers would. All 15 generated scripts passed the
checks on the first attempt. C03 is chained: it runs on A01's approved result
(v001), and its own approved result is v002; v002 differs from the original only
on page 4, in the two edited labels.

```powershell
python -m doc_update.scenarios --pack "tests\data\year-end-test-pack" --provider manual --manual-dir "tests\data\manual-answers\year-end"
python -m pytest -m scenarios     # includes the same run as a regression test
```

<!-- SCENARIOS-YEAR-END-TEST-PACK-MANUAL:BEGIN -->
### year-end-test-pack (manual)

Verdicts: PASS 28

| ID | Expected | Result | Method / reason | Verdict |
|---|---|---|---|---|
| A01 | updated | updated | original_box | PASS |
| A02 | updated | updated | original_box | PASS |
| A03 | updated | updated | horizontal_expansion | PASS |
| A04 | manual_review (not_enough_space) | manual_review | not_enough_space: not enough space for the replacement. original_box: replacement needs 313.3 pt but the original box is 88.1 pt wide / horizontal_expansion: replacement needs 313.3 pt but the space up to the boundary at | PASS |
| A05 | manual_review (ambiguous) | manual_review | ambiguous: 2 places match equally well; the reviewer must say which one is meant | PASS |
| A06 | manual_review (ambiguous) | manual_review | ambiguous: 2 places match equally well; the reviewer must say which one is meant | PASS |
| A07 | manual_review (missing_glyphs) | manual_review | missing_glyphs: the embedded font ESJPOF+CenturyGothic does not contain: '8'. The font must not be switched, so a person has to make this change. | PASS |
| A08 | manual_review (missing_glyphs) | manual_review | missing_glyphs: the embedded font ESJPOF+CenturyGothic does not contain: '(', '&', ')'. The font must not be switched, so a person has to make this change. | PASS |
| A09 | either | updated | original_box | PASS |
| A10 | manual_review (missing_glyphs) | manual_review | missing_glyphs: the embedded font ESJPOF+CenturyGothic does not contain: '–'. The font must not be switched, so a person has to make this change. | PASS |
| A11 | manual_review (not_enough_space) | manual_review | not_enough_space: not enough space for the replacement. original_box: replacement needs 177.4 pt but the original box is 102.5 pt wide / horizontal_expansion: replacement needs 177.4 pt but the space up to the boundary a | PASS |
| A12 | either | manual_review | not_enough_space: not enough space for the replacement. original_box: replacement is 5.1 pt shorter than the original and text follows on the line; the word spacing would be wrong / horizontal_expansion: not allowed here | PASS |
| A13 | updated | updated | horizontal_expansion | PASS |
| A14 | either | updated | paragraph_reflow | PASS |
| A15 | updated | updated | horizontal_expansion | PASS |
| A16 | updated | updated | horizontal_expansion | PASS |
| A17 | updated | updated | original_box | PASS |
| A18 | manual_review | manual_review | not_enough_space: not enough space for the replacement. original_box: the target spans 2 lines but the replacement wraps onto 1; the paragraph would have to be rewrapped (paragraph_reflow) instead of leaving a gap / hori | PASS |
| A19 | updated | updated | original_box | PASS |
| A20 | updated | updated | horizontal_expansion | PASS |
| B01 | updated | updated | form_field_update | PASS |
| B02 | updated | updated | form_field_update | PASS |
| B03 | updated | updated | form_field_update | PASS |
| C01 | updated | updated | horizontal_expansion | PASS |
| C02 | manual_review (scanned_page) | manual_review | scanned_page: page 7 is a scanned image; its visible text cannot be edited | PASS |
| C03 | updated | updated | horizontal_expansion | PASS |
| D01 | manual_review (out_of_scope) | manual_review | out_of_scope: graphic change requested (Remove the green 'Try Smartsheet for FREE' button (raster image, top right)); the tool only replaces text, so a person must make this change | PASS |
| D02 | manual_review (out_of_scope) | manual_review | out_of_scope: graphic change requested (Change the grey shading of the [CURRENT YEAR] column to white (vector fill)); the tool only replaces text, so a person must make this change | PASS |
<!-- SCENARIOS-YEAR-END-TEST-PACK-MANUAL:END -->

Notes on the results:

* "either" means the pack accepts an edit or a clean refusal. A09 and A14 were
  edited; A12 was refused because the shorter wording would leave a gap before
  text on the same line.
* A14 was done with paragraph_reflow, so the whole paragraph from the target
  line on is rewrapped. The visual review flags this as a warning for the reviewer.
* A16: the font has no straight apostrophe, so the replacement's `'` was written
  as the typographic `’` that the font does have. This is noted in
  `report.json` (`replacement_adjusted`) and in the warnings.
* A18: the new label is short enough for one line but the target spans two table
  lines. Leaving an empty line, or reflowing inside a table cell, would not be
  safe, so the change goes to manual review.
* C01 edits text inside the flattened form XObject; C02 (scanned page 7) and
  D01/D02 (graphics) go to manual review as expected.

Changes made to the tool while testing with this pack: case-aware tie-break for
labels that also appear in capitals elsewhere (A03); spacing differences between
runs no longer count as mixed formatting (Quartz PDFs); table rules drawn as thin
filled shapes are recognised; space-only glyphs after a target no longer block
method a; method b's change area now covers the rest of the line it moves (A19);
multi-line form fields (B01/B02); straight/curly quote adaptation (A16);
word-processor-style line splitting and no reflow inside table cells (A18); the
reflow area covers every removed glyph (A14); XMP metadata is kept unchanged on
save; clearer refusal messages for multi-line targets.

The `make_cases.py` generated cases from `C:\Users\soley\pdf-surgeon-eval` were not
part of the upload and have not been run:

```powershell
python "scripts\import_eval_data.py" --eval-root "C:\Users\soley\pdf-surgeon-eval"
python -m doc_update.scenarios --pack "tests\data\year-end-test-pack" --provider bedrock --results-md "RESULTS.md"   # live
```

The runner compares each result with the scenario's `expected_safe_result`
(verdicts: PASS, MISSED = safe but more cautious than expected,
UNSAFE = an edit was produced where manual review was expected, ERROR).
D01 and D02 are always expected to go to manual review. The loader accepts
several layouts of `scenarios.json`; if it cannot map a scenario it reports the
keys it found instead of guessing.

### Recommendations in plain words (year-end pack; edit plans written by hand)

13 recommendations for the year-end report, written the way a compliance team would
(`tests/data/recommendations/year-end.json`), went through `recommend`. For each one,
I wrote the editor's edit plan by hand (`--editor-provider manual`), working from the
same page screenshot and line listing a model receives. The other roles ran offline.
Every plan went through the same code checks and safety pipeline as a Bedrock answer.

All 13 PASS:
- **6 updated, 7 edits in total**, each in one candidate:
  - R02 and R05 combine two edits each;
  - R03 changes the "Product" line under Cost of Sales and leaves the one under Net
    Sales, a repeated label that exact find/replace refuses as ambiguous;
  - R07 and R08 are form-field values.
- **7 manual review, each with the right reason:**
  - missing characters (R04);
  - an image button (R06);
  - a colour change alongside a rename (R09: nothing applied);
  - a vague request (R10);
  - a scanned page (R11, refused before any LLM call);
  - a recommendation naming a section and sentence that are not on the page
    (R12: the editor asks instead of guessing).

The plan check narrowed each edit to the words that change, e.g. "GROSS MARGIN" →
"GROSS PROFIT" became "MARGIN" → "PROFIT", and "in connection with the monthly
projected" → "… the projected" became "monthly projected" → "projected".

| ID | Page | Expected | Result | Edits / reason | Verdict |
|---|---|---|---|---|---|
| R01 | 4 | updated | updated | 'MARGIN' → 'PROFIT' (original_box) | PASS |
| R02 | 4 | updated | updated | 'Sales and Marketing' → 'Marketing' (original_box); 'Other' → 'Other Operating' (horizontal_expansion) | PASS |
| R03 | 4 | updated | updated | 'Product' → 'Goods' (original_box) | PASS |
| R04 | 4 | manual_review (missing_glyphs) | manual_review | missing_glyphs: edit 1 of 1 ('In-Process Research and Development' -> 'In-Process Research and Development (R&D)') cannot be made safely: the embedded font ESJPOF+CenturyGothic | PASS |
| R05 | 7 | updated | updated | 'strictly' → 'entirely' (horizontal_expansion); 'Smartsheet' → 'Northwind Group' (horizontal_expansion) | PASS |
| R06 | 7 | manual_review (not_possible) | manual_review | not_possible: the recommendation cannot be done by replacing text on this page: remove the green 'Try Smartsheet for FREE' button: the button is an image, not text on the pag | PASS |
| R07 | 3 | updated | updated | 'It its' → 'In its' (form_field_update) | PASS |
| R08 | 1 | updated | updated | 'COMPANY NAME' → 'NORTHWIND GROUP' (form_field_update) | PASS |
| R09 | 4 | manual_review (partly_not_possible) | manual_review | partly_not_possible: part of the recommendation cannot be done by replacing text on this page: print the column headers ([CURRENT YEAR], [1 YEAR PRIOR], [2 YEARS PRIOR]) in blue: a  | PASS |
| R10 | 4 | manual_review (needs_clarification) | manual_review | needs_clarification: the recommendation is not clear enough to apply: Which labels should change, and to what wording? The page has 30 labels in the income statement table. | PASS |
| R11 | 7 | manual_review (scanned_page) | manual_review | scanned_page: page 7 is a scanned image; its text cannot be edited | PASS |
| R12 | 6 | manual_review (needs_clarification) | manual_review | needs_clarification: the recommendation is not clear enough to apply: Page 6 has no 'summary of significant accounting policies' and no sentence about cash flows. The only 'monthly  | PASS |
| R13 | 6 | updated | updated | 'monthly projected' → 'projected' (original_box) | PASS |

Unit tests (`tests/test_recommend.py`, `tests/test_llm.py`) also cover:
- quotes that are not on the page, repeated text without a distinguishing line, and
  overlapping edits: the plan is rejected;
- an impossible second edit: nothing is changed;
- form fields, approval of a multi-edit candidate, the CLI, and the Bedrock request
  (the screenshot, the recommendation and the listing are sent; text from the PDF
  cannot close the data block).

### Real-world corpus: 116 PDFs, 2,992 generated change requests (offline provider)

116 real PDFs were downloaded from public GitHub repositories (ordinary websites are
blocked in the build environment). Only PDF files were taken, and none came from a
PDF-editing project:

- 40 research papers (papers-we-love: LaTeX, Word, OpenOffice, Distiller, Quartz,
  Ghostscript, old scans).
- Government reports, tables, forms and malformed or encrypted test files from the
  test data of the text-extraction libraries pdfplumber (30), pdfminer.six (26) and
  camelot (18).
- 2 copies of blank forms (the IRS 1040-NR and an AcroForm sample), filled with
  values whose appearance streams were generated by qpdf.

The PDFs are not included here. `scripts/corpus_test.py` (see README) generates
about 25 change requests per document from pdfium's text layer, not from the
tool's own text reading:
- same-letter and same-digit swaps, and reversed phrases and lines (an edit is
  expected);
- shorter, longer and much longer wording, two-line targets, fuzzy find text, the
  wrong page number, "% signs", and form-field values (either result is
  acceptable);
- characters missing from the font, text that is not there, ambiguous words, and
  empty or unchanged replacements (manual review is required).

Every request went through the public API (ingest, update; for 81 documents also
approve, a second edit on v001, and approve again) with the offline LLM stand-in.
Every edit was then checked by an oracle that shares no code with the tool's
checks:
- the pdfium text of each changed page equals the original with exactly the
  requested substitution;
- rendered pixels change only in the target's line band (or the paragraph, or the
  form widget);
- no other page changes, no new fonts appear, and qpdf reports no new problems;
- the input file is untouched.

| Result | Requests |
|---|---|
| Edited, and the independent oracle confirmed the edit | 1,484 |
| Manual review (or input rejected) where that is required or acceptable | 1,305 |
| Manual review where an edit was expected (safe; see below) | 195 |
| **Edited although the oracle found damage, or where manual review was required** | **0** |
| **Errors or crashes** | **0** |

Methods used: original box 1,080, horizontal expansion 279, reduced font 63,
paragraph reflow 59, form field 3. All 81 two-version chains were approved, and
the second approval succeeded on all 71 documents where the second edit was
possible. Every request that had to go to manual review did: all 146 missing
characters, 158 texts that are not there, 141 ambiguous words, 146 empty and 146
unchanged replacements, and every edit on the 4 encrypted, 8 unreadable and the
digitally signed PDFs. A request takes 1.9 s at the median and 6.3 s at the 95th
percentile.

Of the 1,057 requests where an edit was clearly possible (letter, digit, phrase
and line swaps), 846 were made and 16 were on the signed PDF. The other 195 went to
manual review, broken down as:
- **text not found (101):** pdfium's reading order differs from the tool's line
  layout, e.g. table rows, rotated pages, or Type 3 bitmap fonts;
- **graphic text (34):** chart labels and similar;
- **failed checks (30):** mostly where the new text would touch neighbouring text
  or a rule;
- **mixed fonts (17), missing glyphs (5), ambiguous (4) and unsupported (4).**

Each of these has a reason a reviewer can act on.

The full report, which lists every refused request with its reason, is in
`tests/data/corpus-report.md`.

The corpus test found these problems, all now fixed (each has a regression test):

| Problem | Effect before the fix |
|---|---|
| Fuzzy matching accepted a match missing a word, a number, an accent or a trailing mark ("…Regressions" for "…Regressions 93", "31 December 2024" for "…2023", "Center" for "Centers", "Königsberg" for "Konigsberg") | **Wrong edits**: the leftover part stayed or was duplicated, or the wrong year or word was changed. Now only spacing, punctuation, case and small typos inside the text may differ |
| When the requested page's layout could not be read, or only had an approximate match, the search fell back to a neighbouring page | **Edits on the wrong page**. Now: manual review explaining why |
| Columns aligned with runs of spaces (plain-text reports) were read as one line | **A table row reflowed into running text**. Now each column is a separate cell |
| Reflowed lines were written after the end of the whole text object | Copy/paste, search and screen readers read the paragraph's tail after the rest of the column. Now the lines are placed right after the paragraph |
| Bookmarks, links and /OpenAction pointing at the edited page counted as document changes | Every edit on such pages was refused |
| The checker's text positions were relative to the media box corner, not PDF coordinates | Every edit on pages whose media box does not start at (0, 0) (e.g. IRS forms) was refused |
| An instruction split across two content streams (Distiller) | Text lost from the model; the page crashed or edits were refused |
| A CFF font with an unreadable charset; a `TJ` without operands | Internal error on the whole document |
| Password-protected or broken PDFs | `ingest_document` raised an exception instead of returning an error, and left a half-made document in the store |
| Justified or individually positioned glyphs (OpenOffice, Distiller kerning) | Same-letter swaps fell back to other methods or were refused. Method a now reproduces the original's letter and word spacing |
| The checker's pdfminer drops character spacing (Tc) after each `TJ`, and compared positions rounded to 0.01 pt; anti-aliasing flips on one glyph; overlaps that already existed (italic "y.", text on form rules) | Correct edits were refused. Now: spec-correct Tc, a 0.005 pt tolerance, a re-check at a second resolution, and comparison with the original |
| Horizontal boundary measured left of where the line already ended | Edits in justified lines were refused |

### Synthetic year-end-style pack (offline provider)

`tests/data/synthetic-pack` (built by `tests/make_synthetic_pack.py`) has the
same shape: one `scenarios.json` with an `expected_safe_result` per scenario
next to the PDFs. It was run through the real pipeline (sandbox and checks
included) with the offline stand-in for the LLM. No scenario produced an unsafe
edit; every scenario's result is reported:

<!-- SYNTHETIC-OFFLINE:BEGIN -->
### synthetic-pack (offline)

Verdicts: PASS 20

| ID | Expected | Result | Method / reason | Verdict |
|---|---|---|---|---|
| A01 | updated | updated | original_box | PASS |
| A02 | updated | updated | original_box | PASS |
| A03 | updated | updated | original_box | PASS |
| B01 | updated | updated | horizontal_expansion | PASS |
| B02 | updated | updated | horizontal_expansion | PASS |
| C01 | updated | updated | reduced_font | PASS |
| C02 | manual_review | manual_review | not_enough_space: not enough space for the replacement. original_box: replacement needs 310.6 pt but the original box is 123.5 pt wide / horizontal_expansion: replacement needs 310.6 pt; only 133.4 pt are available up to | PASS |
| D01 | manual_review | manual_review | not_found_graphic: the text was not found on page 1 or the pages either side; parts of the page are drawn as images or vector graphics, so text that is visible on the page may not exist as text (graphic text is out of sc | PASS |
| D02 | manual_review | manual_review | scanned_page: page 1 is a scanned image with an invisible OCR text layer; its visible text cannot be edited | PASS |
| E01 | updated | updated | form_field_update | PASS |
| F01 | manual_review | manual_review | signed_pdf: the PDF is digitally signed; any edit would invalidate the signature. Ask the signer for an unsigned source document. | PASS |
| G01 | manual_review | manual_review | ambiguous: 2 places match equally well; the reviewer must say which one is meant | PASS |
| H01 | manual_review | manual_review | not_found: the text was not found on page 1 or the pages either side | PASS |
| H02 | manual_review | manual_review | missing_glyphs: the embedded font BAAAAA+LiberationSans does not contain: 'z'. The font must not be switched, so a person has to make this change. | PASS |
| R01 | updated | updated | paragraph_reflow | PASS |
| R02 | manual_review | manual_review | not_enough_space: not enough space for the replacement. original_box: replacement needs 992.8 pt but the original box is 116.8 pt wide / horizontal_expansion: replacement needs 992.8 pt; only 116.8 pt are available up to | PASS |
| X01 | updated | updated | paragraph_reflow | PASS |
| X02 | updated | updated | original_box | PASS |
| S01 | updated | updated | original_box | PASS |
| S02 | manual_review | manual_review | ambiguous: 2 places match equally well; the reviewer must say which one is meant | PASS |
<!-- SYNTHETIC-OFFLINE:END -->

## What still goes to manual review, and why

| reason_code | when | what the reviewer can do |
|---|---|---|
| `not_found` | no candidate scores ≥ 90 on the page or the pages either side | check the source text / page; the report lists the closest candidates and their scores |
| `not_found_graphic` | as above, and the page has images or filled vector shapes (D01-type graphic text) | edit the source document; graphic text is out of scope |
| `ambiguous` | several places match equally well | the report lists each candidate's page and box; make the source text more specific |
| `no_change` | the document already has the replacement wording there | nothing to do |
| `signed_pdf` | the PDF carries a digital signature (or DocMDP permissions) | any edit would break the signature; get an unsigned source |
| `encrypted` | the PDF is encrypted | supply an unencrypted copy |
| `scanned_page` | the page is an image (D02-type), possibly with an invisible OCR layer | changing the OCR text would not change what the page shows |
| `missing_glyphs` | the embedded (usually subset) font has no glyph for a character of the replacement | the font may not be switched; edit the source document or reword |
| `mixed_formatting` | the target mixes fonts, sizes, colours or spacing | split the change, or edit the source |
| `not_enough_space` | no allowed method fits; the reason gives the needed and available widths per method | shorten the wording or edit the source |
| `unsupported` | rotated/skewed or vertical text, Type3 fonts, non-Identity CID encodings, unusual form fields | edit the source |
| `planner_manual_review` | the planning model judged the change unsafe | read its reason in the report |
| `checks_failed` | every attempt (2 retries per method) failed the safety checks or did not run | the report keeps every script, its output and the failed checks |
| `llm_budget`, `llm_error` | call/cost limit reached, missing key, Bedrock error | fix the configuration and rerun |
| `internal_error` | unexpected exception (trace in the report) | report it |
| `needs_clarification` | (recommendations) the editor cannot tell what is meant; its question is in the reason | answer it by rewording the recommendation |
| `not_possible`, `partly_not_possible` | (recommendations) the request is not, or not only, a replacement of text on that page (images, colours, layout, new paragraphs, other pages); nothing is applied | make that part by hand, or split the recommendation |
| `plan_rejected` | (recommendations) a quoted text is not on the page word for word, appears more than once without a distinguishing line, or edits overlap | reword or rerun; the report lists each problem |
| any of the above for one edit | (recommendations) the first edit that cannot be made safely; nothing is applied | the report has every edit's own report under `edits/NN/` |

## What was not run here

* **Windows.** The tool was built and tested in a Linux container. The Windows
  Job Object code (`doc_update/sandbox/winjob.py`: process created suspended,
  assigned to the job, memory limit, kill-on-close, UI limits) and the PowerShell
  steps in the README have not been executed; on Linux the sandbox uses
  `RLIMIT_AS`, which the tests exercise. `tests/test_sandbox.py::test_windows_job_object_limits`
  runs automatically on Windows.
* **Live Bedrock.** No Bedrock key was available, so no live call was made. The
  Converse request/response handling is tested with recorded and scripted
  responses. The default model is the newest Claude Sonnet (`claude-sonnet-5-5`)
  inference profile, chosen on first run from the profiles your key can list;
  check the choice with `python -m doc_update models`. Claude Sonnet 5.5 only
  accepts its default temperature, so the configured `temperature: 0` is sent,
  refused, and the call retried without it (recorded in the audit log); determinism
  then comes from the JSON-schema tool output, validation in code, and the checks.
* **make_cases.py cases**, and the year-end pack and the recommendations with a live
  model (see above). The editor prompt was tested with hand-written answers. Its
  behaviour with Claude on Bedrock (wording, the document's style, when to ask)
  should be checked with `python -m doc_update.recommend_pack --provider bedrock`
  before production use.

## Known limitations

* Characters that are not in the embedded font subset are never added; this is
  by design (no font switching) and is the most common reason for manual review
  with real PDFs made by Word or macOS.
* Paragraph reflow (method d) requires one font, size and colour throughout the
  reflowed lines, never hyphenates, and refuses paragraphs with words hyphenated
  across lines. New lines are drawn in a new text block placed right after the
  paragraph's last original line, so the reading order is kept. If the text
  object cannot be split cleanly there, the block goes after the text object
  instead. In tagged PDFs the new lines are not linked to the structure tree, and
  `/ActualText` spans are not updated. Rewrapped lines use single spaces between
  words, so double spaces after full stops in the original are not kept.
* Form fields: text fields with an existing appearance stream, in the field's own
  font. In multi-line fields only changes that stay on one line of the appearance
  are made (the rest of that line moves; line breaks are not changed). Comb,
  password, rich-text and choice fields go to manual review.
* Layout analysis (lines, paragraphs, columns, table cells) is heuristic. When it
  is wrong the checks usually reject the damage, so the result is manual review
  rather than a bad edit, but a workable change may be missed. The corpus test
  showed one kind of damage the checks cannot see: a change of *layout* inside the
  allowed area (a table row rewrapped as running text). Space-aligned columns are
  now treated as cells, but with the offline stand-in no vision check confirms the
  region type; use the Bedrock vision check in production.
* Matching is strict about content. Numbers, whole words, accents and separate
  marks at either end must match the PDF exactly; only spacing, punctuation, case
  and small typos inside the text may differ. A request that names the wrong page
  is matched on a neighbouring page only when the requested page does not contain
  the text at all.
* Pages whose glyph advance widths do not match the glyph positions (seen in some
  iText output) cannot be laid out reliably; text on them goes to manual review.
* The independent glyph check covers embedded TrueType, CFF, OpenType and Type 1
  programs. The unit-test fixtures only contain TrueType fonts; CFF and Type 1 were
  exercised by the corpus test (LaTeX, Distiller and Ghostscript papers).
  Characters pdfminer cannot decode (no ToUnicode) inside the change area fail
  the check.
* Pixel identity is measured with the bundled pdfium; other viewers may
  anti-alias differently, but the before/after comparison always uses one renderer.
* The local sandbox is a safety layer for internal use, **not a hard security
  boundary** (see README).
* The cost limit uses the prices in `config.yaml`; set them to your Bedrock prices.
* The store uses a simple lock file; keep `docs\` out of OneDrive sync conflicts
  (or move `store.root`).
