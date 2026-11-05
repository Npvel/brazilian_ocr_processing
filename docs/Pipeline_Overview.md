# Postprocessing Pipeline — Overview (start here)

**What happens to a reciTAL OCR extraction of a Brazilian financial report, step by step, in the
order the code runs it — and why each step exists.**

| | |
|---|---|
| **Project** | `br_ocr_json_processing` |
| **Audience** | New analysts (what the output means, which rules apply) and developers (where each rule lives) |
| **Status** | Reflects the code after the independent audit fixes of 2026-11-04 (12 PDFs, findings P1–P15), the header zones of 2026-11-05 (Stage-1 step 2c', audit of Cibra 12.2024 K1 / K2 / K4) and the Cibra follow-ups of 2026-11-05 (wrap type C, «DEZEMBRO E» period, income-statement dash test, `provisions`, held-for-sale assets, the «Review» column; findings K5 / K6 / M4 / m1–m4, user decisions O1–O7) |
| **Detail docs** | Stage 1 [`OCR_Table_Reconstruction.md`](OCR_Table_Reconstruction.md) · Stage 2 [`Primary_Statement_Classification.md`](Primary_Statement_Classification.md) · Stage 3 [`SBS_Labelling.md`](SBS_Labelling.md) · Stage 4 [`Excel_Export.md`](Excel_Export.md) · optional [`Words_Recovery.md`](Words_Recovery.md) · UI [`Frontend_Streamlit.md`](Frontend_Streamlit.md) · operations [`../RUNBOOK.md`](../RUNBOOK.md) |

---

## Table of Contents

1. [The pipeline in one picture](#1-the-pipeline-in-one-picture)
2. [Inputs and outputs](#2-inputs-and-outputs)
3. [The run order — every step in sequence](#3-the-run-order--every-step-in-sequence)
4. [Business rules and decisions](#4-business-rules-and-decisions)
5. [Data contract — what the output carries](#5-data-contract--what-the-output-carries)
6. [Tag, never delete — where each kind of row ends up](#6-tag-never-delete--where-each-kind-of-row-ends-up)
7. [Quality control](#7-quality-control)
8. [Known limitations and open items](#8-known-limitations-and-open-items)
9. [Glossary](#9-glossary)

---

## 1. The pipeline in one picture

```
 reciTAL extraction (raw JSON)          OPTIONAL: reciTAL OCR words layer
        │                                         │
        ▼                                         │ (production may not have it)
 ┌───────────────────────────── structure_ocr.process() ─────────────────────────────┐
 │ Stage 0  metadata: date / period / currency / unit, provider tag                    │
 │ Stage 1  table reconstruction: rows, columns, periods, perspectives, codes, names   │
 │          (Stage 2 runs inside it, right after the row anchors are built)            │
 │ Stage 2  page classification: PRIMARY:{BALANCE|INCOME|CASH_FLOW} / NOTE / JUNK      │
 │ Stage 1  … assembly, sign / dash proof, document type                               │
 │ Stage 3  SBS labelling: sbs_label on every row (PASS 1 per statement, PASS 2 global)│
 │ Stage 3e SBS field values: ONE value per SBS field and period (sbs_fields)          │
 └──────────────────────────────────────────────┬─────────────────────────────────────┘
                                                ▼
                               structured JSON  (+ <name>_report.json)
                                                │
                      ┌─────────────────────────┴──────────────────────────┐
                      ▼                                                    ▼
      Stage 4  export_excel.py  (Allianz workbook)        app.py  (Streamlit: same row selection)
```

Three principles hold everywhere:

* **Tag, never delete.** Every figure the extraction delivered stays in the JSON. Steps add flags
  (`needs_review`, `other_statement`, `sign_corrected`, …); only the presentation layer (Excel /
  Streamlit) filters.
* **Never guess.** A value is corrected only when the statement's own arithmetic (CVM code tree, a
  printed subtotal, an accounting identity) proves it; otherwise the row is flagged for review.
* **One source of truth per decision.** Document type → `doc_type.py`; row selection → `selection.py`;
  matching / parsing → `anchors_lib.py`; SBS aliases → `sbs_label_mapping.json` (generated).

---

## 2. Inputs and outputs

| What | Where it comes from | Notes |
|---|---|---|
| Raw extraction `<id>_raw.json` | reciTAL agent `Brazil_OCR202606_V4` (doctype 5485), `harness/recital_fetch.py export <id>` (read-only GET) | A JSON **list** of 11 elements: 4 metadata scalars/groups, 4 statement groups `*_LST`, `ONLY_AMOUNT`, `ACCOUNT_TYPE`, `CODE_VALUE` |
| Words layer `<id>_words.json` | same export | **Optional.** Never required by any stage (see [`Words_Recovery.md`](Words_Recovery.md)) |
| `anchors.yaml` | business-editable | Stage-2 anchor lexicon; also the stop-words / benign words / threshold of the name matcher |
| `sbs_label_mapping.json` | **generated** by `build_sbs_mapping.py` from `labels_OLD.yaml` + `SBS_fields_mapping_with_INDIAN_labels.xlsx` | Never edit by hand; edit `EXTRA_ALIASES` / tables in `build_sbs_mapping.py`, then rebuild |
| Structured output `<name>.json` | `structure_ocr.py <raw> -o <name>.json [--words <words>]` | Metadata, document type, statements (rows), page classification, `sbs_fields` |
| Quality report `<name>_report.json` | same run | Counters of every step (see §5.6) |
| Workbook `<name>.xlsx` | `export_excel.py <name>.json -o <name>.xlsx` | Sheets `metadata`, `balance`, `income`, `cash_flow`, `sbs_fields` |

> ⚠️ Always give `-o` a **new** name. Without it `structure_ocr.py` writes `structured_output.json`, and the
> `structured_output*.json` files are the goldens of the regression harness (`harness/run.py goldens`).
> They are not in git: an overwritten golden cannot be restored.

---

## 3. The run order — every step in sequence

The table below is the **actual execution order** of `structure_ocr.process()` and of the modules it
calls. Step numbers are the historical names used in the code comments and in the stage docs.

### 3.1 Stage 0 — metadata (`structure_ocr.py`)

| # | Function | What it does | Writes |
|---|---|---|---|
| 0.1 | `extract_metadata` | Reads `CLOSING_DATE`, `DURATION`, `CURRENCY`, `UNIT` in either form (scalar or group); keeps the highest-confidence value | `metadata.<KEY>.{value, raw, page_nb, confidence}` |
| 0.2 | `normalize_metadata` | Adds machine values **next to** the provider token (never overwrites): ISO closing date (a bare year or a «12.2024» month-year is completed; a year older than the period text is corrected from `DURATION`), `BRL`, unit multiplier, period length in months (two numeric dates; an explicit «09 meses» / «nove meses» / trimestre / semestre; or two dates with the same day-month one year apart = annual comparison → 12 — also the typo «31 DE DEZEMBRO **E** 2024 E 2023», «e» for «de» before the year, Cibra) | `normalized`, `normalized_from`, `normalized_months` |
| 0.3 | `words_recovery.recover_metadata` | *Optional* — a period stated only in the words layer (Serasa «COMPREENDE 09 MES(ES)») | `DURATION` with `source: ocr_words` |
| 0.4 | `extract_account_type` | Surfaces ONLY a provider tag (`SERASA EXPERIAN`) from the `ACCOUNT_TYPE` group — the per-column Controladora/Consolidado headers are not document metadata | `metadata.ACCOUNT_TYPE` |

### 3.2 Stage 1 — table reconstruction, first part (`structure_ocr.py`)

| # | Function | What it does | Why / evidence |
|---|---|---|---|
| 1 | `build_row_anchors` | One provider `subgroup` of a `*_LST` group = one visual row: name + its `AMOUNT_N` / `AMOUNT_N-1` figures, with geometry (name box, name left edge) | The provider already binds names to figures — mostly |
| — | `build_panels` | Finds side-by-side **panels** (Ativo left \| Passivo right on the same lines) from the columns of names | Used by Stage 2 and by every later geometric step |
| **S2** | `run_classification` → `classify_statements.classify` | **Stage 2 runs here** (see §3.3): every page gets `PRIMARY:*` / `NOTE*` / `JUNK*` | Needs the raw data + the panels only |
| 1b | `detach_offline_amounts` | A figure that is not on its name's visual line (tolerance = 0.45 × the page's line pitch) or sits in another panel is detached and re-placed | Provider binds line *k*'s name to line *k±1*'s figures on dense pages |
| 2 | `augment_with_only_amount` | Adds the `ONLY_AMOUNT` figures (deduplicated by value + position) to the row on their visual line **in the same panel**; otherwise they become orphans | `ONLY_AMOUNT` carries the Consolidado columns; ~6 % of LST figures are missing from it |
| 2b | `materialize_orphans` | Orphans → unnamed rows (`label: null`), routed by panel + the named row above, clustered per line; a wide same-period repeat is flagged `xband_mixed` | Unnamed subtotals are SBS targets — never dropped |
| 2c | `correct_periods_by_column_grid` | The period of a figure follows its right-aligned **column** (purity ≥ 0.9 of the reliable tags of the full rows), not the provider tag | Provider tag wrong in rows with empty cells; single-value `ONLY_AMOUNT` always says `AMOUNT_N` |
| **2c'** | `apply_header_zones` | **Header zones.** Where the table header is unambiguous — perspective headers (`ACCOUNT_TYPE`) on one line over consecutive column pairs, and in each pair exactly one column under a frame of the closing date (`CLOSING_DATE`), N left of N-1 — every figure takes the **period and perspective of its column's zone**, whatever the provider tag. Anywhere else Step 2c and the pair rule stand | Cibra 12.2024 (audit 2026-11-05, K1 / K2 / K4): a pair that jumped an empty column was the only «full row» → Step 2c repainted a correct column, and the pair rule put Contr. 2023 into Conso 2024. Zones only on DFP pages with Controladora / Consolidado over the columns |
| 2d | `resolve_extra_period_column` | A balance side printed in **three** columns (N \| N-1 restated \| opening 01/01/N-1): the adjacent balance side printed in two columns proves which column is N (same grand total) and which is N-1 (same N-1 grand total); the remaining column becomes `AMOUNT_N-2` (kept in JSON, never exported) | Dom Atacarejo p6 (audit P3): N-1 was taken from the 01/01/2023 column |
| 4 | `attach_codes` | Binds `CODE_VALUE` (CVM codes `1`, `1.01`, `1.01.01` …) to the row on the same line | ITR / DFP forms |
| 4b | `merge_wrapped_labels` | Joins a name printed over 2+ lines into one label (`label_parts` keeps the pieces, `label_wrapped` = A / B / C / D): A — lowercase or ALL-CAPS «E …» continuation; B — connector + unnamed figures line; **C — an ALL-CAPS name-only line ending with a connector + the ALL-CAPS figures line opening with DA / DO / DE / DOS / DAS / E or indented**; D — ITR, figures on the first line | A fragment matched the wrong SBS field (Cibra p8: «DA CONTRIBUIÇÃO SOCIAL» took income tax with the pre-tax figures — type C) |
| 4d | `tag_non_figure_lines` | An unnamed row with no well-formed figure (column header «(9 meses)», dashes, «100%») → `non_figure_line`. A figure with a debit/credit suffix («1.234,56D») counts as well formed | Header lines returned as figures |
| 4e | `tag_other_statements` | ITR rows / pages of DRA (code 4), DMPL (5), DVA (7) → `other_statement` | Never labelled, never exported |
| — | `detect_number_format`, `infer_unit_from_cents` | US notation detected once per document (rewritten at step 5); no `UNIT` printed + ≥ 80 % of figures with centavos → unit = 1 (`normalized_from: "figures with centavos"`) | Accounting-system dumps (Uniar, Benoit) |
| opt | `words_recovery.recover` | *Optional* — recovers cells / lines / labels the extraction lost, from the words layer; its column grid includes the header-zone columns (2c'), so a column the extraction left almost empty is recovered too | Flags `source` / `label_source: ocr_words` |
| 4c | `tag_serasa_analysis` | Serasa only: rows outside «first balance line … GIR DA ATIVIDADE (EBITDA)» → `serasa_analysis` | Serasa = balance + DRE only |
| 3 | `build_account_headers` | Collects the `ACCOUNT_TYPE` column headers per page | |

### 3.3 Stage 2 — page classification (`classify_statements.py`, runs at step S2)

| Layer | What it decides |
|---|---|
| 1 Negative filter | No figures / too sparse → `JUNK_*` |
| 2 Detector | The required anchor set of a family is complete (`anchors.yaml`, token-coverage rule — never plain fuzzy) |
| 3 Disambiguator | Column model: 2 or 4 columns = primary-like, more = segment note. **Counted per panel** when a side-by-side page exceeds 4 columns in total (Delfia p5). First occurrence wins; a repeat with a *different* column count is kept only when that count is **even** (whole N/N-1 pairs = an Individual/Consolidado copy); an odd count (3 = «original \| ajustes \| reapresentado») is a reconciliation note (Dom p14) |
| 3b Positional safety-net | A solid block (≥ 2 families or a primary BALANCE) + a nearby page with partial anchors of the missing family → flagged primary. The window starts after the block's last page **in canonical order** (Balance → Income → Cash flow); a balance-like note printed after the DRE does not move it (Delfia p11) |
| 3c Multi-page continuation | The 2nd page of a DRE / DFC whose closing anchor is not on the first page |
| 4 Validator | Balance identity raises / flags confidence |

`page_class` is **authoritative only for DFP_4COL and TWO_COL** documents; ITR, UPPERCASE and SERASA
ignore it (see §3.7).

### 3.4 Stage 1 — second part: assembly and proofs

| # | Function | What it does | Why / evidence |
|---|---|---|---|
| 5 | `build_output` → `assign_account_types` | Sorts rows (page, y). A figure of a header zone (2c'; a words-recovered figure is matched to a zone column by its right edge) takes the zone's perspective, traced `account_type_corrected` / `account_type_src` when the pair rule would say otherwise. Every other figure: pairs N / N-1 columns and gives each pair the header above it (Controladora / Consolidado / Individual). A "pair" whose span covers the headers of **two different perspectives** set apart (not one «Controladora e Consolidado» phrase) is split, each cell taking its own header | São Salvador p25 (SSA6): a Controladora dash paired with a Consolidado (20) dropped the line |
| 5 | `build_output` → `dc_signed_value` | Debit/credit suffix: «D»/«C» stripped and signed by the side's normal balance — assets D = +, C = −; liabilities / equity / income C = +, D = − (cash flow untouched) | Uniar: the whole balance carried suffixes and was missing from the Excel (audit P2) |
| 5 | `build_output` (US) | US-notation documents rewritten in Brazilian notation | `value_raw` keeps the original token |
| 5 | `build_output` (flags) | Stamps `page_class`, `code`, `label_missing`, `needs_review` (`xband_mixed`, column collision), the Stage-1 tags | |
| **5b** | `sign_proof.repair` | **Lost minus / dash read as «1»**, repaired only when exactly one explanation exists: ITR — the CVM code tree (parent = Σ children, plus the CVM chains 3.03 … 3.11, 6.05, 6.05.02); a unique single (or, in the code tree, a unique pair) of positive figures whose sign flip makes the failing equation hold without breaking a holding one. Documents without codes — a printed subtotal of the balance / cash flow (sign flips never on the DRE: some templates print costs without a minus), only on the rows the export shows, only under a «Total… / Caixa líquido…» line, never on a «(-)» line or on a total. A «1» whose removal makes an equation exact (off by exactly 1 with it) is a dash — **on the DRE too since 2026-11-05 (dash test only, `DASH_ONLY_BUCKETS`)**. An ITR equation that still fails flags its parent row `needs_review` (`arith_review`) | 13 lost minus signs + 2 dashes in the audit (P7/P8); DRE dash: Cibra p8 (K6), corpus Romagnole p8 / MBP p11 (Controladora cells) |
| 5c | `doc_type.detect_doc_type` | Computed **once**: `SERASA` (provider tag) → `ITR` (any code) → `UPPERCASE` (≥ 50 % all-caps labels) → `DFP_4COL` (Consolidado/Controladora columns) → `TWO_COL` | Every later stage reads `output.document_type` |

### 3.5 Stage 3 — SBS labelling (`sbs_labeler.label_statements`)

Runs on a **view** of the statements without `serasa_analysis`, `non_figure_line` and
`other_statement` rows. Every row gets `sbs_label` (or none) plus provenance.

**PASS 1 — per statement bucket, in this order**

| # | Stage | What it does |
|---|---|---|
| 1.0 | `_compute_sections` | Balance section of every row (current / non-current / equity): **CVM code first** (x.01 / x.02 / 2.03), then header markers (forward), subtotal markers (backward), arithmetic gap-fill (a running-sum subtotal closes a block; a section total printed after a block subtotal closes block + lines; a dash line contributes 0). SERASA/UPPERCASE dumps without codes and without «Total do …» run one bucket-wide arithmetic pass — **except** an accounting-system «balancete» (the side's grand total printed first: totals above their lines), which uses the header markers |
| 1.1 | `_stage_code_match` | CVM levels 1–3 → field straight from `code_map` (locked); since 2026-11-05 also `2.01.06` / `2.02.04` «Provisões» → `provisions` |
| 1.2 | `_stage_match` | Dictionary lookup (`sbs_normalize`), else the token-coverage fuzzy matcher; before-tax guard |
| 1.3 | `_stage_twin_section` | Shared current/non-current captions (debt = **credits and loans**, securities) resolved by qualifier → code → section → else `needs_review` |
| 1.4 | `_stage_empty_label` | Unnamed balance subtotals: grand total, intermediate block (not under «Total do passivo»), or a section total by arithmetic + content |
| 1.5 | `_stage_tax_components` | Between the pre-tax and the post-tax line: every line with figures carrying a tax word (imposto, IR, CS, IRPJ, CSLL, contribuição, corrente(s), diferido(s) — not «antes») is an income-tax line; an unnamed line = their sum is the printed tax total. Needs the pre-tax line to be recognized — a wrapped pre-tax caption must be merged first (step 4b, Cibra «Corrente» / «Diferido») |
| 1.6 | `sbs_section` | The section of every labelled balance row is kept for `sbs_fields` |

**PASS 2 — document level, in this order**

| # | Stage | What it does |
|---|---|---|
| 2.1 | `_stage_scope_gate` | A field outside its statement(s) is blocked if the field exists in-home, else kept + `cross_scope` + review. A **strict-scope** field (`provisions`, since 2026-11-05) is always blocked outside its statement — the cash-flow «Provisões para riscos…» is a flow, never the balance provision |
| 2.2 | `_stage_capex_asset_lines` | A bare «Imobilizado» / «Intangível» **outflow** in the investing section (code 6.02.*, or between the CFO and the CFI totals) = CAPEX |
| 2.3 | `_stage_ebit_before_financial` | An operating-profit label on a line printed **after** the financial result (same page) is withdrawn (`after_financial_result`) — user rule 2026-11-04 |
| 2.4 | `_stage_struct_validate` | Flag-only: income tier order, balance side |
| 2.5 | `_stage_serasa_net_income` | SERASA: the after-dividends «RESULTADO LIQUIDO» is not post-tax profit |
| 2.6 | `_stage_balance_liability_totals` | Per balance cell: equity-inclusive non-current subtotal demoted (RC#2); a missing LT or equity block filled by the identity (RC#3 — never for a zero block); `totalLiabilitiesAmount` = current + non-current liabilities, relabelled on a printed «Total do passivo» or **derived** as a synthetic row |
| 2.7 | `_stage_dedup` | Same page + name + figures, or same field + figures (same page or page kind). Figures compared **as numbers** ('-13.534.197' = '(13.534.197)'); rows with **different CVM codes are never duplicates**; the survivor is the best-placed row |
| 2.8 | index remap | `sbs_duplicate_of` is rewritten as the index in the bucket's **full** list |

### 3.6 Stage 3 end — one value per field and period (`sbs_fields.compute`)

| # | Sub-step | What it does |
|---|---|---|
| 1 | Candidates | Labelled rows **the export shows** (`selection.passing_rows`), of the field's own statement. Income tax is valued from the DRE only (`VALUE_STATEMENTS`) — the DFC tax line is an add-back with the opposite sign |
| 2 | Hierarchy | A candidate that is a **component** of another candidate of the same field is not counted: CVM code descendant, or a line inside a contiguous block (≥ 2 lines, same page, right below or above) that sums to the other line in every period (summed fields only) |
| 3 | Current only | Trade debtors / creditors / stock: the Stage-3 section decides; only rows without a section fall back to «printed before the current total» |
| 4 | Options | TOTAL fields pick one line (same value / printed total / the line before the financial result for EBIT / identity); other fields are summed unless one line is the printed total of the others (`provisions`: current + non-current summed). **Held for sale** (since 2026-11-05): an «Ativo(s) mantido(s) / disponível(is) para venda» line printed OUTSIDE both balance subtotals adds an alternative «printed current total + that line» (`held_for_sale+identity`) to `totalCurrentAssetsAmount` — the same for «Passivos … associados a ativos mantidos para venda» → `financialLiabilitiesShortTerm` |
| 5 | Identities | `assets = current + non-current`; `assets = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm + equity`; `totalLiabilities = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm`; `gross = turnover ± COGS`; `post-tax = pre-tax ± tax`. Defaults are kept when they hold; else the fewest fields switch to the alternative that makes it hold (two alternatives with the same figures are one answer — the held-for-sale alternative is taken only here, where the identity proves the line stands outside: Cibra 2023 current assets 2.232.467 + 30.000); a block nobody printed is 0 when the identity leaves nothing for it |
| 6 | Review | `needs_review` on: first of several different lines, figures on the next line, any field of an identity that fails in that period (`review_reason`); EBIT printed only after the financial result → the field is present but **empty**, with review |

### 3.7 Stage 4 — presentation (`selection.py` → `export_excel.py`, `app.py`)

Row selection (`selection.passing_rows`, shared by the Excel, the Streamlit screen, the harness and
`sbs_fields`), checks in this order:

1. drop `sbs_duplicate_of`, `serasa_analysis`, `non_figure_line`, `other_statement`;
2. keep the derived total-liabilities row if it has a figure;
3. **ITR: only the pages of the CVM forms** (pages carrying codes) — note / commentary pages never;
4. DFP_4COL / TWO_COL: only `PRIMARY:*` pages, unless the statement spans ≤ 2 consecutive pages;
5. Consolidado-only when the document presents consolidated figures (decided once per document);
6. the row must have a figure in N or N-1 (`pick_cells`: first N / first N-1 cell by period).

Workbook rendering: figures with decimals (EPS, centavos) keep them; per-share rows (code 3.99, EPS
captions, «ON / PN» with decimals) carry «(per share)»; column headers show the period dates where
the metadata proves them (ITR / Serasa balance N-1 = 31/12 of the previous year, annual N-1 = one
year earlier, ITR DRE/DFC = the same period one year earlier); the `sbs_fields` sheet shows the
review reason and the identities per period. Since 2026-11-05 a row flagged `needs_review` is filled
light red on the balance / income / cash_flow sheets and its reason is written in a 7th column
«Review» (`selection.review_reason`: a failing equation, figures of two panels on one line, two
figures for one column — «the first is shown» —, the SBS field chosen between / not resolved, a
withdrawn label, the Stage-3 review reason), with a legend note under the sheet; the Streamlit tables
show the same (light red row, «Review» column, REVIEW badge, «Needs review» KPI).

---

## 4. Business rules and decisions

| Rule | Decided by / when | Where |
|---|---|---|
| Consolidated figures if the document presents them, else the company's own — decided once per document, never per row | user 2026-11-04 | `selection.consolidado_filter` |
| SERASA = balance + DRE only (analysis sections never exported) | user 2026-11-04 | Stage 1 step 4c |
| Production may have **no** words layer: no stage may depend on it | user 2026-11-04 | `words_recovery.py` optional |
| Column (x position) wins over the provider's period tag | user 2026-11-04 | step 2c |
| An unambiguous table header wins over the tag and over the pair rule: period + perspective from the column's zone; the S2 alternative (every LST figure votes + the 6051 singles vote) is rejected | user 2026-11-05, simulation `_audit/ZONES_SIMULATION_2026-11-05.md` | step 2c', Step 5 |
| **Liability fields (pinned):** `shortTermLiabilitiesAmount` / `longTermLiabilitiesAmount` = short- / long-term **credits and loans** (empréstimos e financiamentos, debêntures, títulos emitidos, mútuos/empréstimos de sócios); `financialLiabilitiesShortTerm` / `financialLiabilitiesLongTerm` = **total** current / non-current liabilities. Opposite of the xlsx Description text — do not revert | user 2026-11-04 | `build_sbs_mapping.py` JOIN, `code_map`, twin group `debt`, Stage-3 section totals, `sbs_fields` identities |
| `totalLiabilitiesAmount` = current + non-current liabilities, equity excluded, never the balance grand total | design | Stage 3 PASS 2.6 |
| Bio-asset amortisation is depreciation; bio-asset purchases are CAPEX | user 2026-11-04 | aliases in `build_sbs_mapping.py` |
| EBIT = the operating result BEFORE the financial result; a «resultado operacional» after it is not EBIT (left empty + review if nothing else) | user 2026-11-04 | Stage 3 PASS 2.3, `sbs_fields._ebit_not_printed` |
| Cost signs «as printed» (no normalisation of expense signs) | user 2026-11-04 | `sbs_fields` |
| A sign / dash is corrected only when proven; otherwise flagged | brief 2026-11-04 | `sign_proof.py` |
| `provisions` = IAS 37 liability provisions only (contingencies / lawsuits, decommissioning, warranties, onerous contracts), current + non-current summed, balance liabilities only (strict scope); payroll accruals, taxes payable, asset allowances, cash-flow lines and captions that do not say what they provide for are not provisions | user 2026-11-05 | `build_sbs_mapping.py` `EXTRA_ALIASES` / `EXTRA_SCOPE` / `STRICT_SCOPE`, `code_map` 2.01.06 / 2.02.04 |
| Depreciation excludes right-of-use amortisation, PPA («mais valia») amortisation and the amortisation of borrowing costs | user 2026-11-05 | no alias catches them (checked on the corpus) |
| Related-party balances are not loans (reconfirmed) | user 2026-11-04 / 2026-11-05 | no loan alias |
| Interest paid = the DRE «Despesas financeiras» (not the DFC «Juros pagos»); a separate «Variação cambial» is not added | user 2026-11-05 (auditor's recommendation) | alias of `interestPaidAmount`, scope DRE |
| Held-for-sale assets (and the liabilities associated with them) are current: a line printed outside both subtotals joins the current total where the balance identity proves it | user 2026-11-05 (IFRS 5 / IAS 1.66) | `sbs_fields.HELD_FOR_SALE_FIELD` |
| Agent 6051 (ONLY_AMOUNT `AMOUNT_N-1` primary, one subgroup per figure) for production; the words layer is still requested from reciTAL | user 2026-11-05 | — (extraction configuration) |
| Local goldens may be regenerated only after their diff has been shown | user 2026-11-04 | `harness/run.py goldens` |

---

## 5. Data contract — what the output carries

### 5.1 Top level

```jsonc
{
  "metadata":        { "CLOSING_DATE": {...}, "DURATION": {...}, "CURRENCY": {...}, "UNIT": {...},
                       "ACCOUNT_TYPE": {...} },        // ACCOUNT_TYPE only for a provider tag
  "document_type":   "ITR" | "DFP_4COL" | "TWO_COL" | "UPPERCASE" | "SERASA",
  "number_format":   "BR" | "US",
  "statements":      { "BALANCE_ASSETS_LST": [row…], "BALANCE_LIABILITIES_LST": [row…],
                       "INCOME_STATEMENT_LST": [row…], "CASH_FLOW_LST": [row…] },
  "page_classification": { "<page>": {page_class, confidence, reasons, n_cols, …} },
  "classification_summary": { primary_pages, class_distribution, fuzzy_backend, … },
  "sbs_fields":      { "perspective", "fields": {<field>: entry}, "checks": [identity…] }
}
```

### 5.2 Row

| Key | Set by | Meaning |
|---|---|---|
| `page_nb`, `label`, `columns[]` | Stage 1 | the visual row |
| `code` | step 4 | CVM account code |
| `label_parts`, `label_wrapped` | step 4b | the lines of a wrapped name; the merge type `A` / `B` / `C` / `D` |
| `label_missing` | step 5 | unnamed row (subtotal whose name the OCR dropped) |
| `page_class`, `page_confidence` | Stage 2 | class of the row's page |
| `other_statement` | step 4e | `DRA` / `DMPL` / `DVA` (ITR) |
| `serasa_analysis` | step 4c | outside the Serasa statement zone |
| `non_figure_line` | step 4d | a header line returned as figures |
| `xband_mixed` | step 2b | an orphan row mixing two panels |
| `arith_review` | step 5b | the CVM equation of this parent row does not add up |
| `source`, `label_source` | words | recovered from the OCR words layer |
| `needs_review` | any step | OR of every uncertainty (Stage 3 never clears it, except SERASA twins resolved by arithmetic); its reason in words (`selection.review_reason`) is the «Review» column of the Excel / Streamlit statement tables |
| `sbs_label`, `sbs_scope`, `sbs_match_method`, `sbs_match_source`, `sbs_match_score` | Stage 3 | the SBS field and how it was found |
| `sbs_candidates`, `sbs_review_reason` | Stage 3 | unresolved alternatives / explanation |
| `sbs_blocked_reason` | Stage 3 | `out_of_scope`, `net_after_dividends`, `includes_equity`, `after_financial_result` |
| `sbs_duplicate_of` | Stage 3 | index (full list) of the kept row this one duplicates |
| `sbs_section` | Stage 3 | balance section of a labelled row |
| `sbs_derived` | Stage 3 | the synthesized total-liabilities row (no page) |

### 5.3 Column (cell)

| Key | Meaning |
|---|---|
| `account_type` | `Controladora` / `Consolidado` / `Individual` / other header text / `null` |
| `period` | `AMOUNT_N`, `AMOUNT_N-1`, or `AMOUNT_N-2` (an extra older column, never exported) |
| `value` | the figure as a Brazilian-notation string (after the US / D-C / sign / dash rewrites) |
| `value_raw` | the token exactly as recognized, whenever `value` was rewritten |
| `period_corrected`, `period_src` | the column grid (2c), the header zone (2c') or the three-column proof (2d) changed the provider's period |
| `account_type_corrected`, `account_type_src` | the header zone (2c') gave another perspective than the pair rule (`account_type_src`) |
| `sign_corrected` / `dash_corrected`, `proof` | step 5b repair and the equation that proves it (`dash_corrected` also on the income statement since 2026-11-05) |
| `source: ocr_words` | recovered from the words layer |

### 5.4 `sbs_fields` entry

`statement`, `AMOUNT_N`, `AMOUNT_N-1`, `method{period}`, `verified{period}` (identities that hold),
`rows[{page, label, AMOUNT_N, AMOUNT_N-1, used}]` (`used: false` = a row not counted — a component of
another line, a non-current row of a current-only field —, shown «(not counted)» on the `sbs_fields`
sheet), `note`, `needs_review`, `review_reason`, `held_for_sale`
(the held-for-sale line(s) a current total may add). Methods: `single`, `same_value`, `sum`,
`total_of_components`, `first_of_several`, `before_financial_result`, `identity`, `sum+identity`,
`subset+identity`, `zero_by_identity`, `held_for_sale+identity`, `derived = current + non-current`,
`figures_on_next_line`.

### 5.5 `sbs_match_method` values

`code`, `dict`, `unique`, `scope`, `qualifier`, `positional`, `unresolved_ambiguous`,
`empty_label_arith`, `empty_label_unresolved`, `empty_label_intermediate`, `empty_label_grand_total`,
`balance_identity`, `derived`, `tax_component`, `tax_component_total`, `investing_asset_line`.

### 5.6 Report counters (`<name>_report.json`)

`rows` (incl. `wrapped_label_A|B|C|D`, `non_figure_line`, `other_statement_*`, `serasa_analysis`,
`arith_review`), `columns` (incl. `dc_suffix_signed`, `sign_corrected`, `dash_corrected`),
`only_amount` (incl. `lst_detached`, `period_corrected`, `zone_panels`, `zone_period_set`,
`extra_column_resolved`, `xband_mixed_rows`), `account_type` (incl. `zone`, `zone_type_changed`),
`code_value`, `classification`, `sbs` (per-stage counts, `fields_valued`,
`identity_ok`, `identity_failed`), `words` (optional step).

---

## 6. Tag, never delete — where each kind of row ends up

| Row carries | JSON | SBS labelling | `sbs_fields` | Excel / Streamlit |
|---|---|---|---|---|
| `other_statement` (DRA/DMPL/DVA) | kept | skipped | no | no |
| `serasa_analysis` | kept | skipped | no | no |
| `non_figure_line` | kept | skipped | no | no |
| `sbs_duplicate_of` | kept | labelled | no | no |
| page without CVM codes in an ITR (notes) | kept | labelled | no | no |
| `NOTE*` page in DFP_4COL / TWO_COL (statement > 2 pages) | kept | labelled | no | no |
| `AMOUNT_N-2` cell | kept | ignored | no | no |
| `sign_corrected` / `dash_corrected` cell | kept, `value_raw` = OCR token | yes | yes | yes (repaired figure) |
| `sbs_derived` row | appended | — | yes | yes (no page) |
| `needs_review` row | kept | labelled | yes (with its own review) | yes — light red, reason in «Review» |

---

## 7. Quality control

| Tool | Command | Use |
|---|---|---|
| Regression harness | `python3.13 -E -P harness/run.py snapshot <tag> [--words]` then `diff <a> <b> -v` | 22 reciTAL PDFs (`_recital/`), cell + Excel + SBS-field diff, scoreboard (conservation, off-line figures, period grid, missed words, missing statements, identities OK/✗, goldens). Run before AND after any change; explain every diff |
| Local goldens | `python3.13 -E -P harness/run.py goldens` | 8 `structured_output*.json` byte-identical to the code's output; regenerate only after showing the diff |
| Self-test | `python3.13 -E -P harness/run.py selftest` | company-only rule, ITR Consolidado-only row counts, words step pluggability |
| Independent audit | `AUDIT_BRIEF.md`, `python3.13 -E -P harness/audit_run.py <id> … --all-pages` | a separate session builds its own reference from page images; findings → `_audit/`; the reference of each audited document is kept as `_audit/<id>/auditor_reference.txt` (`sheet\|page\|key\|N\|N-1`; Cibra: 8 fields with the Controladora columns) to re-check a later fix |

Invariants that must hold after any change: conservation (output figures = in-scope extracted
figures), `process(data)` without words = the words-free output, 0 failed identities on the corpus
(168 OK in production mode, 172 with words), no `needs_review` cleared, the 8 goldens explained.

---

## 8. Known limitations and open items

* **Panel boundary for short figures (DEL10, latent).** Figures are assigned to a panel by their
  centre x with a 0.01 margin before the next panel's names; the short numbers of the left panel's
  last column can fall into the right panel (Delfia p5: 58 / 371 / 933). Not fixed yet.
* **Figures the extraction lost.** Not recoverable without the words layer (Delfia p5/p9 Consolidado
  cells, p6 revenue N-1); with it, `--words` (a zone column joins the words grid even when the
  extraction left it almost empty — Cibra p7 Conso 2024).
* **Header zones are not built** (previous behaviour stands) on:
  - pages with two tables or a table continued from the previous page;
  - a page whose date frame is centred over a wide column (MBP p14);
  - a panel with a stray column (Delfia p5 right panel, DEL10);
  - documents without a per-column perspective header: TWO_COL, ITR, SERASA, UPPERCASE.

  1Q26 note pages p49 / p56 / p66 keep 25 Step-2c period errors (not exported).
* **Agent 6051 outside the zones — not measured yet.** Where no zone is built, Step 2c gives no vote
  to the 6051 singles (`oa_single`), so a 6051 period mis-tag there stays uncorrected. To be checked on
  the corpus re-extracted with the 6051 configuration before production.
* **Consolidado 2024 column of a side-by-side balance** (8 figure columns, Cibra p7) lost by both
  reciTAL agents; recovered only with the words layer (reported to reciTAL 2026-11-05).
* **An OCR token that is not a figure** («C» for 0, Serasa) stays an empty cell.
* **Period length from a single date** («31 DE DEZEMBRO DE 2023») is not inferred (a 9-month report
  can also end on 31/12).
* **Currency** is never inferred when not printed.
* **Streamlit** shows decimals and the «Review» column but not the «(per share)» marker nor the dated
  column headers (Excel only).
* Fields without aliases: `goodwillAmount`, `netGroupProfitAmount`,
  `pensionFundsAndOtherStaffCommitmentsAmount`, `exploitProvisionAmount` (+ the alias-less
  `totalLiabilitiesAmount`, resolved arithmetically). `provisions` has aliases since 2026-11-05.

---

## 9. Glossary

| Term | Meaning |
|---|---|
| **DFP / ITR** | annual / quarterly CVM filing; ITR forms carry account codes |
| **Controladora / Individual vs Consolidado** | company-only vs group figures (two "perspectives") |
| **N / N-1** | current / comparative period columns (`AMOUNT_N`, `AMOUNT_N-1`) |
| **Panel** | one of two tables printed side by side on a page (Ativo \| Passivo) |
| **Primary statement** | balance sheet (BP), income statement (DRE), cash-flow statement (DFC) — not notes |
| **SBS field** | one of the 43 target fields of the downstream SBS system (`sbs_label_mapping.json`) |
| **Twin** | a caption shared by a current / non-current pair of SBS fields |
| **Balancete** | accounting-system dump printing each group total above its lines, often with D/C suffixes |
