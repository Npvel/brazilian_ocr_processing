# Excel Export

**Rendering the structured JSON into a polished, Allianz-styled workbook**

| | |
|---|---|
| **Component** | `export_excel.py` (uses `openpyxl`) |
| **Input** | A structured output (Stage 1+2+3) JSON |
| **Output** | `<output>_report.xlsx` — 4 sheets: metadata / balance / income / cash_flow |
| **Audience** | Python developers & business stakeholders |
| **Status** | Validated on 4 document shapes (annual DFP, ITR, 2-col, uppercase) |
| **Related** | [`SBS_Labelling.md`](SBS_Labelling.md) — Stage 3 (produces `sbs_label`) |

> **Where this fits.** Stages 1-3 produce a rich, tagged JSON. **This is Stage 4:** the presentation
> layer. Unlike the earlier stages it *does* filter — it selects the rows a business reader expects
> (consolidated primary statements) and lays them out as a clean, branded Excel report. It is a
> **separate command**, not part of `structure_ocr.py`.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary) *(business)*
2. [Workbook Layout](#2-workbook-layout)
3. [Document-Type Detection](#3-document-type-detection) *(developers)*
4. [Filtering Rules](#4-filtering-rules) *(business + developers)*
5. [Why Sample 2 Has No "Consolidado"](#5-why-sample-2-has-no-consolidado)
6. [Value & Metadata Handling](#6-value--metadata-handling) *(developers)*
7. [Styling](#7-styling)
8. [Results on the Reference Samples](#8-results-on-the-reference-samples)
9. [How to Run](#9-how-to-run) *(developers)*
10. [Design Decisions & Open Items](#10-design-decisions--open-items)
11. [Glossary](#11-glossary)

---

## 1. Executive Summary

Stage 4 turns the structured JSON into the artefact a human actually reads: a four-sheet Excel
workbook in Allianz colours. **Metadata** on sheet 1; **Balance**, **Income**, **Cash Flow** on
sheets 2-4, each laid out as `Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N`.

The hard part is **knowing what to keep**. Four document shapes behave differently — annual reports
have a consolidated/standalone split, ITR forms carry hierarchical codes, some provider dumps are
all-uppercase, and the simplest are single 2-column tables. The exporter **auto-detects the type** and
applies the right filter, so the operator runs one command regardless of source.

---

## 2. Workbook Layout

| Sheet | Source groups | Columns |
|---|---|---|
| `metadata` | `metadata{}` | Field · Value (closing date, period, currency, unit, account type) |
| `balance` | `BALANCE_ASSETS_LST` + `BALANCE_LIABILITIES_LST` | Code · SBS Label · Label · Page · AMOUNT_N-1 · AMOUNT_N |
| `income` | `INCOME_STATEMENT_LST` | *(same)* |
| `cash_flow` | `CASH_FLOW_LST` | *(same)* |

The balance sheet shows two section dividers (**Assets** / **Liabilities & Equity**); the single-
statement sheets show none.

---

## 3. Document-Type Detection

`detect_doc_type()` inspects the statements and returns one of four types. **Order matters — first
match wins:**

1. **ITR** — *any* row carries a `code` (populated `CODE_VALUE`, i.e. hierarchical row codes).
2. **UPPERCASE** — ≥ 50 % of labels are entirely upper-case (a provider dump).
3. **DFP_4COL** — some column carries a `Consolidado` / `Controladora` `account_type`.
4. **TWO_COL** — none of the above: a single-perspective table (`account_type = null`).

The detected type is printed to the console on every run.

---

## 4. Filtering Rules

| Type | `page_class` filter | Columns kept |
|---|---|---|
| **ITR** | **ignored** (codes mark the real statement, not `page_class`) | **Consolidado only** |
| **UPPERCASE** | **ignored** (take everything) | **all** |
| **DFP_4COL** | `PRIMARY:*` only | **Consolidado only** |
| **TWO_COL** | `PRIMARY:*` only | **all** |

A row is written if it has **at least one kept amount** — a label is *not* required. `label_null`
rows (subtotals whose name the OCR dropped) carry real ONLY_AMOUNT figures and are shown in full; their
SBS field, when Stage 3 could recover it arithmetically, appears in the SBS Label column.

> **Key rule — do not break it.** "Consolidado only" applies **only where that split exists** (ITR,
> DFP_4COL). On TWO_COL and UPPERCASE documents there is nothing to filter, so both columns are taken.
> This is why the rule is *conditional*, not global.

### Small-span override (rescues mislabelled short statements)

The `page_class = PRIMARY:*` filter exists only to separate a primary statement from its **segment
copies / note repeats** — a risk that arises only when a statement is spread across *many* pages. So
the filter is **bypassed per statement group when that group's labelled rows occupy at most a 1- or
2-page consecutive block** (`is_small_span`). A short, self-contained statement is then taken in full
regardless of `page_class`.

This rescues the realistic case where Stage-2 mislabels a genuine primary page as `NOTE` on wording
alone — e.g. `complicated_empty_labels_sample.json`, whose balance says **`TOTAL DO PASSIVO`** instead
of `Total do passivo e patrimônio líquido`, so the BALANCE anchor set was incomplete → `NOTE` → the
whole balance would otherwise vanish from the workbook. With the override the 1-page balance is
emitted in full (asset = liability = 198,132, identity intact).

It is conservative — multi-page statements (e.g. the annual DFP balance spanning pages 20, 49, 51, …)
still keep the `PRIMARY`-only filter, so segment copies are not pulled in. This is a presentation-layer
safety net, not a fix to the classifier (whose anchor lexicon remains imperfect for non-standard
wording — a separate, tracked open item).

---

## 5. Why Sample 2 Has No "Consolidado"

A natural worry: *"Sample 2 must have consolidated data — why does the exporter take both columns?"*

Tracing the source settles it. In `sample_2.json` the words `Consolidado` / `Controladora` appear
**only on page 30** — and page 30 is a **note** carrying an `ONLY_AMOUNT` reconciliation table, with
no named statement lines. The actual primary statements (balance p7-8, income p9, cash-flow p13) have
**no `account_type` at all**: they are single 2-column tables (AMOUNT_N / AMOUNT_N-1).

So sample 2 is correctly classified **TWO_COL**, and both columns are taken. There is no
consolidated/standalone split to filter on, and Stage 1 is right to leave `account_type = null` on
those pages — that must **not** be "fixed".

By contrast, the annual DFP (`structured_output.json`) genuinely has 4 columns per row with clear
`Consolidado` / `Controladora` headers, and the exporter keeps only the Consolidado pair — without
disturbing that well-defined 4-column logic.

---

## 6. Value & Metadata Handling

- **Amounts → numbers** via `anchors_lib.parse_amount` (dot = thousands separator, parentheses =
  negative). The exporter additionally repairs OCR bracket artefacts (`[1.234]` / `(2.138]` →
  parentheses) before parsing, and **skips** `%` and ratio-like tokens (not figures).
- **Columns are matched by `period`, not position.** Provider dumps emit `AMOUNT_N-1` *before*
  `AMOUNT_N`; mapping by the `period` field puts each value in the correct column regardless of order.
- **Metadata is cleaned for display** — truncated OCR tokens are normalised: `(Reais` → `Reais`,
  `Mil)` → `Mil`, `C-31/12/2024` → `31/12/2024`.
- **Numbers use the format** `#,##0;(#,##0)` so thousands group and negatives show in parentheses.

---

## 7. Styling

The workbook follows the Allianz palette: primary blue `#003781`, accent blue `#006192`, light fill
`#E8EEF4`, corporate gold `#DAA900`.

- A two-row branded banner spans each sheet (white bold title on Allianz blue + a subtitle showing the
  detected document type).
- Column headers on accent blue; frozen header row; gridlines hidden; coloured sheet tabs.
- `Total…` rows are bolded with a light fill; body rows use a subtle zebra stripe.
- Right-aligned numeric columns, centered page numbers, the SBS Label column in bold Allianz blue.

---

## 8. Results on the Reference Samples

Rows written per sheet (balance / income / cash_flow):

| File | Type | balance | income | cash_flow |
|---|---|---|---|---|
| `structured_output.json` | DFP_4COL | 64 | 20 | 64 |
| `structured_output_sample2.json` | TWO_COL | 31 | 15 | 29 |
| `structured_output_itr.json` | ITR | 75 | 50 | 41 |
| `structured_output_provider.json` | UPPERCASE | 35 | 18 | 0 |

(The provider sample has no cash-flow rows in its source — hence 0; its sheet shows a friendly "no
rows matched" note.)

---

## 9. How to Run

```bash
python3.13 export_excel.py structured_output.json                 # -> structured_output_report.xlsx
python3.13 export_excel.py structured_output_itr.json -o itr.xlsx # explicit output name
```

The console prints the detected type and the filters applied. See RUNBOOK §9 for operational detail
and troubleshooting.

---

## 10. Design Decisions & Open Items

- **Auto-detect, don't ask.** The operator runs one command for any source; the type rules live in
  one function (`detect_doc_type`) and are printed for transparency.
- **Conditional Consolidado filter.** Applying it only where a split exists is the crux — it keeps the
  4-column annual logic intact while not starving 2-column documents.
- **Map by `period`, never by position** — robust to providers that reverse column order.
- **Presentation only.** Stage 4 reads the tagged JSON and never writes back to it; all analytical
  decisions were already made in Stages 1-3.
- **Open — section dividers** are emitted only on the balance sheet (two sections); income/cash-flow
  are single-section by design.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **DFP** | *Demonstrações Financeiras Padronizadas* — the annual standardised statements (4-column). |
| **ITR** | *Informações Trimestrais* — quarterly form; carries hierarchical `CODE_VALUE`. |
| **Consolidado / Controladora** | Consolidated vs parent-company (standalone) account perspective. |
| **AMOUNT_N / AMOUNT_N-1** | Current-period vs prior-period figure. |
| **doc type** | One of ITR / UPPERCASE / DFP_4COL / TWO_COL — decides the filter. |

---

*Document for the `br_ocr_json_processing` project, Stage 4. Reproducible by running
`python3.13 export_excel.py <structured_output>.json`. See [`SBS_Labelling.md`](SBS_Labelling.md) for
Stage 3 (the `sbs_label` it renders).*
