# CLAUDE.md — br_ocr_json_processing

Project guide for Claude Code. Read this first; it points to the deeper docs.

> **New to the project? Start with [`docs/Pipeline_Overview.md`](docs/Pipeline_Overview.md)** — every
> step of the postprocessing in the order the code runs it (what it does, what it writes, why), the
> business rules / user decisions, the data contract (row / cell / `sbs_fields` keys and flags) and the
> quality-control workflow. This file keeps the condensed rules and the traps ("sensitive details").

## What this project does

Turns **raw OCR-provider JSON** of Brazilian financial reports (Portuguese) into a clean,
structured, **page-classified**, **SBS-labelled** JSON. Three stages:

- **Stage 1 — table reconstruction** (`structure_ocr.py`): rebuild rows/columns, attach line-item
  names to their AMOUNT_N / AMOUNT_N-1 figures, tag each column's `account_type`
  (Controladora / Consolidado) — where the table header is unambiguous, period and perspective come
  from the header zone of the figure's column (step 2c', sensitive detail #31) — attach row codes,
  carry document metadata; then repair the OCR sign / dash errors the statement's own arithmetic
  proves (`sign_proof.py`, step 5b — sensitive detail #26).
- **Stage 2 — page classification** (`classify_statements.py`): label each page
  `PRIMARY:{BALANCE|INCOME|CASH_FLOW}` / `NOTE` / `JUNK` with confidence + reasons.
  Philosophy: **tag, never delete.**
- **Stage 3 — SBS labelling** (`sbs_labeler.py`): stamp each row with its SBS Field Name
  (`sbs_label`, e.g. `cashAndSecuritiesAmount`) by matching `label` against the Portuguese
  aliases in `sbs_label_mapping.json`. Same **tag, never delete** philosophy.
- **Stage 3 end — SBS field values** (`sbs_fields.py`, output key `sbs_fields`): ONE value per SBS
  field and period — the contract handed downstream (user 2026-11-04: «it depends on us»). Several
  rows with one field are SUMMED (components), or their printed TOTAL is taken, or ONE is picked — and
  the choice is PROVEN by the accounting identities (assets = current + non-current = ST + LT + equity;
  gross profit = turnover ± COGS; net = pre-tax ± tax). See sensitive detail #20. A held-for-sale line
  printed OUTSIDE both balance subtotals joins the current total where the identities prove it (user
  decision 2026-11-05, #20g).
  ⚠️ **Liability field mapping pinned by the user 2026-11-04** (opposite of the xlsx Description):
  `financialLiabilitiesShortTerm` / `financialLiabilitiesLongTerm` = the TOTAL current / non-current
  liabilities; `shortTermLiabilitiesAmount` / `longTermLiabilitiesAmount` = short- / long-term CREDITS
  AND LOANS. "ST + LT + equity" above therefore means the two financialLiabilities totals (#21).
- **Stage 4 — Excel export** (`export_excel.py`): render the structured JSON into a polished,
  Allianz-styled `.xlsx` (sheets: metadata / balance / income / cash_flow / sbs_fields). This stage
  **does** filter (see "Document type" below) — it is the presentation layer, run separately. A row the
  pipeline flagged `needs_review` is filled light red on the statement sheets, its reason in words in the
  «Review» column (`selection.review_reason`, same on the Streamlit screen — #30).

`structure_ocr.py` runs Stages 1-3 and embeds Stage-2/3 results into its output. `export_excel.py`
is a separate Stage-4 step over that output.

- **OPTIONAL — OCR words recovery** (`words_recovery.py`, `docs/Words_Recovery.md`): when the reciTAL
  words layer (`GET /files/{id}/words/`) is supplied, figures / lines / labels the extraction lost
  (~8% of the table figures) are recovered from it, flagged `source` / `label_source: "ocr_words"`.
  ⚠️ **Production may NOT have the words layer** (asked to reciTAL 2026-11-04; their answer proposed the
  agent-6051 configuration instead and did not confirm words in production — our reply, «6051 accepted,
  words still needed», was drafted 2026-11-05): the pipeline must keep working without it —
  `process(data)` never calls the module, `words=[]` or a deleted module give the words-free output
  exactly (`harness/run.py selftest` asserts it). Never make a Stage depend on it.

## Run it

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 structure_ocr.py <input.json> -o <output.json>      # full pipeline (Stage 1+2+3)
python3.13 structure_ocr.py <in.json> -o <out.json> --words <words.json>  # + OPTIONAL words recovery
python3.13 sbs_labeler.py <structured_output.json> --dry-run    # Stage-3 only, report (no write)
python3.13 sbs_labeler.py <structured_output.json> -o <out>     # Stage-3 only, post-process
python3.13 export_excel.py <structured_output.json> -o <x.xlsx> # Stage-4 Allianz Excel export
python3.13 diagnose_pages.py <input.json>                       # read-only Stage-2 diagnostics
python3.13 -m streamlit run app.py                              # web UI (upload/review/export)
```

**New PDF end to end (reciTAL → Streamlit):** upload the PDF in the reciTAL web UI (agent
`Brazil_OCR202606_V4`, doctype 5485) → `python3.13 -E -P harness/recital_fetch.py files 5485 20` (status
`to_verify` = extraction done) → `python3.13 -E -P harness/recital_fetch.py export <file_id>` →
upload `_inbox/<id>/<id>_raw.json` (+ optionally `<id>_words.json`) in the app. READ-ONLY on reciTAL.

**Agent 6051 `Brazil_OCR202606_V4 copy reciTAL`** (created by reciTAL 2026-11-04): agent 5485 with
`AMOUNT_N-1` also a primary label of the group ONLY_AMOUNT — every figure comes in its OWN subgroup
(singles tagged N or N-1, no pairs). More complete extraction (Cibra 12.2024, audit 2026-11-05:
Consolidado figures 92 % vs 77 %); chosen for production by the user 2026-11-05. The pipeline reads
both shapes; their period traps differ (#14, #31). List its files with `recital_fetch.py files 6051 20`.

**Independent audit (separate session):** brief = `AUDIT_BRIEF.md`; runner
`python3.13 -E -P harness/audit_run.py <file_id> ...` → `_audit/<id>/` (both modes, Excel, page images,
summary). `_recital/` = harness corpus (22 files of `ground_truth.yaml`), `_inbox/` / `_audit/` = new
documents; all git-ignored (confidential).

Full operational detail (scenarios, troubleshooting, reading outputs): **`RUNBOOK.md`**.

**Regression harness (run before AND after any change to Stages 1-4):**

```bash
python3.13 -E -P harness/run.py snapshot <tag>        # 22 reciTAL PDFs (_recital/) -> scoreboard
python3.13 -E -P harness/run.py snapshot <tag> --words  # same, OCR-words recovery ON (compare both modes)
python3.13 -E -P harness/run.py diff <tagA> <tagB> -v  # cell-level + Excel-level differences
python3.13 -E -P harness/run.py goldens                # the 8 local structured_output*.json in sync?
python3.13 -E -P harness/run.py selftest               # company-only rule + words step pluggability
```

Corpus = the 22 files of the reciTAL agent `Brazil_OCR202606_V4` (doctype 5485), exported read-only to
`_recital/<file_id>/` (values/groups/words/page images; the 8 local samples are 8 of them). Ground truth
(statement pages per document, Serasa statement zone, eye-verified golden cells) is
`harness/ground_truth.yaml`. Metrics: conservation, off-line amounts, multi-line null rows, period vs
column grid, wrapped-label fragments, numbers on paper missing from the output (OCR words), statements
missing from the Excel, Serasa analysis rows leaking into the Excel, golden pass/fail. Snapshots are
confidential data (git-ignored `*.json`).

## File map

| File | Role | Run directly? |
|---|---|---|
| `structure_ocr.py` | **Entry point** — Stage 1 + Stage 2 + Stage 3; computes `document_type`; header zones (step 2c', #31) | Yes (CLI) |
| `words_recovery.py` | **OPTIONAL** Stage-1 add-on — recovery from the OCR words layer (cells / lines / labels / Serasa DURATION); its column grid also takes the header-zone columns (#31); deletable | No (`structure_ocr.py --words`) |
| `sign_proof.py` | Stage-1 step 5b — lost minus / dash read as «1» repaired ONLY where the CVM code tree or a printed subtotal proves it (`sign_corrected` / `dash_corrected`, `value_raw`, `proof`); the income statement gets the dash test only, never a sign flip (#26); stdlib only | No (library) |
| `sbs_fields.py` | Stage-3 end — ONE value per SBS field and period, identity-proven, hierarchy-aware (`sbs_fields` key, Excel sheet `sbs_fields`) | No (library) |
| `selection.py` | **Shared** — which rows / figures the export shows (`passing_rows`, `pick_cells`, `consolidado_filter`, ITR `form_pages`, `is_per_share`) and why a row is flagged (`review_reason`, the «Review» column); stdlib only, used by Excel, Streamlit, harness, `sbs_fields` | No (library) |
| `classify_statements.py` | Stage-2 classifier (4 layers + safety-net) | No (library) |
| `doc_type.py` | **Shared** — document-type detection + filter policy (single source of truth) | No (library) |
| `sbs_labeler.py` | Stage-3 SBS labeller (`sbs_label`) | Yes (CLI, post-process) |
| `export_excel.py` | Stage-4 Allianz Excel export (metadata/balance/income/cash_flow/sbs_fields; flagged rows light red + «Review» column) | Yes (CLI) |
| `app.py` | **Streamlit front-end** — upload/review/export; reuses `export_excel` (screen == Excel, incl. the «Review» column) | Yes (`streamlit run`) |
| `.streamlit/config.toml` | **Config** — Streamlit Allianz theme + 50 MB upload limit | — |
| `build_sbs_mapping.py` | Regenerates `sbs_label_mapping.json` from YAML + xlsx (+ `EXTRA_ALIASES`, `EXTRA_SCOPE`, `STRICT_SCOPE`, `code_map`) | Yes (CLI) |
| `anchors_lib.py` | **Shared** matching/parsing/geometry — single source of truth | No (library) |
| `diagnose_pages.py` | Read-only Stage-2 diagnostics | Yes (run from project dir) |
| `harness/run.py`, `harness/hcore.py` | **Regression harness** on the 22 reciTAL PDFs (scoreboard, diff) | Yes (`python3.13 -E -P`) |
| `harness/recital_fetch.py` | READ-ONLY reciTAL API (agents / files / fetch / **export** for Streamlit / page images) | Yes (`python3.13 -E -P`) |
| `harness/audit_run.py` | Audit runner: fetch + pipeline in both modes + Excel + page images → `_audit/<id>/` | Yes (`python3.13 -E -P`) |
| `AUDIT_BRIEF.md` | Brief for an independent auditor session (rules, checklist, report format) | — |
| `harness/ground_truth.yaml` | **Config** — statement pages, Serasa zones, eye-verified golden cells | — |
| `anchors.yaml` | **Config** — anchor lexicon, business-editable | — |
| `sbs_label_mapping.json` | **Config** — SBS Field → Portuguese aliases (generated) | — |
| `labels_OLD.yaml` | Source: old primary codes + aliases (input to mapping build) | — |
| `SBS_fields_mapping_with_INDIAN_labels.xlsx` | Source: new SBS Field Names (input to mapping build) | — |
| `docs/Pipeline_Overview.md` | **Start here** — every step in run order, business rules, data contract, QC | — |
| `docs/OCR_Table_Reconstruction.md` | Stage-1 methodology | — |
| `docs/Primary_Statement_Classification.md` | Stage-2 methodology | — |
| `docs/SBS_Labelling.md` | Stage-3 methodology | — |
| `docs/Excel_Export.md` | Stage-4 methodology | — |
| `docs/Words_Recovery.md` | Optional OCR-words recovery methodology | — |
| `docs/Frontend_Streamlit.md` | Streamlit front-end (`app.py`) methodology | — |
| `RUNBOOK.md` | How to run / troubleshoot | — |

## Test documents & their outputs (kept separate — do NOT overwrite)

| Input | Output | Shape | Notes |
|---|---|---|---|
| `comprehansive_sample.json` | `structured_output.json` | 69 LST pages, 4-col (DFP annual) | CODE_VALUE empty; metadata as scalars; export type **DFP_4COL** |
| `sample_2.json` | `structured_output_sample2.json` | 15 pages, 2-col, 2024/2023 | CLOSING_DATE as group; export type **TWO_COL** (no Consolidado split) |
| `itr_samples.json` | `structured_output_itr.json` | ITR quarterly, hierarchical codes | CODE_VALUE populated (300); export type **ITR** |
| `provider_sample.json` | `structured_output_provider.json` | uppercase-label dump, reversed N-1/N | all NOTE pages; export type **SERASA** (provider tag `SERASA EXPERIAN`); single-page balance, ST/LT loans (EMPREST + TITULOS EMITIDOS) + non-current `APLICACOES FINANCEIRAS` resolved by bucket-wide sections |
| `complicated_empty_labels_sample.json` | `structured_output_complicated.json` | 2 pages, empty labels, no codes/headers | balance reads `TOTAL DO PASSIVO` → Stage-2 mislabels NOTE; export type **TWO_COL**, rescued by small-span override |
| `complicated_sample_2.json` | `structured_output_complicated_sample2.json` | 3 LST pages, uppercase ALL-CAPS labels, no codes | export type **UPPERCASE**; detail-heavy income statement; SBS recall driven by `token_fuzzy` engine (the file that surfaced the strict-coverage root cause). **Side-by-side balance** (Ativo left x≈0.44 / Passivo right x≈0.94 on the SAME p2 rows) — surfaced the `augment_with_only_amount` x-band gap (see #11): 5 empty-label subtotals (2 assets, 3 liabilities incl. equity). Single-page → bucket-wide sections ≡ page-local. ⚠️ on-disk filename is the long `complicated_sample_2 (Demonstrativos 2023 Alvorada Pneus Distribuidora).json` |
| `serasa_report_1.json` | `structured_output_serasa1.json` → `serasa_report_1.xlsx` | SERASA Experian, ALL-CAPS, no codes, single-page balance | export type **SERASA**; non-current `APLICACOES FINANCEIRAS` → `financialAssetsAmount` via bucket-wide arithmetic sections |
| `serasa_report_2.json` | `structured_output_serasa2.json` → `serasa_report_2.xlsx` | SERASA Experian, balance **split across p3/p4/p5** | export type **SERASA**; the page-split case — both `EMPREST E FINANCIAMENTOS` → ST/LT positional only because sections are computed bucket-wide |

Each run also writes `<output>_report.json`. `export_excel.py` writes `<output>_report.xlsx`.
`page_diagnostics.json` comes from `diagnose_pages.py`.

## ⚠️ Sensitive details — get these wrong and results break

1. **Input shape differs from the obvious.** Root is a **list of 11**, NOT a dict with `group_id`.
   Groups are list elements with `group_name` + `subgroups`; a subgroup = one visual table row
   (its elements share ~one `y`). `value`/`label`/`location` live in `subgroups[].values[]`.
   ⚠️ Mostly — on dense pages the provider binds a name to its neighbour line's figures (see #13).

2. **Metadata comes in TWO forms.** CLOSING_DATE/DURATION/CURRENCY/UNIT may be a **scalar**
   (`data_point_name` + `value:{}`) OR a **group** (`group_name` + subgroups — e.g. CLOSING_DATE in
   ITR/sample_2). `extract_metadata` handles both, picking the highest-confidence value. Do not
   assume "scalars only".

3. **CODE_VALUE may be empty OR populated.** Empty in `comprehansive_sample.json`; populated in
   `itr_samples.json` (300 hierarchical codes `1`/`1.01`/`1.01.01`, sitting left at x≈0.05).
   `attach_codes` binds by page + y (the page's row tolerance, #12). In an ITR the codes also decide
   which pages are FORMS (#22), the balance sections (#28) and the sign proof (#26).

4. **Group tag `*_LST` classifies CONTENT, not LOCATION.** The provider tags `CASH_FLOW_LST` on 42
   pages but only **1** is the real cash-flow statement. Never decide page type from the group tag —
   Stage-2 uses **anchor completeness** instead.

5. **ONLY_AMOUNT is NOT a complete superset** (~6% of LST amounts missing). Columns = UNION(LST +
   ONLY_AMOUNT), deduped by `(value, round(x_min,4), round(y_min,4))`.

6. **ACCOUNT_TYPE covers a column PAIR** (N, N-1), often hovering over the gap between them. Match by
   x-overlap with the pair, take the nearest header ABOVE. Never merge header fragments by y alone.
   ⚠️ A "pair" (N cell + next N-1 cell) whose span covers the headers of TWO DIFFERENT perspectives
   set apart (gap ≥ `PERSPECTIVE_HEADER_GAP_MIN` = 0.03) is split — each cell takes its own header
   (São Salvador p25: a Controladora dash «1» paired with a Consolidado (20) typed both Controladora and
   the Consolidado filter dropped the line). Two perspective fragments printed side by side are ONE
   «Controladora e Consolidado» header (ALUBAR p47, the OCR dropped the «e») — not split.
   ⚠️ Since 2026-11-05 the pair rule types only the figures OUTSIDE the header zones (#31): on a panel
   whose header is unambiguous the perspective is the zone's. The pair rule pairs by PERIOD, so one wrong
   period moves a figure into the neighbouring perspective (Cibra p7: Contr. 2023 tagged N + Conso 2023
   N-1 → a pair under «Consolidado» → Conso 2024 = Contr. 2023). A zone perspective that differs from
   the pair rule's is traced: `account_type_corrected` + `account_type_src`.

7. **Balance is a TWO-PAGE statement** (Ativo on one page, Passivo+PL on the next). Completeness and
   the disambiguator treat adjacent balance pages as one statement, not a repeat — otherwise the 2nd
   page gets wrongly demoted.

8. **Name matching: naïve fuzzy FAILS.** `token_set_ratio >= 90` scores subtotals
   (`Total do ativo circulante`) as 100 against `Total do ativo`. The working rule (in `anchors_lib`):
   all canon tokens present + extra tokens only from `benign_insertions` (foreign words like
   `circulante`/`segmento` → reject) + `token_sort_ratio >= 85` backstop. Each anchor carries its own
   full canonical token set.

9. **UTF-8 is fine; the console lies.** Files are valid UTF-8 (`Títulos`, `30/06/2025`). A `�` in a
   Windows console is a cp1252 rendering artifact, not corrupt data.

10. **Conservation invariant.** Stage 1 loses no figures: output columns == unique in-scope amounts
    (e.g. 3,677 == 3,677 on sample 1). If you change Stage-1, re-verify this. ⚠️ Conservation checks
    COUNT, not CORRECTNESS of attachment — see #11.

11. **Side-by-side (two-panel) layouts: route sums by x-band, not y alone — in BOTH amount stages.**
    Some balances print Ativo (left, x≈0.33-0.44) and Passivo (right, x≈0.82-0.95) on the SAME physical
    rows (same y). TWO Stage-1 stages place amounts by y and BOTH must be x-band-aware:
    (a) `augment_with_only_amount` (runs FIRST) attaches an `ONLY_AMOUNT` sum to a NAMED LST row — it
    keeps only candidate rows whose statement's NAMED x-band overlaps the sum's x, else drops the sum to
    orphan (`orphan_xband` counter); (b) `materialize_orphans` (runs second) routes each orphan sum to
    the statement whose NAMED-column x-band it overlaps (`build_statement_xbands` / `_statement_for_orphan`),
    THEN clusters by y within that statement. ⚠️ The x-band fix was originally applied ONLY to (b); a
    pure-y (a) silently GLUED a liability subtotal onto an assets row as a spurious 2nd column (and vice
    versa) — AND corrupted the very x-bands (b) relies on, cascading. Symptom on
    `complicated_sample_2`: 3 liability empty-label subtotals lost while all assets ones survived.
    A pure-y rule also merged assets+liabilities into single 4-column `label_null` rows in a 2-col doc —
    conservation still passed (count ok) but rows were corrupted. Guard: `materialize_orphans` flags a
    synthetic row `xband_mixed` + `needs_review` when one period repeats with a wide x-gap
    (`XBAND_GAP_MIN=0.25`); a narrow repeat is a legit 4-col Controladora|Consolidado block, not a merge.
    (Seen in `complicated_empty_labels_sample.json` (merge) and `complicated_sample_2` (glue).)
    ⚠️ **2026-11: the gate is now the physical PANEL, not per-statement x-bands** (`build_panels`):
    panels = columns of NAMES (≥0.15 apart, ≥3 distinct names, own figures, ≥2 shared lines). The
    statement x-bands were built from LST figures, which carry only the first column pair, so the
    Consolidado columns fell in no band (ALUBAR p6: a liability figure glued onto an asset row), and on
    single-panel pages the provider tags rows of ONE table into two statements (Romagnole p11 cash flow
    with `BALANCE_*` rows) → overlapping bands rejected the right row of the line. Orphans route by
    panel + the named row ABOVE (a subtotal closes the block above). Panels are found on exactly the 6
    side-by-side balance pages of the 22-PDF corpus (a `Comprado/Vendido` value column is NOT a panel).

12. **"Same line" tolerance comes from the PAGE's line pitch, never a constant.** Same-line spread p99
    ≤ 0.0017; pitch goes down to 0.0079 (Raízen p26 primary cash flow). The old 0.012/0.010 exceeded one
    line → cascade (a neighbour's figure takes the slot, the real one goes `label_null`) and chained
    orphan rows (16 figures in one row). Tolerance = 0.45 × pitch in [0.003, 0.0065] against the row's
    reference y (median y of its figures). Orphan clusters are bounded from their top member, never two
    figures in one column.

13. **The provider's row binding is NOT always right** (Step 1b `detach_offline_amounts`): on dense
    pages it binds the name of line k to the figures of line k±1 (DUTRA p2, Raízen p26). A figure off its
    name's visual line (outside tolerance AND outside the name box) or in another panel is re-placed.

14. **The period tag is NOT always right** (Step 2c `correct_periods_by_column_grid`): wrong in rows with
    empty cells (Raízen p26: 15 Contr-2025 figures tagged N-1, proven by the cash arithmetic), and a
    SINGLE-value ONLY_AMOUNT subgroup is ALWAYS tagged `AMOUNT_N`. The column (right-edge grid) wins —
    reference = full rows' reliable tags (LST / paired ONLY_AMOUNT), purity ≥ 0.9; corrected cells carry
    `period_corrected` + `period_src`. User decision 2026-11-04. 0 period/grid conflicts left on PRIMARY.
    ⚠️ The vote uses FULL rows only on purpose: letting every LST figure vote (simulation S2, 2026-11-05)
    brought back the Raízen p26 errors (3 golden cells) — the tags of the incomplete rows are the wrong
    ones — and letting agent 6051's single-value subgroups vote cancelled 11 correct corrections. Its blind
    spot: an ONLY_AMOUNT pair that jumped an empty column can be the only «full row» and repaint a whole
    correct column (Cibra p7, K1/K2). Where the table header is unambiguous, step 2c' overrides 2c (#31).

15. **Wrapped names are merged** (Step 4b `merge_wrapped_labels`, `label_parts`): DFP prints figures on
    the LAST line (type A: lowercase continuation; type B: connector + unnamed figures line), ITR on the
    FIRST (type D). Unmerged, the figure row carried a fragment (`da contribuição social` → mislabelled
    income tax; EBIT, COGS, minority interests unlabelled). In ALL-CAPS headings the continuation starts
    with the conjunction «E» (ALUBAR p7 «… IMPOSTO DE RENDA» / «E CONTRIBUIÇÃO SOCIAL» — the fragment was
    labelled income tax): `_continues` = lowercase start OR «E». Only «E»: «A vencer…», «De 1 a 3 anos»
    are line items of their own.
    ⚠️ Since 2026-11-05 also **type C** (`_caps_continuation`, audit Cibra K5): an ALL-CAPS name-only line
    ENDING with a connector (`_WRAP_CONNECTOR`) + the next ALL-CAPS line WITH figures that opens with a
    preposition / conjunction no line item starts with (`_CAPS_CONT_START`: DA / DO / DE / DOS / DAS / E)
    or is printed indented (`WRAP_INDENT_MIN` = 0.005 of x) → merged, figures of the 2nd line
    (`label_wrapped: "C"`, report `rows.wrapped_label_C`). Cibra p8 «LUCRO (PREJUÍZO) ANTES DO IMPOSTO DE
    RENDA E» / «  DA CONTRIBUIÇÃO SOCIAL»: the fragment had taken `incomeTaxExpenseRefundAmount` with the
    pre-tax figures and left `pretaxProfitAmount` empty; merged, the pre-tax line is labelled and the tax
    components «Corrente» / «Diferido» below it are found (#29a). A section header does not end with a
    connector, so it never swallows the first line of its section.

16. **The provider returns column headers as figures** (Step 4d `tag_non_figure_lines` → `non_figure_line`):
    Moreno p6 «(9 meses) (9 meses) (3 meses)» became an unnamed row `'(9' 'meses)' '(9' '(3'` → «9 / 9» in
    the Excel. An unnamed row with NO well-formed figure (text, dashes, `100%`, an unbalanced `(9`) is
    tagged (kept in JSON), never labelled, never exported. A row with ≥ 1 real figure is untouched.

17. **Metadata is normalized, never overwritten** (`normalize_metadata`): `CLOSING_DATE.normalized` (ISO;
    a bare year — or a year OLDER than the period text — is completed from the latest date of DURATION:
    Romagnole CLOSING_DATE `2024` is the comparative year, the statements are 31/12/2025),
    `CURRENCY.normalized` = `BRL` (`(Reais`/`reais)`/`REAL`), `UNIT.normalized` = multiplier (1 / 1.000 /
    1.000.000), `DURATION.normalized_months` for a date range. The Excel / Streamlit show the normalized
    value with the OCR token alongside. Since the audit 2026-11-04 (P14): a month-year closing date
    «12.2024» → the last day of the month; `normalized_months` also from an explicit count («09 meses»,
    «nove meses», trimestre / semestre) or from two dates with the SAME day-month one year apart
    («31 de dezembro de 2024 e 2023» = annual comparison → 12; since 2026-11-05 also the document's own
    typo «31 DE DEZEMBRO E 2024 E 2023» — «e» for «de» between the month and the year, `_DATE_TXT`,
    Cibra m3 — so the Excel AMOUNT_N-1 header carries 31/12/2023). A single date («31 DE DEZEMBRO DE 2023»)
    or two different day-months (an interim balance vs the year-end) state NO period length — never
    guessed (a 9-month report can end on 31/12: Grupo Moreno). No UNIT printed + ≥ 80 % of the figures
    with centavos → `UNIT.normalized = 1`, `normalized_from: "figures with centavos"` (Uniar, Benoit).
    Currency is never inferred.

18. **Balance subtotal arithmetic runs inside ONE account perspective and on a subtotal TREE**
    (Stage 3, `_perspective` / `_subtotal_tree`). (a) "First AMOUNT_N cell" mixed Controladora and
    Consolidado figures as soon as a Controladora cell was a dash (MBP p10: the derived total liabilities
    was 390.217 = ST only, true 925.621). (b) A block subtotal inside a section (`Realizável a longo prazo`)
    equals the running sum of its details just as a section total does → it took
    `totalFixedAssetsAmount` next to the printed total (Raízen p20, Moreno p5, 1Q26 p21). The tree (a row
    = sum of the last k ≥ 2 open items closes them) tells them apart: consumed by a section-level subtotal
    → `empty_label_intermediate` (no field, no review); closing everything with a subtotal child → page
    GRAND total (`totalAssetsAmount` on the assets side, no field on the liabilities side —
    `empty_label_grand_total`); a section total printed after block subtotals is proven by the tree.
    ⚠️ Except under a printed «Total do passivo» (`_TOTAL_LIAB_CAPS`, liabilities only): that line groups
    the SECTION totals (current + non-current) and is itself summed with the equity total into the grand
    total — its unnamed children are the section totals, not blocks (DIMENSIONAL p1 214.180 / 50.606,
    audit D1–D3); RC#1 then relabels «Total do passivo» as `totalLiabilitiesAmount`.

19. **ITR forms carry other statements** (Stage-1 step 4e `tag_other_statements` → `other_statement`):
    CVM codes 4 / 5 / 7 = DRA / DMPL / DVA. The provider puts DVA rows in INCOME_/CASH_FLOW_LST and ITR
    ignores page_class → the Eztec / Copasa Excel carried 29 / 36 DVA rows, and DVA «Receitas» /
    «Receitas Financeiras» / «Dividendos» took turnover / interest income / dividends next to the DRE /
    DFC. A page whose codes are mostly 4 / 5 / 7, and any row with such a code, is tagged: kept in JSON,
    never labelled, never exported.

20. **SBS fields: one value per field and period, proven** (`sbs_fields.compute`, `output["sbs_fields"]`).
    Candidates = the EXPORTED rows (same selection as the Excel) of the field's own statements (a
    cash-flow movement never values a balance field, a balance payable never the personnel expense).
    TOTAL fields (totals / results): one distinct value; else the printed total of the others; else the
    alternative an identity proves (one line, or their SUM — MBP non-current assets = two block
    subtotals 178.256 + 285.932 = assets − current assets; Solar: the right one of two «Total … não
    circulante»); operating profit = the line before the financial result (Serasa «RESULT ATIVIDADE»,
    not «RESULT OPERACIONAL»); else the first printed + `needs_review`. CURRENT fields (trade debtors /
    creditors, stock — SBS says *_CURRENT): non-current rows not counted (`sbs_section` from Stage 3).
    Other fields: summed, unless one line is the printed total of the others (ALUBAR tax 27.583 =
    −10.153 + 37.736); an identity may keep a subset (Copasa: the DRE cost line, not its detail line).
    Moreno COGS = biological-assets change + cost of sales, PROVEN by gross profit (500.441). A block
    nobody printed is 0 when the identity leaves nothing for it (`zero_by_identity`). Identity tolerance
    is ABSOLUTE (± 1 per term): a relative one let two different equity totals both «fit».
    Corpus (22 PDFs): 168 identity checks pass, 0 fail (172 with the words layer).
    Since the audit 2026-11-04: (a) **hierarchy** — a summed field never adds a line to its own
    component: a CVM code descendant (ZAMP 1.01.02 + 1.01.02.01.03, WEG 1.01.01 + 1.01.01.0x, Unipar /
    Eztec / Copasa 2.01.04 + 2.01.04.01), or, without codes, a line inside a contiguous block (≥ 2
    lines, same page, right below or above) that sums to the other line in every period (Uniar
    DISPONÍVEL = CAIXA + BANCOS + APLICAÇÕES, Benoit INTANGÍVEL net = gross + amortisation) —
    `_drop_components`, summed fields only (the cumulative result lines of a DRE are not components);
    the parent left alone is `total_of_components`; (b) **income tax is valued from the DRE only**
    (`VALUE_STATEMENTS`): the DFC tax line is an add-back with the opposite sign (São Salvador +147.177
    for a (147.177) expense); (c) two identity alternatives with the SAME figures are ONE answer (no more
    «ambiguous» on a duplicated cost line); (d) any field of an identity that FAILS in a period gets
    `needs_review` + `review_reason`; the Excel «Verified by» column is per period; (e) EBIT printed only
    AFTER the financial result → the field exists but is EMPTY with review (`_ebit_not_printed`, #29);
    (f) `_current_only` trusts the Stage-3 section and falls back to the position test only for rows
    WITHOUT a section (the old fallback dropped the current rows of a header-led ITR page: Unipar UNP1).
    Since the Cibra audit 2026-11-05: (g) **held for sale** (`HELD_FOR_SALE_FIELD`, `_is_held_for_sale`,
    user decision 2026-11-05 — IFRS 5 / IAS 1.66: held-for-sale assets are CURRENT): an «Ativo(s) (não
    circulante(s)) mantido(s) / disponível(is) para venda» line (not the IAS 39 «ativos financeiros
    disponíveis para venda») printed OUTSIDE the current and non-current subtotals joins
    `totalCurrentAssetsAmount` — printed current total + that line, method `held_for_sale+identity` — ONLY
    where the balance identity proves it stands outside (total assets = current + the line + non-current,
    the printed total alone failing). Liabilities the same: «Passivo(s) … associados a ativos mantidos
    para venda» outside the current-liabilities total → `financialLiabilitiesShortTerm` (proved by assets
    = ST + LT + equity). A line printed INSIDE the current section leaves the identity holding with the
    printed total: nothing changes, nothing is counted twice. Cibra p7: «Ativo mantido para venda» 30.000
    (2023) between «Total do ativo circulante» and «ATIVO NÃO CIRCULANTE» → current assets 2023 =
    2.232.467 + 30.000 = 2.262.467, the identity holds, the REVIEW is gone; (h) **`provisions`** (user
    decision 2026-11-05, #32) is an «other» field: current + non-current summed, the hierarchy (a) keeps a
    CVM «Provisões» group (2.01.06 / 2.02.04, code map) and drops its alias-labelled children (Eztec
    2.02.04 8.849 + 2.02.04.01 + 2.02.04.01.05 → counted once, the children «(not counted)» in the Excel).

21. **Liability field mapping — PINNED by the user 2026-11-04** (do not revert to the xlsx Description,
    which reads the other way): `shortTermLiabilitiesAmount` / `longTermLiabilitiesAmount` = short- /
    long-term CREDITS AND LOANS (empréstimos e financiamentos, debêntures, TITULOS EMITIDOS, mútuos /
    empréstimos de sócios); `financialLiabilitiesShortTerm` / `financialLiabilitiesLongTerm` = the TOTAL
    current / non-current liabilities («Total do passivo circulante / não circulante», CVM 2.01 / 2.02).
    Implemented as a pure rename at the source (`build_sbs_mapping.py` JOIN rows swap their old primaries
    but keep the alias-set order, `code_map` 2.01 / 2.01.04 / 2.02 / 2.02.01, twin group `debt` = the
    loans pair, `_SECTION_TOTAL_FIELD`, `_stage_balance_liability_totals`, `sbs_fields` identities):
    verified byte-equivalent on 8 goldens + 22 corpus PDFs + 12 audit PDFs before any other change. The
    identities read `assets = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm + equity`,
    `totalLiabilities = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm`. A loan caption
    is a twin (current / non-current by qualifier → code → section): add new loan captions to BOTH the
    `shortTermLiabilitiesAmount` aliases and the `debt` shared list.

22. **ITR: only the CVM FORM pages are exported** (`selection.form_pages`, audit P1). Every line of a
    CVM form carries its account code; note / commentary pages carry none (WEG 160/160 primary rows
    coded vs 0/252 note rows; Unipar 172 vs 0/130). ITR ignores page_class and the notes' own
    «Consolidado» headers passed the Consolidado filter → 61 % / 43 % of the WEG / Unipar workbooks were
    notes and broke fixedAssets, COGS, depreciation, trade creditors. The restriction is by PAGE (a
    code-less line of a form page stays), the derived total-liabilities row always passes, and
    `document_has_consolidado` uses the same pages.

23. **Debit / credit suffixes** (`dc_signed_value`, Stage-1 step 5, audit P2): accounting-system dumps
    print «1.120.836.736,37D» / «102.178.388,07 C»; `parse_amount` read none of them (Uniar's whole
    balance and 3 Benoit contra-accounts vanished). Signed by the side's normal balance — assets D = +,
    C = −; liabilities / equity / income C = +, D = −; cash flow untouched. `value_raw` keeps the token,
    report `columns.dc_suffix_signed`; `is_wellformed_figure` accepts the suffix (step 4d).

24. **Stage-2 column model per PANEL, odd repeats, canonical block end** (audit P2): (a) a side-by-side
    page whose panels print > 4 columns IN TOTAL is judged per panel (`extract_page_features(panel_of)`,
    panels from `structure_ocr.build_panels` passed in by `process`) — Delfia p5 (4 + 2 bands) was a
    "segment note" and the whole balance disappeared; pages within 4 columns keep their page-level
    count (byte-identical verdicts); (b) a later repeat of a family with a DIFFERENT, ODD column count is
    a reconciliation note («Originalmente apresentado | Ajustes | Reapresentado», Dom p14 = PRIMARY:INCOME
    before, 32 rows in the Excel), an even count stays an Individual/Consolidado copy; (c) the safety-net
    window starts after the block's last page IN CANONICAL ORDER (`_block_end`): a balance-like note
    printed after the DRE (Delfia p11) no longer pushes the window past the DFC; (d) DFC anchors gained
    the verbatim «Fluxo de caixa líquido gerado nas / proveniente das (utilizado nas) …» and «Caixa
    líquido utilizado / proveniente …» forms — several benign words push the fuzzy backstop under 85.

25. **Balance printed in THREE columns** (`resolve_extra_period_column`, Stage-1 step 2d, audit P3):
    a restated comparative prints «31/12/2024 | 31/12/2023 Reapresentado | 01/01/2023 Reapresentado»;
    the provider tags the middle column `AMOUNT_N` and the opening one `AMOUNT_N-1` → N-1 = 01/01/2023
    (Dom p6: total assets 918.483 instead of 1.174.378, 8 fields wrong). Proof by the ADJACENT balance
    side printed in two columns: the band whose largest figure (= the side's grand total) equals the
    other side's N total is N, exactly one other band equals its N-1 total → N-1, the remaining band →
    `AMOUNT_N-2` (kept with `period_src`, never exported, ignored by `pick_cells`). No proof → nothing
    changes. Note pages also show 3-band panels with repeated tags — the two-total proof keeps them out.

26. **Lost minus / dash read as «1» — repaired only when PROVEN** (`sign_proof.repair`, Stage-1 step 5b,
    after `build_output`, audit P7/P8). ITR: per form instance (consecutive pages of one bucket) and per
    cell (account type, period), the code tree (parent = Σ direct children; not 3 / 6 / 6.05 / 3.99.*)
    and the CVM chains (3.03 = 3.01 + 3.02, 3.05, 3.07, 3.09, 3.11, 6.05 = 6.01 + … + 6.04, 6.05.02 =
    6.05.01 + 6.05): a failing equation is repaired by the UNIQUE single positive figure (else the unique
    pair of positive children: ZAMP 6.01.02.14 + .17) whose sign flip makes it hold without breaking an
    equation that holds — iterated; a still-failing equation flags its parent `needs_review` +
    `arith_review`. Without codes: the Stage-3-like subtotal stack of the BALANCE and CASH-FLOW buckets
    (sign flips never on the DRE — templates print costs unsigned by convention, S5 = «as printed»;
    since 2026-11-05 the DRE runs the DASH test only, `DASH_ONLY_BUCKETS`, audit Cibra K6: p8
    «Resultado de equivalência patrimonial» Consolidado 2024 printed «–», read «1» in both layers —
    627.476 − 54.935 − 88.252 − 10.784 − 12.363 = 461.142 = «LUCRO ANTES DO RESULTADO FINANCEIRO» exactly
    without it; on the corpus also Romagnole p8 «Acionistas controladores» / MBP p11 «Receitas
    financeiras», Controladora cells, not exported), only on the rows
    the export shows (PRIMARY page or ≤ 2-page statement), a flip only under a «Total… / Caixa líquido…
    / Fluxo de caixa líquido…» line (not «Aumento…» / «Saldo final…»: a document's own error — São
    Salvador's FX line — or a lost figure — Delfia p9 — made flips «fit»), never on a «(-)…» line or a
    total, one-line blocks closed first (São Salvador p20 1.063.668 ×3). A «1» whose removal makes the
    equation EXACT while it is off by exactly 1 with it → dash (`value` = «-»). Every repair keeps
    `value_raw` + `sign_corrected` / `dash_corrected` + `proof`. Verified on page images: ZAMP ×5,
    WEG ×4, Unipar ×2, Dom ×2, Eztec ×2, Copasa ×2, the Moreno p5 / Raízen p21 dashes. Related guards:
    RC#3 never fills a block the identity leaves at ZERO (a «1» dash took LT: São Salvador SSA7); in
    section arithmetic a line printed with a dash is a figure line (contributes 0), not a header.

27. **Duplicates and their index** (Stage-3 `_stage_dedup`, audit P5 / P15): amounts are compared as
    PARSED numbers ('-13.534.197' = '(13.534.197)': WEG's COGS was summed twice) — exact, not rounded
    (basic EPS 0,66015 ≠ diluted 0,65999); rows with DIFFERENT CVM codes are never duplicates (a coded
    parent and its only child are both form lines — the Excel now shows them, `sbs_fields` counts the
    parent once, #20a; this is why the selftest ITR balance has 76 rows); `sbs_duplicate_of` is the
    index in the bucket's FULL list (it pointed into the filtered view, i.e. at a DVA row).

28. **Balance sections** (`_compute_sections`, audit P6): (a) the CVM code IS the section (x.01 current,
    x.02 non-current, 2.03 equity) — authoritative, markers / arithmetic fill the rest; (b) a «balancete»
    (accounting-system dump whose every balance page STARTS with the side's grand total —
    `_is_total_first`) is not the SERASA shape: totals sit above their lines, so the header markers lead
    (Benoit: every section was empty, a non-current FORNECEDORES joined trade creditors); (c) arithmetic
    gap-fill: a section total printed after a block subtotal of its own section closes «that block + the
    lines since» (Serasa GRANJA FARIA: TOT INVESTIMENTOS after REALIZAVEL L PRAZO → non-current →
    `financialAssetsAmount`).

29. **Positional income / cash-flow rules** (Stage 3, audit P9 / P10 / P11): (a) tax lines — between the
    pre-tax and the post-tax line, EVERY line with figures carrying a tax word (imposto, IR, CS, IRPJ,
    CSLL, contribuição, corrente(s), diferido(s); not «antes») is an income-tax line, an unnamed line =
    their sum is the printed total (`_stage_tax_components`: DIMENSIONAL «Despesa de IR… correntes /
    diferidos», Benoit «(-) DESPESA COM IMPOSTOS» above «PROVISÃO P/ IR / CS», São Salvador «Correntes /
    Diferidos», Cibra p8 «Corrente» (1.200) / «Diferido» 1.681 → tax 481 / 117.541 once the pre-tax line is
    merged, #15 type C); (b) EBIT = the operating result BEFORE the financial result (user 2026-11-04): an
    operating-profit label printed after a financial-result line on the same page is withdrawn
    (`_stage_ebit_before_financial`, `after_financial_result`: Uniar, DUTRA «LUCRO OPERACIONAL», Serasa
    «RESULT OPERACIONAL»); (c) CAPEX — a bare «Imobilizado» / «Intangível» OUTFLOW in the investing
    section (code 6.02.* or between the CFO and CFI totals) is the acquisition of that class
    (`_stage_capex_asset_lines`, WEG 6.02.02 / .03); (d) user decision 2026-11-04: bio-asset amortisation
    counts in depreciation, bio-asset purchases in CAPEX (aliases).

30. **Presentation** (Stage 4, audit P13 / X1): figures with decimals keep them (`#,##0.00##`: EPS,
    centavos); per-share lines (`selection.is_per_share`: code 3.99.*, EPS captions, «ON / PN» with
    decimals) are tagged «(per share)» in the Label column; the AMOUNT_N-1 / AMOUNT_N headers carry the
    period dates the metadata proves (`export_excel.period_labels`: «31/12/2023» on the balance, «12m to
    31/12/2023» on the flows when the months are known). Streamlit shows the decimals but not yet the
    marker / dates. Since 2026-11-05 (audit Cibra m4): a row flagged `needs_review` is filled light red on
    the balance / income / cash_flow sheets and its reason is written in words in a 7th column «Review»
    (`selection.review_reason`: a failing equation `arith_review`, figures of two panels `xband_mixed`,
    «two figures for one column (…): the first is shown», «SBS field chosen between … / not resolved»,
    a withdrawn label, `sbs_review_reason`), with a legend note under the sheet; the Streamlit tables
    show the same (light red row, «Review» column, REVIEW badge, «Needs review» KPI). Before, a flagged
    line looked like any other one (Cibra 6051 before #31: two Consolidado N-1 cells in one cash-flow
    line, the first shown silently).

31. **Header zones: period and perspective from the table header** (Stage-1 step 2c'
    `apply_header_zones`, right after step 2c; user decision 2026-11-05; simulation and evidence:
    `_audit/ZONES_SIMULATION_2026-11-05.md`). reciTAL returns the header frames: `CLOSING_DATE`
    («31/12/2024» over each N column) and `ACCOUNT_TYPE` («Controladora» / «Consolidado» over each N | N-1
    pair). On a page panel whose header is UNAMBIGUOUS, every figure takes the period and perspective of
    the zone (column) it stands in, whatever the provider tag; anywhere else step 2c + the pair rule stand.
    Unambiguous means:
    - perspective headers on ONE line, above the first table row;
    - the table columns split in order into consecutive pairs, 2 per header, each header over its own
      pair. Table columns are right-edge clusters of ANY count that hold figures — not note references,
      not «(i)/(ii)» markers (1Q26 p68: such a column next to a lost one shifted every pair);
    - in a pair exactly one column stands under a frame of the closing date (`CLOSING_DATE.normalized`)
      → N, the other → N-1; no unclear or later date over the pair, no earlier date over N;
    - N is LEFT of N-1 in every pair (127/127 zoned pairs of 36 documents);
    - a date over no column (statement title, signature line — Benoit) is ignored.

    Why: on Cibra 12.2024 (audit 2026-11-05, K1/K2/K4) agent 5485 paired ONLY_AMOUNT figures across the
    empty Consolidado 2024 column. Step 2c then repainted the correct Contr. 2023 column N, and the pair
    rule put Contr. 2023 into Conso 2024: TOTAL DO PASSIVO E PL 2024 = 2.433.930 instead of 4.392.444. Agent
    6051's singles tagged Conso 2024 as N-1 stayed uncorrected.

    Effect:
    - Cibra silent Excel errors 4 / 16 / 8 / 8 → 1 / 1 / 1 / 1 (A / B / C / D; the left one is the K6 dash);
      all provider period errors corrected, 0 own errors;
    - the other 34 local documents: 0 changes in Excel, `sbs_fields`, golden cells, identities and
      conservation; 1 JSON cell changed (Raízen p82), and it is correct.

    Coverage: DFP pages with Controladora / Consolidado over the columns (31 of 32 such Excel pages).
    TWO_COL / SERASA / UPPERCASE have no type header over the columns; ITR prints «Consolidadas» in the
    form TITLE — no zones there.

    Provenance:
    - `period_corrected` + `period_src` against the provider tag (a 2c correction the zone undoes is
      dropped);
    - `account_type_corrected` + `account_type_src` against the pair rule without zones;
    - debug `_match: "zone"`;
    - report `only_amount.zone_panels` / `zone_period_set`, `account_type.zone` / `zone_type_changed`.

    The optional words step gets the zone columns in its grid: a column the header prints is a column
    even with 1–2 extracted figures (Cibra p7 Conso 2024 → 188/188 Consolidado cells with words).
    ⚠️ Zones never depend on words. ⚠️ Rejected in the simulation: S2 «every LST figure votes + 6051
    singles vote» — golden −3 (Raízen p26), 69 wrong vs 43 right changes, K2 back on 6051.

32. **SBS semantic decisions of the user 2026-11-05** (Cibra audit, `_audit/AUDIT_REPORT_2026-11-05.md`
    §5) — do not «fix» them back:
    - **`provisions` = IAS 37 liability provisions** («Provisions and reserves for liabilities and
      charges», Indian SHORT_TERM_PROVISIONS + LONG_TERM_PROVISIONS): contingencies / lawsuits (tax,
      civil, labour), decommissioning / environmental restoration, warranties, onerous contracts —
      circulante + não circulante, SUMMED (#20h). Aliases in `EXTRA_ALIASES`, scope
      `BALANCE_LIABILITIES_LST` (`EXTRA_SCOPE`) and `STRICT_SCOPE`: never kept outside the balance
      liabilities — the cash-flow «Provisões para riscos…» is the period's adjustment (Cibra p11 887 /
      1.767), never the balance provision (65.986 / 65.099), even when the balance line went unlabelled.
      CVM `code_map` 2.01.06 / 2.02.04 «Provisões» → `provisions` (WEG prints only 2.02.04). NOT
      provisions (no alias, keep it so): «Salários, provisões e encargos sociais», «Provisão para férias /
      13º» (payroll accruals), «Provisão para IR e CS» (taxes payable), «Provisão para perdas esperadas /
      PCLD» (asset allowances), «Provisão para perdas em investimentos», and captions that do not say
      what they provide for — bare «Provisões» (Alvorada: a payroll-like current line), «Provisões
      diversas», «Outras provisões», «Provisões a pagar». The old anchor PROVISION (PECLD) is a different
      concept and is not reused. Corpus effect: `provisions` filled on 13 of the 22 PDFs, nothing else
      changed; goldens (N-1 / N) Raízen 1.918.835 / 1.533.431, RECONTEC 14.526 / 24.210, Eztec 30.225 /
      31.884.
    - **`depreciationAmount` excludes** «Amortização de direitos de uso» (right of use), «Amortização de
      mais valia de ativos identificados em combinação de negócios» (PPA) and «Amortização dos custos de
      captação» (part of interest). No alias catches them (checked on the corpus); bio-asset amortisation
      still counts (#29d).
    - **Related-party balances are not loans** (reconfirmed): «Partes relacionadas» in the liabilities
      stays out of `short/longTermLiabilitiesAmount`, even when the cash flow shows interest on it.
    - **`interestPaidAmount` = the DRE «Despesas financeiras»** (accepted on the auditor's recommendation:
      a P&L field, «Interest expenses on borrowed funds», the Indian «finance costs»; the DFC «Juros pagos»
      is a cash figure and not always printed). A separately printed «Variação cambial» is not added.
    - **Held-for-sale assets are current** — implemented in `sbs_fields` (#20g).

## Stage-2 layers (in `classify_statements.py`)

1. **Negative filter** — no amounts / too sparse → JUNK.
2. **Detector** — anchor-set **completeness** per family (the workhorse).
3. **Disambiguator** — primary vs. segment copy: column count (2/4 = primary, more = segment note;
   counted PER PANEL when a side-by-side page exceeds 4 in total — #24), page order, repeated set
   (a repeat with a different column count stays primary only if that count is EVEN — whole N/N-1
   pairs; an odd count is a reconciliation note). Balance-continuation special case (see #7).
3b. **Positional safety-net** — if a solid primary block exists (≥2 families **or a primary BALANCE**,
    the canonical block start) and a nearby page has **partial** anchors of the missing family (≥1) +
    primary-like columns, promote it as a FLAGGED primary (conf 0.55, `needs_review`). The search
    window follows the canonical order (advances past each promoted family) and starts after the
    block's last page IN CANONICAL ORDER (`_block_end`: an out-of-sequence balance-like note after the
    DRE does not move it — #24). Conservative: never
    promotes on position/columns alone; silent when anchors are intact. ⚠️ The balance-only gate
    (2026-11) restored the DRE + DFC of Nissan / 1Q26 / Grupo Moreno, which the Excel had dropped.
3c. **Multi-page continuation** — the page after a primary INCOME/CASH_FLOW page whose closing anchor
    (net income / financing total) is NOT on it, mostly the same family's rows, same columns, no other
    family's anchor → flagged primary. Keeps the 2nd page of a 2-page DRE/DFC in the Excel.
4. **Validator** — accounting identity (balance: ativo == passivo+PL) raises/flags confidence.

## Stage-3 SBS labelling (in `sbs_labeler.py`)

A pipeline of small stages (two passes). Adds `sbs_label` (+ `sbs_scope`, `sbs_match_method`,
`sbs_match_source`, `sbs_match_score`, `sbs_blocked_reason`, `sbs_duplicate_of`) to each row.
PASS 1 per bucket: sections (code → markers → arithmetic) → (0a) **code-match** → (0) match → (1) twin/section →
(2) empty-label → (S3) tax lines → `sbs_section` kept.
PASS 2 document-level: (3) scope-gate → capex asset lines → EBIT-before-financial → (4) struct-validate →
(SERASA) net-income → (5c/5d) liability totals → (5) dedup → `sbs_duplicate_of` remapped to the full list.
Step-by-step table: `docs/Pipeline_Overview.md` §3.5. ⚠️ Liability names follow the user mapping (#21):
"ST / LT" below = the section TOTALS `financialLiabilitiesShortTerm` / `financialLiabilitiesLongTerm`.

0a. **Code-first (CVM/B3 codes — ITR/DFP).** A row whose `code` is in the standardized **level-1..3** map
   (`sbs_label_mapping.json → code_map`, built by `build_sbs_mapping.py`) is labelled directly from the code
   (`sbs_match_source=code`, score 100) and the text stages skip it. An authoritative, OCR-robust signal that
   does not depend on the line text. ⚠️ Only levels 1..3 are mapped — deeper codes (`6.01.02.01`…) are
   **positional**, not semantic (same code = different concept on different pages), so they fall back to text.
   ⚠️ Code `2` ("Passivo Total") is **DELIBERATELY UNMAPPED**: it is the balance **grand total** (liabilities +
   equity == total assets == code `1`), NOT total liabilities (`totalLiabilitiesAmount` = liabilities **only**,
   see below). Mapping it double-counts equity. `totalLiabilitiesAmount` is resolved arithmetically in PASS 2
   instead (see point 5c). User mapping (#21): `2.01` → `financialLiabilitiesShortTerm`, `2.02` →
   `financialLiabilitiesLongTerm` (section totals), `2.01.04` / `2.02.01` → `shortTermLiabilitiesAmount` /
   `longTermLiabilitiesAmount` (credits and loans). Since 2026-11-05 (#32): `2.01.06` / `2.02.04` «Provisões» →
   `provisions` (their alias-labelled children are not counted twice: `sbs_fields` hierarchy, #20a). Measured
   (before the provisions codes): on the ITR golden the code map agrees with the text engine on all 39 rows
   (0 conflicts) — its value is robustness + auditability, not a different answer. Gated on `code` being present,
   so the 7 code-less shapes are untouched.

1. **Engine = DICT-FIRST, then `anchor_match` fallback.** Stage 0 first does an **exact** lookup in the
   alias index built on **`sbs_normalize`** (= shared `normalize` + drop dual-sign `prejuizo`/`prejuizos`,
   which never distinguish a field but BLOCK coverage when left in an alias). A clean dict hit wins
   (`sbs_match_source=dict`, score 100); a dict **conflict** (shared twin caption) defers to Stage 1.
   Measured: exact dict reproduces ~86% of all labels + recovers 3 TPs the fuzzy-only engine missed.
   ⚠️ On a dict MISS, fall back to `anchor_match` **unchanged** with `token_fuzzy=SBS_TOKEN_FUZZY` (88)
   — token-level fuzzy/stem coverage (`credito`≈`creditos`, OCR drift); strict default (`token_fuzzy=0`,
   Stage-2 + `diagnose_pages`) compares token *sets* exactly. **Stage-2 is byte-identical** (never passes
   the flag). ⚠️ A PT stemmer (RSLP) and IDF weights were MEASURED and add nothing on this corpus — the
   fuzzy engine is kept as-is; do NOT reintroduce them. `sbs_normalize` is SBS-only — do NOT change the
   shared `normalize` (Stage-2 depends on it).
2. **Scope-gate = FILTER, not tie-breaker (Stage 3, точка 1).** Each field declares `allowed_statements`
   in the mapping. An out-of-statement match is BLOCKED (`sbs_label→null`, `sbs_blocked_reason=out_of_scope`)
   IF the same field is captured in-home elsewhere in the doc (no loss — e.g. `Contas a receber`/`Fornecedores`
   as working-capital movements in cash-flow), else RECOVERED (kept + `cross_scope` + `needs_review`, e.g. a
   real `Resultado do exercício` on an ITR balance with no income page). ⚠️ This REPLACES the old "scope is a
   tie-breaker, never a filter" rule. **Double-home whitelist** (`allowed_statements` len 2): `depreciationAmount`,
   `dividendAmount`, `posttaxProfitAmount`, `incomeTaxExpenseRefundAmount` — these legitimately live in two
   statements, never blocked. ⚠️ **Strict scope** (`STRICT_SCOPE` in `build_sbs_mapping.py` → `strict_scope`
   in the mapping, since 2026-11-05: `provisions`): such a field is ALWAYS blocked outside its statements, no
   `cross_scope` recovery — the cash flow repeats the balance caption as a flow, never the balance figure
   (#32).
3. **`sbs_scope` flag.** `in_scope` = field's home statement == row's bucket; `cross_scope` = a recovered
   out-of-scope match (kept because it had no in-home copy).
4b. **Struct-validate (Stage 4, точка 5) — FLAG-ONLY, never strips a label.** Income TIER order
   (`canonical_ordinal` from xlsx grouped into `income_tier`; siblings share a tier so their order never
   trips — a *later* tier above an earlier one, e.g. net income above turnover, flags) + balance asset/liability
   side check (`home_section`). ⚠️ Order anomalies are usually OCR row-shuffle, so flag (`needs_review`), never
   block. Raw `canonical_ordinal` is a schema enum that orders siblings arbitrarily → use TIERS, not the bare #.
4c. **Dedup (Stage 5, точка 6) — TWO complementary keys, both within ONE bucket, both on the full
   `(account_type, period, value)` amount signature.** Tag `sbs_duplicate_of`; export (`row_passes`) drops it.
   ⚠️ `account_type` MUST be in the amount key — Individual(Controladora) vs Consolidado rows share name+value
   but are distinct perspectives, never merge. ⚠️ Differing amounts ⇒ never merge (an ITR `Receitas Financeiras`
   pair with same AMOUNT_N but OCR-drifted AMOUNT_N-1 `83.715`/`83.716` stays two rows — correct, "never guess").
   **(a) NAME** — same `page` + `normalize(label)` + amounts (catches an OCR re-print, incl. one differing only by
   a stripped `(-)`). **(b) SBS** — same `sbs_label` + amounts when the two rows are on the SAME page OR share the
   same **page kind** (`PRIMARY`/`NOTE`/`JUNK`, via `_page_kind`; `NOTE_segment`→`NOTE`). (b) catches the same
   figure RE-WORDED under a different caption that (a) misses — the real bug on `complicated_sample_2`: `(-) CUSTOS
   DE REVENDA DE MERCADORIAS` vs `CUSTOS DE MERCADORIAS REVENDIDAS`, both `rawMaterialsAndConsumablesAmount`, same
   −170.584.068,84 (also its cross-page `RESULTADO DO EXERCICIO`/`LUCRO LÍQUIDO` posttax pair p4/p5).
   ⚠️ Since the audit 2026-11-04 (#27): amounts are compared as PARSED numbers, exactly; rows with DIFFERENT
   CVM codes are NEVER duplicates (ITR `2.01.02 Fornecedores` / `2.01.02.01 Fornecedores Nacionais` are both
   printed form lines — `sbs_fields` counts the parent once by hierarchy); `sbs_duplicate_of` = full-list index. ⚠️ The **page-OR-kind** scope is the precise
   discriminator: it extends dedup across the pages of ONE statement copy (same kind) yet NEVER merges a NOTE copy
   onto a PRIMARY line (different kind) — preserving the primary-vs-segment-copy separation the page_class filter
   relies on (DFP/sample2 segment copies are left untagged; they're dropped by the page filter, not by dedup).
   (b) skips rows already removed by (a) so no duplicate-of chains. `duplicate_tagged_sbs` counts (b)'s tags.
   ⚠️ **SURVIVOR CHOICE — keep the best-PLACED copy, not the first.** Within each duplicate cluster the row KEPT
   (and shown in Excel) is the one with the fewest **sequence inversions** against the statement's canonical order,
   not blindly the earliest index. The order comes from the income field's `income_tier` (turnover=1 → … →
   posttaxProfit=9) via `_income_tier_positions`/`_seq_inversions`; a row in its canonical place scores 0, an
   OCR-displaced copy of the same field scores many. **Ties — and every non-income bucket (no tier) — fall back to
   the earliest index**, so balance/cash-flow and same-tier clusters keep prior behaviour. Real fix on
   `complicated_sample_2`: net income printed BOTH at the top (`RESULTADO DO EXERCICIO`, idx1, 13 inversions) and in
   its correct place at the bottom (`(=) LUCRO LÍQUIDO DO EXERCICIO`, idx46, 0 inversions) — the **bottom** copy now
   survives (was the top). Survivor selection is shared by both passes (`survivor()` inside `_stage_dedup`), so (a)
   and (b) agree on the removal target. Isolated diff on 8 goldens: exactly idx1↔idx46 flip; other 7 byte-identical.

4d. **Pre-tax guard (`_is_pretax_label` in `_match_all`).** A "before-tax" caption
   (`...antes do imposto ... e contribuição social`) is token-set-IDENTICAL to the income-tax alias — only
   the whitelisted word `antes` differs, so a bag-of-tokens match silently flipped the EBT line into
   `incomeTaxExpenseRefundAmount` (real bug on the DFP cash-flow EBT). The guard drops the tax field from
   the candidate set whenever the label carries a pre-tax marker. Pre-tax recall aliases (`...e contribuição
   social` forms) were added to `pretaxProfitAmount` so these lines label correctly. ⚠️ Do NOT remove
   `antes`/`prejuizo` from `benign_insertions` to "fix" this — they're needed elsewhere; the guard is the fix.

4e. **SERASA double net income (`_stage_serasa_net_income`, doc-type gated).** The Serasa template prints
   two net-income lines: `RESULT EXERCICIO` (profit for the period) then `DIVIDENDOS` then `RESULTADO LIQUIDO`
   (= period profit − dividends, retained). Both fuzzy-match `posttaxProfitAmount`. Per agreed semantics
   post-tax profit = **before dividends**, so the SECOND posttax line (the one AFTER a `dividendAmount` row)
   is cleared (`sbs_label→null`, `sbs_blocked_reason=net_after_dividends`, `needs_review`). Gated to
   `document_type=="SERASA"` AND the posttax-after-dividend ordering — never touches a single-net-income doc.

5c. **`totalLiabilitiesAmount` = ARITHMETIC, not aliases (`_stage_balance_liability_totals`, PASS 2).**
   (Names: "ST" / "LT" in this point = `financialLiabilitiesShortTerm` / `financialLiabilitiesLongTerm`, the
   section totals of the user mapping #21; the loans fields play no part here.)
   ⚠️ Per the SBS xlsx (`Description` col, field #23) `totalLiabilitiesAmount` = *"Total liabilities
   (short-term + long-term + provisions)"* — **liabilities ONLY, EXCLUDING equity**. So the balance
   identity is `totalAssets == totalLiabilities + shareholdersFunds`. The Portuguese balance bottom lines
   (`Total do passivo e patrimônio líquido`, `Passivo Total`) are the **GRAND TOTAL** (liabilities + equity
   == total assets) — a DIFFERENT concept. **Text-aliasing them mislabels the grand total as total
   liabilities** (the prior bug). So the field carries **NO aliases** and code `2` is unmapped; it is
   resolved arithmetically per **balance cell** = one `(account_type, period)` pair (each is its own
   self-balancing statement, so ITR Individuais/Consolidado & DFP's two perspectives never mix):
   - **RC#1 — relabel-or-derive.** A printed `Total do passivo`-type line (caption in `_TOTAL_LIAB_CAPS`)
     gets `totalLiabilitiesAmount` IFF its value == `ST + LT` (the true liabilities total), never the grand
     total. If no such line is printed (the common case — 7/8 goldens print only the grand total), a single
     **derived** row (`sbs_match_method=derived`, `sbs_derived=true`, label "Total liabilities (derived = …)")
     carrying `ST + LT` per cell is synthesized and appended to the liabilities bucket. The exporter always
     shows derived rows (bypasses the page-class filter; `row_passes`).
   - **RC#2 — equity-in-long-term fix.** Some templates (notably SERASA) print a `PASSIVO NÃO CIRCULANTE`
     subtotal that **already includes equity** (`EXIGÍVEL A LONGO PRAZO + PATRIMÔNIO LÍQUIDO`); the section
     logic then labels that inflated figure `financialLiabilitiesLongTerm` and leaves the real LT line
     (`EXIGÍVEL L PRAZO`) unlabelled — **double-counting equity, breaking the identity by exactly EQ** (the
     user's symptom: `ST+LT+EQ ≠ Assets`). When the labelled LT == `Assets − ST` AND `EQ ≠ 0` AND
     LT ≠ `Assets − ST − EQ`, the LT label is on the equity-inclusive line: demote it (`sbs_blocked_reason=
     includes_equity`, `needs_review`) and promote the unlabelled line whose value == `Assets − ST − EQ`
     (`sbs_match_method=balance_identity`). ⚠️ The trigger is **mathematically exclusive** — a correct
     balance has `LT == Assets−ST−EQ`, so `LT == Assets−ST` can hold only when `EQ==0`; requiring `EQ≠0`
     means it can NEVER misfire on a balanced statement (verified: 0 false fires on all 8 goldens).
   Runs after scope-gate (ST/LT/EQ stable) and before dedup. Self-gating no-op on already-consistent
   balances. Effect: balance identity `ST+LT+EQ==Assets` and `totalLiab==ST+LT` now hold on **all 8 goldens,
   every cell, both periods** (19/19). 3 SERASA files get LT promoted/demoted; DFP relabels its real
   `Total do passivo`; the other 7 synthesize a derived total. ⚠️ `label_statements` strips prior
   `sbs_derived` rows on entry → re-labelling is idempotent (no accumulation).

The two structural-resolution stages of PASS 1 (unchanged logic, now isolated as named functions):

S1. **Current vs non-current TWINS (Stage 1)** — shared captions split across a current/non-current pair of SBS
   fields. Declared as DATA in `sbs_label_mapping.json → ambiguity_groups` (built by
   `build_sbs_mapping.py`); the labeller is generic, so new pairs need no code change. Two today:
   **debt** (credits and loans, user mapping #21: `shortTermLiabilitiesAmount` / `longTermLiabilitiesAmount`;
   captions `emprestimos e financiamentos`, `debentures`, `titulos emitidos`, `bancos com emprestimos`,
   `emprestimos e outras obrigacoes`, `emprestimos socios (clientes)`, `mutuos socios`) and
   **securities** (`cashAndSecuritiesAmount` current vs **`financialAssetsAmount`** non-current,
   captions `titulos e valores mobiliarios` / `aplicacoes financeiras` / `investimentos`). NOT a tie —
   the shared caption lives only under one twin, so a naïve match silently mislabels the other (really
   happened: 3 ITR debt rows, plus a non-current `Títulos` collapsing into cash). A row matching a
   twin via a **shared** caption (a caption unique to one twin, e.g. pure `caixa`, is taken directly)
   is resolved in order: (a) **explicit qualifier** in its own label (`circulante`/`curto prazo` vs
   `nao circulante`/`longo prazo`), else (b) **CODE_VALUE** prefix (`x.01.*`=current, `x.02.*`=non-
   current; assets `1.01`/`1.02`, liabilities `2.01`/`2.02`), else (c) **positional section**
   (`_compute_sections`, page-local, THREE passes: forward headers lead downward, backward subtotals
   close upward — both via the per-scope marker vocabulary — THEN an **arithmetic gap-fill**: a row
   whose AMOUNT_N equals the running sum of its preceding contiguous detail rows is a subtotal closing
   a block, and blocks are ordered current→non-current with an equity block recognised by content. The
   arithmetic pass NEVER overrides a marker-derived section — it only fills `None` gaps — so it's the
   safety net for **abbreviated / OCR-mangled boundary wording the markers can't read**, e.g. provider/
   SERASA's `PASSIVO CIRC` / `EXIGIVEL L PRAZO`, where two identically-named `EMPREST E FINANCIAMENTOS`
   rows correctly split into ST (current block) and LT (non-current block)), else
   (d) `sbs_label=null` + `sbs_candidates=[both]` + `needs_review`. **Never guesses.** A twin caption
   outside its home scope (e.g. in cash-flow) is a flow → tagged `cross_scope`, never twin-resolved.

   ⚠️ **SERASA-shape balances: sections go BUCKET-WIDE, arithmetic-AUTHORITATIVE.** On a balance whose
   `document_type ∈ {SERASA, UPPERCASE}` AND has no `code` AND no lexical `total do …` subtotal
   (`_is_serasa_shape`), `_compute_sections` runs ONE bucket-wide `_fill_sections_by_arithmetic` over all
   rows in document order instead of the page-local marker passes. Two reasons the page-local passes fail
   on this shape: (1) the logical balance is **split across pages** (serasa2 passivos on p3/p4/p5), so a
   block's detail rows and its closing subtotal land on different pages and page-local arithmetic can't
   sum them; (2) a bare `ativo circulante` line here is a **closing subtotal carrying the block sum**, not
   an opening header, so the forward pass would stamp `current` on the non-current rows below it (the
   `APLICACOES FINANCEIRAS`→cash bug). Summing the whole bucket sidesteps both. The gate is **combined**
   (doc-type + no-code + no-`total do`) as defence-in-depth: DFP/sample2 carry `total do …` and ITR carries
   codes, so neither can ever enter this branch — its many repeated balance copies are never summed across
   pages. When a twin then resolves `positional` on a `document_type=="SERASA"` bucket, the section is
   deterministic arithmetic (not a guess), so the ambiguity `needs_review` + `sbs_candidates` raised by the
   match stage are **retracted** (`_stage_twin_section(clear_review=True)`) — the ONE deliberate exception
   to "needs_review is never cleared", gated to SERASA so the lone UPPERCASE golden keeps its flags
   (it is single-page, so bucket-wide ≡ page-local there: 0 section diffs, 0 label changes). Effect:
   serasa2 both `EMPREST E FINANCIAMENTOS` → ST/LT (was unresolved), serasa1 + provider non-current
   `APLICACOES FINANCEIRAS` → `financialAssetsAmount` (was cash); DFP/ITR/sample2/complicated/complicated2
   byte-identical.

S2. **Empty-label subtotals (`label_null`, Stage 2).** Rows whose name the OCR dropped but with intact amounts
   are kept by Stage 1 (`label_missing:true`) and **shown in Excel** (a row qualifies on AMOUNTS, not
   label). Stage 3 recovers an SBS field for them — balance scopes only, conservative: gated to rows
   the report shows (small-span statement or PRIMARY page — skips multi-page NOTE-table coincidental
   sums); OCR-duplicate guard (value == neighbour → skip); then **arithmetic** (value == running sum of
   preceding section detail rows → that section's total). **Which section** (priority): (a) CONTENT —
   if feeding detail rows are **equity** items (`_EQUITY_KEYWORDS`: `capital social`/`reservas`/
   `lucros|prejuizos acumulados`/`patrimonio liquido`/`acoes em tesouraria`/**`resultado do exercicio`**/
   **`ajustes de exercicios anteriores`**/**`distribuicao de lucros`**/dividends) → `shareholdersFundsAmount`
   (⚠️ liabilities has THREE blocks current/non-current/**equity** — a binary rule mislabels equity as
   longTerm). ⚠️ The content test requires **ALL** feeding rows to be equity, so the keyword list must
   cover the period-result lines that sit inside equity (`RESULTADO DO EXERCICIO`/`AJUSTES…`/`DISTRIBUIÇÃO
   DE LUCROS`); one missing caption breaks `all()` → ordinal fallback wrongly stamps non-current (real bug
   on `complicated_sample_2`'s 49.766.376,21 equity total). The list is consulted ONLY in
   `BALANCE_LIABILITIES_LST` scope (income's `Resultado do exercício` is untouched);
   (b) positional marker; (c) **ordinal fallback** over NON-equity subtotals (1st=current→
   `financialLiabilitiesShortTerm`/`totalCurrentAssetsAmount`, rest=non-current→`financialLiabilitiesLongTerm`/
   `totalFixedAssetsAmount`). Else `needs_review`. Income subtotals (subtractions) not attempted yet.
   Methods: `empty_label_arith` / `empty_label_unresolved` / `empty_label_intermediate` /
   `empty_label_grand_total`. ⚠️ Arithmetic is per account perspective (consolidated if presented) and
   checked against the page's subtotal tree — see sensitive detail #18. An unnamed row repeating the
   named line just above is the subtotal of a ONE-LINE block when the tree's grand total sums it
   (DUTRA p1 «Permanente» 7.179: 198.132 = 167.392 + 23.561 + 7.179) — the OCR-duplicate guard yields.

S4. **PASS-2 positional rules (audit 2026-11-04, #29)** — `_stage_capex_asset_lines` (a bare «Imobilizado» /
   «Intangível» outflow in the investing section = CAPEX, `investing_asset_line`) and
   `_stage_ebit_before_financial` (an operating-profit label after the financial result on the same page is
   withdrawn, `sbs_blocked_reason=after_financial_result` + review — user rule: EBIT is BEFORE the financial
   result). Both run right after the scope-gate.

S3. **Income-tax components** (`_stage_tax_components`, widened 2026-11-04 — #29a: EVERY line with figures and a
   tax word between pre-tax and post-tax is a tax line, not only bare «Corrente(s)/Diferido(s)»): between the pre-tax line and net income, a bare
   «Corrente(s)» / «Diferido(s)» line is an income-tax component (Raízen p22, 1Q26 p23, ITR forms,
   RECONTEC p9 whose header line was lost) and an unnamed line = their sum is the printed tax total.

5d. **RC#3 — identity fill** (in `_stage_balance_liability_totals`): when exactly one of LT / equity is
   unlabelled for a balance cell, the unlabelled liabilities line whose value is exactly what
   `assets − ST − (the other)` leaves takes it (1Q26 p22 equity 2.106.440).

Regenerate the mapping after editing `labels_OLD.yaml` or the xlsx: `python3.13 build_sbs_mapping.py`.
`sbs_label_mapping.json` carries `fields` (per-SBS aliases + `statement_scope` + `needs_review` notes),
`alias_index`/`alias_conflicts` (reverse lookup), and `unmapped_old_anchors` (orphaned old aliases:
OPERATING_EXPENSES, FFO, WCN, PROVISION, FINANCIAL_RESULT, EXCEPTIONAL_RESULT).

## Document type (in `doc_type.py` — single source of truth)

⚠️ The doc TYPE drives everything downstream. It is computed **ONCE** in Stage 1 (`structure_ocr.process`
→ `detect_doc_type`), stamped on the output as `document_type`, and read by Stage 3 and the Excel exporter
(the exporter recomputes only as a legacy fallback for JSON predating the field). Detection order (first
match wins):

| Type | Detected by | page_class filter | columns kept |
|---|---|---|---|
| **SERASA** | provider tag `SERASA EXPERIAN` in `metadata.ACCOUNT_TYPE` | **ignored**; analysis rows (`serasa_analysis`) dropped | **all** |
| **ITR** | any row has a `code` (CODE_VALUE) | **ignored**; only the CVM **form pages** (pages carrying codes, #22) | **Consolidado if presented, else company** |
| **UPPERCASE** | ≥50% of labels are ALL-CAPS (generic provider dump) | **ignored** | **all** |
| **DFP_4COL** | a column carries `Consolidado`/`Controladora` | `PRIMARY:*` only | **Consolidado if presented, else company** |
| **TWO_COL** | none of the above (single-perspective, `account_type=null`) | `PRIMARY:*` only | **all** |

**SERASA precedes UPPERCASE** because a Serasa report is also all-caps (UPPERCASE would catch it), but the
provider tag is exact whereas "≥50% caps" is incidental. Filter behaviour is identical to UPPERCASE; the
distinct type lets Serasa-specific Stage-3 fixes target it (e.g. the double-net-income demotion). The
`SERASA EXPERIAN` tag sits in the `ACCOUNT_TYPE` group on a cover page — `extract_account_type` surfaces
ONLY a known provider tag into `metadata.ACCOUNT_TYPE`, never the per-column `Controladora`/`Consolidado`
headers (so DFP/ITR metadata stays clean).

**Business rule (user 2026-11-04): consolidated figures if the document presents them, else the company's
own** — decided ONCE per document (`export_excel.consolidado_filter`), never per row (a row whose
consolidated cell the OCR lost must not fall back to the company figure — a different number). A
company-only DFP/ITR used to give an EMPTY workbook. **SERASA = only Balance + DRE**: Stage 1 tags rows
outside the zone first balance row .. `GIR DA ATIVIDADE (EBITDA)` as `serasa_analysis` (kept in JSON,
skipped by SBS, never exported). **US-style documents** (`193,081`) are detected once in Stage 1
(`number_format: "US"`) and rewritten in Brazilian notation (`value_raw` keeps the original) — the
Documento1 workbook was 1000x too small. **Debit/credit suffixes** («…D» / «…C») are signed in Stage 1
by the balance side (#23). Row selection lives in ONE function, `selection.passing_rows` (re-exported by
`export_excel`; Excel sheets + Streamlit screen + harness + `sbs_fields`). Its order: duplicates / Serasa
analysis / non-figure / other-statement rows out → the derived total passes → ITR form pages only →
PRIMARY-only (DFP_4COL / TWO_COL, small-span bypass) → Consolidado-only (document-level) → a figure in N
or N-1.

**Key rule (don't break it):** "Consolidado only" applies ONLY where a Consolidado/Controladora split
*exists* (ITR, DFP_4COL). On 2-col / uppercase / SERASA docs there is nothing to filter → take both columns.

**Small-span override:** the `PRIMARY:*` filter is bypassed per statement group when that group's
labelled rows span ≤2 consecutive pages (`is_small_span`). A short self-contained statement is taken
in full regardless of `page_class` — rescues genuine primaries that Stage-2 mislabels `NOTE` on
wording (e.g. `complicated_empty_labels_sample.json`: balance says `TOTAL DO PASSIVO`, not `…e
patrimônio líquido` → anchor set incomplete → NOTE → balance would vanish). Conservative: multi-page
statements (annual DFP balance on pages 20/49/51/…) keep filtering, so segment copies aren't pulled in.
**Sample2 has NO Consolidado on its primary pages** (those headers sit only on a NOTE page, p30, over an
ONLY_AMOUNT reconciliation) — its primary statements are single 2-col tables. The Stage-1 pipeline is
correct leaving `account_type=null` there; do NOT "fix" it.

Other export facts: amounts → numbers via `anchors_lib.parse_amount` (+ OCR `[..]`→`(..)` normalization,
`%`/ratios skipped); columns mapped by **`period`**, not position (provider reverses N-1/N); metadata
tokens cleaned for display (`(Reais`→`Reais`, `C-31/12/2024`→`31/12/2024`). Sheet layout:
`Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N` — the two amount headers carry the period
dates the metadata proves (`period_labels`), figures with decimals keep them, per-share lines are tagged
«(per share)» (#30).

## Conventions for this project

- Always `python3.13`. `rapidfuzz` (3.14.5, installed via proxy) with automatic `difflib` fallback —
  console header shows the active backend.
- `pip` only as `python3.13 -m pip install <pkg> --proxy http://127.0.0.1:9000 --trusted-host pypi.org --trusted-host files.pythonhosted.org`.
- When testing a new document, write to a NEW output file — the user wants every example kept.
- Memory holds the deeper rationale: see `[[br-ocr-json-structure]]`,
  `[[br-primary-statement-detection]]` and the pinned liability mapping `[[br-sbs-liability-mapping]]`.
- Code edits that embed regexes: write the patch with raw strings (`r"..."`) — a shell heredoc + Python
  string once turned `\b` into a backspace and `\1` into `\x01` inside `structure_ocr.py` / `selection.py`.
- Audit fixes are measured with `_temp/fix/`-style scripts (pipeline CLI on `_audit/<id>/`, own comparator
  against `auditor_reference.txt`) — keep them in `_temp/`, never in the data folders.

## Known open items (not bugs)

- Anchor lexicon (`anchors.yaml`) tuned for annual DFP wording; **ITR (quarterly) uses different
  phrasings** (`Ativo Total` vs `Total do ativo`), so BALANCE/INCOME under-detect on ITR. Extend
  aliases as needed.
- OCR reads a dash as `1` in BOTH layers (values and words): since 2026-11-04 repaired WHEN PROVEN by the
  code tree / a printed subtotal (#26 — Moreno p5, Raízen p21, Entrepay p9, Delfia p5, São Salvador
  Controladora dashes), since 2026-11-05 on the income statement too, dash test only (Cibra p8, Romagnole
  p8, MBP p11). An unproven «1» stays a figure.
- **Agent 6051 outside the header zones — not measured yet.** On TWO_COL / ITR / SERASA / UPPERCASE pages
  the zones (#31) do not switch on, and step 2c gives no vote to the 6051 singles (`oa_single`), so a 6051
  period mis-tag there (Cibra p11 had 7 before the zones) would stay uncorrected. Before production on
  6051: re-extract the 22-PDF corpus + the 12 audit documents with the 6051 configuration and compare with
  5485 (harness + `auditor_reference.txt`).
- **Extraction gap reported to reciTAL (2026-11-05):** on a side-by-side balance with 8 figure columns
  (Cibra p7) the Consolidado 2024 column of the right panel is lost by both agents (5485: 16 of 17, 6051:
  15 of 17); only the words layer recovers it (the zone columns in the words grid, #31).
- **Panel boundary of short figures (latent, audit DEL10).** Figures are assigned to a panel by their
  centre x with a 0.01 margin before the next panel's names (`_panel_index`); the short numbers of the left
  panel's LAST column can fall into the right panel (Delfia p5 Consolidado 2022: 58 / 371 / 933 → wrong
  N-1 of «Provisões para férias» / «Passivo de arrendamento», an extra unnamed row). Fix idea: use the
  figure's right edge (x_max) against the next panel's name edge. Not done yet.
- Figures the extraction lost (Delfia p5/p9 Consolidado cells, p6 revenue N-1 — DEL6/DEL11) are only
  recoverable with the words layer. A non-figure token («C» for 0, Serasa S2) stays an empty cell.
- **Header-zone coverage gaps (#31)** — the previous behaviour (step 2c + pair rule) stands there:
  - MBP p14: a year frame centred over a wide column; Contr N read «2.024»;
  - Delfia p5 right panel: 5 columns because of DEL10;
  - Delfia p6: no date frames over Consolidado — only the Controladora pair is zoned;
  - note pages with two tables or a table continued from the previous page (`type_header_inside_table`).

  On 1Q26 p49 / p56 / p66 (notes, not exported) step 2c keeps 25 period errors. A per-table zone
  (nearest header above the row) would cover them — not built yet.
- The period length is not inferred from a single date («31 DE DEZEMBRO DE 2023», Delfia) and the
  currency is never inferred when not printed.
- Streamlit shows decimals and (since 2026-11-05) the «Review» column, but not the «(per share)» marker
  nor the dated column headers (Excel only).
- The 8 local goldens (+ their Excel files) were regenerated 2026-11-05 twice, each time after an
  independent review of the diff and the user's approval: after the audit fixes, and after the Cibra
  follow-ups (#15 type C, #17, #20g, #26 DRE dash, #30 «Review», #32) — only `provisions` changed, in 3 of
  the 8 JSON (Raízen, RECONTEC, Eztec), the other 5 byte-identical; all 8 Excel files re-exported («Review»
  column, dated headers). `harness/run.py goldens` = 8 in sync. Regenerate again only after showing the
  new diff to the user.
- Equity printed as «Reserva de lucros» (a header WITH its total) above its components double-counts in
  the running sum → the equity subtotal stays `empty_label_unresolved` (1Q26 p22). Not handled.
- ~7% of rows on dense segment-note pages carry `needs_review: true` (intrinsically ambiguous layout).
- **Stage-3 SBS labelling**: 33/43 SBS fields have aliases (none for the 5 metadata fields,
  `totalLiabilitiesAmount` — arithmetic by design —, `goodwillAmount`,
  `pensionFundsAndOtherStaffCommitmentsAmount`, `exploitProvisionAmount`, `netGroupProfitAmount`;
  `provisions` got its IAS 37 aliases + CVM codes 2026-11-05, #32; `ebitdaAmount` got Serasa's
  `GIR DA ATIVIDADE (EBITDA)` 2026-11-04;
  corpus-audit aliases for EBIT / COGS / tax / capex / CFO added the same day, independent-audit aliases
  (pre-tax «antes de IRPJ e CSLL» / «Lucro operacional antes do IR…», EBIT «antes das receitas e despesas
  financeiras» / «Resultado operacional antes do resultado financeiro», COGS «custo das merc. e serviços»,
  stock «estoques próprio», depreciation «… e Exaustão» / «… do imobilizado e intangível» / bio-assets,
  CAPEX «Compras de imobilizado e intangível» / bio-assets, dividends + JCP, CFI «proveniente das
  (utilizado nas)», loans, financial assets «TOT INVESTIMENTOS» / «…a valor justo através do resultado»,
  non-operating income) also the same day; add via `EXTRA_ALIASES` in `build_sbs_mapping.py` (+
  `EXTRA_SCOPE` if source-less, + the twin group's shared list for a current/non-current caption), or
  `labels_OLD.yaml`, then rebuild).
  ⚠️ `totalLiabilitiesAmount` is INTENTIONALLY alias-less — it is resolved **arithmetically** (current +
  non-current totals), not by text (see Stage-3 point 5c); do NOT add grand-total aliases back.
  `totalRemunWagesAmount` (despesas com pessoal) and `interestIncomeAndSimilarAmount` (receitas
  financeiras) were given aliases via `EXTRA_ALIASES`/`EXTRA_SCOPE` — **SEMANTIC, verify**.
  6 SBS fields carry `needs_review` in the mapping (semantic overrides vs the Indian-label hint —
  e.g. `posttaxProfitAmount`←NET_INCOME, `netCashInvestmentAmount`←CFI); confirm intended semantics.
- `Mudança no valor justo de ativos biológicos` → `rawMaterialsAndConsumablesAmount` is DELIBERATE
  (`labels_OLD.yaml`), not a mislabel: with the cost of sales also labelled (`custo(s) das vendas`, added
  2026-11-04) the two COGS lines sum to the gross-profit identity (Moreno p6: 2.726.257 − 137.606 −
  2.088.210 = 500.441). How rows sharing one SBS field combine is decided by the pipeline itself
  (`sbs_fields`, sensitive detail #20): here the sum, PROVEN by the gross-profit identity.
- Many detail line items legitimately have **no** SBS field (sales taxes ICMS/PIS/COFINS, equity
  components `reservas de capital`/`reservas de lucros`, sundry expenses) — their `sbs_label=null` is
  CORRECT, not a miss. SBS is a ~43-field aggregate taxonomy; do not force-map detail rows. Overall
  matched-row recall across the 6 goldens is ~16% by design (most rows are sub-line detail).
  Unqualified loan rows on NOTE/JUNK pages (no code, no section) stay `needs_review` — by design.
  Not counted as loans: lines without loan wording (Dom «Partes relacionadas» in non-current liabilities,
  Serasa «C/C EMP INVESTIDAS/SOCIOS») — the caption cannot tell a loan from a current account
  (reconfirmed by the user 2026-11-05 on Cibra, whose related-party liabilities bear interest — #32).
