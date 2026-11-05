# RUNBOOK — OCR Financial-Statement Processing

**Operational guide: how to run the scripts, read the outputs, and recover from errors.**

| | |
|---|---|
| **Project** | `br_ocr_json_processing` |
| **Purpose** | Turn raw OCR JSON into structured, classified, **SBS-labelled** financial statements, **one value per SBS field** (`sbs_fields`), and a polished **Allianz Excel** report |
| **Audience** | Operators & developers running the pipeline |
| **Scope** | *How to run* — for *how it works*, see `docs/` (linked below) |

> This is an **operational** runbook, not an algorithm description. **Every step in the order the code
> runs it** (what it does, what it writes, why) is in [`docs/Pipeline_Overview.md`](docs/Pipeline_Overview.md)
> — start there. Methodology per stage:
> [`docs/OCR_Table_Reconstruction.md`](docs/OCR_Table_Reconstruction.md) (Stage 1),
> [`docs/Primary_Statement_Classification.md`](docs/Primary_Statement_Classification.md) (Stage 2),
> [`docs/SBS_Labelling.md`](docs/SBS_Labelling.md) (Stage 3 + `sbs_fields`),
> [`docs/Excel_Export.md`](docs/Excel_Export.md) (Stage 4),
> [`docs/Words_Recovery.md`](docs/Words_Recovery.md) (optional words layer),
> [`docs/Frontend_Streamlit.md`](docs/Frontend_Streamlit.md) (web UI).

---

## Table of Contents

1. [TL;DR — the one command](#1-tldr--the-one-command)
2. [Environment & Dependencies](#2-environment--dependencies)
3. [File Map — inputs, outputs, code](#3-file-map--inputs-outputs-code)
4. [The Pipeline at a Glance](#4-the-pipeline-at-a-glance)
5. [Run Scenarios](#5-run-scenarios)
6. [Reading the Outputs](#6-reading-the-outputs)
7. [Editing the Anchor Lexicon](#7-editing-the-anchor-lexicon)
8. [SBS Labelling (Stage 3)](#8-sbs-labelling-stage-3)
9. [Excel Export (Stage 4)](#9-excel-export-stage-4)
10. [Streamlit Front-End (web UI)](#10-streamlit-front-end-web-ui)
11. [Validation Checklist for a New Document](#11-validation-checklist-for-a-new-document)
12. [Troubleshooting](#12-troubleshooting)
13. [Quick Command Reference](#13-quick-command-reference)

---

## 1. TL;DR — the one command

From the project directory, process a document end-to-end (Stage 1 table reconstruction, Stage 2 page
classification, Stage 3 SBS labelling and one value per SBS field):

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 structure_ocr.py comprehansive_sample.json -o my_output.json
```

This writes **`my_output.json`** (the result: statements with `sbs_label` on every matched row, page
classification, `sbs_fields`) and **`my_output_report.json`** (quality report), and prints a summary to
the console.

> ⚠️ **Always pass `-o` with a new file name.** Without `-o` the result goes to `structured_output.json`,
> and the `structured_output*.json` files are the **goldens** of the regression harness
> (`harness/run.py goldens`, §3). They are not in git (`*.json` is git-ignored): an overwritten golden
> cannot be restored. Regenerate goldens only deliberately, after the diff has been reviewed.

Then render the polished **Allianz Excel** report (Stage 4 — a separate step):

```bash
python3.13 export_excel.py my_output.json            # -> my_output_report.xlsx
```

This writes **`my_output_report.xlsx`** (sheets: metadata / balance / income / cash_flow / sbs_fields).

Or skip the CLI entirely and use the **web UI** — upload a file, review the statements, download the
Excel (§10):

```bash
python3.13 -m streamlit run app.py      # then open http://localhost:8501
```

---

## 2. Environment & Dependencies

| Requirement | Version (verified) | Required? | Notes |
|---|---|---|---|
| Python | 3.13.12 (`python3.13`) | **Yes** | Standard library only for the core. |
| `pyyaml` | 6.0.3 | For Stage 2 | Reads `anchors.yaml` (and `labels_OLD.yaml` in `build_sbs_mapping.py`). |
| `rapidfuzz` | 3.14.5 | Optional | Better/faster fuzzy matching; falls back to stdlib `difflib` if absent. |
| `openpyxl` | 3.1.5 | For Stage 4 | Writes the Excel workbook (`export_excel.py`); reads the SBS xlsx in `build_sbs_mapping.py`. |
| `streamlit` | 1.58.0 | For the web UI | Only `app.py` needs it (pulls in `pandas`). |

**Verify the environment:**

```bash
python3.13 --version
python3.13 -c "import yaml; print('pyyaml', yaml.__version__)"
python3.13 -c "import rapidfuzz; print('rapidfuzz', rapidfuzz.__version__)"   # optional
python3.13 -c "import openpyxl; print('openpyxl', openpyxl.__version__)"      # Stage 4
python3.13 -c "import streamlit; print('streamlit', streamlit.__version__)"   # web UI
```

**Install missing packages (corporate proxy required):**

```bash
python3.13 -m pip install pyyaml rapidfuzz openpyxl streamlit \
  --proxy http://127.0.0.1:9000 \
  --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

> **`rapidfuzz` is optional.** If it is not installed, the code automatically uses `difflib`. The
> console reports which backend is active: `[fuzzy backend: rapidfuzz]` or `[fuzzy backend: difflib]`.
> Стейкхолдерам: отсутствие `rapidfuzz` не ломает пайплайн — только чуть снижает качество нечёткого
> сопоставления названий.

---

## 3. File Map — inputs, outputs, code

### Inputs (you provide)

| File | Description |
|---|---|
| `comprehansive_sample.json` | The raw OCR document (reference sample). Any same-shape OCR JSON works. Excel type **DFP_4COL**. |
| `sample_2.json` | Second test document (15 pages, 2-column, period 2024/2023). Excel type **TWO_COL**. |
| `itr_samples.json` | ITR quarterly form — has **populated `CODE_VALUE`** (hierarchical row codes) and `CLOSING_DATE` as a group. Excel type **ITR**. |
| `provider_sample.json` | Serasa Experian dump with **ALL-UPPERCASE labels** and reversed N-1/N columns. Excel type **SERASA** (provider tag `SERASA EXPERIAN`). |
| `complicated_empty_labels_sample.json` | 2-page doc, empty labels, no codes/headers; balance reads `TOTAL DO PASSIVO` (Stage-2 mislabels NOTE). Excel type **TWO_COL**; balance rescued by the small-span override (§9). |
| `complicated_sample_2 (Demonstrativos 2023 Alvorada Pneus Distribuidora).json` | 3 pages, ALL-CAPS labels, no codes; **side-by-side balance** (Ativo left, Passivo right on the same lines). Excel type **UPPERCASE**. |
| `serasa_report_1.json`, `serasa_report_2.json` | Serasa Experian reports; the second splits the balance over pages 3-5. Excel type **SERASA**. |
| `anchors.yaml` | **Config** — anchor lexicon for Stage 2/3. Edit to tune detection (see §7). |
| `sbs_label_mapping.json` | **Config** — SBS Field → Portuguese aliases for Stage 3. Generated by `build_sbs_mapping.py` (see §8). |
| `labels_OLD.yaml`, `SBS_fields_mapping_with_INDIAN_labels.xlsx` | Source files for the mapping build (see §8). |
| `harness/ground_truth.yaml` | **Config** — statement pages, Serasa zones and eye-verified golden cells of the 22-PDF corpus. |
| `_recital/`, `_inbox/`, `_audit/` | reciTAL exports: harness corpus / new documents / independent audit. Confidential, git-ignored. |

> **Keep outputs separate.** Each test document writes to its own output file — do **not** overwrite
> another document's result, and never a golden (below).

### Code

| File | Role | Run directly? |
|---|---|---|
| `structure_ocr.py` | **Main entry point.** Stages 1-3 + `sbs_fields` (Stage 2 runs inside Stage 1). | **Yes** — has a CLI. |
| `sign_proof.py` | Stage-1 step 5b — lost minus / dash read as «1» repaired only when the code tree or a printed subtotal proves it (income statement: the dash test only, never a sign flip). | **No** — library. |
| `sbs_labeler.py` | Stage-3 SBS labeller. Also a standalone post-processor CLI. | **Yes** — CLI or library (imported by `structure_ocr.py`). |
| `sbs_fields.py` | Stage-3 end — one value per SBS field and period (sums / printed totals / identity-proven picks, hierarchy-aware). | **No** — library. |
| `selection.py` | Which rows / figures the export shows (`passing_rows`, `pick_cells`, `consolidado_filter`, ITR `form_pages`, `is_per_share`) and why a row is flagged (`review_reason` → the «Review» column). | **No** — library (Excel, Streamlit, harness, `sbs_fields`). |
| `doc_type.py` | Document type + filter policy (single source of truth). | **No** — library. |
| `export_excel.py` | **Stage 4** — render structured JSON to Allianz Excel. | **Yes** — has a CLI. |
| `app.py` | Streamlit web UI (upload / review / download the same Excel). | **Yes** — `streamlit run`. |
| `words_recovery.py` | OPTIONAL — recovery from the reciTAL OCR words layer. | **No** — `structure_ocr.py --words`. |
| `build_sbs_mapping.py` | Regenerates `sbs_label_mapping.json` from YAML + xlsx (+ `EXTRA_ALIASES`, twin groups, code map, the pinned liability mapping). | **Yes** — run after editing the sources (§8). |
| `diagnose_pages.py` | Read-only Stage-2 diagnostics → table + `page_diagnostics.json` (simplified preview: page-level columns). | **Yes** — but run *from the project dir* (uses relative paths). |
| `classify_statements.py` | Stage-2 classifier logic. | **No** — library; used by `structure_ocr.py` or imported. |
| `anchors_lib.py` | Shared primitives (matching, parsing, geometry). | **No** — library. |
| `harness/run.py`, `harness/hcore.py` | Regression harness (22 reciTAL PDFs, goldens, selftest). | **Yes** — `python3.13 -E -P`. |
| `harness/recital_fetch.py`, `harness/audit_run.py` | READ-ONLY reciTAL access; independent-audit runner (`AUDIT_BRIEF.md`). | **Yes** — `python3.13 -E -P`. |

### Outputs (generated)

| File | Produced by | Contents |
|---|---|---|
| `structured_output.json`, `structured_output_sample2.json`, `structured_output_itr.json`, `structured_output_provider.json`, `structured_output_complicated.json`, `structured_output_complicated_sample2.json`, `structured_output_serasa1.json`, `structured_output_serasa2.json` | `structure_ocr.py` on the 8 inputs above (in that order) | **Goldens** of the regression harness (`harness/run.py goldens`). Not in git — never overwrite; regenerate only deliberately (§1). |
| `<output>.json` | `structure_ocr.py -o <output>.json` | Metadata, document type, reconstructed statements, page classification, `sbs_label` per row, `sbs_fields` (§6.1). |
| `<output>_report.json` | `structure_ocr.py` | Quality counters of every step (§6.4). |
| `<output>_report.xlsx` | `export_excel.py` | **Allianz Excel** — metadata / balance / income / cash_flow / sbs_fields sheets. |
| `page_diagnostics.json` | `diagnose_pages.py` | Per-page signals (anchors, density, columns, identity). |
| `harness/snapshots/<tag>/` | `harness/run.py snapshot <tag>` | Per-document scoreboard data compared by `harness/run.py diff`. Confidential, git-ignored. |
| `__pycache__/` | Python | Compiled bytecode cache — safe to delete anytime. |

---

## 4. The Pipeline at a Glance

```
   raw OCR JSON  (+ optional OCR words layer, --words)
        │
        ▼
   structure_ocr.py · process()
        │  Stage 0   metadata — extract, normalize (ISO date, BRL, unit multiplier, period months)
        │  Stage 1   table reconstruction — rows, columns, periods, codes, account_type,
        │            debit / credit suffixes, sign / dash proof (sign_proof.py)
        │            └─ Stage 2 page classification runs inside Stage 1, right after the row
        │               anchors and panels (classify_statements.py + anchors_lib + anchors.yaml)
        │  doc type  doc_type.py — SERASA / ITR / UPPERCASE / DFP_4COL / TWO_COL
        │  Stage 3   SBS labelling (sbs_labeler.py + sbs_label_mapping.json), then one value
        │            per SBS field and period (sbs_fields.py; rows chosen by selection.py)
        ▼
   <output>.json                                    <output>_report.json
   { metadata, document_type, number_format,        counters of every step (§6.4)
     statements (rows carry sbs_label …),
     page_classification, classification_summary,
     sbs_fields }
        │
        ├──▶ export_excel.py  — Stage 4, separate step (openpyxl) ──▶ <output>_report.xlsx
        │       selection.passing_rows → metadata / balance / income / cash_flow / sbs_fields
        └──▶ app.py (Streamlit) — the same selection on screen, the same workbook as download

   (diagnose_pages.py is a separate READ-ONLY tool for the Stage-2 signals)
```

**Stages 2 & 3 are optional and soft-fail:** if `anchors.yaml` / `pyyaml` is missing, Stage 1 still
runs and produces the output *without* the `page_classification` keys (report shows
`classification: {skipped_no_anchors: 1}`). Likewise if `sbs_label_mapping.json` is missing, rows
simply carry no `sbs_label` (report shows `sbs: {skipped_no_mapping: 1}`). **Stage 4 is a separate
command**, not part of `structure_ocr.py`.

**The order inside `structure_ocr.process()`** (details and reasons: `docs/Pipeline_Overview.md` §3):

```
metadata (extract → normalize; optional words metadata; provider tag)
→ Step 1  row anchors → panels → STAGE 2 page classification (columns per panel)
→ Step 1b detach off-line figures → 2 place ONLY_AMOUNT → 2b unnamed rows
→ Step 2c period from the column grid → 2c' header zones → 2d three-column balance (N | N-1 | opening)
→ Step 4  codes → 4b wrapped names (A / B / C / D) → 4d header lines → 4e DRA / DMPL / DVA (ITR)
→ number format, unit from centavos → (optional) words recovery → 4c Serasa zone
→ Step 3  ACCOUNT_TYPE headers → 5 assembly (perspectives, D/C suffixes, US notation, flags)
→ Step 5b sign / dash proof (income statement: dash test only) → document type
→ STAGE 3 PASS 1 per statement: sections → code match → match → twins → empty-label subtotals → tax lines
          PASS 2: scope-gate → CAPEX asset lines → EBIT before the financial result → struct checks
                  → Serasa net income → liability totals → dedup → duplicate-index remap
→ STAGE 3 end: sbs_fields (candidates = exported rows → hierarchy → current-only → held-for-sale
               alternative → identities → review)
```

---

## 5. Run Scenarios

> All commands assume you are in the project directory:
> `cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing`

### 5.1 Full pipeline (Stages 1-3 + `sbs_fields`) — default

```bash
python3.13 structure_ocr.py comprehansive_sample.json -o my_output.json
```

### 5.2 Process a different document

```bash
python3.13 structure_ocr.py path/to/another_document.json -o another_output.json
```

`structure_ocr.py` locates `anchors.yaml` next to itself, so it can be run from any directory and
still find the lexicon. The **output report** is always written next to the output file as
`<output-name>_report.json`.

### 5.3 Debug mode — keep coordinates for visual cross-checking

```bash
python3.13 structure_ocr.py comprehansive_sample.json -o my_output_debug.json --debug
```

Adds `x`, `y`, and `_match` fields to columns/rows so results can be lined up against the original
PDF. Use this when verifying a tricky page; omit for clean production output.

### 5.4 A new PDF end to end: reciTAL → Streamlit

1. Upload the PDF in the reciTAL web UI, agent **Brazil_OCR202606_V4** (doctype 5485) — or its copy
   **Brazil_OCR202606_V4 copy reciTAL** (6051: ONLY_AMOUNT `AMOUNT_N-1` primary, one subgroup per figure,
   more complete; the configuration chosen for production 2026-11-05).
2. Wait for the extraction, find the file id (status `to_verify` = done):
   `python3.13 -E -P harness/recital_fetch.py files 5485 20` (or `files 6051 20`)
3. Download it in the shape the app takes (READ-ONLY GETs):
   `python3.13 -E -P harness/recital_fetch.py export <file_id>` →
   `_inbox/<id>/<id>_raw.json` (main upload) + `_inbox/<id>/<id>_words.json` (optional words layer).
4. `python3.13 -m streamlit run app.py` → upload the raw JSON (and, to try the recovery, the words file)
   → review the tabs (balance / DRE / DFC / **SBS fields**) → download the Excel.
   The app serves on `localhost` only (`.streamlit/config.toml`).

For an independent check of several PDFs, see `AUDIT_BRIEF.md` + `harness/audit_run.py`.

### 5.5 OPTIONAL — recover lost figures from the OCR words layer

```bash
python3.13 structure_ocr.py my_doc.json -o my_doc_output.json --words my_doc_words.json
```

`my_doc_words.json` = the reciTAL words layer (`GET /files/{id}/words/`, a list per page of
`{word, coordinates}`). Recovered cells / lines / labels carry `source` / `label_source: "ocr_words"`
(report block `words`) and are shown italic on light yellow in the Excel. Without `--words` the output is
the words-free one exactly — production may not receive that layer. See `docs/Words_Recovery.md`.

### 5.6 Read-only diagnostics (the structured result is not touched)

```bash
python3.13 diagnose_pages.py                       # uses comprehansive_sample.json
python3.13 diagnose_pages.py path/to/another.json  # any document
```

Prints a per-page table (rows, amounts, columns, vspan, complete families, class) and writes
`page_diagnostics.json`. **Run this from the project directory** — it opens `anchors.yaml` by
relative path.

### 5.7 Stage 1 only (skip classification)

Temporarily make `anchors.yaml` unavailable, or run in an environment without `pyyaml`. The cleanest
way without deleting config (Stage 3 and `sbs_fields` still run; the PRIMARY-only Excel filter then has no
page classes to use):

```bash
python3.13 -c "import json, structure_ocr as s; \
d=json.load(open('comprehansive_sample.json',encoding='utf-8')); \
s._CLASSIFY_AVAILABLE=False; o,r=s.process(d); \
json.dump(o, open('structured_stage1_only.json','w',encoding='utf-8'), ensure_ascii=False, indent=2); \
print('Stage-1 only:', list(o.keys()))"
```

### 5.8 Re-run Stage 3 only (SBS labelling) on an existing output

Stage 3 runs automatically inside `structure_ocr.py`. To (re)apply it to an already-structured file
— e.g. an older output, or after editing `sbs_label_mapping.json` — run the labeller standalone:

```bash
python3.13 sbs_labeler.py my_output.json --dry-run   # report only, no write
python3.13 sbs_labeler.py my_output.json             # overwrite in place (never on a golden)
python3.13 sbs_labeler.py my_output.json -o labelled.json
```

The standalone run also recomputes `sbs_fields` (the values depend on the labels just stamped). It does
not redo Stage 1: changes to the reconstruction need a full `structure_ocr.py` run.

### 5.9 Export to Excel (Stage 4)

```bash
python3.13 export_excel.py my_output.json                 # -> my_output_report.xlsx
python3.13 export_excel.py my_output.json -o my.xlsx      # explicit name
```

The console prints the **document type** and which filters it applied. See §9 for the full
type/filter matrix.

---

## 6. Reading the Outputs

### 6.1 `<output>.json` — top-level shape

```jsonc
{
  "metadata":              { "CLOSING_DATE": {...}, "DURATION": {...},     // each with the OCR value
                             "CURRENCY": {...}, "UNIT": {...} },           // + "normalized"
  "document_type":         "DFP_4COL",          // SERASA / ITR / UPPERCASE / DFP_4COL / TWO_COL
  "number_format":         "BR",                // "US" when the document prints 193,081
  "statements": {
    "BALANCE_ASSETS_LST":      [ <row>, ... ],
    "BALANCE_LIABILITIES_LST": [ ... ],
    "INCOME_STATEMENT_LST":    [ ... ],
    "CASH_FLOW_LST":           [ ... ]
  },
  "page_classification":   { "20": {page_class, confidence, reasons, ...}, ... },
  "classification_summary":{ primary_pages, class_distribution, fuzzy_backend },
  "sbs_fields":            { "perspective": ..., "fields": { <field>: {...} }, "checks": [ ... ] }
}
```

`metadata.ACCOUNT_TYPE` appears only for a known provider tag (`SERASA EXPERIAN`). Each **row** carries
`page_nb`, `label`, `columns[]` (cells with `value`, `period`, `account_type`), and (when Stage 2 ran)
`page_class` + `page_confidence`. When Stage 3 ran and the label matched, the row also carries
`sbs_label` plus `sbs_scope` (`in_scope` / `cross_scope`), `sbs_match_method` and `sbs_match_score`
(see §8). `sbs_fields` holds one value per SBS field and period, how it was obtained and the accounting
checks.

Flags worth knowing when reading a JSON (full list: `docs/Pipeline_Overview.md` §5):

| Where | Key | Meaning |
|---|---|---|
| cell | `value_raw` | the token as the OCR read it, whenever `value` was rewritten (US notation, D/C suffix, sign / dash repair) |
| cell | `sign_corrected` / `dash_corrected` + `proof` | a lost minus / a dash read as «1», repaired because the code tree or a subtotal proves it (a dash also on the income statement) |
| cell | `period_corrected` + `period_src`; period `AMOUNT_N-2` | the column grid / the header zone / the three-column proof changed the provider's period; `AMOUNT_N-2` = an extra older column, never exported |
| cell | `account_type_corrected` + `account_type_src` | the header zone gave another perspective than the pair rule |
| row | `label_parts` + `label_wrapped` | a name printed over several lines, merged (type A / B / C / D) |
| row | `needs_review` | some step was unsure — the reason in words is the «Review» column of the Excel / Streamlit (`selection.review_reason`) |
| row | `other_statement`, `serasa_analysis`, `non_figure_line` | kept in JSON, never labelled, never exported |
| row | `arith_review` (+ `needs_review`) | an ITR code-tree equation of this parent does not add up |
| row | `sbs_blocked_reason` | `out_of_scope`, `includes_equity`, `net_after_dividends`, `after_financial_result` |
| row | `sbs_duplicate_of` | index (full bucket list) of the kept duplicate; dropped on export |

### 6.2 The most common task — filter to PRIMARY statements only

```bash
python3.13 -c "import json; o=json.load(open('my_output.json',encoding='utf-8')); \
print(json.dumps(o['classification_summary']['primary_pages'], ensure_ascii=False))"
```

Filter the **rows** that sit on primary pages (the business view):

```python
import json
out = json.load(open("my_output.json", encoding="utf-8"))
for stmt, rows in out["statements"].items():
    primary = [r for r in rows if r.get("page_class", "").startswith("PRIMARY")]
    print(stmt, len(primary), "primary rows")
```

### 6.3 Reference-sample expected result (sanity baseline)

`comprehansive_sample.json`, current code, production mode (no words layer):

| Metric | Expected value |
|---|---|
| Statement-bearing pages | 69 |
| Columns (figures) | 3,677 — the conservation count: no change to Stage 1 may move it |
| Rows in `statements` | 1,477 — moves when a Stage-1 fix splits or merges lines (the columns must not) |
| PRIMARY pages | 6 — BALANCE [20, 21], INCOME [22, 46, 47], CASH_FLOW [26] |
| NOTE / JUNK | 54 / 9 |
| Excel rows balance / income / cash flow | 65 / 24 / 64 |

### 6.4 `<output>_report.json` — quality counters

Key blocks: `rows`, `columns`, `only_amount`, `account_type`, `code_value`, `classification`, `sbs`,
`words` (optional layer), plus `document_type` / `number_format`. The **conservation invariant** to
check: `columns.total` equals the unique in-scope amount count (3,677 on the sample) — see Stage-1 doc §9.

Counters worth a look: `columns.dc_suffix_signed` (debit / credit suffixes), `columns.sign_corrected` /
`columns.dash_corrected` (step 5b repairs), `rows.arith_review` (ITR equations that do not add up),
`rows.wrapped_label_A|B|C|D` (wrapped names merged, step 4b),
`rows.non_figure_line`, `rows.serasa_analysis`, `rows.xband_mixed`, `only_amount.lst_detached`,
`only_amount.period_corrected`, `only_amount.zone_panels` / `only_amount.zone_period_set` /
`account_type.zone` / `account_type.zone_type_changed` (header zones, step 2c'),
`only_amount.extra_column_resolved` (three-column balance),
`sbs.fields_valued` / `sbs.identity_ok` / `sbs.identity_failed` (`sbs_fields`).

### 6.5 `page_classification` — why a page got its label

Each verdict includes a `reasons` list — read it to understand any classification. Pages flagged
`needs_review: true` need a human glance (e.g. the Individual/Consolidado income split on 46/47).

---

## 7. Editing the Anchor Lexicon

`anchors.yaml` is the **business-owned config** that drives Stage-2 detection. Edit it, then re-run —
no code change needed.

**Common edits:**

- **Add a wording variant** an OCR produced for an existing total line → add to that anchor's
  `aliases`.
- **Allow a new in-phrase word** (synonym/parenthetical) → add to `benign_insertions`.
- **Change what makes a statement "complete"** → edit `required_anchors`.
- **Loosen/tighten typo tolerance** → change `match_threshold` (higher = stricter).

After editing, **verify** with the read-only diagnostic before a full run:

```bash
python3.13 diagnose_pages.py comprehansive_sample.json
```

> Do **not** add narrowing qualifiers (e.g. `circulante`, `segmento`) to `benign_insertions` — that
> would make subtotals match the report total. See Stage-2 doc §5.

---

## 8. SBS Labelling (Stage 3)

Stage 3 stamps each row with its **SBS Field Name** (`sbs_label`, e.g. `cashAndSecuritiesAmount`) by
matching the Portuguese `label` against the alias lexicon in `sbs_label_mapping.json`. It runs
automatically inside `structure_ocr.py`; re-run it standalone with `sbs_labeler.py` (§5.8).

**Row fields added (when a label matches):**

| Field | Meaning |
|---|---|
| `sbs_label` | The resolved SBS Field Name (or `null` if a genuine ambiguity couldn't be resolved). |
| `sbs_scope` | `in_scope` (field belongs to this statement) or `cross_scope` (e.g. a balance caption appearing in cash-flow = a *movement*, not the stock). |
| `sbs_match_method` | `dict` / `unique` / `scope` / `qualifier` / `code` / `positional` / `unresolved_ambiguous` / `empty_label_arith` / `empty_label_unresolved` / `empty_label_intermediate` / `empty_label_grand_total` / `balance_identity` / `derived` / `tax_component` / `tax_component_total` / `investing_asset_line`. |
| `sbs_match_score` | Fuzzy score (0-100) of the winning alias (100 for an exact dict / arithmetic hit). |
| `sbs_candidates`, `sbs_review_reason` | Present only when left unresolved; `needs_review` is also set. |
| `sbs_derived` | `true` on the synthesized `totalLiabilitiesAmount` row (= `financialLiabilitiesShortTerm` + `financialLiabilitiesLongTerm`, the current + non-current totals, equity excluded; no source line). Always shown on export. |

**The matching engine is dict-first** (exact `sbs_normalize` lookup), falling back to `anchor_match`
(the same token-coverage + fuzzy rule Stage 2 uses, opted into `token_fuzzy`); on filings with CVM
codes a code-first stage precedes both. Statement scope is a **FILTER** (the scope-gate): an
out-of-statement match is blocked when the field is captured in-home elsewhere, else kept + flagged
`needs_review`. `sbs_match_source` records which engine matched (`code` | `dict` | `dict_conflict` | `fuzzy`).

> ⚠️ **Liability fields — pinned by the user 2026-11-04:** `shortTermLiabilitiesAmount` /
> `longTermLiabilitiesAmount` = short- / long-term **credits and loans**; `financialLiabilitiesShortTerm` /
> `financialLiabilitiesLongTerm` = the **total** current / non-current liabilities (the balance identity and
> `totalLiabilitiesAmount` use these totals). Do not revert to the xlsx Description.

**Current-vs-non-current "twin" ambiguities** are shared captions split across a current/non-current
pair of SBS fields, declared as data in `sbs_label_mapping.json → ambiguity_groups`. Two exist today:
**debt = credits and loans** (`shortTermLiabilitiesAmount` vs `longTermLiabilitiesAmount`) and **securities** (`cashAndSecuritiesAmount`
current vs `financialAssetsAmount` non-current — e.g. `Títulos e valores mobiliários`, `Aplicações
financeiras`, `Investimentos`). A row matching a twin via a shared caption is resolved by:
(1) explicit qualifier in its label (`circulante`/`longo prazo`), else (2) `CODE_VALUE` prefix
(`x.01.*`=current, `x.02.*`=non-current), else (3) positional section tracking, else (4) left `null` +
`needs_review`. It **never guesses**. Add a new twin pair by editing the mapping, not the code.

**Inspect SBS results quickly:**

```bash
python3.13 sbs_labeler.py my_output.json --dry-run
```

Prints matched/unmatched counts, in/cross-scope split, resolution methods, and per-field totals.

### Regenerating the mapping (`sbs_label_mapping.json`)

The mapping is **generated**, not hand-edited. After changing the aliases in `labels_OLD.yaml` or the
SBS field list in `SBS_fields_mapping_with_INDIAN_labels.xlsx`:

```bash
python3.13 build_sbs_mapping.py        # rewrites sbs_label_mapping.json
```

The script joins the two sources on the xlsx **`Indian Labels`** column (= old primary slug), reports
any field-name mismatch, and prints the alias conflicts + orphaned anchors. After regenerating,
re-run Stage 3 (§5.8) so outputs pick up the new aliases.

> 33/43 SBS fields currently carry aliases; without aliases: `goodwillAmount`, `netGroupProfitAmount`,
> `pensionFundsAndOtherStaffCommitmentsAmount`, `exploitProvisionAmount`, the 5 metadata fields — and
> `totalLiabilitiesAmount` **on purpose** (resolved arithmetically). Add captions in `EXTRA_ALIASES` of
> `build_sbs_mapping.py` (+ `EXTRA_SCOPE` for a source-less field; a current / non-current caption also in
> the twin group's shared list) or in `labels_OLD.yaml`, then rebuild. A field in `STRICT_SCOPE` is never
> kept outside its statements (today `provisions`: the cash-flow «Provisões para riscos…» is a flow). 6
> fields carry a `needs_review` note in the mapping (semantic overrides vs the Indian-label hint).
>
> **User decisions 2026-11-05** (`CLAUDE.md` #32): `provisions` = IAS 37 liability provisions only
> (contingencies / lawsuits, decommissioning, warranties, onerous contracts — not payroll accruals, taxes
> payable, asset allowances, nor a bare «Provisões»), current + non-current summed, CVM 2.01.06 / 2.02.04;
> depreciation without right-of-use / PPA («mais valia») / borrowing-cost amortisation; related-party
> balances are not loans; interest paid = the DRE «Despesas financeiras».

**SBS field values (`sbs_fields`, sheet `sbs_fields`).** One value per field and period, from the rows the
Excel shows: components of another line of the same field are not counted (CVM code descendants, or a block
that sums to the line), income tax is taken from the DRE only, the *_CURRENT fields keep the current rows,
the identities (`assets = current + non-current = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm
+ equity`, `gross = turnover ± COGS`, `post-tax = pre-tax ± tax`) choose between alternatives, and every field
of a failing identity is flagged `needs_review` with its `review_reason`. EBIT is the line BEFORE the
financial result (user rule) — if only a later «resultado operacional» exists, the field is empty + review.
A held-for-sale line printed OUTSIDE both balance subtotals («Ativo mantido para venda» between the current
total and the non-current block) joins `totalCurrentAssetsAmount` — and «Passivos associados a ativos
mantidos para venda» `financialLiabilitiesShortTerm` — only where the identity proves it (method
`held_for_sale+identity`, user decision 2026-11-05).

---

## 9. Excel Export (Stage 4)

`export_excel.py` renders a structured JSON into a polished, **Allianz-styled** workbook with five
sheets: **metadata**, **balance** (assets + liabilities/equity), **income**, **cash_flow**, **sbs_fields**.

```bash
python3.13 export_excel.py my_output.json            # -> my_output_report.xlsx
python3.13 export_excel.py my_output.json -o my.xlsx
```

**Statement-sheet columns:** `Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N`.

### ⚠️ The document TYPE decides what is kept

The type is detected once in Stage 1 (`doc_type.py`, stamped on the output as `document_type`) in this
order — first match wins — and the exporter prints it:

| Type | Detected by | `page_class` filter | Columns kept |
|---|---|---|---|
| **SERASA** | provider tag `SERASA EXPERIAN` | **ignored**; Serasa analysis rows dropped | **all** |
| **ITR** | any row has a `code` (CODE_VALUE) | **ignored**; only the CVM **form pages** (pages with codes) | **Consolidado if presented, else company** |
| **UPPERCASE** | ≥50% of labels are ALL-CAPS | **ignored** | **all** |
| **DFP_4COL** | a column carries `Consolidado`/`Controladora` | `PRIMARY:*` only | **Consolidado if presented, else company** |
| **TWO_COL** | none of the above (`account_type=null`) | `PRIMARY:*` only | **all** |

**Key rule:** "Consolidado only" applies **only where that split exists** (ITR, DFP_4COL), and it is
decided once per document: consolidated figures if the document presents them, else the company's own.
On 2-col / uppercase / Serasa documents there is nothing to filter, so both columns are taken. (Example: `sample_2.json`
has no Consolidado on its primary pages — its headers sit only on a NOTE page over a reconciliation
table — so it is **TWO_COL** and both columns appear. This is correct, not a bug.)

**Small-span override:** the `PRIMARY:*` filter is bypassed for any statement group whose labelled
rows span at most **1–2 consecutive pages**. A short, self-contained statement is emitted in full
regardless of `page_class` — this rescues a genuine primary that Stage-2 mislabelled `NOTE` on wording
alone (e.g. `complicated_empty_labels_sample.json`: balance reads `TOTAL DO PASSIVO`, the anchor set is
incomplete → `NOTE`, and without this rule the whole balance would be missing). Multi-page statements
keep the filter, so segment copies are never pulled in.

**Other export behaviour:**

- Which rows are written is decided by ONE function, `selection.passing_rows` (shared with Streamlit,
  the harness and `sbs_fields`): duplicates / Serasa analysis / header lines / DRA-DMPL-DVA rows out → the
  derived total passes → ITR form pages only → `PRIMARY:*` only (DFP_4COL / TWO_COL, small-span bypass)
  → Consolidado rule → at least one figure in N or N-1.
- Amounts → numbers via `anchors_lib.parse_amount`, plus OCR bracket repair (`[1.234]` / `(2.138]` →
  `(1.234)` / `(2.138)`, i.e. negative); `%` and ratio tokens are skipped (not figures).
- Columns are matched by **`period`** (`AMOUNT_N` / `AMOUNT_N-1`), *not* position — provider dumps
  reverse the order.
- Metadata tokens are cleaned for display (`(Reais`→`Reais`, `C-31/12/2024`→`31/12/2024`); normalized
  values shown with the OCR token alongside (closing date, BRL, unit multiplier, period months).
- `Total…` rows are bolded with a light fill; numbers use `#,##0;(#,##0)`, figures with decimals
  `#,##0.00##` (centavos, EPS).
- Per-share lines (code 3.99, «Lucro por ação», «Básico e diluído», «ON / PN» with decimals) are tagged
  «(per share)» in the Label column.
- The `AMOUNT_N-1` / `AMOUNT_N` headers carry the period dates when the metadata proves them (ITR /
  Serasa balance N-1 = 31/12 of the previous year; annual N-1 = one year earlier; ITR DRE / DFC = the same
  period one year earlier).
- Sheet `sbs_fields`: one value per SBS field and period, how it was obtained, the rows used / not counted,
  the identities per period, the review reason; then the accounting checks (✓ / ✗).
- Statement sheets have a 7th column **«Review»**: a row the pipeline flagged `needs_review` is light red
  and the reason is written there in words (a failing equation, two panels on one line, «two figures for
  one column: the first is shown», «SBS field chosen between … / not resolved», a withdrawn label…); a
  legend note sits under the sheet. Same on the Streamlit screen.

Row counts per sheet are tracked by the regression harness (scoreboard columns `xlB` / `xlI` / `xlC`,
`python3.13 -E -P harness/run.py snapshot <tag>`), not by fixed numbers here: the counts of the local
samples changed with the audit fixes of 2026-11-04 (ITR form pages only, coded lines no longer
de-duplicated). The selftest pins one: the Eztec ITR with both perspectives exports balance 76 /
income 26 / cash flow 36.

---

## 10. Streamlit Front-End (web UI)

`app.py` is the interactive web entry point to the whole pipeline. A reviewer uploads an OCR JSON,
reviews the Balance / Income / Cash Flow statements on screen, and downloads the same Allianz Excel the
CLI produces. Full methodology: [`docs/Frontend_Streamlit.md`](docs/Frontend_Streamlit.md).

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 -m streamlit run app.py                                  # opens http://localhost:8501
python3.13 -m streamlit run app.py --server.headless true --server.port 8501   # explicit port
```

**What it does:**

- **Auto-detects the upload.** Root is a `list` → raw OCR, runs `structure_ocr.process()` (Stages 1-3);
  root is a `dict` with a `statements` key → a structured output, used as-is.
- **Optional words layer.** A second uploader takes the reciTAL words JSON (`<id>_words.json`); figures /
  lines / labels recovered from it are marked like in the workbook (WORDS badge, italic on light yellow).
- **Screen == Excel.** Which rows appear is decided by `export_excel.passing_rows` (=
  `selection.passing_rows`), which figures by `pick_cells` / `consolidado_filter`, and the **Download
  Excel** button calls `export_excel.build_workbook` directly — so the downloaded `.xlsx` is
  content-identical to `python3.13 export_excel.py …`. The screen shows decimals, but not yet the
  «(per share)» marker nor the dated amount headers (workbook only). Rows flagged `needs_review` are
  light red with their reason in the «Review · Revisão» column, as in the workbook (since 2026-11-05).
- **SBS fields tab.** The `sbs_fields` values per period, how each was obtained, and the accounting checks.
- **Per-section expand control.** Each statement section starts **collapsed to SBS-matched rows**
  (`− Matched`); the `+ All` toggle shows every exported row.
- **Metadata ribbon** atop each tab (doc-type badge + dates left, Currency/Unit right), **bilingual
  EN · PT** labels, Allianz theme.

**Notes:**

- Needs `streamlit` (§2). The largest sample (~3.4 MB) is well within the 50 MB upload limit set in
  `.streamlit/config.toml`.
- It is a long-running server — stop it with `Ctrl-C` (or kill the port).
- Presentation only: it never writes back to the structured JSON; all analytical decisions were made
  in Stages 1-3.

---

## 11. Validation Checklist for a New Document

When running on a document for the first time, confirm:

- [ ] **It ran clean** — no traceback; console shows a classification block.
- [ ] **Backend** — note `rapidfuzz` vs `difflib` in the console header.
- [ ] **Conservation** — in the report, `columns.total` matches the document's unique in-scope
      amount count (no silent loss).
- [ ] **Primary pages look sane** — `classification_summary.primary_pages` lists a small set in
      canonical order (Balance → Income → Cash-Flow).
- [ ] **Review the flags** — inspect rows/pages with `needs_review: true` (in the Excel / Streamlit: the
      light red rows, the reason in the «Review» column).
- [ ] **Anchors fired** — if a statement family found 0 primary pages, its wording may differ; add
      `aliases` to `anchors.yaml` (§7) and re-run.
- [ ] **SBS labels populated** — report `sbs.row_matched` is non-zero; spot-check key lines
      (`Caixa`→`cashAndSecuritiesAmount`, `Total do ativo`→`totalAssetsAmount`). Review
      `sbs.row_sbs_needs_review` rows.
- [ ] **Excel type is right** — `export_excel.py` prints the detected type; confirm it matches the
      document (SERASA / ITR / UPPERCASE / DFP_4COL / TWO_COL) and the row counts look sane (§9).
- [ ] **`sbs_fields` checks** — sheet `sbs_fields`: every accounting check ✓; red rows (review) have a
      reason; key fields present (total assets, current / non-current totals, equity, turnover, COGS,
      EBIT, pre-tax, tax, net income, CFO, CFI, CAPEX).
- [ ] **Repairs are plausible** — cells with `sign_corrected` / `dash_corrected` carry a `proof`; rows with
      `arith_review` point at an equation that does not add up (look at the page).
- [ ] **ITR** — the workbook holds only the form pages (no note pages); the `sbs_fields` values do not
      include note figures.

Quick check script:

```bash
python3.13 -c "import json; o=json.load(open('my_output.json',encoding='utf-8')); \
s=o.get('classification_summary',{}); \
cols=sum(len(r['columns']) for rows in o['statements'].values() for r in rows); \
print('backend:', s.get('fuzzy_backend')); \
print('columns:', cols); \
print('primary:', s.get('primary_pages')); \
print('classes:', s.get('class_distribution'))"
```

---

## 12. Troubleshooting

Grouped by where the symptom shows. «Fixed 2026-11» / «2026-11-04» = handled by the current code; if the
symptom comes back, the rule named in the Fix column is the place to look (step numbers:
`docs/Pipeline_Overview.md` §3).

### 12.1 Environment and running

| Symptom | Cause | Fix |
|---|---|---|
| `UnicodeEncodeError` / garbled `?` in console | Windows console is cp1252 | Cosmetic only — the **files** are valid UTF-8. The scripts already force UTF-8 stdout; ignore console rendering. |
| `structured_output.json` (a golden) changed after a run | `structure_ocr.py` without `-o` writes to `structured_output.json` | The goldens are not in git (`*.json` is git-ignored) — there is no copy to restore. Always pass `-o` with a new name (§1). |
| `FileNotFoundError: anchors.yaml` running `diagnose_pages.py` | Run from wrong directory (it uses a relative path) | `cd` into the project directory first. |
| `[fuzzy backend: difflib]` unexpectedly | `rapidfuzz` not importable | Reinstall via proxy (§2); harmless otherwise. |
| `ModuleNotFoundError: openpyxl` running `export_excel.py` / `build_sbs_mapping.py` | Dependency missing | Install `openpyxl` via proxy (§2). |
| `ModuleNotFoundError: streamlit` running `app.py` | Web-UI dep missing | Install `streamlit` via proxy (§2). |
| `streamlit run` "port already in use" | A previous server is still running | Use `--server.port <n>`, or kill the listener on 8501. |
| `pip` fails / "not on PATH" | Bare `pip` not available | Always use `python3.13 -m pip ... --proxy http://127.0.0.1:9000 ...`. |
| Stale results after editing code | `__pycache__` bytecode | Safe to `rm -rf __pycache__` and re-run. |

### 12.2 Pages and rows — what the Excel shows

| Symptom | Cause | Fix |
|---|---|---|
| A row is light red in the Excel / on the screen | The pipeline flagged it `needs_review` | Read its «Review» column (the reason in words) and check the page; not an error by itself. |
| Output has **no** `page_classification` key | Stage 2 was skipped | Check report `classification` block: install `pyyaml`, ensure `anchors.yaml` exists (§2). |
| A real statement classified as `NOTE` | Its total-line wording differs from the lexicon | Add the variant to `aliases` in `anchors.yaml` (§7), re-run diagnostics. |
| A note classified as `PRIMARY` | Anchor set too loose, or it is a segment table with ≤4 columns | Tighten `required_anchors`; check the `reasons` list for why. A later repeat with a different, ODD column count is already demoted (reconciliation note). |
| A side-by-side balance (Ativo \| Passivo) classified `NOTE_segment` («6 columns > 4») | Columns counted for the whole page | Fixed 2026-11-04: Stage 2 counts per panel. |
| A whole statement (income / cash flow) is missing from the Excel of a DFP / 2-col document | Stage 2 found only the balance and the DRE/DFC wording misses the lexicon | Fixed 2026-11: the safety-net also starts from a primary balance; add the new wording to `anchors.yaml` aliases. Verify with `harness/run.py` (metric `MISS`) and `harness/ground_truth.yaml` statement pages. |
| Excel sheet is empty / wrong rows | Wrong document type detected, or over-strict filter | Check the type `export_excel.py` printed against §9. SERASA / ITR / UPPERCASE ignore `page_class` (ITR keeps the form pages only); DFP_4COL / TWO_COL keep `PRIMARY:*` only (small-span override). |
| ITR workbook full of note tables (segments, roll-forwards) / `sbs_fields` mixing note figures | Notes' «Consolidado» headers passed the filter | Fixed 2026-11-04: ITR exports only the CVM form pages (`selection.form_pages`). |
| ITR income / cash-flow sheet shows «Valor Adicionado…», «Distribuição do Valor Adicionado» rows | The DVA (and DMPL / DRA) of the ITR form, codes 7 / 5 / 4 | Fixed 2026-11 (Stage-1 step 4e `other_statement`): never exported, never labelled. |
| A row «9 / 9» (or `%`, `Total`, `-` only) appears as an unnamed line | The provider returned a column header («(9 meses)») as figures | Fixed 2026-11 (Stage-1 step 4d): such rows carry `non_figure_line` and are never exported (report `rows.non_figure_line`). |
| A whole balance missing for an accounting-system dump; values like `1.234,56D` / `… C` | Debit / credit suffix not parsed | Fixed 2026-11-04: Stage 1 signs them by the balance side (`value_raw` keeps the token). |
| EPS shown as «2» / «1» | Thousands format on a per-share value | Fixed 2026-11-04: decimals kept, «(per share)» marker (workbook). |
| Front-end shows different rows than the Excel | Should be impossible — both use the same selection | The screen calls `export_excel.passing_rows` (= `selection.passing_rows`) / `pick_cells` and downloads via `build_workbook`. If they diverge it is a real bug — check the data logic was edited in `selection.py` / `export_excel.py`, not only in `app.py` (§10, `docs/Frontend_Streamlit.md`). |

### 12.3 Figures — Stage 1

| Symptom | Cause | Fix |
|---|---|---|
| `label_null` row has >2 columns in a 2-col doc / assets mixed with liabilities | Side-by-side layout (Ativo left, Passivo right on the same lines) merged by y | Fixed: **both** amount stages are panel-aware — `augment_with_only_amount` keeps only same-panel candidates (report `only_amount.orphan_xband`), then `materialize_orphans` routes orphans by panel before clustering by y. Rows it still cannot separate → `xband_mixed` + `needs_review` (report `only_amount.xband_mixed_rows`). |
| Side-by-side balance loses empty-label subtotals on ONE side (e.g. 3 liability subtotals gone, assets fine) | A liability sum got glued onto a same-`y` assets row as a spurious 2nd column (pre-fix `augment_with_only_amount` bug) | Fixed by the panel gate (Stage-1 doc §7.3a). Verify the recovered `label_null` rows sum to the side's grand total (`complicated_sample_2`: assets 70.076.711,53 + 4.597.550,90; liabilities 22.168.649,61 + 2.739.236,61 + 49.766.376,21 = 74.674.262,43). |
| A figure sits on the neighbour line / a row shows the figures of the line above | Provider bound the name to the wrong line, or the old fixed 0.012 tolerance | Fixed 2026-11 (Stage-1 Steps 1b + page-pitch tolerance). Check `only_amount.lst_detached` / `lst_rebound_*` in the report; harness metric `offln`. |
| A value appears in the wrong year column | Provider period tag wrong in rows with empty cells; single-value ONLY_AMOUNT always tagged `AMOUNT_N` | Fixed 2026-11 (Step 2c, column grid wins): such cells carry `period_corrected` + `period_src`; harness metric `perGr`. |
| A Consolidado cell shows the Controladora figure of the other year (Cibra p7: TOTAL DO PASSIVO E PL 2024 = 2.433.930 = Contr. 2023), or a Conso 2024 figure sits in the 2023 column | An `ONLY_AMOUNT` pair jumped an empty column, or agent 6051 tagged a Conso 2024 single `N-1`. Step 2c and the pair rule then mis-place the figure | Fixed 2026-11-05 (step 2c', header zones): where `CLOSING_DATE` + `ACCOUNT_TYPE` frames make the header unambiguous, the column's zone decides. Look for `period_src` / `account_type_src` on the cell, `_match: "zone"` with `--debug`, report `only_amount.zone_panels`. No zone on the page: see the next row. |
| The header is printed but no zone is built (in the `--debug` output no cell of the page has `_match: "zone"`) | Not unambiguous: a perspective header in the form title (ITR), no type header at all (2-col, Serasa), two tables on the page, a stray column (DEL10), a date frame centred over a wide column (MBP p14) | By design: Step 2c and the pair rule stand there. The reason per page and panel is the `status` that `structure_ocr.build_header_zones` returns (`no_type_header`, `ncols_mismatch`, `type_header_inside_table`, `header_not_over_pair`, `pair_dates`, `n_right_of_n1`, …). The conditions are in Stage-1 doc §6, Step 2c'. |
| A balance N-1 taken from an «01/01/…» opening column | Three-column restated balance | Fixed 2026-11-04 (step 2d): proven by the other balance side; the extra column becomes `AMOUNT_N-2`. |
| A figure shown without its minus / a «1» where the page has a dash | OCR lost the sign / read the dash | Repaired only when proven (step 5b, `sign_corrected` / `dash_corrected`, `value_raw`, `proof`); on the income statement only the dash is repaired (since 2026-11-05), never a sign; else an ITR parent may carry `arith_review`. |
| A figure is visible on the PDF but absent from the output | The extraction lost it (~8% of table figures) | With the words layer: `--words` (§5.5). Without it: not recoverable — check `harness` metric `missW`. |
| Closing date shows only a year, or the comparative year | Provider's `CLOSING_DATE` token | `metadata.CLOSING_DATE.normalized` (from DURATION) is shown with `(OCR: …)`; check `normalized_from`. |

### 12.4 SBS labels and `sbs_fields` — Stage 3

| Symptom | Cause | Fix |
|---|---|---|
| Rows have **no** `sbs_label` | Stage 3 skipped, or label didn't match | Report `sbs` block: `skipped_no_mapping` → ensure `sbs_label_mapping.json` exists / rebuild (§8). If only some rows miss it, the label simply isn't in the lexicon — that's expected ("tag, never delete"). |
| Twin row (loans ST / LT, or securities cash / financial assets) labelled wrong or `null` | No qualifier, `code`, or section marker to resolve from | Expected on NOTE/JUNK pages — the row stays `needs_review` with both candidates (§8). On primary pages it resolves via code or position. |
| Equity subtotal labelled `financialLiabilitiesLongTerm` instead of `shareholdersFundsAmount` | A period-result line (`RESULTADO DO EXERCICIO`/`AJUSTES…`/`DISTRIBUIÇÃO DE LUCROS`) inside the equity block wasn't in `_EQUITY_KEYWORDS` → `all()`-equity content test failed → ordinal fallback | Add the caption to `_EQUITY_KEYWORDS` in `sbs_labeler.py` (consulted in `BALANCE_LIABILITIES_LST` scope only). |
| A block subtotal («Realizável a longo prazo») labelled `totalFixedAssetsAmount` next to the printed non-current total, or the derived total liabilities = current liabilities only | Subtotal arithmetic mixed Controladora/Consolidado cells, or could not tell a block subtotal from a section total | Fixed 2026-11 (Stage 3: one account perspective + subtotal tree): `empty_label_intermediate` / `empty_label_grand_total` methods. |
| Two (or more) rows carry the same SBS field — which value counts? | Components printed on several lines, a total + its parts, a block decomposition | Read sheet `sbs_fields` / key `sbs_fields`: ONE value per field and period, with *how* and the identity that proves it. `needs_review` there = undecidable. |
| A field doubled (parent + child summed, e.g. cash = 1.01.01 + 1.01.01.01) | Same SBS field on a line and on its own component | Fixed 2026-11-04: `sbs_fields` hierarchy (codes / block sums). |
| Income tax with the opposite sign | Value taken from the DFC add-back | Fixed 2026-11-04: tax valued from the DRE only. |
| EBIT empty + REVIEW although a «Resultado operacional» is printed | That line comes after the financial result | By design (user rule 2026-11-04): EBIT = before the financial result. |
| Income tax = the pre-tax figures, `pretaxProfitAmount` empty, the DRE label is a fragment («DA CONTRIBUIÇÃO SOCIAL») | A wrapped ALL-CAPS pre-tax caption («… IMPOSTO DE RENDA E» / «DA CONTRIBUIÇÃO SOCIAL») left unmerged | Fixed 2026-11-05: wrap type C (Stage 1 step 4b). A new variant that still fails: check `label_parts` / `label_wrapped` of the row. |
| `provisions` empty although the balance prints a «Provisões…» line | The caption does not say what it provides for (bare «Provisões», «Outras provisões», «Provisões diversas») or is a payroll / tax accrual | By design (user decision 2026-11-05): only IAS 37 captions. A real IAS 37 caption missing → add it to `provisions` in `EXTRA_ALIASES`, rebuild (§8). The cash-flow «Provisões para riscos…» is never the balance provision (strict scope). |
| `totalCurrentAssetsAmount` ≠ the printed «Total do ativo circulante» (method `held_for_sale+identity`) | An «Ativo mantido para venda» line printed outside both subtotals | By design (user decision 2026-11-05): held-for-sale assets are current; taken only where total assets prove it. |
| An accounting check ✗ in `sbs_fields` | A figure lost by the extraction, a mislabelled line, or a field the lexicon misses | Harness metric `idBad`; with the words layer most losses come back (`--words`). Every field of the failing identity carries `needs_review` + `review_reason`. |

### 12.5 Development and the harness

| Symptom | Cause | Fix |
|---|---|---|
| `harness/run.py goldens` says OUT OF SYNC | A change to Stages 1-3 (or to the mapping) changed the outputs | Expected after a pipeline change: review the diff (rows, cells, labels, `sbs_fields`, Excel), show it to the user, regenerate only after approval (`structure_ocr.py <input> -o <golden>`, then `export_excel.py`; the two Serasa goldens also `-o serasa_report_1.xlsx` / `serasa_report_2.xlsx`). Back up the 8 JSON + their `_report.json` / `_report.xlsx` first (`_temp/golden_backup_<date>/`): they are not in git. Last regenerations: 2026-11-05 after the audit fixes, and 2026-11-05 after the Cibra follow-ups (only `provisions` changed, in 3 JSON; all 8 Excel re-exported with the «Review» column). |
| A regex in a patched `.py` no longer matches | `\b` / `\1` turned into control characters by a shell heredoc | Patch code with raw strings from a file; scan for control characters (`ord(ch) < 32`). |

---

## 13. Quick Command Reference

```bash
# --- setup ---------------------------------------------------------------
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 --version
python3.13 -c "import yaml, rapidfuzz, openpyxl; print(yaml.__version__, rapidfuzz.__version__, openpyxl.__version__)"

# --- full pipeline (Stages 1-3 + sbs_fields); ALWAYS -o with a new name ----
python3.13 structure_ocr.py comprehansive_sample.json -o my_output.json

# --- Excel export (Stage 4) ----------------------------------------------
python3.13 export_excel.py my_output.json

# --- web UI (upload / review / export) -----------------------------------
python3.13 -m streamlit run app.py            # then open http://localhost:8501

# --- another document, end to end ----------------------------------------
python3.13 structure_ocr.py my_doc.json -o my_doc_output.json
python3.13 export_excel.py my_doc_output.json -o my_doc.xlsx

# --- re-run Stage 3 only (after editing the mapping) ---------------------
python3.13 sbs_labeler.py my_output.json --dry-run     # report only
python3.13 sbs_labeler.py my_output.json               # write in place (never on a golden)

# --- regenerate the SBS mapping ------------------------------------------
python3.13 build_sbs_mapping.py

# --- OPTIONAL: + recovery from the OCR words layer ------------------------
python3.13 structure_ocr.py my_doc.json -o my_doc_output.json --words my_doc_words.json

# --- regression harness (22 reciTAL PDFs; both modes) ----------------------
python3.13 -E -P harness/run.py snapshot base_p && python3.13 -E -P harness/run.py snapshot base_w --words
python3.13 -E -P harness/run.py diff base_p base_w -v
python3.13 -E -P harness/run.py goldens
python3.13 -E -P harness/run.py selftest

# --- new PDF from reciTAL (READ-ONLY) / independent audit ------------------
python3.13 -E -P harness/recital_fetch.py files 5485 20
python3.13 -E -P harness/recital_fetch.py export <file_id>          # -> _inbox/<id>/<id>_raw.json (+ _words.json)
python3.13 -E -P harness/audit_run.py <file_id> [...] --all-pages   # -> _audit/<id>/ (see AUDIT_BRIEF.md)

# --- debug (keep coordinates) --------------------------------------------
python3.13 structure_ocr.py comprehansive_sample.json -o my_output_debug.json --debug

# --- read-only diagnostics -----------------------------------------------
python3.13 diagnose_pages.py comprehansive_sample.json

# --- inspect result ------------------------------------------------------
python3.13 -c "import json; o=json.load(open('my_output.json',encoding='utf-8')); \
print(o['document_type'], o['classification_summary']['primary_pages'])"

# --- install deps (corporate proxy) --------------------------------------
python3.13 -m pip install pyyaml rapidfuzz openpyxl streamlit \
  --proxy http://127.0.0.1:9000 \
  --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

---

*Runbook for the `br_ocr_json_processing` project. Step-by-step process: `docs/Pipeline_Overview.md`;
methodology and design rationale: the stage documents under `docs/`.*
