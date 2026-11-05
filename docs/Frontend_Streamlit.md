# Streamlit Front-End

**An interactive web UI to upload, review, and export OCR financial statements**

| | |
|---|---|
| **Component** | `app.py` (uses `streamlit`; reuses `structure_ocr` + `export_excel`) |
| **Config** | `.streamlit/config.toml` (Allianz theme + upload limit) |
| **Input** | A raw OCR provider JSON (root = list) **or** a structured output (root = dict with `statements`) |
| **Output** | On-screen Balance / Income / Cash Flow tables + a download of the Allianz Excel (and structured JSON) |
| **Audience** | Business reviewers (Brazil) & developers |
| **Status** | Validated on all 8 reference samples × both input modes; Excel download verified byte-content-identical to `export_excel.py` (0 cell diffs) |
| **Related** | [`Excel_Export.md`](Excel_Export.md) — Stage 4 (the workbook this downloads) |

> **Where this fits.** Stages 1-3 build the tagged JSON; [Stage 4 / `export_excel.py`](Excel_Export.md)
> renders the Excel. **This front-end is the human entry point to the whole pipeline:** a reviewer
> uploads a file, sees the statements on screen, and downloads the exact same Excel the CLI produces.
> It introduces **no new analytical logic** — every row/amount/filter decision is delegated to the
> existing modules (see [§3 Design Contract](#3-design-contract)).

---

## Table of Contents

1. [Executive Summary](#1-executive-summary) *(business)*
2. [Layout & Features](#2-layout--features)
3. [Design Contract — Screen == Excel](#3-design-contract--screen--excel) *(developers)*
4. [Input Auto-Detection](#4-input-auto-detection) *(developers)*
5. [Expand / Collapse Behaviour](#5-expand--collapse-behaviour)
6. [Metadata Ribbon](#6-metadata-ribbon)
7. [Row Styling & Badges](#7-row-styling--badges)
8. [Styling & Theme](#8-styling--theme)
9. [Performance — Caching](#9-performance--caching) *(developers)*
10. [How to Run](#10-how-to-run) *(developers)*
11. [Verification](#11-verification)
12. [Design Decisions & Open Items](#12-design-decisions--open-items)

---

## 1. Executive Summary

`app.py` is a polished Streamlit web app in Allianz colours. A reviewer **uploads** an OCR JSON in the
sidebar; the app **runs the pipeline if needed**, then shows three tabs — **Balance · Balanço**,
**Income · DRE**, **Cash Flow · Fluxo de Caixa** — each topped with a document-metadata ribbon and an
expand/collapse control. A **Download Excel** button (sidebar + below the tables) returns the same
workbook `export_excel.py` writes.

Labels are **bilingual (EN · PT)** so the Brazilian audience and the international Allianz team read
the same screen. The theme mirrors the Excel brand exactly, so the app and its export feel like one
product.

---

## 2. Layout & Features

| Area | Contents |
|---|---|
| **Sidebar** | Allianz Trade logo · file uploader · detected doc-type + description · KPI metrics (LST pages / rows shown / SBS matched / needs review) · **Download Excel** · Download structured JSON (raw uploads only) · About |
| **Hero banner** | Title, subtitle, source filename — Allianz-blue gradient with a gold bottom rule |
| **Per-tab ribbon** | Doc-type badge + Closing date / Period / Account type (left); Currency / Unit (right) — see [§6](#6-metadata-ribbon) |
| **Statement tables** | One per group, with sticky header, section dividers, zebra rows, SBS dots & badges |
| **Footer download** | A second Download-Excel button below the tables, as requested |

---

## 3. Design Contract — Screen == Excel

The single most important property: **what you see on screen and what you download can never disagree.**
This is guaranteed by delegation — `app.py` imports and reuses the exporter's decision functions rather
than re-implementing them:

| Decision | Function reused (from `export_excel`) |
|---|---|
| Which rows appear | `selection.passing_rows()` (via `export_excel`): duplicates / Serasa analysis / header lines / DRA-DMPL-DVA out, ITR form pages only, page-class with small-span override, document-level Consolidado rule |
| Which amounts appear | `pick_amounts()` (period-mapped, doc-type-aware Consolidado filter; `AMOUNT_N-2` never) |
| Section titles | `SECTION_TITLES` |
| Metadata field labels | `META_PRETTY` |
| Total-row detection | `_looks_like_total()` |
| Metadata cleanup | `clean_meta()` |
| Doc-type resolution | `resolve_doc_type()` |
| **The .xlsx download** | **`build_workbook()`** — the exact function the CLI calls |

`app.passing_rows()` is the one place the screen selects rows, and it delegates to
`export_excel.passing_rows` (2026-11) — the same function the sheets use, which applies the page filter,
the Serasa analysis cut and the document-level "Consolidado, else company" rule. Because the download calls `build_workbook` directly, the file is
**content-identical** to `python3.13 export_excel.py …` (verified — see [§11](#11-verification)).

> **Implication for maintainers.** To change *what data* is shown or exported, edit `selection.py` /
> `export_excel.py` (or the stages upstream). `app.py` only governs *presentation*. This keeps the CLI and
> the UI in lock-step.
>
> **Presentation differences (2026-11-04).** The workbook tags per-share lines «(per share)» and writes the
> period dates into the amount headers (`export_excel.period_labels`); the screen shows decimals (EPS,
> centavos) through `fmt_amount` but neither the marker nor the dates yet. The figures and rows are
> identical. **Since 2026-11-05 the review marking is shared:** a row flagged `needs_review` is light red on
> the screen as in the workbook, with the same reason in words in the «Review · Revisão» column
> (`export_excel.review_reason` = `selection.review_reason`).

---

## 4. Input Auto-Detection

The uploader accepts either end of the pipeline and detects which by the JSON root type
(`load_and_process()`):

| Uploaded JSON | Detected as | Action |
|---|---|---|
| Root is a **`list`** | Raw OCR provider dump | Runs `structure_ocr.process()` → Stages 1-3, then renders |
| Root is a **`dict`** with a `statements` key | A structured output | Used as-is (no re-processing) |
| Anything else | — | Friendly error explaining the two accepted shapes |

**Optional second upload — OCR words layer.** Below the main uploader, *OCR words layer (optional)*
accepts the reciTAL words layer (`GET /files/{id}/words/`). With it, a raw upload runs
`structure_ocr.process(data, words=...)`: figures / lines / labels the extraction lost are recovered
(`docs/Words_Recovery.md`). Without it the app behaves exactly as before — production may not have the
words layer.

This mirrors the input-shape fact from `CLAUDE.md` (the raw OCR root is a **list of 11**, not a dict).
A raw upload also offers a **Download structured JSON** button so the reviewer can keep the Stage-1-3
artefact.

---

## 5. Expand / Collapse Behaviour

Per-section control (a `st.segmented_control`), as chosen for this build:

- **`− Matched · N`** *(default)* — shows only rows that carry an `sbs_label`. This is the curated,
  decision-relevant view.
- **`+ All · N`** — expands to **every row the Excel would contain** for that section (detail lines,
  empty-label subtotals, totals…).

Each statement section (e.g. **Assets** and **Liabilities & Equity** on the balance) has its **own**
toggle, so a reviewer can expand liabilities while keeping assets compact. The counts in the labels
(`Matched · 9`, `All · 15`) are live per section.

If a section has rows but **none** are SBS-matched, the collapsed view shows a hint pointing to the
`+ All` button instead of an empty table.

---

## 6. Metadata Ribbon

Per the request, metadata sits at the **top of every tab**, arranged so it reads as part of the
statement rather than a separate dump:

- **Left:** the **doc-type badge** (blue pill, gold inner ring) + **Closing date**, **Period**,
  **Account type** — the "what statement is this" facts.
- **Right (top-right over the table):** **Currency** and **Unit** — the figures' scale, where a reader
  instinctively looks to interpret the numbers (e.g. *Reais · milhares*).

Values come straight from `metadata{}` via the exporter's `clean_meta()` (so `(Reais` → `Reais`,
`C-31/12/2024` → `31/12/2024`), and the field labels reuse `META_PRETTY` — identical wording to the
Excel metadata sheet.

---

## 7. Row Styling & Badges

Each row is classed for at-a-glance reading:

| Class | Looks like | Meaning |
|---|---|---|
| **detail** | plain, zebra-striped | an ordinary line item |
| **sbs** | gold **●** dot + blue field name, faint gold tint | carries an `sbs_label` |
| **total** | bold, light-blue fill | a `Total…` / `(=)` line (`_looks_like_total`) |
| **derived** | italic, amber fill | a Stage-3 synthesized row (e.g. derived `totalLiabilitiesAmount`) |
| **review** | light red fill (`#FDECEA`), the reason in the «Review · Revisão» column | `needs_review = true` (since 2026-11-05, same as the workbook) |

Inline badges next to the SBS field name surface the flags at a glance:

| Badge | Trigger | Tooltip |
|---|---|---|
| **REVIEW** | `needs_review = true` | the reason in words (`export_excel.review_reason`: failing equation, two panels on one line, two figures for one column, SBS field chosen / not resolved, withdrawn label, `sbs_review_reason`) |
| **CROSS** | `sbs_scope = cross_scope` | match recovered outside its home statement |
| **DERIVED Σ** | `sbs_derived = true` | computed value, no source page |

Items recovered from the OCR words layer are marked like in the workbook: the figure *italic dark gold
on light yellow* (tooltip "recovered from the OCR words layer"; the cell actually written is found with
`export_excel.pick_cells`), a recovered label the same plus a **WORDS** badge.

A fourth tab **SBS fields · Campos SBS** shows the workbook's `sbs_fields` sheet: one value per SBS field
and period, how it was obtained (badge **✓ IDENTITY** when an accounting identity proves it, **REVIEW**
when it could not be decided), the rows used, and the accounting checks.

The metadata ribbon shows the same text as the workbook's metadata sheet (`export_excel.meta_display`:
normalized closing date / currency / unit, OCR token alongside).

The **Code** column appears only when codes exist in that tab (ITR), so 2-col/uppercase views aren't
cluttered with an empty column. Negative amounts render in parentheses and red.

---

## 8. Styling & Theme

The palette mirrors `export_excel.py` so the screen and workbook are one brand:

- Primary blue `#003781`, accent blue `#006192`, light fill `#E8EEF4`, corporate gold `#DAA900`,
  section fill `#C8D5E3`.
- Hero banner: blue gradient with a gold bottom rule. Dark-navy sidebar with white/gold logo.
- Tables: sticky accent-blue header (bilingual sub-labels), hover highlight, zebra body, scrollable
  to ~74 vh so long statements page within the tab.
- `.streamlit/config.toml` sets the Streamlit base theme (`primaryColor = #003781`, light background)
  and `maxUploadSize = 50` MB (the largest sample, `comprehansive_sample.json`, is ~3.4 MB; the limit
  is generous headroom). CSS beyond the base theme is injected once via `inject_css()`.

---

## 9. Performance — per-session memo (no app-wide cache)

Every toggle triggers a Streamlit rerun, so the pipeline result and the workbook are memoized — but
**per browser session**, in `st.session_state` (`_session_memo`), keyed by a SHA-256 of the uploaded
bytes (raw JSON + optional words file) and of the output:

- **`load_and_process(file_bytes, filename, words_bytes)`** — parsing + Stages 1-3, once per session
  and document; toggles are instant.
- **`build_excel_bytes(output)`** — the workbook, built in memory (`BytesIO`, never written to disk).

Why not `@st.cache_data` (used until 2026-11-04): that cache is **app-wide** — on a deployed app it is
shared by every user and kept in the server's memory until the app restarts — and the old workbook key
(file name + JSON length) could, in theory, hand one user the Excel of another user's different file
with the same name and length. The session store holds only the current document and disappears with
the session.

> **Where the processing runs.** Locally (`streamlit run app.py`) everything happens on the laptop. On a
> hosted deployment (e.g. Streamlit Community Cloud) the Python back-end runs on the HOST's servers: the
> uploaded JSON travels there (HTTPS), is processed in memory, and the Excel travels back. Nothing is
> written to disk by the app, but the data does leave the laptop — check the host is approved for this
> data (see §12).

---

## 10. How to Run

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 -m streamlit run app.py
```

Then open **http://localhost:8501**. Upload any sample (raw OCR or `structured_output_*.json`) from the
project directory. To run headless on a specific port:

```bash
python3.13 -m streamlit run app.py --server.headless true --server.port 8501
```

**Dependencies** (installed via the corporate proxy):

```bash
python3.13 -m pip install streamlit --proxy http://127.0.0.1:9000 \
  --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

Installed versions: `streamlit 1.58.0`, `pandas 3.0.3` (pulled in as a dependency). `openpyxl` was
already present for `export_excel.py`.

---

## 11. Verification

Done at build time (2026-06-30):

- **Excel parity** — built the workbook through the app's `build_excel_bytes` path from
  `structured_output_complicated.json` and compared cell-by-cell to the on-disk
  `structured_output_complicated_report.xlsx`: **0 content diffs across 430 cells.**
- **Row-selection parity** — `app.passing_rows()` counts match `build_workbook()`'s per-sheet counts
  (e.g. balance 15+15 = 30, income 24, cash_flow 0 on the complicated sample).
- **Raw vs structured parity** — uploading raw `complicated_empty_labels_sample.json` and the
  pre-built `structured_output_complicated.json` produce the **same** rows/SBS/xlsx size (54 rows, 23
  SBS, 11079 B), confirming the raw→process path reproduces the structured file.
- **All shapes** — rendered + exported with no errors across all reference samples: TWO_COL,
  DFP_4COL, ITR, SERASA (both input modes).
- **Boots** — `streamlit run` serves HTTP 200; all used APIs (`segmented_control`, `tabs`,
  `download_button`, `metric`, …) exist in 1.58.0.

---

## 12. Design Decisions & Open Items

- **Delegate, don't duplicate.** All data decisions reuse `export_excel`; the app is presentation only.
  This is the guardrail that keeps screen and Excel identical as the pipeline evolves.
- **Auto-detect the input end.** One uploader handles both raw OCR and structured JSON — the reviewer
  needn't know which they have.
- **Bilingual EN · PT**, **Allianz Corporate** theme, **per-section** expand control — the choices made
  for this build. Easy to revisit (see below).
- **Open — styling pass expected.** The user has flagged upcoming style/element revisions; this is a
  first cut, not the final visual.
- **Open — possible additions** (discussed, not yet built): row search/filter, a dedicated metadata
  tab, an Allianz logo image in the hero, per-row review-resolution controls.
- **Not wired to Jira/Outlook.** Standalone local tool; no network calls beyond Streamlit itself.
- **Open — hosting of the Brazilian user acceptance (UAT).** The app is deployed on Streamlit Community
  Cloud from the GitHub repo `Npvel/brazilian_ocr_processing`, which is PUBLIC (checked 2026-11-04). There
  the back-end runs on the host's servers, so the uploaded JSON (company financials, Serasa reports) is
  processed outside the laptop — in memory, not stored by the app, but on third-party infrastructure.
  To decide with Allianz InfoSec / Data Protection: (a) keep Community Cloud with a private repo and
  the app shared only with invited viewers; (b) host it inside the Allianz perimeter (internal server /
  Allianz cloud tenant / Streamlit in Snowflake if available); (c) run the app IN THE BROWSER (stlite /
  Pyodide: the Python code, incl. the pipeline, executes on the user's laptop, the host only serves
  static files — the data never leaves the laptop). The repo docs also quote company names and figures
  of the sample documents.

---

*Document for the `br_ocr_json_processing` project — the Streamlit front-end over Stages 1-4.
Reproducible by running `python3.13 -m streamlit run app.py`. See [`Excel_Export.md`](Excel_Export.md)
for the workbook it downloads.*
