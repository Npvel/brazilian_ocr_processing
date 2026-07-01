# RUNBOOK — OCR Financial-Statement Processing

**Operational guide: how to run the scripts, read the outputs, and recover from errors.**

| | |
|---|---|
| **Project** | `br_ocr_json_processing` |
| **Purpose** | Turn raw OCR JSON into structured, classified, **SBS-labelled** financial statements, and a polished **Allianz Excel** report |
| **Audience** | Operators & developers running the pipeline |
| **Scope** | *How to run* — for *how it works*, see `docs/` (linked below) |

> This is an **operational** runbook, not an algorithm description. For the methodology see
> [`docs/OCR_Table_Reconstruction.md`](docs/OCR_Table_Reconstruction.md) (Stage 1),
> [`docs/Primary_Statement_Classification.md`](docs/Primary_Statement_Classification.md) (Stage 2),
> [`docs/SBS_Labelling.md`](docs/SBS_Labelling.md) (Stage 3) and
> [`docs/Excel_Export.md`](docs/Excel_Export.md) (Stage 4).

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

From the project directory, process a document end-to-end (Stage 1 reconstruction + Stage 2
classification + Stage 3 SBS labelling):

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 structure_ocr.py comprehansive_sample.json -o structured_output.json
```

This writes **`structured_output.json`** (the result, with `sbs_label` on every matched row) and
**`structured_output_report.json`** (quality report), and prints a summary to the console.

Then render the polished **Allianz Excel** report (Stage 4 — a separate step):

```bash
python3.13 export_excel.py structured_output.json
```

This writes **`structured_output_report.xlsx`** (sheets: metadata / balance / income / cash_flow).

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
| `pyyaml` | 6.0.3 | For Stage 2/3 | Reads `anchors.yaml`. |
| `rapidfuzz` | 3.14.5 | Optional | Better/faster fuzzy matching; falls back to stdlib `difflib` if absent. |
| `openpyxl` | 3.1.5 | For Stage 4 | Writes the Excel workbook. Only `export_excel.py` needs it. |
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
| `provider_sample.json` | Provider dump with **ALL-UPPERCASE labels** and reversed N-1/N columns. Excel type **UPPERCASE**. |
| `complicated_empty_labels_sample.json` | 2-page doc, empty labels, no codes/headers; balance reads `TOTAL DO PASSIVO` (Stage-2 mislabels NOTE). Excel type **TWO_COL**; balance rescued by the small-span override (§9). |
| `anchors.yaml` | **Config** — anchor lexicon for Stage 2/3. Edit to tune detection (see §7). |
| `sbs_label_mapping.json` | **Config** — SBS Field → Portuguese aliases for Stage 3. Generated by `build_sbs_mapping.py` (see §8). |
| `labels_OLD.yaml`, `SBS_fields_mapping_with_INDIAN_labels.xlsx` | Source files for the mapping build (see §8). |

> **Keep outputs separate.** Each test document writes to its own output file (below) — do **not**
> overwrite another document's result.

### Code

| File | Role | Run directly? |
|---|---|---|
| `structure_ocr.py` | **Main entry point.** Stage 1 + (optional) Stage 2 + (optional) Stage 3. | **Yes** — has a CLI. |
| `export_excel.py` | **Stage 4** — render structured JSON to Allianz Excel. | **Yes** — has a CLI. |
| `sbs_labeler.py` | Stage-3 SBS labeller. Also a standalone post-processor CLI. | **Yes** — CLI or library (imported by `structure_ocr.py`). |
| `build_sbs_mapping.py` | Regenerates `sbs_label_mapping.json` from YAML + xlsx. | **Yes** — run after editing the sources (§8). |
| `diagnose_pages.py` | Read-only Stage-2 diagnostics → table + `page_diagnostics.json`. | **Yes** — but run *from the project dir* (uses relative paths). |
| `classify_statements.py` | Stage-2 classifier logic. | **No** — library; used by `structure_ocr.py` or imported. |
| `anchors_lib.py` | Shared primitives (matching, parsing, geometry). | **No** — library. |

### Outputs (generated)

| File | Produced by | Contents |
|---|---|---|
| `structured_output.json` | `structure_ocr.py` on `comprehansive_sample.json` | Metadata + reconstructed statements + page classification + `sbs_label`. |
| `structured_output_sample2.json` | `structure_ocr.py` on `sample_2.json` | Same shape, second document. |
| `structured_output_itr.json` | `structure_ocr.py` on `itr_samples.json` | Same shape; rows carry `code` from `CODE_VALUE`. |
| `structured_output_provider.json` | `structure_ocr.py` on `provider_sample.json` | Same shape; uppercase labels. |
| `<output>_report.json` | `structure_ocr.py` | Quality counters (rows, columns, classification, **sbs**, code_value, etc.). |
| `<output>_report.xlsx` | `export_excel.py` | **Allianz Excel** — metadata / balance / income / cash_flow sheets. |
| `page_diagnostics.json` | `diagnose_pages.py` | Per-page signals (anchors, density, columns, identity). |
| `__pycache__/` | Python | Compiled bytecode cache — safe to delete anytime. |

---

## 4. The Pipeline at a Glance

```
   raw OCR JSON
        │
        ▼
   structure_ocr.py ─────────────────────────────────────────┐
        │                                                     │
        │  Stage 1: reconstruct tables                        │
        │    rows + columns + account_type                    │
        │                                                     │
        │  Stage 2: classify pages   (needs anchors.yaml)     │
        │    run_classification() → classify_statements.py    │
        │      uses anchors_lib + anchors.yaml                │
        │                                                     │
        │  Stage 3: SBS labelling (needs sbs_label_mapping)   │
        │    run_sbs_labeling() → sbs_labeler.py              │
        │      uses anchors_lib + sbs_label_mapping.json      │
        │                                                     │
        ▼                                                     ▼
   structured_output.json                      structured_output_report.json
   { metadata, statements (rows now                { ... , sbs: {...} }
     carry sbs_label/sbs_scope),
     page_classification,
     classification_summary }
        │
        ▼
   export_excel.py  ── Stage 4 (separate step; needs openpyxl) ──▶  *_report.xlsx
        detects doc type → filters → 4 styled sheets               (metadata/balance/
                                                                     income/cash_flow)

   (diagnose_pages.py is a separate READ-ONLY tool for the same Stage-2 signals)
```

**Stages 2 & 3 are optional and soft-fail:** if `anchors.yaml` / `pyyaml` is missing, Stage 1 still
runs and produces `structured_output.json` *without* the `page_classification` keys (report shows
`classification: {skipped_no_anchors: 1}`). Likewise if `sbs_label_mapping.json` is missing, rows
simply carry no `sbs_label` (report shows `sbs: {skipped_no_mapping: 1}`). **Stage 4 is a separate
command**, not part of `structure_ocr.py`.

---

## 5. Run Scenarios

> All commands assume you are in the project directory:
> `cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing`

### 5.1 Full pipeline (Stage 1 + Stage 2) — default

```bash
python3.13 structure_ocr.py comprehansive_sample.json -o structured_output.json
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
python3.13 structure_ocr.py comprehansive_sample.json --debug
```

Adds `x`, `y`, and `_match` fields to columns/rows so results can be lined up against the original
PDF. Use this when verifying a tricky page; omit for clean production output.

### 5.4 Read-only diagnostics (no output file mutation of the structured result)

```bash
python3.13 diagnose_pages.py                       # uses comprehansive_sample.json
python3.13 diagnose_pages.py path/to/another.json  # any document
```

Prints a per-page table (rows, amounts, columns, vspan, complete families, class) and writes
`page_diagnostics.json`. **Run this from the project directory** — it opens `anchors.yaml` by
relative path.

### 5.5 Stage 1 only (skip classification)

Temporarily make `anchors.yaml` unavailable, or run in an environment without `pyyaml`. The cleanest
way without deleting config:

```bash
python3.13 -c "import json, structure_ocr as s; \
d=json.load(open('comprehansive_sample.json',encoding='utf-8')); \
s._CLASSIFY_AVAILABLE=False; o,r=s.process(d); \
json.dump(o, open('structured_stage1_only.json','w',encoding='utf-8'), ensure_ascii=False, indent=2); \
print('Stage-1 only:', list(o.keys()))"
```

### 5.6 Re-run Stage 3 only (SBS labelling) on an existing output

Stage 3 runs automatically inside `structure_ocr.py`. To (re)apply it to an already-structured file
— e.g. an older output, or after editing `sbs_label_mapping.json` — run the labeller standalone:

```bash
python3.13 sbs_labeler.py structured_output.json --dry-run   # report only, no write
python3.13 sbs_labeler.py structured_output.json             # overwrite in place
python3.13 sbs_labeler.py structured_output.json -o labelled.json
```

### 5.7 Export to Excel (Stage 4)

```bash
python3.13 export_excel.py structured_output.json                 # -> structured_output_report.xlsx
python3.13 export_excel.py structured_output_itr.json -o itr.xlsx # explicit name
```

The console prints the **detected document type** and which filters it applied. See §9 for the full
type/filter matrix.

---

## 6. Reading the Outputs

### 6.1 `structured_output.json` — top-level shape

```jsonc
{
  "metadata":              { "CLOSING_DATE": {...}, "DURATION": {...},
                             "CURRENCY": {...}, "UNIT": {...} },
  "statements": {
    "BALANCE_ASSETS_LST":      [ <row>, ... ],
    "BALANCE_LIABILITIES_LST": [ ... ],
    "INCOME_STATEMENT_LST":    [ ... ],
    "CASH_FLOW_LST":           [ ... ]
  },
  "page_classification":   { "20": {page_class, confidence, reasons, ...}, ... },
  "classification_summary":{ primary_pages, class_distribution, fuzzy_backend }
}
```

Each **row** carries `page_nb`, `label`, `columns[]`, and (when Stage 2 ran) `page_class` +
`page_confidence`. When Stage 3 ran and the label matched, the row also carries `sbs_label` plus
`sbs_scope` (`in_scope` / `cross_scope`), `sbs_match_method` and `sbs_match_score` (see §8).

### 6.2 The most common task — filter to PRIMARY statements only

```bash
python3.13 -c "import json; o=json.load(open('structured_output.json',encoding='utf-8')); \
print(json.dumps(o['classification_summary']['primary_pages'], ensure_ascii=False))"
```

Filter the **rows** that sit on primary pages (the business view):

```python
import json
out = json.load(open("structured_output.json", encoding="utf-8"))
for stmt, rows in out["statements"].items():
    primary = [r for r in rows if r.get("page_class", "").startswith("PRIMARY")]
    print(stmt, len(primary), "primary rows")
```

### 6.3 Reference-sample expected result (sanity baseline)

| Metric | Expected value |
|---|---|
| Statement-bearing pages | 69 |
| Rows / Columns | 1,453 / 3,677 |
| PRIMARY pages | 6 — BALANCE [20, 21], INCOME [22, 46, 47], CASH_FLOW [26] |
| NOTE / JUNK | 54 / 9 |

### 6.4 `structured_output_report.json` — quality counters

Key blocks: `rows`, `columns`, `only_amount`, `account_type`, `classification`. The
**conservation invariant** to check: `columns.total` equals the unique in-scope amount count
(3,677 on the sample) — see Stage-1 doc §9.

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
automatically inside `structure_ocr.py`; re-run it standalone with `sbs_labeler.py` (§5.6).

**Row fields added (when a label matches):**

| Field | Meaning |
|---|---|
| `sbs_label` | The resolved SBS Field Name (or `null` if a genuine ambiguity couldn't be resolved). |
| `sbs_scope` | `in_scope` (field belongs to this statement) or `cross_scope` (e.g. a balance caption appearing in cash-flow = a *movement*, not the stock). |
| `sbs_match_method` | `dict` / `unique` / `scope` / `qualifier` / `code` / `positional` / `unresolved_ambiguous` / `empty_label_arith` / `empty_label_unresolved` / `balance_identity` / `derived`. |
| `sbs_match_score` | Fuzzy score (0-100) of the winning alias (100 for an exact dict / arithmetic hit). |
| `sbs_candidates`, `sbs_review_reason` | Present only when left unresolved; `needs_review` is also set. |
| `sbs_derived` | `true` on the synthesized `totalLiabilitiesAmount` row (= short-term + long-term, equity excluded; no source line). Always shown on export. |

**The matching engine is dict-first** (exact `sbs_normalize` lookup), falling back to `anchor_match`
(the same token-coverage + fuzzy rule Stage 2 uses, opted into `token_fuzzy`); on filings with CVM
codes a code-first stage precedes both. Statement scope is a **FILTER** (the scope-gate): an
out-of-statement match is blocked when the field is captured in-home elsewhere, else kept + flagged
`needs_review`. `sbs_match_source` records which engine matched (`code` | `dict` | `dict_conflict` | `fuzzy`).

**Current-vs-non-current "twin" ambiguities** are shared captions split across a current/non-current
pair of SBS fields, declared as data in `sbs_label_mapping.json → ambiguity_groups`. Two exist today:
**debt** (`financialLiabilitiesShortTerm` vs `...LongTerm`) and **securities** (`cashAndSecuritiesAmount`
current vs `financialAssetsAmount` non-current — e.g. `Títulos e valores mobiliários`, `Aplicações
financeiras`, `Investimentos`). A row matching a twin via a shared caption is resolved by:
(1) explicit qualifier in its label (`circulante`/`longo prazo`), else (2) `CODE_VALUE` prefix
(`x.01.*`=current, `x.02.*`=non-current), else (3) positional section tracking, else (4) left `null` +
`needs_review`. It **never guesses**. Add a new twin pair by editing the mapping, not the code.

**Inspect SBS results quickly:**

```bash
python3.13 sbs_labeler.py structured_output.json --dry-run
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
re-run Stage 3 (§5.6) so outputs pick up the new aliases.

> 28/43 SBS fields currently carry aliases; the 15 brand-new fields (e.g. `goodwillAmount`,
> `ebitdaAmount`, `totalLiabilitiesAmount`) have none yet — extend `labels_OLD.yaml` and rebuild.
> 6 fields carry a `needs_review` note in the mapping (semantic overrides vs the Indian-label hint).

---

## 9. Excel Export (Stage 4)

`export_excel.py` renders a structured JSON into a polished, **Allianz-styled** workbook with four
sheets: **metadata**, **balance** (assets + liabilities/equity), **income**, **cash_flow**.

```bash
python3.13 export_excel.py structured_output.json            # -> structured_output_report.xlsx
python3.13 export_excel.py structured_output.json -o my.xlsx
```

**Statement-sheet columns:** `Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N`.

### ⚠️ The document TYPE decides what is kept

Detection runs in this order (first match wins) and is printed to the console:

| Type | Detected by | `page_class` filter | Columns kept |
|---|---|---|---|
| **ITR** | any row has a `code` (CODE_VALUE) | **ignored** | **Consolidado only** |
| **UPPERCASE** | ≥50% of labels are ALL-CAPS | **ignored** | **all** |
| **DFP_4COL** | a column carries `Consolidado`/`Controladora` | `PRIMARY:*` only | **Consolidado only** |
| **TWO_COL** | none of the above (`account_type=null`) | `PRIMARY:*` only | **all** |

**Key rule:** "Consolidado only" applies **only where that split exists** (ITR, DFP_4COL). On 2-col /
uppercase documents there is nothing to filter, so both columns are taken. (Example: `sample_2.json`
has no Consolidado on its primary pages — its headers sit only on a NOTE page over a reconciliation
table — so it is **TWO_COL** and both columns appear. This is correct, not a bug.)

**Small-span override:** the `PRIMARY:*` filter is bypassed for any statement group whose labelled
rows span at most **1–2 consecutive pages**. A short, self-contained statement is emitted in full
regardless of `page_class` — this rescues a genuine primary that Stage-2 mislabelled `NOTE` on wording
alone (e.g. `complicated_empty_labels_sample.json`: balance reads `TOTAL DO PASSIVO`, the anchor set is
incomplete → `NOTE`, and without this rule the whole balance would be missing). Multi-page statements
keep the filter, so segment copies are never pulled in.

**Other export behaviour:**

- Amounts → numbers via `anchors_lib.parse_amount`, plus OCR bracket repair (`[1.234]`→`1234`); `%`
  and ratio tokens are skipped (not figures).
- Columns are matched by **`period`** (`AMOUNT_N` / `AMOUNT_N-1`), *not* position — provider dumps
  reverse the order.
- Metadata tokens are cleaned for display (`(Reais`→`Reais`, `C-31/12/2024`→`31/12/2024`).
- `Total…` rows are bolded with a light fill; numbers use `#,##0;(#,##0)`.

Expected row counts on the reference samples (balance / income / cash_flow):

| File | Type | balance | income | cash_flow |
|---|---|---|---|---|
| `structured_output.json` | DFP_4COL | 64 | 20 | 64 |
| `structured_output_sample2.json` | TWO_COL | 31 | 15 | 29 |
| `structured_output_itr.json` | ITR | 75 | 50 | 41 |
| `structured_output_provider.json` | UPPERCASE | 35 | 18 | 0 |

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
- **Screen == Excel.** Which rows and amounts appear is decided by reusing `export_excel.row_passes` /
  `is_small_span` / `pick_amounts`, and the **Download Excel** button calls `export_excel.build_workbook`
  directly — so the downloaded `.xlsx` is content-identical to `python3.13 export_excel.py …`.
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
- [ ] **Review the flags** — inspect rows/pages with `needs_review: true`.
- [ ] **Anchors fired** — if a statement family found 0 primary pages, its wording may differ; add
      `aliases` to `anchors.yaml` (§7) and re-run.
- [ ] **SBS labels populated** — report `sbs.row_matched` is non-zero; spot-check key lines
      (`Caixa`→`cashAndSecuritiesAmount`, `Total do ativo`→`totalAssetsAmount`). Review
      `sbs.row_sbs_needs_review` rows.
- [ ] **Excel type is right** — `export_excel.py` prints the detected type; confirm it matches the
      document (ITR / UPPERCASE / DFP_4COL / TWO_COL) and the row counts look sane (§9).

Quick check script:

```bash
python3.13 -c "import json; o=json.load(open('structured_output.json',encoding='utf-8')); \
s=o.get('classification_summary',{}); \
cols=sum(len(r['columns']) for rows in o['statements'].values() for r in rows); \
print('backend:', s.get('fuzzy_backend')); \
print('columns:', cols); \
print('primary:', s.get('primary_pages')); \
print('classes:', s.get('class_distribution'))"
```

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `UnicodeEncodeError` / garbled `?` in console | Windows console is cp1252 | Cosmetic only — the **files** are valid UTF-8. The scripts already force UTF-8 stdout; ignore console rendering. |
| `FileNotFoundError: anchors.yaml` running `diagnose_pages.py` | Run from wrong directory (it uses a relative path) | `cd` into the project directory first. |
| Output has **no** `page_classification` key | Stage 2 was skipped | Check report `classification` block: install `pyyaml`, ensure `anchors.yaml` exists (§2). |
| Rows have **no** `sbs_label` | Stage 3 skipped, or label didn't match | Report `sbs` block: `skipped_no_mapping` → ensure `sbs_label_mapping.json` exists / rebuild (§8). If only some rows miss it, the label simply isn't in the lexicon — that's expected ("tag, never delete"). |
| `[fuzzy backend: difflib]` unexpectedly | `rapidfuzz` not importable | Reinstall via proxy (§2); harmless otherwise. |
| A real statement classified as `NOTE` | Its total-line wording differs from the lexicon | Add the variant to `aliases` in `anchors.yaml` (§7), re-run diagnostics. |
| A note classified as `PRIMARY` | Anchor set too loose, or it is a segment table with ≤4 columns | Tighten `required_anchors`; check the `reasons` list for why. |
| `ModuleNotFoundError: openpyxl` running `export_excel.py` | Stage-4 dep missing | Install `openpyxl` via proxy (§2). |
| `ModuleNotFoundError: streamlit` running `app.py` | Web-UI dep missing | Install `streamlit` via proxy (§2). |
| Front-end shows different rows than the Excel | Should be impossible — both use the same filter | The screen reuses `export_excel.row_passes`/`pick_amounts` and downloads via `build_workbook`. If they diverge it's a real bug — check you edited the data logic in `export_excel.py`, not just `app.py` (§10, `docs/Frontend_Streamlit.md`). |
| `streamlit run` "port already in use" | A previous server is still running | Use `--server.port <n>`, or kill the listener on 8501. |
| Excel sheet is empty / wrong rows | Wrong document type detected, or over-strict filter | Check the type `export_excel.py` printed against §9. ITR/UPPERCASE ignore `page_class`; DFP/TWO_COL keep `PRIMARY:*` only. |
| Twin row (debt ST/LT, or securities cash/financial-assets) labelled wrong or `null` | No qualifier, `code`, or section marker to resolve from | Expected on NOTE/JUNK pages — the row stays `needs_review` with both candidates (§8). On primary pages it resolves via code or position. |
| `label_null` row has >2 columns in a 2-col doc / assets mixed with liabilities | Side-by-side layout (Ativo left, Passivo right on same rows) merged by y | Fixed: **both** amount stages are x-band-aware — `augment_with_only_amount` keeps only same-statement candidates (report `only_amount.orphan_xband`), then `materialize_orphans` routes orphans by x-band before clustering by y. Rows it still can't separate → `xband_mixed` + `needs_review` (report `only_amount.xband_mixed_rows`). |
| Side-by-side balance loses empty-label subtotals on ONE side (e.g. 3 liability subtotals gone, assets fine) | A liability sum got glued onto a same-`y` assets row as a spurious 2nd column (pre-fix `augment_with_only_amount` bug) | Fixed by the §7.3a x-band gate. Verify the recovered `label_null` rows sum to the side's grand total (`complicated_sample_2`: assets 70.076.711,53 + 4.597.550,90; liabilities 22.168.649,61 + 2.739.236,61 + 49.766.376,21 = 74.674.262,43). |
| Equity subtotal labelled `longTermLiabilitiesAmount` instead of `shareholdersFundsAmount` | A period-result line (`RESULTADO DO EXERCICIO`/`AJUSTES…`/`DISTRIBUIÇÃO DE LUCROS`) inside the equity block wasn't in `_EQUITY_KEYWORDS` → `all()`-equity content test failed → ordinal fallback | Add the caption to `_EQUITY_KEYWORDS` in `sbs_labeler.py` (consulted in `BALANCE_LIABILITIES_LST` scope only). |
| `pip` fails / "not on PATH" | Bare `pip` not available | Always use `python3.13 -m pip ... --proxy http://127.0.0.1:9000 ...`. |
| Stale results after editing code | `__pycache__` bytecode | Safe to `rm -rf __pycache__` and re-run. |

---

## 13. Quick Command Reference

```bash
# --- setup ---------------------------------------------------------------
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 --version
python3.13 -c "import yaml, rapidfuzz, openpyxl; print(yaml.__version__, rapidfuzz.__version__, openpyxl.__version__)"

# --- full pipeline (Stage 1+2+3) -----------------------------------------
python3.13 structure_ocr.py comprehansive_sample.json -o structured_output.json

# --- Excel export (Stage 4) ----------------------------------------------
python3.13 export_excel.py structured_output.json

# --- web UI (upload / review / export) -----------------------------------
python3.13 -m streamlit run app.py            # then open http://localhost:8501

# --- another document, end to end ----------------------------------------
python3.13 structure_ocr.py my_doc.json -o my_doc_output.json
python3.13 export_excel.py my_doc_output.json -o my_doc.xlsx

# --- re-run Stage 3 only (after editing the mapping) ---------------------
python3.13 sbs_labeler.py structured_output.json --dry-run     # report only
python3.13 sbs_labeler.py structured_output.json               # write in place

# --- regenerate the SBS mapping ------------------------------------------
python3.13 build_sbs_mapping.py

# --- debug (keep coordinates) --------------------------------------------
python3.13 structure_ocr.py comprehansive_sample.json --debug

# --- read-only diagnostics -----------------------------------------------
python3.13 diagnose_pages.py comprehansive_sample.json

# --- inspect result ------------------------------------------------------
python3.13 -c "import json; o=json.load(open('structured_output.json',encoding='utf-8')); \
print(o['classification_summary'])"

# --- install deps (corporate proxy) --------------------------------------
python3.13 -m pip install pyyaml rapidfuzz openpyxl streamlit \
  --proxy http://127.0.0.1:9000 \
  --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

---

*Runbook for the `br_ocr_json_processing` project. For methodology and design rationale, see the
two documents under `docs/`.*
