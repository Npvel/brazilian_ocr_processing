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

Stage 4 turns the structured JSON into the artefact a human actually reads: an Excel workbook in
Allianz colours. **Metadata** on sheet 1; **Balance**, **Income**, **Cash Flow** on sheets 2-4, each laid
out as `Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N | Review` (the two amount headers carry
the period dates when the metadata proves them; a row the pipeline flagged is light red with its reason
in «Review»); **SBS fields** on sheet 5 — one value per SBS field and period. The full step order of the
pipeline is in [`Pipeline_Overview.md`](Pipeline_Overview.md).

The hard part is **knowing what to keep**. Four document shapes behave differently — annual reports
have a consolidated/standalone split, ITR forms carry hierarchical codes, some provider dumps are
all-uppercase, and the simplest are single 2-column tables. The exporter **auto-detects the type** and
applies the right filter, so the operator runs one command regardless of source.

---

## 2. Workbook Layout

| Sheet | Source groups | Columns |
|---|---|---|
| `metadata` | `metadata{}` | Field · Value (closing date, period, currency, unit, account type) |
| `balance` | `BALANCE_ASSETS_LST` + `BALANCE_LIABILITIES_LST` | Code · SBS Label · Label · Page · AMOUNT_N-1 · AMOUNT_N · Review |
| `income` | `INCOME_STATEMENT_LST` | *(same)* |
| `cash_flow` | `CASH_FLOW_LST` | *(same)* |
| `sbs_fields` | `sbs_fields{}` (Stage 3 end) | SBS Field · Statement · AMOUNT_N-1 · AMOUNT_N · How · Rows · Verified by — then the accounting checks |

The balance sheet shows two section dividers (**Assets** / **Liabilities & Equity**); the single-
statement sheets show none.

---

## 3. Document-Type Detection

`doc_type.detect_doc_type()` is computed **once in Stage 1** and stamped on the output as
`document_type`; the exporter reads it (recomputing only for a legacy JSON without the field). It returns
one of five types. **Order matters — first match wins:**

1. **SERASA** — the provider tag `SERASA EXPERIAN` in `metadata.ACCOUNT_TYPE`.
2. **ITR** — *any* row carries a `code` (populated `CODE_VALUE`, i.e. hierarchical row codes).
3. **UPPERCASE** — ≥ 50 % of labels are entirely upper-case (a provider / accounting-system dump).
4. **DFP_4COL** — some column carries a `Consolidado` / `Controladora` `account_type`.
5. **TWO_COL** — none of the above: a single-perspective table (`account_type = null`).

The detected type is printed to the console on every run.

---

## 4. Filtering Rules

| Type | `page_class` filter | Columns kept |
|---|---|---|
| **SERASA** | **ignored**; rows outside the statement zone (`serasa_analysis`) **dropped** | **all** |
| **ITR** | **ignored**; only the **CVM form pages** (pages carrying account codes) — note / commentary pages never | **Consolidado if the document presents it, else the company's** |
| **UPPERCASE** | **ignored** (take everything) | **all** |
| **DFP_4COL** | `PRIMARY:*` only | **Consolidado if the document presents it, else the company's** |
| **TWO_COL** | `PRIMARY:*` only | **all** |

A row is written if it has **at least one kept amount** — a label is *not* required. `label_null`
rows (subtotals whose name the OCR dropped) carry real ONLY_AMOUNT figures and are shown in full; their
SBS field, when Stage 3 could recover it arithmetically, appears in the SBS Label column.

> **Key rule — do not break it.** "Consolidado only" applies **only where that split exists** (ITR,
> DFP_4COL). On TWO_COL and UPPERCASE documents there is nothing to filter, so both columns are taken.
> This is why the rule is *conditional*, not global.

> **Business rule (user, 2026-11-04): consolidated if presented, else the company's own figures.**
> `consolidado_filter` decides it **once per document** (`document_has_consolidado`: any Consolidado
> figure on the rows the export considers). A company-only DFP / ITR (Controladora / Individuais
> columns only) used to give an **empty workbook**; it now exports the company figures. The decision is
> deliberately **not per row**: a row whose consolidated cell the OCR lost must not fall back to the
> company figure (a different number — Raízen closing cash 6.886.721 Controladora vs 21.721.393
> Consolidado); it stays out until the consolidated value is recovered.

> **SERASA = only the balance and the income statement** (user rule 2026-11-04). Stage 1 tags every row
> outside the statement zone (`serasa_analysis`): the zone runs from the first balance row to the
> income statement's closing line `GIR DA ATIVIDADE (EBITDA)` (+ rows printed right after it). The Serasa
> analysis sections (indices, Evolução PL / Imobilizado, the Serasa "FLUXO DE CAIXA", Indicadores) are
> kept in the JSON but never exported (serasa2: 49 such rows used to reach the balance / income sheets).

> **Not statement lines.** Besides `serasa_analysis`, rows Stage 1 tagged `non_figure_line` (a column
> header returned as figures: Moreno's «(9 meses)» came out as a «9 / 9» row) and Stage-3 duplicates
> (`sbs_duplicate_of`) are never written.

> **ITR: only balance, DRE and DFC.** Rows Stage 1 tagged `other_statement` (CVM codes 4 / 5 / 7 =
> DRA / DMPL / DVA) are never written — the ITR income / cash-flow sheets used to carry the DVA.

> **ITR: only the CVM form pages** (`selection.form_pages`, audit 2026-11-04, P1). Every line of a CVM form
> carries its account code, note pages carry none (WEG: 160 / 160 primary rows coded, 0 / 252 note rows).
> Before, the notes' own «Consolidado» headers passed the Consolidado filter and 61 % (WEG) / 43 % (Unipar)
> of the workbook were note tables — segments, roll-forwards, Controladora figures under a Consolidado
> header — which also broke `sbs_fields`. The rule is by page (a code-less line of a form page stays) and
> the Consolidado decision uses the same pages.

> **One selection function.** `selection.passing_rows(statements, doc_type, group_key)` (re-exported as
> `export_excel.passing_rows`) is the only place that decides which rows and amounts are written; the
> sheets, the Streamlit screen, the harness and `sbs_fields` all call it, so they cannot disagree. Its
> checks, in order: duplicates / `serasa_analysis` / `non_figure_line` / `other_statement` out → the derived
> total-liabilities row passes → ITR form pages only → `PRIMARY:*` only for DFP_4COL / TWO_COL (small-span
> bypass) → Consolidado-only (document-level) → at least one figure in N or N-1.

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
- **US-style documents** (`193,081`: comma = thousands, e.g. Documento1 / Solae, an IFF company) are
  detected once in Stage 1 (`number_format: "US"` on the output) and their figures rewritten in the
  Brazilian notation (`193.081`; the original kept as `value_raw`). Before, the whole workbook of such a
  document came out **1000× too small** and Stage-3 arithmetic failed (no derived total liabilities).
- **Debit / credit suffixes** («1.120.836.736,37D», «102.178.388,07 C» — accounting-system dumps) are
  signed in Stage 1 by the balance side (assets D = +, C = −; liabilities / equity / income C = +, D = −);
  `value_raw` keeps the token. The whole Uniar balance used to be missing from the workbook.
- **Repaired figures** (Stage 1 step 5b: a lost minus or a dash read as «1», proven by the CVM code tree
  or a printed subtotal) are written as repaired; `value_raw` / `proof` in the JSON say what the OCR read.
- **Columns are matched by `period`, not position.** Provider dumps emit `AMOUNT_N-1` *before*
  `AMOUNT_N`; mapping by the `period` field puts each value in the correct column regardless of order. A
  cell of an extra older column (`AMOUNT_N-2`, three-column balance) is never written.
- **Metadata is shown normalized** (`meta_display`, Stage-1 `normalize_metadata`): closing date as
  dd/mm/yyyy (a bare OCR year completed from the period text: Raízen `2025` → `31/03/2025`; Romagnole's
  comparative-year `2024` → `31/12/2025   (OCR: 2024)`), currency `BRL   (Reais)`, unit
  `milhares   (× 1.000)`, duration with its months when known (also «12.2024» → 31/12/2024, «… 2024 e
  2023» → 12 months). The OCR token is kept alongside whenever it differs. A unit inferred from figures
  with centavos (nothing printed) reads «(× 1) — not printed; inferred from the figures with centavos».
- **Dated column headers** (`period_labels`, audit X1): `AMOUNT_N` shows the closing date (for the income /
  cash-flow sheets the period: «12m to 31/12/2024», an ITR's «01/01/2025–30/06/2025»); `AMOUNT_N-1` shows a
  date only when it follows from the metadata — ITR and Serasa balance: 31/12 of the previous year; annual
  (12-month) documents: one year earlier; ITR income / cash flow: the same period one year earlier; Serasa
  income: «12m to 31/12 of the previous year». Otherwise the header stays «AMOUNT_N-1». Since 2026-11-05
  the period text «31 DE DEZEMBRO E 2024 E 2023» (a typo of the document, Cibra) also reads as 12 months, so
  its AMOUNT_N-1 header shows 31/12/2023 / «12m to 31/12/2023».
- **Rows flagged for review** (since 2026-11-05, audit Cibra m4): a row carrying `needs_review` is filled
  light red (`REVIEW_FILL` `#FDECEA`) on the balance / income / cash_flow sheets and its reason is written
  in words in the 7th column «Review» (`selection.review_reason`, shared with Streamlit): a failing
  equation (`arith_review`), «figures of two side-by-side panels merged on one line» (`xband_mixed`),
  «two figures for one column (…): the first is shown», «SBS field chosen between … / not resolved: …»
  (twin captions), «SBS label withdrawn (…)», the Stage-3 `sbs_review_reason`. A legend note under the
  sheet explains the colour. Before, a flagged line looked like any other one (Cibra on agent 6051 before
  the header zones: two Consolidado N-1 figures in one cash-flow line, the first shown silently).
- **Sheet `sbs_fields`** — the SBS contract: ONE value per SBS field and period (`output["sbs_fields"]`,
  computed at the end of Stage 3 by `sbs_fields.py` from the very rows the statement sheets show).
  Columns: field · statement · N-1 · N · *how* (one line / sum of the lines / printed total of its
  components / proven by the accounting identity / derived / 0 by identity / REVIEW) · the rows used
  (and those not counted, e.g. a non-current receivable or a component of another line) · the identities
  that verify it, **per period** when they differ. Below: the accounting checks per period (✓ / ✗). Rows
  needing review are shaded red; the reason (e.g. «AMOUNT_N-1: … does not hold» — any field of a failing
  identity is flagged) is appended to the rows text. An EBIT printed only after the financial result
  appears as an empty, red row (user rule 2026-11-04).
- **Figures recovered from the OCR words layer** (optional step, [`Words_Recovery.md`](Words_Recovery.md);
  `source` / `label_source: "ocr_words"`) are written like any figure but styled *italic dark gold on
  light yellow*, with a legend line under the sheet. The cell actually written is known from
  `pick_cells` (the cell behind `pick_amounts`), so a recovered Controladora figure never marks a
  Consolidado one.
- **Numbers use the format** `#,##0;(#,##0)` so thousands group and negatives show in parentheses; a
  figure with decimals uses `#,##0.00##;(#,##0.00##)` (centavos, EPS).
- **Per-share lines** (`selection.is_per_share`: CVM code 3.99.*, «Lucro por ação», «Básico e diluído», a
  share class «ON / PN / PNB» carrying decimals) keep their decimals and their Label gets «(per share)» —
  they are R$ per share among amounts in R$ thousand (São Salvador's 2,138 was shown as «2»). The JSON is
  unchanged; Streamlit shows the decimals but not the marker yet.

---

## 7. Styling

The workbook follows the Allianz palette: primary blue `#003781`, accent blue `#006192`, light fill
`#E8EEF4`, corporate gold `#DAA900`.

- A two-row branded banner spans each sheet (white bold title on Allianz blue + a subtitle showing the
  detected document type).
- Column headers on accent blue; frozen header row; gridlines hidden; coloured sheet tabs.
- `Total…` rows are bolded with a light fill; body rows use a subtle zebra stripe; a row flagged for review
  is light red (its reason in the «Review» column, dark red text).
- Right-aligned numeric columns, centered page numbers, the SBS Label column in bold Allianz blue.

---

## 8. Results on the Reference Samples

Rows written per sheet (balance / income / cash_flow):

| File | Type | balance | income | cash_flow |
|---|---|---|---|---|
| `structured_output.json` | DFP_4COL | 64 | 20 | 64 |
| `structured_output_sample2.json` | TWO_COL | 31 | 15 | 29 |
| `structured_output_itr.json` | ITR | 75 | 50 | 41 |
| `structured_output_provider.json` | SERASA | 35 | 18 | 0 |

(The provider sample has no cash-flow rows in its source — hence 0; its sheet shows a friendly "no
rows matched" note.) These counts predate the audit fixes of 2026-11-04: since then the ITR keeps only the
CVM form pages and no longer de-duplicates lines with different codes (the selftest's Eztec ITR, both
perspectives: balance 76 / income 26 / cash flow 36). The current counts are those of
`harness/run.py snapshot` (scoreboard columns `xlB` / `xlI` / `xlC`) and of the regenerated goldens.

---

## 9. How to Run

```bash
python3.13 export_excel.py my_output.json                 # -> my_output_report.xlsx
python3.13 export_excel.py my_output.json -o my.xlsx      # explicit output name
```

The input is a structured output of `structure_ocr.py` (run with `-o <new name>`; the
`structured_output*.json` files are harness goldens and may predate the current code). The console
prints the document type and the filters applied. See RUNBOOK §9 for operational detail
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
- **Open — Streamlit parity of the presentation details** («(per share)» marker, dated headers): the row
  and figure selection is shared, and so is the «Review» column (since 2026-11-05); the other formatting is
  not yet.

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
