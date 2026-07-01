# CLAUDE.md — br_ocr_json_processing

Project guide for Claude Code. Read this first; it points to the deeper docs.

## What this project does

Turns **raw OCR-provider JSON** of Brazilian financial reports (Portuguese) into a clean,
structured, **page-classified**, **SBS-labelled** JSON. Three stages:

- **Stage 1 — table reconstruction** (`structure_ocr.py`): rebuild rows/columns, attach line-item
  names to their AMOUNT_N / AMOUNT_N-1 figures, tag each column's `account_type`
  (Controladora / Consolidado), attach row codes, carry document metadata.
- **Stage 2 — page classification** (`classify_statements.py`): label each page
  `PRIMARY:{BALANCE|INCOME|CASH_FLOW}` / `NOTE` / `JUNK` with confidence + reasons.
  Philosophy: **tag, never delete.**
- **Stage 3 — SBS labelling** (`sbs_labeler.py`): stamp each row with its SBS Field Name
  (`sbs_label`, e.g. `cashAndSecuritiesAmount`) by matching `label` against the Portuguese
  aliases in `sbs_label_mapping.json`. Same **tag, never delete** philosophy.
- **Stage 4 — Excel export** (`export_excel.py`): render the structured JSON into a polished,
  Allianz-styled `.xlsx` (sheets: metadata / balance / income / cash_flow). This stage **does**
  filter (see "Excel export filtering" below) — it is the presentation layer, run separately.

`structure_ocr.py` runs Stages 1-3 and embeds Stage-2/3 results into its output. `export_excel.py`
is a separate Stage-4 step over that output.

## Run it

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
python3.13 structure_ocr.py <input.json> -o <output.json>      # full pipeline (Stage 1+2+3)
python3.13 sbs_labeler.py <structured_output.json> --dry-run    # Stage-3 only, report (no write)
python3.13 sbs_labeler.py <structured_output.json> -o <out>     # Stage-3 only, post-process
python3.13 export_excel.py <structured_output.json> -o <x.xlsx> # Stage-4 Allianz Excel export
python3.13 diagnose_pages.py <input.json>                       # read-only Stage-2 diagnostics
python3.13 -m streamlit run app.py                              # web UI (upload/review/export)
```

Full operational detail (scenarios, troubleshooting, reading outputs): **`RUNBOOK.md`**.

## File map

| File | Role | Run directly? |
|---|---|---|
| `structure_ocr.py` | **Entry point** — Stage 1 + Stage 2 + Stage 3; computes `document_type` | Yes (CLI) |
| `classify_statements.py` | Stage-2 classifier (4 layers + safety-net) | No (library) |
| `doc_type.py` | **Shared** — document-type detection + filter policy (single source of truth) | No (library) |
| `sbs_labeler.py` | Stage-3 SBS labeller (`sbs_label`) | Yes (CLI, post-process) |
| `export_excel.py` | Stage-4 Allianz Excel export (metadata/balance/income/cash_flow) | Yes (CLI) |
| `app.py` | **Streamlit front-end** — upload/review/export; reuses `export_excel` (screen == Excel) | Yes (`streamlit run`) |
| `.streamlit/config.toml` | **Config** — Streamlit Allianz theme + 50 MB upload limit | — |
| `build_sbs_mapping.py` | Regenerates `sbs_label_mapping.json` from YAML + xlsx | Yes (CLI) |
| `anchors_lib.py` | **Shared** matching/parsing/geometry — single source of truth | No (library) |
| `diagnose_pages.py` | Read-only Stage-2 diagnostics | Yes (run from project dir) |
| `anchors.yaml` | **Config** — anchor lexicon, business-editable | — |
| `sbs_label_mapping.json` | **Config** — SBS Field → Portuguese aliases (generated) | — |
| `labels_OLD.yaml` | Source: old primary codes + aliases (input to mapping build) | — |
| `SBS_fields_mapping_with_INDIAN_labels.xlsx` | Source: new SBS Field Names (input to mapping build) | — |
| `docs/OCR_Table_Reconstruction.md` | Stage-1 methodology | — |
| `docs/Primary_Statement_Classification.md` | Stage-2 methodology | — |
| `docs/SBS_Labelling.md` | Stage-3 methodology | — |
| `docs/Excel_Export.md` | Stage-4 methodology | — |
| `docs/Frontend_Streamlit.md` | Streamlit front-end (`app.py`) methodology | — |
| `RUNBOOK.md` | How to run / troubleshoot | — |

## Test documents & their outputs (kept separate — do NOT overwrite)

| Input | Output | Shape | Notes |
|---|---|---|---|
| `comprehansive_sample.json` | `structured_output.json` | 69 LST pages, 4-col (DFP annual) | CODE_VALUE empty; metadata as scalars; export type **DFP_4COL** |
| `sample_2.json` | `structured_output_sample2.json` | 15 pages, 2-col, 2024/2023 | CLOSING_DATE as group; export type **TWO_COL** (no Consolidado split) |
| `itr_samples.json` | `structured_output_itr.json` | ITR quarterly, hierarchical codes | CODE_VALUE populated (300); export type **ITR** |
| `provider_sample.json` | `structured_output_provider.json` | uppercase-label dump, reversed N-1/N | all NOTE pages; export type **SERASA** (provider tag `SERASA EXPERIAN`); single-page balance, ST/LT debt + non-current `APLICACOES FINANCEIRAS` resolved by bucket-wide sections |
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

2. **Metadata comes in TWO forms.** CLOSING_DATE/DURATION/CURRENCY/UNIT may be a **scalar**
   (`data_point_name` + `value:{}`) OR a **group** (`group_name` + subgroups — e.g. CLOSING_DATE in
   ITR/sample_2). `extract_metadata` handles both, picking the highest-confidence value. Do not
   assume "scalars only".

3. **CODE_VALUE may be empty OR populated.** Empty in `comprehansive_sample.json`; populated in
   `itr_samples.json` (300 hierarchical codes `1`/`1.01`/`1.01.01`, sitting left at x≈0.05).
   `attach_codes` binds by page + y (tolerance `Y_ROW_TOL = 0.012`).

4. **Group tag `*_LST` classifies CONTENT, not LOCATION.** The provider tags `CASH_FLOW_LST` on 42
   pages but only **1** is the real cash-flow statement. Never decide page type from the group tag —
   Stage-2 uses **anchor completeness** instead.

5. **ONLY_AMOUNT is NOT a complete superset** (~6% of LST amounts missing). Columns = UNION(LST +
   ONLY_AMOUNT), deduped by `(value, round(x_min,4), round(y_min,4))`.

6. **ACCOUNT_TYPE covers a column PAIR** (N, N-1), often hovering over the gap between them. Match by
   x-overlap with the pair, take the nearest header ABOVE. Never merge header fragments by y alone.

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

## Stage-2 layers (in `classify_statements.py`)

1. **Negative filter** — no amounts / too sparse → JUNK.
2. **Detector** — anchor-set **completeness** per family (the workhorse).
3. **Disambiguator** — primary vs. segment copy: column count (2/4 = primary, more = segment note),
   page order, repeated set. Balance-continuation special case (see #7).
3b. **Positional safety-net** — if a solid primary block exists (≥2 families) and a nearby page has
    **partial** anchors of the missing family (≥1) + primary-like columns, promote it as a FLAGGED
    primary (conf 0.55, `needs_review`). Conservative: never promotes on position/columns alone;
    silent when anchors are intact.
4. **Validator** — accounting identity (balance: ativo == passivo+PL) raises/flags confidence.

## Stage-3 SBS labelling (in `sbs_labeler.py`)

A pipeline of small stages (two passes). Adds `sbs_label` (+ `sbs_scope`, `sbs_match_method`,
`sbs_match_source`, `sbs_match_score`, `sbs_blocked_reason`, `sbs_duplicate_of`) to each row.
PASS 1 per bucket: (0a) **code-match** → (0) match → (1) twin/section → (2) empty-label.
PASS 2 document-level: (3) scope-gate → (4) struct-validate → (SERASA) net-income → (5) dedup.

0a. **Code-first (CVM/B3 codes — ITR/DFP).** A row whose `code` is in the standardized **level-1..3** map
   (`sbs_label_mapping.json → code_map`, built by `build_sbs_mapping.py`) is labelled directly from the code
   (`sbs_match_source=code`, score 100) and the text stages skip it. An authoritative, OCR-robust signal that
   does not depend on the line text. ⚠️ Only levels 1..3 are mapped — deeper codes (`6.01.02.01`…) are
   **positional**, not semantic (same code = different concept on different pages), so they fall back to text.
   ⚠️ Code `2` ("Passivo Total") is **DELIBERATELY UNMAPPED**: it is the balance **grand total** (liabilities +
   equity == total assets == code `1`), NOT total liabilities (`totalLiabilitiesAmount` = liabilities **only**,
   see below). Mapping it double-counts equity. `totalLiabilitiesAmount` is resolved arithmetically in PASS 2
   instead (see point 5c). Measured: on the ITR golden the code map agrees with the text engine on all 39 rows
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
   statements, never blocked.
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
   −170.584.068,84 (also its cross-page `RESULTADO DO EXERCICIO`/`LUCRO LÍQUIDO` posttax pair p4/p5; ITR
   `Fornecedores`/`Fornecedores Nacionais` same-page same-value). ⚠️ The **page-OR-kind** scope is the precise
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
     logic then labels that inflated figure `longTermLiabilitiesAmount` and leaves the real LT line
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
   **debt** (`financialLiabilities{Short,Long}Term`, caption `emprestimos e financiamentos`) and
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
   `shortTermLiabilitiesAmount`/`totalCurrentAssetsAmount`, rest=non-current→`longTermLiabilitiesAmount`/
   `totalFixedAssetsAmount`). Else `needs_review`. Income subtotals (subtractions) not attempted yet.
   Methods: `empty_label_arith` / `empty_label_unresolved`.

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
| **SERASA** | provider tag `SERASA EXPERIAN` in `metadata.ACCOUNT_TYPE` | **ignored** | **all** |
| **ITR** | any row has a `code` (CODE_VALUE) | **ignored** | **Consolidado only** |
| **UPPERCASE** | ≥50% of labels are ALL-CAPS (generic provider dump) | **ignored** | **all** |
| **DFP_4COL** | a column carries `Consolidado`/`Controladora` | `PRIMARY:*` only | **Consolidado only** |
| **TWO_COL** | none of the above (single-perspective, `account_type=null`) | `PRIMARY:*` only | **all** |

**SERASA precedes UPPERCASE** because a Serasa report is also all-caps (UPPERCASE would catch it), but the
provider tag is exact whereas "≥50% caps" is incidental. Filter behaviour is identical to UPPERCASE; the
distinct type lets Serasa-specific Stage-3 fixes target it (e.g. the double-net-income demotion). The
`SERASA EXPERIAN` tag sits in the `ACCOUNT_TYPE` group on a cover page — `extract_account_type` surfaces
ONLY a known provider tag into `metadata.ACCOUNT_TYPE`, never the per-column `Controladora`/`Consolidado`
headers (so DFP/ITR metadata stays clean).

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
`Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N`.

## Conventions for this project

- Always `python3.13`. `rapidfuzz` (3.14.5, installed via proxy) with automatic `difflib` fallback —
  console header shows the active backend.
- `pip` only as `python3.13 -m pip install <pkg> --proxy http://127.0.0.1:9000 --trusted-host pypi.org --trusted-host files.pythonhosted.org`.
- When testing a new document, write to a NEW output file — the user wants every example kept.
- Memory holds the deeper rationale: see `[[br-ocr-json-structure]]` and
  `[[br-primary-statement-detection]]`.

## Known open items (not bugs)

- Anchor lexicon (`anchors.yaml`) tuned for annual DFP wording; **ITR (quarterly) uses different
  phrasings** (`Ativo Total` vs `Total do ativo`), so BALANCE/INCOME under-detect on ITR. Extend
  aliases as needed.
- Truncated OCR metadata tokens seen in some files (`CURRENCY='(Reais'`, `'reais)'`) — not yet
  normalized.
- ~7% of rows on dense segment-note pages carry `needs_review: true` (intrinsically ambiguous layout).
- **Stage-3 SBS labelling**: 31/43 SBS fields have aliases (12 new fields — `goodwillAmount`,
  `ebitdaAmount`, `provisions`, … — have none yet; add via `EXTRA_ALIASES`
  in `build_sbs_mapping.py` (+ `EXTRA_SCOPE` if source-less), or `labels_OLD.yaml`, then rebuild).
  ⚠️ `totalLiabilitiesAmount` is INTENTIONALLY alias-less — it is resolved **arithmetically** (ST+LT),
  not by text (see Stage-3 point 5c); do NOT add grand-total aliases back.
  `totalRemunWagesAmount` (despesas com pessoal) and `interestIncomeAndSimilarAmount` (receitas
  financeiras) were given aliases via `EXTRA_ALIASES`/`EXTRA_SCOPE` — **SEMANTIC, verify**.
  6 SBS fields carry `needs_review` in the mapping (semantic overrides vs the Indian-label hint —
  e.g. `posttaxProfitAmount`←NET_INCOME, `netCashInvestmentAmount`←CFI); confirm intended semantics.
- Many detail line items legitimately have **no** SBS field (sales taxes ICMS/PIS/COFINS, equity
  components `reservas de capital`/`reservas de lucros`, sundry expenses) — their `sbs_label=null` is
  CORRECT, not a miss. SBS is a ~43-field aggregate taxonomy; do not force-map detail rows. Overall
  matched-row recall across the 6 goldens is ~16% by design (most rows are sub-line detail).
  Unqualified debt rows on NOTE/JUNK pages (no code, no section) stay `needs_review` — by design.
