# Results

Build date: 24 October 2026; year-end pack tested 25 October 2026. Built and tested in a
Linux container (Python 3.11.17; the last full test run used 3.13.17), not on the
target Windows machine; see "What was not run here" below.

## Test suite

`python -m pytest` (offline, no Bedrock key): **all tests pass** — 171 passed,
2 skipped (Windows Job Object test, generated cases not imported), 1 deselected (live).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_checks.py` | 40 | Step 8 gate. Correct edits pass for every font/operator style (WinAnsi, MacRoman, custom Differences, Type0 Identity-H, non-embedded Helvetica, "1 Tf" + text-matrix size, TJ kerning, Td/TD/T*/'/", multiple content streams). Deliberately broken edits are **all rejected**: neighbouring words shifted, space lost, spaces squeezed out of the replacement, line deleted, white box over the old text, text running into the next column, reflow running into the next paragraph, another form field changed, edit leaking onto a second page that shares the content stream, new font object, font below the minimum scale, glyphs missing from the subset, old text hidden with an invisible render mode, annotation removed, added document JavaScript, added page action, and an unchanged copy. |
| `tests/test_pdf_ops.py` | 28 | Fonts (encodings, widths, real subset coverage, AFM metrics), interpreter (all positioning operators, kerning, text-matrix size, form XObject matrix), edit engine (following text stays in place, same font resource, shared content stream copied, XObject drawn twice copied and renamed, other page keeps the original), methods a–e, reflow (free space, justified, mixed fonts refused). |
| `tests/test_sandbox.py` | 34 | Static rejection of imports, exec/eval/compile/__import__/getattr, dunder access, absolute and parent paths, computed file names, `pikepdf.Job`, classes; empty environment (proved at runtime); audit hook blocks reads/writes outside the folder, subprocess, sockets, `os.system`, file deletion; timeout; memory limit; errors reported. |
| `tests/test_pipeline_e2e.py` | 24 | Whole pipeline offline with **recorded Bedrock Converse responses**: in-place change, white-box script rejected then corrected, crashing script retried, planner's wrong order overridden, all attempts failing → manual review, form field, cautious layout advice disabling expansion, LLM call budget, missing key; PDF text is passed only inside the untrusted `<document_data>` block; manual-review reasons (not found, ambiguous, signed, scanned, missing glyphs, not enough space, graphic text, no change). |
| `tests/test_llm.py` | 14 | Converse logic without network: temperature refused → retried without it, missing tool call → one reminder, schema validation, throttling retry, secrets scrubbed from errors, budget, audit fields, prompt hash, key/region required, bearer token preferred by botocore, model auto-selection. |
| `tests/test_store.py` | 8 | Read-only original with SHA-256, v001/v002 chain, reject deletes the candidate, stale candidate refused, tampered candidate/original refused, manual review recorded without PDF, CLI round trip and exit codes. |
| `tests/test_matching.py` | 10 | Normalisation, exact/approximate/cross-line matches, ambiguity with positions, neighbouring page, form-field values. |
| `tests/test_analysis.py` | 9 | Words/chars, effective size from the text matrix, encodings, signed/scanned/shared detection, fields, measurements (table cell, two columns, TOC dot leaders, free space, missing glyphs). |
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
* **make_cases.py cases**, and the year-end pack with a live model (see above).

## Known limitations

* Characters that are not in the embedded font subset are never added; this is
  by design (no font switching) and is the most common reason for manual review
  with real PDFs made by Word or macOS.
* Paragraph reflow (method d) requires one font, size and colour throughout the
  reflowed lines, never hyphenates, and refuses paragraphs with words hyphenated
  across lines. New lines are drawn in a new text block placed right after the
  paragraph's original block; in tagged PDFs they are not linked to the
  structure tree, and `/ActualText` spans are not updated.
* Form fields: text fields with an existing appearance stream, in the field's own
  font. In multi-line fields only changes that stay on one line of the appearance
  are made (the rest of that line moves; line breaks are not changed). Comb,
  password, rich-text and choice fields go to manual review.
* Layout analysis (lines, paragraphs, columns, table cells) is heuristic. When it
  is wrong the checks reject the damage, so the result is manual review rather
  than a bad edit, but a workable change may be missed.
* The independent glyph check covers embedded TrueType, CFF, OpenType and Type 1
  programs; the test fixtures only contain TrueType fonts (Liberation Sans), so CFF
  and Type 1 paths are less exercised. Characters pdfminer cannot decode
  (no ToUnicode) inside the change area fail the check.
* Pixel identity is measured with the bundled pdfium; other viewers may
  anti-alias differently, but the before/after comparison always uses one renderer.
* The local sandbox is a safety layer for internal use, **not a hard security
  boundary** (see README).
* The cost limit uses the prices in `config.yaml`; set them to your Bedrock prices.
* The store uses a simple lock file; keep `docs\` out of OneDrive sync conflicts
  (or move `store.root`).
