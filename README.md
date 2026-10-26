# Document Update Tool

Turns an approved compliance recommendation ("replace this wording on page N
with that wording") into a **safe, reviewable change inside the uploaded PDF**.

* An LLM on **Amazon Bedrock** (Converse API, called with your Bedrock API key)
  checks the layout, chooses an update method and writes a short Python script
  for the change.
* The script runs in a **sandbox** and can only use the project's helper library
  `pdf_ops` (plus pikepdf, fontTools, pypdfium2, pdfplumber, math, json, re, decimal).
* **Separate checking code** — not the script and not the LLM — decides whether
  the result is safe. Nothing reaches status `updated` unless every check passes.
* The original PDF is never modified. A person approves or rejects every candidate.

Result of one change request:

| status | what you get |
|---|---|
| `updated` | `pending/<change_id>/candidate.pdf` plus `report.json` (method, target location, font-size change, every check result, before/after/difference images), waiting for review |
| `manual_review` | no PDF; `report.json` with a reason a reviewer can act on: not found, ambiguous (with each candidate's page and position), not enough space (with the measured widths), missing characters in the font, signed PDF, scanned page, failed checks, … |

When a change is approved, the candidate becomes the next version
(`versions/v001.pdf`, `v002.pdf`, …). When it is rejected, the candidate is deleted.

---

## How it works (one change request)

1. **Analyse** the current version (plain code): every word and character with
   position, font, effective size (Tf × text matrix × CTM, so macOS "1 Tf" PDFs
   are right) and colour; content-stream structure (BT/ET, Tm/Td/TD/T*, TJ
   kerning, form XObjects, multiple content streams); fonts (type, encoding,
   which glyphs the embedded subset really contains, widths); form fields,
   annotations, images, vector graphics, signatures; a 150 dpi render.
2. **Find the target** (plain code): quotes, dashes, ligatures, soft hyphens,
   words hyphenated across lines, whitespace and case are normalised; fuzzy
   matching on the given page first (page text and form-field values), then the
   pages either side. A match must score ≥ 90 and clearly beat the next best.
3. **Measure the space** (plain code): line, paragraph and column; the column's
   right edge from neighbouring lines and table rules; the nearest text or
   graphic to the right and below; free space below the paragraph; margins;
   missing glyphs.
4. **Layout check** (LLM with vision): page render with the target box (red) and
   measured boundary (blue). Structured JSON advice; wherever it disagrees with
   the measurements the more cautious answer is used.
5. **Plan** (LLM, tool use, temperature 0): first method that can work, in order:
   `original_box` → `horizontal_expansion` → `reduced_font` (never below
   `min_font_scale`, default 0.90) → `paragraph_reflow` → `form_field_update`.
   Code enforces the order and clamps the parameters to the measurements. The
   font is never switched.
6. **Write the script** (LLM) using `pdf_ops`.
7. **Run it in the sandbox.**
8. **Check the result** (plain code): text and 150 dpi pixels identical outside
   the allowed area (2-pixel margin); inside the area the text reads exactly as
   the original with only the target replaced; the old text is gone from the
   content stream (not covered); no new fonts, no missing glyphs, no size below
   the minimum; the new text stays inside its boundary and overlaps nothing;
   page count, page sizes, form fields, annotations, images and links unchanged.
9. **Retry**: on a failed script or failed check, the error and check report go
   back to the LLM for a corrected script — at most 2 retries per method, then
   the next workable method; if all fail → `manual_review`.
10. **Visual review** (optional, LLM): compares before/after crops and can only
    add warnings for the human reviewer.

Every LLM prompt puts PDF text inside a `<document_data>` block, JSON-encoded,
and says it is untrusted data, never instructions.

---

## Set-up on Windows (PowerShell)

Python 3.11 or later must be installed (python.org installer, with the `py` launcher).
The two virtual environments live **outside OneDrive**, in `C:\Users\soley\.venvs\`.

```powershell
cd "C:\Users\soley\OneDrive\Documents\edit tool"

# create both environments and install the exact versions (one-off)
py -3 -m venv "$env:USERPROFILE\.venvs\doc-update"
py -3 -m venv "$env:USERPROFILE\.venvs\doc-update-sandbox"
& "$env:USERPROFILE\.venvs\doc-update\Scripts\python.exe" -m pip install --upgrade pip
& "$env:USERPROFILE\.venvs\doc-update\Scripts\python.exe" -m pip install -r "requirements.txt"
& "$env:USERPROFILE\.venvs\doc-update-sandbox\Scripts\python.exe" -m pip install --upgrade pip
& "$env:USERPROFILE\.venvs\doc-update-sandbox\Scripts\python.exe" -m pip install -r "requirements-sandbox.txt"

# use the main environment in this window
& "$env:USERPROFILE\.venvs\doc-update\Scripts\Activate.ps1"
python -m doc_update check-setup
```

The same steps are in `scripts\setup_windows.ps1`:

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\setup_windows.ps1"
```

If PowerShell refuses to run `Activate.ps1`, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or call
`"$env:USERPROFILE\.venvs\doc-update\Scripts\python.exe"` directly instead of `python`.

The sandbox environment only contains the PDF libraries. `pdf_ops` is copied
into it automatically (into `doc-update-sandbox\pdf_ops_lib`) whenever it changes.

### Bedrock API key and region

The key is read from `AWS_BEARER_TOKEN_BEDROCK`, the region from `AWS_REGION`.
The tool never prints, logs or stores the key, and never passes it to the sandbox.

For the current PowerShell window only (the key is typed, not left in the history):

```powershell
$env:AWS_BEARER_TOKEN_BEDROCK = [System.Net.NetworkCredential]::new("", (Read-Host "Bedrock API key" -AsSecureString)).Password
$env:AWS_REGION = "eu-west-2"     # your Bedrock region
python -m doc_update check-setup   # shows bedrock_key_set: true (never the key itself)
python -m doc_update models        # lists the Claude models / inference profiles the key can use
```

To keep them for your Windows user (stored in your user profile's registry):

```powershell
[Environment]::SetEnvironmentVariable("AWS_REGION", "eu-west-2", "User")
[Environment]::SetEnvironmentVariable("AWS_BEARER_TOKEN_BEDROCK", [System.Net.NetworkCredential]::new("", (Read-Host "Bedrock API key" -AsSecureString)).Password, "User")
```

Model IDs per role are in `config.yaml` (`vision_model`, `planner_model`,
`coder_model`). The default `auto` picks the newest Claude Sonnet inference
profile the key can use (`claude-sonnet-5-5`, preferring a `global.` profile,
then your region's geography) on first run and caches the choice in
`docs\.model_cache.json`. You can also write an ID, e.g.
`global.anthropic.claude-sonnet-5-5`.

---

## Run the tests

```powershell
cd "C:\Users\soley\OneDrive\Documents\edit tool"
& "$env:USERPROFILE\.venvs\doc-update\Scripts\Activate.ps1"
python -m pytest                      # everything offline; no Bedrock key needed
```

Real-PDF scenarios — copy the evaluation data first (the originals are never modified):

```powershell
python "scripts\import_eval_data.py" --eval-root "C:\Users\soley\pdf-surgeon-eval"
python -m pytest -m scenarios         # year-end pack and generated cases, offline
python -m doc_update.scenarios --pack "tests\data\year-end-test-pack" --provider offline --results-md "RESULTS.md"
```

Live mode (runs the year-end scenarios through Bedrock and records the responses
under `tests\data\recorded\live` so they can be replayed offline):

```powershell
python -m pytest -m live
python -m doc_update.scenarios --pack "tests\data\year-end-test-pack" --provider bedrock --results-md "RESULTS.md"
```

Manual mode (a person, or another model, answers each LLM call by hand). Every
call is written to `<manual-dir>\<scenario>\NN_<role>.request.md` with its
images; the answer goes in `NN_<role>.json` as the tool input the model would
return. Run it again after each answer. The answers go through the same parsing,
schema checks, sandbox and checks as Bedrock answers. `tests\data\manual-answers\year-end`
holds a complete set for the year-end pack:

```powershell
python -m doc_update.scenarios --pack "tests\data\year-end-test-pack" --provider manual --manual-dir "tests\data\manual-answers\year-end"
```

Corpus test on any folder of PDFs (your own documents, or a downloaded collection).
For each PDF it generates a few dozen change requests: letter and digit swaps,
reversed phrases and lines, shorter and longer wording, two-line targets, fuzzy
find text, the wrong page, missing characters, text that is not there,
ambiguous words and form-field values. Every edit the tool makes is then checked
by an independent oracle (pdfium text and pixels, fonts, other pages, qpdf),
which shares no code with the tool's own checks. The input files are never
modified.

```powershell
python "scripts\corpus_test.py" --corpus "C:\path\to\pdfs" --work "C:\path\to\corpus-run" --workers 4
# results: corpus-run\report.md, corpus-run\results.jsonl; damaged candidates are kept under corpus-run\docs\
```

`--provider bedrock` runs the same requests through Bedrock. That costs money,
about 3–6 calls per request, so start with `--max-docs 3`.

---

## Run one example change and approve it

```powershell
cd "C:\Users\soley\OneDrive\Documents\edit tool"
& "$env:USERPROFILE\.venvs\doc-update\Scripts\Activate.ps1"

python -m doc_update ingest --pdf "tests\data\synthetic-pack\report.pdf" --doc-id example-report

python -m doc_update update --doc-id example-report --page 1 `
    --find "financial year ending 31 December 2023" `
    --replace "financial year ending 31 December 2024"
# prints status, change_id, report_path and candidate_path
# (add --provider offline to try it without Bedrock)

# look at docs\example-report\pending\<change_id>\report.json and images\before.png, after.png, difference.png
python -m doc_update status  --doc-id example-report
python -m doc_update approve --doc-id example-report --change <change_id> --by "Your Name"
# or
python -m doc_update reject  --doc-id example-report --change <change_id> --by "Your Name" --note "reason"
```

Exit codes: `0` done, `2` manual review needed, `1` error.

### From your application

```python
from doc_update import ingest_document, update_document, approve_change, reject_change, document_status

ingest_document(r"C:\path\policy.pdf", "policy-2024")
result = update_document("policy-2024", find="31 December 2023", replace="31 December 2024", page=3)
if result["status"] == "updated":
    # show result["candidate_path"] and the images listed in result["report_path"] to a reviewer
    approve_change("policy-2024", result["change_id"], approver="J. Smith")
else:
    print(result["reason_code"], result["reason"])
```

Each function returns a dict (`status` is `ingested`, `updated`, `manual_review`,
`approved`, `rejected`, `ok` or `error`).

---

## Document store

```
docs\<doc_id>\original.pdf                read-only, SHA-256 in the manifest
docs\<doc_id>\versions\v001.pdf, ...      approved versions (read-only)
docs\<doc_id>\pending\<change_id>\        candidate.pdf, report.json, images\, scripts\, logs\
docs\<doc_id>\history\<change_id>\        report, images, scripts and logs of decided / manual-review changes
docs\<doc_id>\manifest.json               versions, hashes, changes and approvals
docs\<doc_id>\audit.log                   one JSON line per event (LLM calls: role, model, prompt hash,
                                          token counts, response time, estimated cost; never the key or PDF text)
```

Every change works on the latest approved version (or the original). Approving
re-checks the SHA-256 of the original, of every version and of the candidate,
and refuses a candidate made on an older version. The store root can be moved
out of OneDrive with `store.root` in `config.yaml` (OneDrive sync can briefly
lock files).

---

## Configuration (`config.yaml`)

`editing.min_font_scale` (0.90), `editing.retries_per_method` (2),
`editing.methods` (order / enabled methods), `matching.min_score` (90),
`matching.ambiguity_margin` (5), `checks.render_dpi` (150),
`checks.pixel_margin` (2), `llm.*_model`, `llm.max_calls_per_change` (12),
`llm.max_cost_per_change_usd` (1.00), `llm.prices_per_million_tokens` (set to
your Bedrock prices — used only for the estimate), `llm.provider`
(`bedrock`, `offline` = plain-code stand-in, `replay` = recorded responses,
`manual` = answers written by hand into `llm.manual_dir`),
`sandbox.timeout_seconds` (60), `sandbox.memory_mb` (1536).

Notes on the current Claude models on Bedrock: Claude Sonnet 5.5 accepts only its
default sampling temperature and rejects forced tool choice. The tool therefore
sends `temperature: 0` and, if the model refuses it, retries once without it
(recorded in the audit log); tool use is requested with `toolChoice: auto` and
the answer is validated against the JSON schema in code.

---

## The sandbox — read this

The local sandbox is a **safety layer for internal use, not a hard security
boundary.** It makes accidents and simple misuse unlikely:

* the script is parsed with `ast` and rejected unless it imports only the
  allowlist, uses no `exec`/`eval`/`compile`/`__import__`/`getattr`, touches no
  private or dunder attributes, passes only literal plain file names to anything
  that opens or saves files, and contains the exact replacement text;
* it runs with `python -I`, an empty environment (only `SYSTEMROOT` on
  Windows — never the Bedrock key or any other secret), in a fresh temporary
  folder containing only `input.pdf`, with a 60 s timeout and a memory limit set
  through a **Windows Job Object** (kill-on-close, process limit, UI restrictions);
* an audit hook refuses reads and writes outside the folder, subprocesses,
  sockets, file deletion and loading native libraries.

A determined attacker who controls the script could still find a way around
these layers (it is ordinary Python on your machine). The real safety gate is
the independent check in step 8, and a person approves every change. The
sandbox sits behind the `Sandbox` interface (`doc_update/sandbox/__init__.py`),
so a container or virtual-machine implementation can replace it later.

---

## Project layout

```
pdf_ops\            helper library used by generated scripts (content interpreter, fonts,
                    edit engine, methods a-e, forms, reflow)
doc_update\         pipeline, analysis, matching, measurement, checks, LLM providers,
                    sandbox, store, CLI and API
tests\              unit, end-to-end (recorded LLM responses), scenario and live tests;
                    tests\pdfgen.py builds the synthetic PDFs; tests\data\fonts holds
                    Liberation Sans (SIL OFL) used only to build test PDFs
scripts\            setup_windows.ps1, import_eval_data.py, corpus_test.py (verified tests on any folder of PDFs)
```

Libraries: pikepdf (MPL-2.0), pdfplumber and pdfminer.six (MIT), pypdfium2
(Apache-2.0/BSD-3), fontTools (MIT), RapidFuzz (MIT), Pillow, numpy, boto3,
PyYAML, pytest. No PyMuPDF. All code here was written from scratch.

Licence: MIT (see `LICENSE`).
