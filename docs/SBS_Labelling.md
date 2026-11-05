# SBS Labelling

**Mapping each Portuguese line item to its standardised SBS Field Name**

| | |
|---|---|
| **Component** | `sbs_labeler.py` + `sbs_label_mapping.json` (built by `build_sbs_mapping.py`) |
| **Input** | A structured output (Stage 1+2) — `statements[*][]` rows with `label` |
| **Output** | The same rows, each stamped with `sbs_label` (+ scope / method / score) |
| **Audience** | Python developers & business stakeholders |
| **Status** | Validated on 4 document shapes (annual DFP, ITR, 2-col, uppercase) |
| **Related** | [`Primary_Statement_Classification.md`](Primary_Statement_Classification.md) — Stage 2; [`Excel_Export.md`](Excel_Export.md) — Stage 4 |

> **Where this fits.** Stage 1 rebuilds the tables; Stage 2 classifies pages. **This is Stage 3:**
> for every reconstructed row, attach the *standardised* SBS field name (e.g.
> `cashAndSecuritiesAmount`) that its Portuguese caption corresponds to. Like Stage 2 it is a
> **labelling layer** — it never deletes a row; it tags it so downstream systems can map figures
> into the SBS schema.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary) *(business)*
2. [What Problem This Solves](#2-what-problem-this-solves)
3. [The Mapping File (`sbs_label_mapping.json`)](#3-the-mapping-file-sbs_label_mappingjson) *(business + developers)* — incl. the pinned liability mapping
4. [How a Row Gets Its Label](#4-how-a-row-gets-its-label) *(developers)*
5. [Statement Scope: the Scope-Gate & the `sbs_scope` Flag](#5-statement-scope-the-scope-gate--the-sbs_scope-flag)
6. [Current vs Non-Current Twins (Ambiguity Groups)](#6-current-vs-non-current-twins-ambiguity-groups) *(developers)*
   - 6b [Empty-Label Subtotals](#6b-empty-label-subtotals-label_null) · 6c [Structural Validation](#6c-structural-validation-stage-4--flag-never-strip) · 6d [Deduplication](#6d-deduplication-stage-5) · 6e [Total Liabilities](#6e-total-liabilities--arithmetic-not-text-stage-6)
   - 6f [Serasa analysis rows](#6f-rows-outside-the-statement-zone-serasa-analysis) · 6g [Corpus-audit aliases](#6g-corpus-audit-aliases-2026-11-04) · 6h [One Value per Field (`sbs_fields`)](#6h-one-value-per-field--the-sbs-contract-sbs_fieldspy)
   - 6i [Positional rules: tax lines, EBIT, CAPEX](#6i-positional-rules-of-the-income-and-cash-flow-statements-2026-11-04) · 6j [Balance sections](#6j-balance-sections-_compute_sections) · 6k [Provisions and the decisions of 2026-11-05](#6k-provisions-and-the-other-sbs-decisions-of-2026-11-05-cibra-audit)
7. [Row Fields Produced](#7-row-fields-produced)
8. [Rebuilding the Mapping](#8-rebuilding-the-mapping) *(developers)*
9. [Results on the Reference Samples](#9-results-on-the-reference-samples)
10. [How to Run](#10-how-to-run) *(developers)*
11. [Design Decisions & Open Items](#11-design-decisions--open-items)
12. [Glossary](#12-glossary)

---

## 1. Executive Summary

Brazilian financial reports use dozens of different Portuguese captions for the same accounting
concept — `Caixa e equivalentes de caixa`, `Disponibilidades`, `Numerários` all mean *cash*. The SBS
schema, by contrast, uses one fixed field name per concept (`cashAndSecuritiesAmount`). **Stage 3
bridges the two**: it reads each row's caption and stamps the matching SBS Field Name onto the row.

Stage 3 is a pipeline of small, independently-tested functions in two passes (the exact run order is
the table in [`Pipeline_Overview.md`](Pipeline_Overview.md) §3.5):

* **PASS 1, per statement bucket** — balance **sections** (CVM code → header / subtotal markers →
  arithmetic, §6j); **code match** (CVM levels 1–3); **match** (exact dictionary first, then the fuzzy
  anchor engine Stage 2 uses); **twin / section** (current vs non-current loans and securities resolved
  by qualifier → code → section, never guessed, §6); **empty-label** subtotals (§6b); **tax lines**
  between pre-tax and post-tax (§6i).
* **PASS 2, document level** — **scope-gate** (§5); **CAPEX asset lines** and **EBIT before the
  financial result** (§6i); **structural validation** (flag only, §6c); SERASA after-dividends net
  income; **balance liability totals** (`totalLiabilitiesAmount` by arithmetic, equity folded into the
  non-current subtotal, identity fill — §6e); **dedup** (§6d) and the duplicate-index remap.
* **Stage 3 end — `sbs_fields.py`**: ONE value per SBS field and period, hierarchy-aware and proven by
  the accounting identities (§6h).

> ⚠️ **Liability fields — mapping pinned by the user 2026-11-04** (the opposite of the xlsx Description):
> `shortTermLiabilitiesAmount` / `longTermLiabilitiesAmount` = short- / long-term **credits and loans**;
> `financialLiabilitiesShortTerm` / `financialLiabilitiesLongTerm` = the **total** current / non-current
> liabilities. Every "ST / LT" below means what this mapping says (§3).

On the reference documents the dictionary-first match reproduces every previously-correct label and
recovers a few the fuzzy-only engine missed, with zero new integrity errors.

---

## 2. What Problem This Solves

Downstream consumers need figures keyed by a **stable identifier**, not free-text Portuguese. Two
naïve approaches both fail:

- **Exact string lookup** misses almost everything — OCR introduces typos, note references
  (`(Nota 6.2)`), and accents that break equality.
- **Loose fuzzy matching** over-fires — `token_set_ratio >= 90` scores the subtotal
  `Total do ativo circulante` as a perfect match against `Total do ativo`.

Stage 3 instead reuses the corpus-validated rule from `anchors_lib` (token coverage + benign
insertions + a fuzzy backstop), the same one Stage 2 trusts for page classification.

---

## 3. The Mapping File (`sbs_label_mapping.json`)

The lexicon that drives Stage 3. It is **generated** from two business-owned sources and should not
be hand-edited (see §8). Top-level keys:

| Key | Contents |
|---|---|
| `fields` | Per SBS Field Name: `category`, `indian_label`, `source_old_primary`, **`statement_scope`**, `match_type`, `needs_review`, `note`, the Portuguese **`aliases`**, and the **structural address** below. |
| `alias_index` | Flat reverse index `normalized_alias → sbs_field` for unambiguous aliases (O(1) lookup). |
| `alias_conflicts` | Normalized aliases that map to >1 field (need page context). |
| `unmapped_old_anchors` | Old anchor sets with no SBS target (kept for reference): `OPERATING_EXPENSES`, `FFO`, `WCN`, `PROVISION`, `FINANCIAL_RESULT`, `EXCEPTIONAL_RESULT`. |

`statement_scope` (one of `BALANCE_ASSETS_LST` / `BALANCE_LIABILITIES_LST` / `INCOME_STATEMENT_LST` /
`CASH_FLOW_LST` / `META`) records which statement a field's rows belong to.

**Structural address** (per field, consumed by Stages 3-4; all generated as data, no code change to
add a field):

| Field key | Drives | Source |
|---|---|---|
| `allowed_statements` | the scope-gate (§5); length 1 normally, 2 for the double-home whitelist | `statement_scope` + `DOUBLE_HOME` table |
| `home_statement` | the field's primary home (= `allowed_statements[0]`) | derived |
| `home_section` | the balance side check (§6c) — `current` / `noncurrent` / `equity` / `…_total` | `HOME_SECTION` table |
| `canonical_ordinal` / `income_tier` | the income order check (§6c) | xlsx `#` column + `INCOME_TIER` table |

A separate top-level key, **`ambiguity_groups`**, declares the current/non-current twin pairs and the
normalized captions they share (see §6). **`code_map`** maps the standardized CVM codes (levels 1–3)
to fields.

> **Liability mapping (user decision 2026-11-04, pinned).** In `build_sbs_mapping.py` the JOIN rows of the
> four liability fields swap their old primaries (alias sets) — `financialLiabilitiesShortTerm` ←
> `CURRENT_LIABILITIES` (total), `financialLiabilitiesLongTerm` ← `NON_CURRENT_LIABILITIES` (total),
> `shortTermLiabilitiesAmount` ← `SHORT_TERM_DEBT` (loans), `longTermLiabilitiesAmount` ← `LONG_TERM_DEBT`
> (loans) — keeping the alias-set order, so the change was a pure rename (verified byte-equivalent on the
> 8 goldens, the 22 corpus and the 12 audit PDFs). `code_map`: `2.01` / `2.02` → the two totals, `2.01.04`
> / `2.02.01` → the loans; the `debt` twin group is the loans pair; `HOME_SECTION`: loans `current` /
> `noncurrent`, totals `current_total` / `noncurrent_total`. Do not revert to the xlsx text.

> **`provisions` (user decision 2026-11-05, §6k).** IAS 37 liability provisions only — aliases in
> `EXTRA_ALIASES`, scope `BALANCE_LIABILITIES_LST` (`EXTRA_SCOPE`), **strict scope** (`STRICT_SCOPE` →
> `strict_scope: true` in the mapping, §5), `code_map` `2.01.06` / `2.02.04` («Provisões») → `provisions`.

> **Coverage today:** 33 of 43 SBS fields carry aliases. Without aliases: `goodwillAmount`,
> `netGroupProfitAmount`, `pensionFundsAndOtherStaffCommitmentsAmount`, `exploitProvisionAmount`, the 5
> metadata fields — and `totalLiabilitiesAmount` on purpose (arithmetic, §6e). Extend `labels_OLD.yaml`
> or `EXTRA_ALIASES` in `build_sbs_mapping.py` and rebuild (§8).

---

## 4. How a Row Gets Its Label

The pipeline runs in two passes. **Pass 1** works one statement bucket at a time (Stages 0-2);
**Pass 2** is document-level — it needs every bucket already labelled (Stages 3-5).

For each row with a non-empty `label`, **Stage 0 (Match)**:

1. **Exact dictionary first.** Look the row's **SBS-normalized** caption up in the alias index
   (`sbs_normalize` = the shared `normalize` + dropping the dual-sign word `prejuízo`, which never
   distinguishes two fields). A clean hit wins immediately (`sbs_match_source = dict`, score 100). The
   dictionary carries ~86 % of all labels — it is fast, exact, and can never drift.
2. **A dictionary *conflict*** (a caption shared by a twin pair) returns both fields for Stage 1 to
   resolve (`sbs_match_source = dict_conflict`).
3. **Fuzzy fallback** only when the dictionary misses: `anchor_match` against every field's aliases —
   the same token-coverage + benign-insertion + fuzzy ≥ threshold rule as Stage 2 (`anchors_lib`),
   so the two stages can never disagree about what a caption says (`sbs_match_source = fuzzy`).
4. **Tie-break.** One hit → done. Several hits → prefer the field whose home statement is the row's
   bucket, else the clear top score, else flag `unresolved_ambiguous`.

> **Why a dictionary first?** It was measured: exact lookup alone reproduces ~86 % of all labels and
> recovers a few the fuzzy-only engine missed (an alias carrying `prejuízo` used to *block* a label
> that omitted it). Adding a Portuguese stemmer or IDF weights changed nothing on the corpus, so the
> fuzzy engine is kept exactly as-is — the dictionary does the heavy lifting.

Stages 1 (twins, §6) and 2 (empty-label, §6b) then refine the per-bucket result before Pass 2.

---

## 5. Statement Scope: the Scope-Gate & the `sbs_scope` Flag

Every row lives under a statement bucket (assets / liabilities / income / cash-flow). Each SBS field
declares its **home statement(s)** in `allowed_statements` (config, §3). **Stage 3 (Scope-Gate)**
enforces that a field is only kept in a statement it belongs to — an income-statement field must not
surface on the balance, and vice-versa.

When a match lands **outside** the field's allowed statements:

- **Block it** (`sbs_label → null`, `sbs_blocked_reason = out_of_scope`) **if** the same field is
  also captured **in-home** elsewhere in the document — so nothing is lost. This is the common case:
  `Contas a receber` / `Fornecedores` repeating as working-capital *movements* inside the cash-flow
  statement, while the real **stock** sits on the balance.
- **Recover it** (keep the label, mark `sbs_scope = cross_scope` + `needs_review`) **if** there is no
  in-home copy — e.g. an ITR balance carrying a real `Resultado do exercício` with no separate income
  page. The label is never silently dropped when it is the only occurrence.

> **Double-home whitelist.** A few concepts legitimately live in **two** statements and are declared
> with two `allowed_statements`, so the gate never blocks them: `depreciationAmount` (P&L expense *and*
> cash-flow add-back), `dividendAmount`, `posttaxProfitAmount` (net income is also the cash-flow
> opening line), `incomeTaxExpenseRefundAmount`.

> **Strict scope (2026-11-05).** A field listed in `STRICT_SCOPE` (`build_sbs_mapping.py`, today only
> `provisions`) is ALWAYS blocked outside its statements — no `cross_scope` recovery, even when the
> balance line went unlabelled: the cash flow repeats the balance caption as a flow («Provisões para
> riscos fiscais, cíveis e trabalhistas» 887 / 1.767 on Cibra p11 is the period's adjustment, the balance
> provision is 65.986 / 65.099).

This **replaces** the earlier "scope is a tie-breaker, never a filter" design: scope is now a real
filter, but a *traceable* one (block reason recorded, single-occurrence figures recovered) — consistent
with **tag, never delete**.

---

## 6. Current vs Non-Current Twins (Ambiguity Groups)

Some captions are shared by a **current/non-current twin pair** of SBS fields. The same Portuguese
line can mean either, depending on which section of the balance sheet it sits in. The twin pairs are
declared **as data** in `sbs_label_mapping.json → ambiguity_groups` (built by `build_sbs_mapping.py`),
so adding a new pair needs no code change. Today there are two:

| Group | Current twin | Non-current twin | Shared captions (examples) |
|---|---|---|---|
| **debt** (credits and loans) | `shortTermLiabilitiesAmount` | `longTermLiabilitiesAmount` | `empréstimos e financiamentos`, `debêntures`, `títulos emitidos`, `bancos com empréstimos`, `empréstimos e outras obrigações`, `empréstimos sócios / clientes`, `mútuos sócios` |
| **securities** | `cashAndSecuritiesAmount` | `financialAssetsAmount` | `títulos e valores mobiliários`, `aplicações financeiras`, `investimentos`, `tot investimentos` (Serasa), `aplicações financeiras avaliadas a valor justo através do resultado` (CVM 1.0x.0x.01) |

A new loan / securities caption must be added to the field's aliases (`EXTRA_ALIASES`) **and** to the
group's shared list (`AMBIGUITY_GROUPS`), otherwise it is taken as the current twin without resolution.

This is **not** a mere tie. For debt, the unqualified caption lives only in the short-term alias set;
for securities, the merged STI aliases live under cash. So a naïve matcher would silently take the
current twin even for a non-current row (this really happened: 3 ITR debt rows mislabelled, and a
non-current `Títulos e valores mobiliários` collapsed into cash).

**The trigger.** A row enters resolution when it matches a twin field of a group **in its own
statement scope** *and* the match is driven by a **shared** caption (one listed under both twins). A
caption unique to one twin — e.g. pure `caixa` — is never ambiguous and is taken directly.

**The resolution order** (per row, first that fires wins):

1. **Explicit qualifier** in the row's own label — `circulante` / `curto prazo` → current,
   `não circulante` / `longo prazo` → non-current. Self-describing, most trustworthy.
2. **CODE_VALUE prefix** — ITR hierarchical codes: `x.01.*` = current, `x.02.*` = non-current
   (liabilities `2.01`/`2.02`, assets `1.01`/`1.02`).
3. **Positional section** — two-pass, page-local. A **header** (`Ativo/Passivo (não) circulante`)
   leads its section downward; a **subtotal** (`Total do … (não) circulante`) closes its section
   upward. Both layouts occur in real data (ITR is header-led, the annual DFP is subtotal-led). The
   marker vocabulary is per-scope so assets and liabilities never cross-trigger.
4. **None available** → `sbs_label = null`, `sbs_candidates = [both twins]`, `needs_review = true`.
   The labeller **never guesses.**

A twin caption appearing **outside** its home scope (e.g. `Empréstimos e financiamentos` or
`Imobilizado` inside the cash-flow statement) is a *movement/flow*, not a stock — it is tagged
`cross_scope` (§5) and never twin-resolved.

> **Worked example — the annual DFP, page 20.** `Títulos e valores mobiliários` appears twice: once
> above `Total do ativo circulante` and once between the two subtotals. With no codes, the positional
> scan resolves the first to `cashAndSecuritiesAmount` (current) and the second to
> `financialAssetsAmount` (non-current). On ITR the same split is driven by codes `1.01.02` vs
> `1.02.02`.

---

## 6b. Empty-Label Subtotals (`label_null`)

Stage 1 keeps rows whose **name the OCR dropped** but whose ONLY_AMOUNT figures are intact
(`label_missing: true`). On the balance these are almost always **section subtotals** (a
`Total do ativo circulante` whose caption was lost). Stage 3 recovers an SBS field for them —
**conservatively, balance scopes only, arithmetic-driven**:

1. **Guard** — runs only on rows the report will actually show: a small-span statement (≤2 consecutive
   pages) or a `PRIMARY`-class page. This deliberately skips multi-page NOTE tables, whose rows carry
   coincidental sums that would otherwise false-match.
2. **OCR-duplicate guard** — a value identical to an adjacent row (an OCR shift, e.g. a liabilities
   `7.179` that equals the assets `Imobilizado`) is *not* assigned.
3. **Arithmetic** — if the value equals the running sum of the immediately preceding *detail* rows of
   the same section, it is that section's subtotal. The sum walks **backward** from the row and stops
   at the first boundary: a previous `label_null` row (the prior subtotal) **or** a change of section
   (`sections[j] != sections[idx]`). Match tolerance is `max(2.0, 1% of the target)` — only an
   arithmetic hit within tolerance is accepted; anything else is left unresolved. The matching field:
   `current → totalCurrentAssetsAmount`, `non-current → totalFixedAssetsAmount`,
   `current liab → financialLiabilitiesShortTerm`, `non-current liab → financialLiabilitiesLongTerm`
   (the section TOTALS of the user mapping),
   `equity → shareholdersFundsAmount` (the five entries of `_SECTION_TOTAL_FIELD` — a `label_null` row
   can only ever become one of these section totals, never a line-item field).
4. **Which section** — identified in priority order:
   - **Content (liabilities)** — if the feeding detail rows are **equity** items (`_EQUITY_KEYWORDS`:
     `capital social`, `reservas`, `lucros/prejuízos acumulados`, `patrimônio líquido`, `ações em
     tesouraria`, **`resultado do exercício`**, **`ajustes de exercícios anteriores`**, **`distribuição
     de lucros`**, dividends, …), the subtotal is the equity total → `shareholdersFundsAmount`. ⚠️ The
     liabilities side has **three** blocks (current / non-current / **equity**), so a pure
     current-vs-non-current rule would wrongly tag the equity total as the non-current total —
     content detection prevents that. ⚠️ The content test demands **ALL** feeding rows be equity, so
     the keyword list must include the period-result/appropriation lines that live inside equity
     (`resultado do exercício`, `ajustes de exercícios anteriores`, `distribuição de lucros`). A single
     missing caption breaks the `all()` test. Note `_EQUITY_KEYWORDS` feeds **two** consumers: this
     content check **and** `_fill_sections_by_arithmetic`, which decides whether a closed block is
     `equity` vs `noncurrent`. So a missing caption fails it in **both**: the block is sectioned
     `noncurrent`, and the content override also misses — the row then resolves via the (now wrong)
     positional section to the non-current total, not via the ordinal fallback. That was the
     `complicated_sample_2` `49.766.376,21` bug. The list is consulted **only** in
     `BALANCE_LIABILITIES_LST` scope, so income's `Resultado do exercício` is never affected.
   - **Positional section** — `sections[idx]`, the section computed by **Stage-3**
     `_compute_sections` (NOT Stage-1, which never computes sections). For a normal balance that is the
     page-local marker scan (forward headers / backward subtotals + an arithmetic gap-fill); for a
     **SERASA/UPPERCASE shape** (`_is_serasa_shape`: that doc type, no `code`, no `total do …` markers)
     the whole bucket is sectioned by **bucket-wide arithmetic** (`_fill_sections_by_arithmetic`)
     instead — this is the path that sections `complicated_sample_2`, so its four non-equity subtotals
     resolve via this branch, not via lexical headers.
   - **Ordinal fallback** — only when no section could be derived at all (`sections[idx]` is `None`):
     the Nth **non-equity** `label_null` balance subtotal maps current-then-non-current (equity
     subtotals are removed from the ordinal count first).
5. **Otherwise** → left unresolved (`sbs_match_method = empty_label_unresolved`, `needs_review`,
   reason recorded). Never guesses.

**One account perspective, one subtotal tree (2026-11-04).** Two rules sit on top of steps 3-4:

* **Perspective.** Every sum runs inside ONE account perspective — the page's (consolidated if presented,
  else the most frequent), then any other the row carries (`_perspective`, `_amount_n(row, at)`). The old
  "first `AMOUNT_N` cell of each row" mixed Controladora and Consolidado figures as soon as a
  Controladora cell was a dash: MBP p10's non-current subtotal stayed unresolved and the derived total
  liabilities came out as ST only (390.217 instead of 925.621). In the section arithmetic a row without
  a figure in the perspective (a dash there) now contributes 0 instead of ending the block.
* **Subtotal tree** (`_subtotal_tree`, per page): reading the rows in print order, a row equal to the sum
  of the last k ≥ 2 open items (details or already-closed subtotals) closes them. A block subtotal
  inside a section (`Realizável a longo prazo`) also equals the running sum of its own details — the
  test of step 3 alone labelled it `totalFixedAssetsAmount` next to the printed non-current total
  (Raízen p20 22.881.939, Moreno p5 123.889, 1Q26 p21). With the tree:
  - consumed by a section-level subtotal → `empty_label_intermediate` (no field, NOT a review case);
  - closes everything still open on the page and has a subtotal child → the page's **grand total**:
    `totalAssetsAmount` on the assets side (Guabi p1 391.259, Documento1 p1 637.480), no field on the
    liabilities side (`empty_label_grand_total` — total liabilities = current + non-current, §6e);
  - a section total printed after block subtotals (1Q26 p21: non-current = 354.054 + 1.944.582) is
    proven by the tree even though the backward running sum stops at the block subtotal;
  - ⚠️ **not intermediate under «Total do passivo»** (2026-11-04, audit D1–D3): a printed liabilities-only
    total (`_TOTAL_LIAB_CAPS`) groups the SECTION totals and is itself summed with equity into the grand
    total, so its unnamed children are the current / non-current totals, not blocks (DIMENSIONAL p1:
    214.180 + 50.606 = «Total do passivo» 264.786); §6e then relabels it `totalLiabilitiesAmount`.

Worked example (`complicated_empty_labels_sample.json`): the nameless asset subtotals `167.392` and
`23.561` resolve to `totalCurrentAssetsAmount` / `totalFixedAssetsAmount`; the three nameless
liability subtotals resolve to `financialLiabilitiesShortTerm` (`169.135`), `financialLiabilitiesLongTerm`
(`12.652`) and **`shareholdersFundsAmount`** (`16.344` = `Capital Social` + `Lucros/Prejuízos
Acumulados`, recognised as equity by content). The asset `7.179` stays unresolved (doesn't sum — OCR
shift). Second worked example (`complicated_sample_2`, a side-by-side balance): once Stage-1's x-band
gate (OCR_Table_Reconstruction §7.3a) stops gluing cross-statement sums, the five nameless subtotals
resolve to `totalCurrentAssetsAmount` (`70.076.711,53`) / `totalFixedAssetsAmount` (`4.597.550,90`)
and `financialLiabilitiesShortTerm` (`22.168.649,61`) / `financialLiabilitiesLongTerm` (`2.739.236,61`) /
**`shareholdersFundsAmount`** (`49.766.376,21` = Capital Social + Reservas + Lucros Acumulados +
Ajustes, equity by content). Income subtotals (which involve subtractions, not plain sums) are
**not** attempted yet — a tracked next step.

## 6c. Structural Validation (Stage 4) — flag, never strip

A labelled row is checked against its expected position. Both checks **only flag** (`needs_review`);
they never remove a label, because a positional anomaly is usually OCR row-shuffle, not a wrong match.

- **Income order (tiers).** Each income field has a `canonical_ordinal` (data-driven from the SBS
  xlsx `#` column) grouped into **tiers** — true siblings (e.g. `interestPaidAmount` and
  `interestIncomeAndSimilarAmount`) share a tier, so their relative order never trips the check. A
  *later* tier appearing **above** an earlier one within a page-run (e.g. net income above turnover)
  flags both rows. (Tiers, not the raw ordinal, because the xlsx `#` is a schema enumeration that
  orders siblings arbitrarily — a strict sequence fired false positives on 4 of 6 documents.)
- **Balance side.** A field whose `home_section` is an asset section appearing in the liabilities
  bucket (or vice-versa) is flagged.

## 6d. Deduplication (Stage 5)

A row is a **duplicate** when another row on the **same page** has the same caption, the same
`account_type`, **and** the same AMOUNT values. The duplicate is tagged `sbs_duplicate_of` (the kept
row's index) + `needs_review`; the Excel export (`row_passes`) physically drops it. JSON keeps both —
**tag, never delete**.

> ⚠️ **`account_type` is part of the key on purpose.** Individual (Controladora) and Consolidado rows
> often carry the *same caption and the same value* yet are distinct perspectives — they must NOT be
> merged. Including `account_type` (and scoping to a single page) keeps those pairs, and also leaves a
> NOTE page that merely repeats a PRIMARY line untouched (the export already filters NOTE pages).

A second key catches the same figure re-worded: same `sbs_label` + the same amounts on the same page or
the same page kind. The survivor of a cluster is the best-placed row (fewest income-tier inversions).

**Audit fixes (2026-11-04):**

* **Amounts are compared as parsed numbers** (`_amount_token`): `'-13.534.197'` and `'(13.534.197)'` are one
  figure (WEG: the DRE cost line and its note copy were summed). Exactly — never rounded: basic EPS
  0,66015 and diluted 0,65999 are two figures.
* **Rows with different CVM codes are never duplicates** — a coded parent and its only child (2.01.02
  Fornecedores / 2.01.02.01 Fornecedores Nacionais), basic and diluted EPS (3.99.01.01 / 3.99.02.01) are all
  printed form lines. The Excel shows them; `sbs_fields` counts the parent once (hierarchy, §6h).
* **`sbs_duplicate_of` is the index in the bucket's full list** — the stages run on a view without the
  `serasa_analysis` / `non_figure_line` / `other_statement` rows, and the view index used to point at a DVA
  line.

## 6e. Total Liabilities — Arithmetic, Not Text (Stage 6)

`totalLiabilitiesAmount` (SBS field #23) is, per the SBS xlsx `Description`, **"Total liabilities
(short-term + long-term + provisions)"** — liabilities **only, excluding equity**. The balance identity
is therefore:

```
total assets  ==  total liabilities  +  shareholders' funds
totalAssetsAmount == totalLiabilitiesAmount + shareholdersFundsAmount
```

The Portuguese balance bottom lines we see in the wild — `Total do passivo e patrimônio líquido`,
`Total passivo e patrimônio líquido`, `Passivo Total`, and the CVM code `2` ("Passivo Total") — are the
**grand total** (liabilities **+** equity == total assets), a *different* concept. Text-aliasing them
silently mislabels the grand total as total liabilities (and double-counts equity downstream). So
`totalLiabilitiesAmount` carries **no aliases** and code `2` is **unmapped**; it is resolved
arithmetically in `_stage_balance_liability_totals`, per **balance cell** (one `account_type` + period —
each is its own self-balancing statement, so ITR `Individuais`/`Consolidado` and the DFP's two
perspectives never mix):

Names in this section: **ST** = `financialLiabilitiesShortTerm` (total current liabilities), **LT** =
`financialLiabilitiesLongTerm` (total non-current liabilities), **EQ** = `shareholdersFundsAmount` (user
mapping, §3).

1. **Relabel-or-derive.** A printed `Total do passivo`-type line (caption in `_TOTAL_LIAB_CAPS`) is
   relabelled `totalLiabilitiesAmount` **iff** its value equals `ST + LT` (the true
   liabilities total), never the grand total. If no such line is printed — the common case; most
   templates print only the grand total — a single **derived** row is synthesized (`sbs_match_method =
   derived`, `sbs_derived = true`, label `"Total liabilities (derived = current + non-current)"`) carrying
   `ST + LT` for every cell, and appended to the liabilities bucket. The exporter always shows derived
   rows (they have no source page, so `row_passes` bypasses the page-class filter for them).
2. **Equity-in-long-term fix.** Some templates (notably **Serasa**) print a `PASSIVO NÃO CIRCULANTE`
   subtotal that **already includes equity** (`EXIGÍVEL A LONGO PRAZO + PATRIMÔNIO LÍQUIDO`). The section
   logic then labels that inflated figure as LT and leaves the *real* long-term line
   (`EXIGÍVEL L PRAZO`) unlabelled — double-counting equity and breaking the identity by exactly the equity
   amount. When the labelled long-term value equals `assets − short-term` **and** a non-zero equity exists
   **and** it differs from `assets − short-term − equity`, the long-term label is on the wrong
   (equity-inclusive) line: it is **demoted** (`sbs_blocked_reason = includes_equity`, `needs_review`) and
   the unlabelled line whose value equals the true long-term (`assets − ST − EQ`) is **promoted**
   (`sbs_match_method = balance_identity`).

   > **Why the trigger can never misfire.** A correct balance has `LT == assets − ST − EQ`, so
   > `LT == assets − ST` can only hold when `EQ == 0`. Requiring `EQ ≠ 0` makes the condition
   > mathematically exclusive to the equity-folded case — verified as 0 false fires on all 8 goldens.
3. **Identity fill (RC#3).** When exactly one of LT / EQ is unlabelled for a cell, the unlabelled
   liabilities line whose value is exactly what `assets − ST − (the other)` leaves takes the field (1Q26
   p22 equity). ⚠️ Never for a block the identity leaves at **zero** (`|need| ≤ 2`): an absent block is not a
   printed line — a Controladora dash read as «1» took LT this way (São Salvador p21 «Outras obrigações»,
   audit SSA7); `sbs_fields` values such a block `zero_by_identity` instead.

The stage runs **after** the scope-gate (so ST/LT/EQ labels are stable) and **before** dedup. It is a
self-gating no-op on a balance that is already internally consistent (DFP/ITR/2-col long-term lines are
untouched). `label_statements` strips any prior `sbs_derived` rows on entry, so re-labelling is
idempotent. After this stage the identity `ST + LT + EQ == total assets` and `totalLiab == ST + LT` holds
on all eight reference documents, every cell, both periods.

## 6f. Rows Outside the Statement Zone (Serasa analysis)

Rows Stage 1 tagged `serasa_analysis` (the Serasa analysis sections after the income statement:
indices, Evolução PL / Imobilizado, the Serasa "FLUXO DE CAIXA", Indicadores) are **not statement
lines**: `label_statements` runs every stage on a view without them, so they are never labelled and never
take part in scope-gate or dedup decisions (rows a stage appends — the derived total — are merged back).
Measured: the SBS labels of every statement row of the 7 Serasa reports are unchanged. Column-header
lines Stage 1 tagged `non_figure_line` (step 4d: «(9 meses)» returned as figures) are excluded the same
way. Lines / labels recovered from the optional OCR words layer (`source` / `label_source:
"ocr_words"`, [`Words_Recovery.md`](Words_Recovery.md)) are ordinary rows here: labelled like the rest.

## 6g. Corpus-audit aliases (2026-11-04)

Excel rows of the 22 reciTAL PDFs with an aggregate caption but no SBS field, fixed in
`build_sbs_mapping.py → EXTRA_ALIASES` (exact dictionary entries; the fuzzy rule rejected them because
an extra token is foreign to the short aliases):

| Field | Added captions (normalized) | Seen in |
|---|---|---|
| `operatingProfitAmount` | `lucro antes do resultado financeiro e do imposto sobre a renda e da contribuicao social` (+ `de renda`), `… e dos impostos`, `lucro / resultado antes das receitas despesas financeiras liquidas e impostos`, `resultado antes do efeito financeiro` | Raízen, Moreno, Romagnole, Guabi, MBP |
| `rawMaterialsAndConsumablesAmount` | `custo das vendas`, `custos das vendas`, `custo indl coml` (Serasa) | Moreno, Romagnole, Guabi, Serasa |
| `grossProfitAmount` | `lucro operacional bruto` | MBP, Nissan |
| `ebitdaAmount` (scope income) | `gir da atividade ebitda`, `ebitda` | 6 Serasa reports |
| `pretaxProfitAmount` | `lucro do exercicio antes do ir e csll` | Documento1 |
| `incomeTaxExpenseRefundAmount` | `impostos sobre o lucro`, `imposto de renda pessoa juridica irpj`, `contribuicao social sobre o lucro liquido cssl` | ALUBAR, DUTRA |
| `operatingCashFlowAmount` | `caixa liquido consumido pelas atividades operacionais` | Nissan |
| `capitalExpenditureAmount` | `aquisicao de ativos imobilizado e intangivel`, `aquisicao de bens do imobilizado e intangivel`, `aquisicao de imobilizado ativo biologico intangivel`, `acrescimo do imobilizado intangivel`, `adicao de imobilizado`, `aquisicao de intangivel` | Raízen, Eztec, Solar, RECONTEC, MBP, 1Q26 |

**Independent audit (12 PDFs, 2026-11-04) — further aliases:**

| Field | Added captions (normalized) | Seen in |
|---|---|---|
| `pretaxProfitAmount` | `lucro antes de irpj e csll`, `lucro antes do irpj e da csll`, `lucro operacional antes do imposto de renda e (da) contribuicao social` | Entrepay, São Salvador |
| `operatingProfitAmount` | `lucro / resultado antes das receitas e despesas financeiras`, `resultado / lucro operacional antes do resultado financeiro` | DIMENSIONAL, Delfia |
| `rawMaterialsAndConsumablesAmount` | `custo das merc e servicos`, `custo(s) das mercadorias e servicos` | Benoit |
| `stockAmount` | `estoques proprio(s)`, `estoque proprio` (the company's own stock; «de terceiros» = goods held for others) | Benoit |
| `depreciationAmount` | `depreciacao amortizacao (e) exaustao`, `depreciacao e amortizacao do imobilizado e intangivel`, `amortizacao do ativo biologico`, `amortizacao de ativos biologicos`, `exaustao do ativo biologico` (bio-assets: user decision) | WEG, ZAMP, São Salvador |
| `capitalExpenditureAmount` | `compras de (ativo) imobilizado e intangivel`, `aquisicao de matrizes de producao`, `aquisicao de mudas de eucaliptos`, `aquisicao de ativo(s) biologico(s)` (bio-assets: user decision) | Unipar, São Salvador |
| `dividendAmount` | `pagamento de dividendos (e) juros s(obre) (o) capital proprio`, `dividendos e juros sobre capital proprio pagos` (JCP is the Brazilian dividend form) | WEG |
| `netCashInvestmentAmount` | `fluxo de caixa liquido (proveniente das utilizado nas) atividades de investimento` | São Salvador |
| `nonTradingIncomeAmount` | `outras receitas (despesas) nao operacionais`, `outras receitas nao operacionais` | DIMENSIONAL |
| `shortTermLiabilitiesAmount` (+ debt shared list) | `emprestimos e outras obrigacoes`, `bancos com emprestimos`, `emprestimos socios (clientes)`, `emprestimos de socios`, `mutuos socios`, `titulos emitidos` (bare `mutuos` NOT: it is the group header above `mutuos socios`) | Entrepay, Benoit, Serasa |
| `financialAssetsAmount` (+ securities shared list) | `tot investimentos`, `aplicacoes financeiras avaliadas a valor justo atraves do resultado` | Serasa, WEG, ZAMP |

Deliberately NOT added: `caixa proveniente(s) das atividades operacionais` (an intermediate line before
interest / taxes paid, not the CFO total — ALUBAR p10, Romagnole p11), `faturamento bruto` /
`receita operacional bruta` (gross, turnover is net), net financial result lines (no SBS field), DVA lines
of the ITR forms (`valor adicionado …`, `custos prods mercs e servs vendidos`). The fuzzy backstop
(`token_sort_ratio` on the full line) also rejects 2 of 4 matches only because of benign words — but the
other 2 rejections are protective (`receitas financeiras liquidas` ≠ financial income), so the matcher is
unchanged and the good cases are explicit aliases. `Mudança no valor justo de ativos biológicos` →
COGS stays (from `labels_OLD.yaml`): with the cost of sales labelled too, the two COGS lines sum to the
gross-profit identity (Moreno p6: 2.726.257 − 137.606 − 2.088.210 = 500.441) — the field value is
their sum, proven by that identity (§6h).

## 6h. One Value per Field — the SBS Contract (`sbs_fields.py`)

`sbs_label` says WHAT a printed line is. The SBS system needs ONE figure per field and period. How
rows sharing a field were combined downstream was unknown; the user's answer (2026-11-04): «it depends
on us». So the pipeline hands over the decision itself — `output["sbs_fields"]` (and the Excel sheet
`sbs_fields`):

```jsonc
"sbs_fields": {
  "perspective": "Consolidado",                      // the Excel's document-level choice
  "fields": {
    "totalFixedAssetsAmount": {
      "statement": "BALANCE_ASSETS_LST",
      "AMOUNT_N": 464188.0, "AMOUNT_N-1": 451425.0,
      "method": {"AMOUNT_N": "sum+identity", "AMOUNT_N-1": "sum+identity"},
      "verified": {"AMOUNT_N": ["totalAssetsAmount = totalCurrentAssetsAmount + totalFixedAssetsAmount"]},
      "rows": [{"page": 9, "label": null, "AMOUNT_N": 178256.0, "used": true}, ...]
    }, ...
  },
  "checks": [{"identity": "...", "period": "AMOUNT_N", "ok": true, "lhs": 1251944.0, "rhs": 1251944.0}, ...]
}
```

**Candidates** = the rows the Excel shows (`selection.passing_rows`, same Consolidado decision) whose
`sbs_label` is the field, in the field's own statements (`allowed_statements`; for a double-home field
the first one that has rows). A field found only OUTSIDE its statements gets no value (a cash-flow
movement «Empréstimos e financiamentos» is not a balance figure; a balance «Salários e encargos» payable
is not the personnel expense).

**Rules per kind:**

| Kind | Fields | Several different candidates → |
|---|---|---|
| TOTAL | totals and results (`total*`, the `financialLiabilities*` totals, equity, turnover, gross / operating / pre-tax / post-tax profit, EBITDA, CFO, CFI) | the printed total of the others; else what an identity proves (one line, or their sum); operating profit = the line before the financial result; else the first printed + `needs_review` |
| CURRENT | `tradeDebtorsAmount`, `tradeCreditorsAmount`, `stockAmount` (SBS: *_CURRENT) | non-current rows not counted (the Stage-3 `sbs_section` decides; only rows WITHOUT a section fall back to «printed after the current total»), then summed |
| components | everything else (cash + securities, tax current + deferred, cost lines, capex, loans…) | the components of another candidate dropped (hierarchy, below); summed, unless one is the printed total of the others; an identity may keep a subset |

**Hierarchy (2026-11-04, audit P4).** A summed field never adds a line to its own component
(`_drop_components`): a candidate whose CVM code descends from another candidate's (ZAMP 1.01.02 +
1.01.02.01.03, WEG 1.01.01 + 1.01.01.01/.02, Unipar 2.01.04 + 2.01.04.01, Eztec / Copasa 2.01.04 +
2.01.04.01), or — without codes — a candidate lying inside a contiguous block (≥ 2 lines, same page, right
below or right above another candidate) whose figures sum to that candidate in every period it prints
(Uniar DISPONÍVEL = CAIXA + BANCOS + APLICAÇÕES; Benoit INTANGÍVEL 465.137,12 = gross 2.065.303,75 −
amortisation 1.600.166,63). TOTAL fields are excluded (the cumulative result lines of a DRE — EBIT inside
the block that sums to pre-tax — are not components). A parent left alone is `total_of_components`; the
dropped lines stay listed as «not counted».

**Value statement (P9).** `incomeTaxExpenseRefundAmount` is labelled in the DRE and the DFC, but valued
from the **DRE only** (`VALUE_STATEMENTS`): the DFC line is an indirect-method add-back with the opposite
sign (São Salvador: +147.177 for a (147.177) expense) or the tax paid.

**Identities** (per period, ABSOLUTE tolerance ± 1 per term — printed figures are rounded):
`totalAssets = totalCurrentAssets + totalFixedAssets`,
`totalAssets = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm + equity`,
`totalLiabilities = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm`,
`grossProfit = turnover ± COGS`, `posttax = pretax ± tax` (costs / taxes printed negative or positive —
user decision: signs «as printed»). When the defaults satisfy an identity they are marked verified; when not,
the fewest fields are switched to the alternatives that satisfy it (a unique minimum only), and those
fields are locked for the following identities. A block nobody printed is 0 when the identity leaves
nothing for it (`zero_by_identity`: Serasa 105563483 has no long-term liabilities; MBP no income tax;
Entrepay no tax line — 0). Two alternatives with the **same figures** are one answer (no «ambiguous» on a
duplicated line).

**Held for sale (user decision 2026-11-05, IFRS 5 / IAS 1.66).** Assets held for sale are current — and so
are the liabilities associated with them. A balance may print the line OUTSIDE both subtotals (Cibra p7:
«Ativo mantido para venda» 30.000 in 2023, between «Total do ativo circulante» and «ATIVO NÃO CIRCULANTE»).
`sbs_fields` then offers «printed current total + that line» as an ALTERNATIVE of `totalCurrentAssetsAmount`
(`HELD_FOR_SALE_FIELD`, method `held_for_sale+identity`; `_is_held_for_sale`: «Ativo(s) (não circulante(s))
mantido(s) / disponível(is) para venda», not the IAS 39 «ativos financeiros disponíveis para venda»), taken
only where the identity proves the line stands outside — total assets = current + the line + non-current,
the printed total alone failing (Cibra 2023: 2.232.467 + 30.000 = 2.262.467). The same on the liabilities
side: «Passivo(s) … associados a ativos mantidos para venda» → `financialLiabilitiesShortTerm`, proved by
assets = ST + LT + equity. A line printed INSIDE the current section leaves the identity holding with the
printed total: nothing changes, nothing is counted twice.

**Review (2026-11-04, P12).** `needs_review` is set on: the first of several different lines, figures on
the next line, and **every field of an identity that fails** in that period (`review_reason`: «AMOUNT_N-1:
… does not hold»). The Excel «Verified by» column lists the identities **per period** when they differ.
EBIT printed only after the financial result (§6i) leaves `operatingProfitAmount` present but EMPTY, with
review and the withdrawn line listed — never the pre-tax-like figure.

**Worked cases (corpus):** Moreno COGS = −137.606 (biological assets) + −2.088.210 (cost of sales) =
−2.225.816, verified by gross profit 500.441 = 2.726.257 − 2.225.816 · MBP non-current assets = two block
subtotals 178.256 + 285.932 = 464.188 = total assets 1.251.944 − current 787.756 · Solar: of two
«Total … não circulante» lines the identity keeps 185.403.989 · ALUBAR tax 27.583 = the printed total of
−10.153 and 37.736 · Copasa COGS: the DRE line (3.02), not its detail line · RECONTEC equity: of two
«Total do patrimônio líquido» the identity keeps the total with non-controlling interests · Serasa
operating profit = «RESULT ATIVIDADE» (before «RESULT FINANCEIRO»), not «RESULT OPERACIONAL».

**Result:** 168 identity checks pass and 0 fail on the 22 reciTAL PDFs (172 / 0 with the words layer);
the remaining unvalued key fields are figures the extraction lost (Guabi liabilities, Serasa 92644954 net
income — both back with the words layer) or lines the report does not print (Nissan has no pre-tax / tax
line). Harness metrics `idOK` / `idBad` / `sbsRv`.

## 6i. Positional rules of the income and cash-flow statements (2026-11-04)

Three rules decide from the **position** of a line, not from its words alone:

1. **Tax lines** (`_stage_tax_components`, PASS 1). Between the pre-tax line (`pretaxProfitAmount`) and the
   next post-tax line on the same page, every line WITH figures whose caption carries a tax word — imposto(s),
   IR, CS, IRPJ, CSLL, CSSL, contribuição, tributo(s), corrente(s), diferido(s) — and no «antes» is an
   income-tax line (`tax_component`); an unnamed line equal to the sum of the tax lines above is their
   printed total (`tax_component_total`). Covers «Despesa de imposto de renda e contribuição social
   correntes / diferidos» + an unnamed total (DIMENSIONAL p2), a total printed ABOVE its components «(-)
   DESPESA COM IMPOSTOS» / «-PROVISÃO P/ IR» / «-PROVISÃO P/ CS» (Benoit p4), bare «Correntes» / «Diferidos»
   (São Salvador p22, Raízen p22, 1Q26 p23). `sbs_fields` then takes the printed total (hierarchy).
2. **EBIT before the financial result** (`_stage_ebit_before_financial`, PASS 2 — user decision
   2026-11-04). An `operatingProfitAmount` label on a line printed AFTER a financial-result line
   (`interestPaid` / `interestIncome` labels or captions «resultado / receitas / despesas financeiras»,
   «RES FINANCEIRO») on the same page is withdrawn (`after_financial_result`, review) — a «Resultado
   operacional» after the financial lines includes them (Uniar, DUTRA, Serasa «RESULT OPERACIONAL»). Coded
   EBIT (CVM 3.05) is never touched.
3. **CAPEX asset lines** (`_stage_capex_asset_lines`, PASS 2, after the scope-gate). In the investing
   section — CVM code 6.02.*, or between the operating-cash-flow total and the investing total on the same
   page — a bare asset class («Imobilizado», «Intangível», «Ativo imobilizado», …) with an OUTFLOW (negative
   N) is the acquisition of that class → `capitalExpenditureAmount` (`investing_asset_line`). WEG 6.02.02 /
   6.02.03 were matched to the balance fields and blocked by the scope-gate before. A positive asset line
   there is a disposal and stays unlabelled.

## 6j. Balance sections (`_compute_sections`)

The section (current / non-current / equity) of every balance row feeds the twins (§6), the empty-label
subtotals (§6b) and the *_CURRENT fields of `sbs_fields` (via `sbs_section`). Sources, in priority order:

1. **CVM code** (2026-11-04): `x.01` current, `x.02` non-current, `2.03` equity — authoritative where present.
2. **Header markers** (forward: `Ativo / Passivo (não) circulante`, `Exigível a longo prazo`, … lead their
   block downward) and **subtotal markers** (backward: `Total do … (não) circulante` close their block
   upward), page-local.
3. **Arithmetic gap-fill** (`_fill_sections_by_arithmetic`): a line equal to the running sum of the lines
   above closes a block (current first, then non-current; equity recognised by content); a section total
   printed after a block subtotal of its own section closes «that block + the lines since» (Serasa GRANJA
   FARIA: TOT INVESTIMENTOS after REALIZAVEL L PRAZO → non-current); a line printed with a dash contributes 0
   (it is not a header).

**SERASA / UPPERCASE shape** (`_is_serasa_shape`: that doc type, no codes, no `Total do …` marker): one
bucket-wide arithmetic pass replaces the page-local markers (the Serasa balance is split across pages and its
`ATIVO CIRCULANTE` is a CLOSING subtotal). **Except a «balancete»** (`_is_total_first`, 2026-11-04): an
accounting-system dump whose every balance page starts with the side's grand total prints each group total
ABOVE its lines, so the header markers lead (Benoit: all sections were empty and a non-current FORNECEDORES
was summed into trade creditors).

## 6k. Provisions and the other SBS decisions of 2026-11-05 (Cibra audit)

Decisions of the user after the independent audit of Cibra 12.2024 (`_audit/AUDIT_REPORT_2026-11-05.md`
§5, observations O1–O7). Do not «fix» them back.

**`provisions`** — «Provisions and reserves for liabilities and charges» (Indian SHORT_TERM_PROVISIONS +
LONG_TERM_PROVISIONS). Only the **IAS 37 liability provisions**: contingencies / lawsuits (tax, civil,
labour), decommissioning / environmental restoration, warranties, onerous contracts — circulante + não
circulante, **summed** (an «other» field of `sbs_fields`, §6h).

| Where | What |
|---|---|
| `EXTRA_ALIASES` | the IAS 37 captions: «Provisões / Provisão para riscos fiscais, cíveis e trabalhistas», «… tributários, cíveis e trabalhistas», «Provisões tributárias, cíveis e trabalhistas», «Provisões fiscais, previdenciárias, trabalhistas e cíveis» (CVM 2.0x.0x.01), «Provisão para riscos (trabalhistas / fiscais / cíveis)», «Provisão para contingências (…)», «Contingências», «Contingências LP» (Serasa), «Provisões para demandas / processos judiciais», «Provisão para riscos e processos judiciais», «Provisões para passivos ambientais e de desativação», «Provisão para desmobilização (de ativos) / recuperação ambiental», «Provisão para / de garantia(s) (de produtos)», «Provisão de contrato oneroso / para contratos onerosos» (36 aliases) |
| `EXTRA_SCOPE` + `STRICT_SCOPE` | `BALANCE_LIABILITIES_LST`; never kept outside it (§5) — the cash-flow add-back is not the balance figure |
| `code_map` | `2.01.06` / `2.02.04` «Provisões» → `provisions` (WEG prints only 2.02.04); their alias-labelled children are not counted twice (`sbs_fields` hierarchy: Eztec 2.02.04 8.849 + 2.02.04.01 + 2.02.04.01.05 → 8.849 once, the children «(not counted)» on the Excel sheet) |
| **Not provisions** (no alias, keep it so) | «Salários, provisões e encargos sociais», «Provisão para férias / 13º» (payroll accruals); «Provisão para IR e CS» (taxes payable); «Provisão para perdas esperadas / PCLD» (asset allowances); «Provisão para perdas em investimentos» (an investee's negative equity); captions that do not say what they provide for — bare «Provisões» (Alvorada: a payroll-like current line), «Provisões diversas», «Outras provisões», «Provisões a pagar». The old anchor PROVISION (PECLD, a cash-flow allowance) is a different concept and is not reused |

Effect on the 22-PDF corpus: `provisions` is valued on 13 documents, nothing else changes (harness diff, both
modes); goldens (N-1 / N) Raízen 1.918.835 / 1.533.431, RECONTEC 14.526 / 24.210 (onerous contract +
warranties + lawsuits), Eztec 30.225 / 31.884; Cibra 65.099 / 65.986.

**`depreciationAmount`** excludes «Amortização de direitos de uso» (right of use), «Amortização de mais valia
de ativos identificados em combinação de negócios» (PPA) and «Amortização dos custos de captação» (part of the
interest). No alias catches them (checked on the corpus); bio-asset amortisation still counts (user
2026-11-04).

**Related-party balances are not loans** (reconfirmed): «Partes relacionadas» in the liabilities stays out of
`short/longTermLiabilitiesAmount`, even when the cash flow shows interest on it (Cibra p11).

**`interestPaidAmount`** = the DRE «Despesas financeiras» (accepted on the auditor's recommendation: a P&L
field, «Interest expenses on borrowed funds», the Indian «finance costs»; the DFC «Juros pagos» is a cash
figure and not always printed). A separately printed «Variação cambial» is not added.

**Held-for-sale assets are current** — `sbs_fields` (§6h, «Held for sale»).

## 7. Row Fields Produced

| Field | Always? | Meaning |
|---|---|---|
| `sbs_label` | when matched | The resolved SBS Field Name, or `null` if unresolved / blocked. |
| `sbs_scope` | when labelled | `in_scope` / `cross_scope` (§5). |
| `sbs_match_method` | when matched | `dict` / `unique` / `scope` / `qualifier` / `code` / `positional` / `unresolved_ambiguous` / `empty_label_arith` / `empty_label_unresolved` / `empty_label_intermediate` / `empty_label_grand_total` / `balance_identity` / `derived` / `tax_component` / `tax_component_total` / `investing_asset_line` (§6b, §6e, §6i). |
| `sbs_match_source` | when matched | Which engine matched: `dict` / `dict_conflict` / `fuzzy` / `arithmetic` (§4, §6e). |
| `sbs_match_score` | when labelled | Score (100 for an exact dictionary hit). |
| `sbs_candidates` | only if unresolved | The competing fields. |
| `sbs_review_reason` | debt / unresolved / scope / struct | Human-readable note. |
| `sbs_blocked_reason` | label withdrawn | `out_of_scope` (§5), `includes_equity` (a non-current subtotal that folded in equity, §6e), `net_after_dividends` (SERASA), `after_financial_result` (an operating result after the financial result, §6i). |
| `sbs_section` | labelled balance rows | `current` / `noncurrent` / `equity` from `_compute_sections` (§6j) — read by `sbs_fields` for the *_CURRENT fields. |
| `sbs_duplicate_of` | duplicate rows | Index of the kept row this one duplicates (§6d); dropped on export. |
| `sbs_derived` | synthesized totals | `true` on the Stage-6 derived `totalLiabilitiesAmount` row (no source line, §6e); always shown on export. |
| `needs_review` | OR-ed true on uncertainty | Never clears an existing flag set by Stage 1/2. |

---

## 8. Rebuilding the Mapping

`sbs_label_mapping.json` is generated by `build_sbs_mapping.py` from:

- **`labels_OLD.yaml`** — old primary codes + their Portuguese `aliases`.
- **`SBS_fields_mapping_with_INDIAN_labels.xlsx`** — the new SBS `Field Name` list. Its
  **`Indian Labels`** column equals the old primary slug, and is the **join key** between the two
  files.

```bash
python3.13 build_sbs_mapping.py        # rewrites sbs_label_mapping.json
```

The script validates that every joined field exists in the xlsx (and vice-versa), de-duplicates
aliases by normalized form, derives each field's `statement_scope` from its source section, splits
the reverse index into clean vs conflicting, and prints the alias conflicts + orphaned anchors. After
rebuilding, re-run Stage 3 so outputs pick up the new aliases.

> Six fields carry a `needs_review` note in the mapping — these are **semantic overrides** against the
> Indian-label hint (e.g. `posttaxProfitAmount` ← NET_INCOME rather than the literal RETAINED_PROFIT;
> `netCashInvestmentAmount` ← CFI although the xlsx says "No direct match"). Confirm the intended SBS
> semantics before relying on them.

---

## 9. Results on the Reference Samples

| Document | Type | Rows matched | in / cross scope | needs_review |
|---|---|---|---|---|
| `structured_output.json` | annual DFP | 110 | 57 / 50 | 3 |
| `structured_output_sample2.json` | 2-column | 42 | 26 / 16 | 0 |
| `structured_output_itr.json` | ITR | 66 | 53 / 13 | 0 |
| `structured_output_provider.json` | uppercase | 27 | 24 / 1 | 0 |

The low *overall* match rate (rows matched ≪ rows total) is expected and correct: the lexicon covers
~43 key statement lines, while the structured output also contains hundreds of note / subtotal rows
the schema has no field for. The three `needs_review` rows on the annual sample are unqualified loan
captions on NOTE/JUNK pages with no code and no section — correctly left for a human rather than
guessed.

> The counts above predate the audit fixes of 2026-11-04 (user liability mapping, new aliases, dedup by
> code, sections by code). The current figures are those of the regression harness
> (`harness/run.py snapshot`) and of the regenerated goldens; on the 12 audit PDFs every `sbs_fields` value
> matched the auditors' references after the fixes.

---

## 10. How to Run

Stage 3 runs automatically inside `structure_ocr.py`. To apply or re-apply it standalone:

```bash
python3.13 sbs_labeler.py my_output.json --dry-run   # report only, no write
python3.13 sbs_labeler.py my_output.json             # overwrite in place
python3.13 sbs_labeler.py my_output.json -o out.json
```

The dry-run prints matched/unmatched counts, the in/cross-scope split, resolution methods, and
per-field totals. A run that writes also recomputes `sbs_fields` (the values depend on the labels).
Never run it in place on a `structured_output*.json` golden of the harness. See the RUNBOOK §8 for
operational detail.

---

## 11. Design Decisions & Open Items

- **Engine = dict-first, then `anchor_match`.** An exact `sbs_normalize` lookup carries ~86% of labels;
  the fuzzy fallback (shared with Stage 2, opted into `token_fuzzy`) covers the rest. Code-first precedes
  both on filings with CVM codes.
- **Scope is a FILTER, not a tie-breaker** (the scope-gate, §5). An out-of-statement match is BLOCKED when
  the field is captured in-home elsewhere, else RECOVERED + flagged. This REPLACED the earlier
  tie-breaker-only design; double-home fields (depreciation, dividend, post-tax, income-tax) are exempt.
- **Never guess twins.** Both ambiguity groups (debt, securities) are resolved only from an explicit
  qualifier, code, or layout; otherwise flagged. The groups are config, not code — new pairs need only
  a `sbs_label_mapping.json` entry.
- **`totalLiabilitiesAmount` is arithmetic, not text** (§6e). It is liabilities-only (= total current +
  total non-current liabilities, the two `financialLiabilities*` totals), resolved from the balance
  identity — NOT aliased to the Portuguese grand-total bottom line, which is liabilities + equity. Do not
  add grand-total aliases back.
- **Liability mapping pinned by the user (2026-11-04).** `shortTermLiabilitiesAmount` /
  `longTermLiabilitiesAmount` = credits and loans, `financialLiabilities*` = section totals (§3). Never
  revert to the xlsx Description text.
- **Position beats words where the words are ambiguous** (§6i): tax lines between pre-tax and post-tax,
  EBIT only before the financial result (user decision), bare asset classes in the investing section =
  CAPEX. Bio-asset amortisation / purchases count in depreciation / CAPEX (user decision).
- **The pipeline decides how lines combine, with a proof** (§6h): hierarchy (codes or block sums),
  identities, review on failure. Cost signs stay «as printed» (user decision).
- **Not counted as loans:** captions without loan wording (Dom «Partes relacionadas» in non-current
  liabilities, Serasa «C/C EMP INVESTIDAS/SOCIOS»): the caption cannot tell a loan from a current account.
  Reconfirmed by the user 2026-11-05 (Cibra: related-party liabilities that bear interest stay out too).
- **User decisions 2026-11-05 (Cibra audit, §6k):** `provisions` = IAS 37 liability provisions (strict
  scope); `depreciationAmount` excludes right-of-use amortisation, PPA («mais valia») amortisation and the
  amortisation of borrowing costs; `interestPaidAmount` = the DRE «Despesas financeiras» (not the DFC
  «Juros pagos»); held-for-sale assets / liabilities are current (§6h).
- **Open — new fields.** 10 SBS fields have no aliases (goodwill, net group profit, pension funds,
  operating provisions, the 5 metadata fields, and `totalLiabilitiesAmount` on purpose); the first four
  are matched by text once aliases are added.
- **Open — semantic overrides.** 6 fields need a business sign-off (§8).
- **Open — ITR wording.** Quarterly ITR uses different phrasings than annual DFP; some balance/income
  lines under-match until aliases are extended (shared open item with Stage 2).

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **SBS Field Name** | The standardised target identifier (camelCase, e.g. `turnoverAmount`). |
| **alias** | A Portuguese caption variant that maps to an SBS field. |
| **old primary** | The legacy SNAKE_CASE code (e.g. `TURNOVER`) that bridged old aliases to SBS via the xlsx `Indian Labels` column. |
| **scope** | The statement bucket a field's rows belong to; used to disambiguate. |
| **in_scope / cross_scope** | Whether a matched field's home statement equals the row's statement. |
| **anchor_match** | The shared token-coverage + fuzzy matching rule in `anchors_lib`. |

---

*Document for the `br_ocr_json_processing` project, Stage 3. Reproducible by running
`python3.13 sbs_labeler.py <structured_output>.json --dry-run`. See
[`Primary_Statement_Classification.md`](Primary_Statement_Classification.md) for Stage 2 and
[`Excel_Export.md`](Excel_Export.md) for Stage 4.*
