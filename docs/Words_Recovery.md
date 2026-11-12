# OCR Words Recovery (on by default)

**Recovering what the provider extraction lost, from the reciTAL OCR words layer**

| | |
|---|---|
| **Component** | `words_recovery.py` (called by `structure_ocr.process(data, words=...)`) |
| **Input** | the Stage-1 rows + the reciTAL words layer (`GET /files/{id}/words/`: every word of every page with its box) |
| **Output** | the same structured JSON, with recovered cells / lines / labels flagged `source` / `label_source: "ocr_words"` |
| **Status** | **ON by default** (user decision 2026-11-12), pluggable: without the layer the pipeline runs the words-free fallback, exactly. reciTAL confirmed the layer in production on 2026-11-05 (§2a). |
| **Related** | [`OCR_Table_Reconstruction.md`](OCR_Table_Reconstruction.md) (Stage 1), [`SBS_Labelling.md`](SBS_Labelling.md) |

---

## 1. Why

The extraction (values + groups) misses **~8% of the figures printed in the statement tables**:
1,024 of 12,568 on the 22-PDF corpus of agent `Brazil_OCR202606_V4`, up to 19% of a page. The words
layer has every one of them, with boxes **identical** to the extracted value boxes. Three kinds of loss:

| Loss | Example (corpus) | Effect without recovery |
|---|---|---|
| **CELL** — the row is there, one figure is not | Raízen p26: Consolidado closing cash 21.721.393 / 14.819.906; ALUBAR p10: the Consolidado column on ~17 lines | the Excel row is empty or missing |
| **LINE** — the whole printed line is missing | Guabi p1: the four liability subtotals 177.359 / 20.973 / 192.927 / 391.259 | no ST / LT liabilities, the balance does not close |
| **LABEL** — the figures are there, the name is not | RECONTEC p13: «Lucro líquido antes do imposto de renda e contribuição social» 63.964 / 65.095 | an unnamed row, no SBS field |
| **COLUMN** of a continued page | Serasa 92644954 p4: the DRE tail kept only 2023, the 2024 column (net income 3.213) is in the words only | latest-year figures missing |
| **METADATA** | Serasa: the period length only in «Observações» («A DEMONSTRACAO DE RESULTADOS COMPREENDE 09 MES(ES)») | DURATION empty |

## 2. On by default, pluggable by design (user decisions 2026-11-12 / 2026-11-04)

Nothing in the pipeline depends on the step — the layer can still be missing (fetch failed, already
deleted after validation), and then the fallback is the words-free output, exactly:

| Call | Result |
|---|---|
| `structure_ocr.process(data, words=words)` (**production mode**) | the step runs once, at the end of Stage 1 (after the wrapped-label merge, before the Serasa zone and Stage 2/3) |
| `structure_ocr.process(data)` (fallback: no layer) | the step never runs; output exactly the words-free one |
| `words=[]`, or `words_recovery.py` deleted | words-free output (guarded import) |

`harness/run.py selftest` asserts the three equalities. The entry points use the layer by default:
`python3.13 structure_ocr.py <id>_raw.json -o <out.json>` takes `<id>_words.json` from the same folder
(`--words <file>` names another one, `--no-words` runs the fallback); the Streamlit app takes it as the
second upload and warns when it is missing; `harness/run.py snapshot <tag>` runs with it (`--no-words` =
the fallback); `harness/audit_run.py` writes both (`out_words` = production, `out_nowords` = fallback).

### 2a. In production (reciTAL, Jiaqi Chang, 2026-11-05)

| Question | reciTAL's answer | What it means for the integration |
|---|---|---|
| `GET /production/files/{id}/words/` for every file? | Yes | fetch it for every file, with values / groups |
| Deletion (`post_processing_deletion` is on)? | The words are deleted with the other results, after validation in the review screen | fetch them right after the extraction (status `to_verify`), before any validation — or disable the automatic deletion and delete the document by API at the end |
| In the callback? Limits? | Not in the callback: one extra call per file; only the whole file for now (per-page retrieval = a reciTAL ticket); rate limits not a concern | one GET per file: ≈ 61 KB a page on the corpus (up to 77), a 175-page report = 12.3 MB |
| Same coordinates as values / groups? | Yes (normalized 0-1, same page numbering) | nothing to convert (checked on the corpus: word boxes = value boxes) |

If the GET fails or returns nothing, call `process(data)` — the fallback — rather than failing the file.

## 3. What it does

1. **Page grid.** Columns = right edges (`x_max`) of the extracted figures, per page and per physical
   panel (side-by-side balances), ≥ 3 figures, gap 0.02; each column's **period** = its figures' period
   (purity ≥ 0.9, after the Stage-1 grid correction). A column of 1-2 digit integers is a note-reference
   column, not a figure column.
   Since 2026-11-05 the columns of the Stage-1 **header zones** (step 2c', `recover(..., zones=)`) join
   the grid with the zone's period, whatever their count. Background: Cibra p7, audit 2026-11-05 M3 —
   the Consolidado 2024 column of the liabilities held 1–2 extracted figures, so it stayed `skip_off_grid`
   and was never recovered. With the zone column, all 15–16 cells come back, verified. A recovered figure
   in a zone column takes the zone's period and perspective in Stage-1 Step 5. The zones never depend on
   the words layer: without words nothing changes.
2. **Candidates.** Every word of the page that is a figure (balanced parentheses; `(9` of «(9 meses)» is
   not), inside the table zone, in a column of the grid, NOT at the spot of an extracted figure (centre of
   one box inside the other) and NOT inside an extracted name.
3. **CELL.** The candidate's visual line (page-pitch tolerance) carries a row of its panel → the figure
   joins that row, with the column's period; never into a column the row already fills.
4. **LINE.** Candidates on a line without any row → one new row per line (no two figures of one column);
   name = the free words left of its first figure; statement = the named neighbour above (same rule as
   Stage-1 orphans). A line with one figure needs a name and ≥ 3 digits («Acima de 5 anos» is not a line).
5. **LABEL.** An unnamed row whose name is printed on its line takes it. A **wrapped name** is completed: a
   fragment that continues the line above (lowercase start / bare parenthetical «(Notas 11.2 e 17.4)»)
   takes the figure-less name line above (absorbed, like Stage-1 step 4b) or the free words above; a name
   ending with a connector takes the lowercase line below.
6. **COLUMN borrowing.** A statement continued on the next page with one column lost there entirely: the
   column is taken from the adjacent page when both print the same statement and EVERY column of the page
   is a column of the neighbour at the same x with the same period.
7. **METADATA.** `DURATION` missing → Serasa «COMPREENDE NN MES(ES)» → `normalized_months`.

### Guards (each counted in `report["words"]`)

| Counter | Meaning |
|---|---|
| `skip_off_grid` | a figure outside every column of the page (note references, dates, page numbers) |
| `skip_column_without_period` | the column's figures disagree on the period |
| `skip_small_in_large_column` | 1 digit, or a bare 2-digit number, in a column of large figures (a dash read as `1`, a footnote `(2)`, a day `31`) — `(27)` / `-45` are kept |
| `skip_column_filled` | the row already has a figure in that column |
| `skip_lone_figure` | one unnamed figure on a line: too little evidence |

## 4. Provenance and presentation

* cell `source: "ocr_words"`, row `source: "ocr_words"`, label `label_source: "ocr_words"`
  (+ `label_parts`, `label_wrapped: "W"` for a completed wrapped name), metadata `source: "ocr_words"`.
* **Excel / Streamlit**: recovered figures and labels in *italic dark gold on light yellow*, with a legend
  line under the sheet; the Streamlit label gets a `WORDS` badge. The selection (`passing_rows`) is the
  same — a recovered figure is a figure.
* **Conservation** (harness `cons`) counts extracted cells only; recovered ones are `wCell` / `wRow` /
  `wLab`.
* **Later Stage-1 steps treat recovered cells like extracted ones** (2026-11-04): the debit / credit suffix
  signing and the sign / dash proof (`sign_proof.py`, step 5b) run after the recovery, so a recovered
  figure can complete a subtotal that proves a repair (MBP p10 in words mode). The words layer never
  changes the repair rules — a lost minus is lost in both layers, so it does not help there.
* **Note pages.** Recovered figures on note pages do not reach the Excel of an ITR any more (only the CVM
  form pages are exported, audit P1): before, WEG / Unipar / Dom note figures recovered from the words
  joined the interest, COGS and tax fields in words mode.

## 5. Results on the corpus (22 PDFs, harness `s6p` → `s6w`)

| Metric | without words | with words |
|---|---|---|
| figures on paper missing from the output (`missW`) | 1,289 | 255 |
| recovered cells / new lines / labels | — | 1,116 / 148 / 627 |
| golden cells (eye-verified) | 18 / 25 | **25 / 25** (all 7 `fix: words`) |
| Excel rows balance / income / cash flow | 913 / 459 / 445 | 920 / 461 / 467 |
| conservation diff / Serasa analysis in Excel | 0 / 0 | 0 / 0 |

Visible in the Excel: Raízen p26 (11 cash-flow cells incl. closing cash), ALUBAR p10 (Consolidado column,
19 rows), Guabi p1 (liability subtotals → ST / LT / equity labelled, derived total liabilities 198.332),
Serasa 92644954 p4 (2024 column of the DRE tail: net income 3.213), RECONTEC / Romagnole / 1Q26 labels.

## 6. Limits

* A dash the OCR reads as `1` is `1` in BOTH layers (Raízen p21, Moreno p5): words cannot tell; only the
  subtotal arithmetic can (MBP p10: 3.549 = the sum without the `1`).
* On NOTE pages the extraction's own period tags are often meaningless (book / fair value, cost /
  depreciation tables); recovered cells there inherit the column's tag. NOTE pages are not exported for
  DFP / TWO_COL documents.
* A column lost on a page whose neighbours do not print the same layout is not recovered.
* Stage 2 (page classification) still reads the extraction only.

## 7. Removing it

Delete `words_recovery.py`. `structure_ocr` imports it under `try` and skips the step; a words layer given
(automatically or with `--words`) then only records `report["words"] = {"skipped_no_module": 1}`.
