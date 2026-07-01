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
3. [The Mapping File (`sbs_label_mapping.json`)](#3-the-mapping-file-sbs_label_mappingjson) *(business + developers)*
4. [How a Row Gets Its Label](#4-how-a-row-gets-its-label) *(developers)*
5. [Statement Scope & the `sbs_scope` Flag](#5-statement-scope--the-sbs_scope-flag)
6. [The One Real Ambiguity: Current vs Non-Current Debt](#6-the-one-real-ambiguity-current-vs-non-current-debt) *(developers)*
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

Stage 3 is a **six-stage pipeline** (each stage a small, independently-tested function):

1. **Match** — an **exact dictionary** lookup first (it carries ~86 % of all labels and never drifts),
   falling back to the fuzzy-anchor engine — the **same** engine Stage 2 uses, so the two can never
   disagree about what a caption says.
2. **Twin / section** — resolve the one genuinely ambiguous case (short- vs long-term debt, current vs
   non-current securities) from the row's qualifier, hierarchical code, or position; **never guessed**.
3. **Empty-label** — recover an SBS field for nameless balance subtotals (the OCR dropped the caption)
   arithmetically.
4. **Scope-gate** — a field is matched **only in its home statement(s)**: an income-statement field
   must not surface in the balance, and vice-versa. An out-of-statement match is blocked (kept only if
   the figure appears nowhere in-home, then flagged).
5. **Structural validation** — flag (never strip) an income line whose canonical order is violated
   (e.g. net income above turnover) or a balance row on the wrong side.
6. **Balance liability totals** — resolve `totalLiabilitiesAmount` **arithmetically** from the balance
   identity (it is liabilities only, not the grand total) and fix templates that fold equity into the
   non-current subtotal (§6e).
7. **Dedup** — tag an exact duplicate row (same caption + perspective + value on the same page) so the
   Excel export drops it.

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
normalized captions they share (see §6).

A separate top-level key, **`ambiguity_groups`**, declares the current/non-current twin pairs and the
normalized captions they share (see §6).

> **Coverage today:** 29 of 43 SBS fields carry aliases. The remaining brand-new fields
> (`goodwillAmount`, `ebitdaAmount`, `totalLiabilitiesAmount`, …) have none yet — extend
> `labels_OLD.yaml` (or `EXTRA_ALIASES` in `build_sbs_mapping.py`) and rebuild.

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
| **debt** | `financialLiabilitiesShortTerm` | `financialLiabilitiesLongTerm` | `empréstimos e financiamentos`, `debêntures` |
| **securities** | `cashAndSecuritiesAmount` | `financialAssetsAmount` | `títulos e valores mobiliários`, `aplicações financeiras`, `investimentos` |

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
   `current liab → shortTermLiabilitiesAmount`, `non-current liab → longTermLiabilitiesAmount`,
   `equity → shareholdersFundsAmount` (the five entries of `_SECTION_TOTAL_FIELD` — a `label_null` row
   can only ever become one of these section totals, never a line-item field).
4. **Which section** — identified in priority order:
   - **Content (liabilities)** — if the feeding detail rows are **equity** items (`_EQUITY_KEYWORDS`:
     `capital social`, `reservas`, `lucros/prejuízos acumulados`, `patrimônio líquido`, `ações em
     tesouraria`, **`resultado do exercício`**, **`ajustes de exercícios anteriores`**, **`distribuição
     de lucros`**, dividends, …), the subtotal is the equity total → `shareholdersFundsAmount`. ⚠️ The
     liabilities side has **three** blocks (current / non-current / **equity**), so a pure
     current-vs-non-current rule would wrongly tag the equity total as `longTermLiabilitiesAmount` —
     content detection prevents that. ⚠️ The content test demands **ALL** feeding rows be equity, so
     the keyword list must include the period-result/appropriation lines that live inside equity
     (`resultado do exercício`, `ajustes de exercícios anteriores`, `distribuição de lucros`). A single
     missing caption breaks the `all()` test. Note `_EQUITY_KEYWORDS` feeds **two** consumers: this
     content check **and** `_fill_sections_by_arithmetic`, which decides whether a closed block is
     `equity` vs `noncurrent`. So a missing caption fails it in **both**: the block is sectioned
     `noncurrent`, and the content override also misses — the row then resolves via the (now wrong)
     positional section to `longTermLiabilitiesAmount`, not via the ordinal fallback. That was the
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

Worked example (`complicated_empty_labels_sample.json`): the nameless asset subtotals `167.392` and
`23.561` resolve to `totalCurrentAssetsAmount` / `totalFixedAssetsAmount`; the three nameless
liability subtotals resolve to `shortTermLiabilitiesAmount` (`169.135`), `longTermLiabilitiesAmount`
(`12.652`) and **`shareholdersFundsAmount`** (`16.344` = `Capital Social` + `Lucros/Prejuízos
Acumulados`, recognised as equity by content). The asset `7.179` stays unresolved (doesn't sum — OCR
shift). Second worked example (`complicated_sample_2`, a side-by-side balance): once Stage-1's x-band
gate (OCR_Table_Reconstruction §7.3a) stops gluing cross-statement sums, the five nameless subtotals
resolve to `totalCurrentAssetsAmount` (`70.076.711,53`) / `totalFixedAssetsAmount` (`4.597.550,90`)
and `shortTermLiabilitiesAmount` (`22.168.649,61`) / `longTermLiabilitiesAmount` (`2.739.236,61`) /
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

1. **Relabel-or-derive.** A printed `Total do passivo`-type line (caption in `_TOTAL_LIAB_CAPS`) is
   relabelled `totalLiabilitiesAmount` **iff** its value equals `shortTerm + longTerm` (the true
   liabilities total), never the grand total. If no such line is printed — the common case; most
   templates print only the grand total — a single **derived** row is synthesized (`sbs_match_method =
   derived`, `sbs_derived = true`, label `"Total liabilities (derived = current + non-current)"`) carrying
   `ST + LT` for every cell, and appended to the liabilities bucket. The exporter always shows derived
   rows (they have no source page, so `row_passes` bypasses the page-class filter for them).
2. **Equity-in-long-term fix.** Some templates (notably **Serasa**) print a `PASSIVO NÃO CIRCULANTE`
   subtotal that **already includes equity** (`EXIGÍVEL A LONGO PRAZO + PATRIMÔNIO LÍQUIDO`). The section
   logic then labels that inflated figure `longTermLiabilitiesAmount` and leaves the *real* long-term line
   (`EXIGÍVEL L PRAZO`) unlabelled — double-counting equity and breaking the identity by exactly the equity
   amount. When the labelled long-term value equals `assets − short-term` **and** a non-zero equity exists
   **and** it differs from `assets − short-term − equity`, the long-term label is on the wrong
   (equity-inclusive) line: it is **demoted** (`sbs_blocked_reason = includes_equity`, `needs_review`) and
   the unlabelled line whose value equals the true long-term (`assets − ST − EQ`) is **promoted**
   (`sbs_match_method = balance_identity`).

   > **Why the trigger can never misfire.** A correct balance has `LT == assets − ST − EQ`, so
   > `LT == assets − ST` can only hold when `EQ == 0`. Requiring `EQ ≠ 0` makes the condition
   > mathematically exclusive to the equity-folded case — verified as 0 false fires on all 8 goldens.

The stage runs **after** the scope-gate (so ST/LT/EQ labels are stable) and **before** dedup. It is a
self-gating no-op on a balance that is already internally consistent (DFP/ITR/2-col long-term lines are
untouched). `label_statements` strips any prior `sbs_derived` rows on entry, so re-labelling is
idempotent. After this stage the identity `ST + LT + EQ == total assets` and `totalLiab == ST + LT` holds
on all eight reference documents, every cell, both periods.

## 7. Row Fields Produced

| Field | Always? | Meaning |
|---|---|---|
| `sbs_label` | when matched | The resolved SBS Field Name, or `null` if unresolved / blocked. |
| `sbs_scope` | when labelled | `in_scope` / `cross_scope` (§5). |
| `sbs_match_method` | when matched | `dict` / `unique` / `scope` / `qualifier` / `code` / `positional` / `unresolved_ambiguous` / `empty_label_arith` / `empty_label_unresolved` / `balance_identity` / `derived` (§6e). |
| `sbs_match_source` | when matched | Which engine matched: `dict` / `dict_conflict` / `fuzzy` / `arithmetic` (§4, §6e). |
| `sbs_match_score` | when labelled | Score (100 for an exact dictionary hit). |
| `sbs_candidates` | only if unresolved | The competing fields. |
| `sbs_review_reason` | debt / unresolved / scope / struct | Human-readable note. |
| `sbs_blocked_reason` | scope-gate block | `out_of_scope` (§5) or `includes_equity` (a non-current subtotal that folded in equity, §6e). |
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
the schema has no field for. The three `needs_review` rows on the annual sample are unqualified debt
captions on NOTE/JUNK pages with no code and no section — correctly left for a human rather than
guessed.

---

## 10. How to Run

Stage 3 runs automatically inside `structure_ocr.py`. To apply or re-apply it standalone:

```bash
python3.13 sbs_labeler.py structured_output.json --dry-run   # report only, no write
python3.13 sbs_labeler.py structured_output.json             # overwrite in place
python3.13 sbs_labeler.py structured_output.json -o out.json
```

The dry-run prints matched/unmatched counts, the in/cross-scope split, resolution methods, and
per-field totals. See the RUNBOOK §8 for operational detail.

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
- **`totalLiabilitiesAmount` is arithmetic, not text** (§6e). It is liabilities-only (= short-term +
  long-term), resolved from the balance identity — NOT aliased to the Portuguese grand-total bottom line,
  which is liabilities + equity. Do not add grand-total aliases back.
- **Open — new fields.** 11 SBS fields have no aliases yet (goodwill, EBITDA, …); these are matched by
  text once aliases are added.
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
