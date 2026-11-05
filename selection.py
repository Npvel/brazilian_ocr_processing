# -*- coding: utf-8 -*-
"""
selection.py — WHICH rows and WHICH figures a statement export shows. Standard library only.

The single source of truth for row selection, shared by the Excel exporter (export_excel), the
Streamlit screen (app), the harness and the SBS field values (sbs_fields, computed in Stage 3):
they can never disagree. Moved out of export_excel (which needs openpyxl) so Stages 1-3 can use it
without any third-party package.

    to_number          Brazilian amount string -> float (OCR bracket repair, % / ratios skipped)
    consolidado_filter the document-level Consolidado-only decision (consolidated if presented,
                       else the company's own figures — user rule 2026-11-04)
    form_pages         ITR: only the pages of the CVM forms (pages carrying account codes) —
                       note / commentary pages are never exported (audit 2026-11-04, P1)
    pick_cells / pick_amounts   the (N-1, N) cells / numbers written for a row
    row_passes / passing_rows   the rows written for a statement group
    review_reason      why a row carries needs_review (the «Review» column of the statement sheets)
"""
import re

from anchors_lib import parse_amount
from doc_type import detect_doc_type, consolidado_only, primary_only

_consolidado_only = consolidado_only
_primary_only = primary_only


def resolve_doc_type(output):
    """The document type for an output dict: the Stage-1-stamped `document_type`
    if present, else recomputed from statements + metadata (legacy fallback)."""
    dtype = output.get("document_type")
    if dtype:
        return dtype
    return detect_doc_type(output.get("statements", {}), output.get("metadata"))


# --------------------------------------------------------------------------- #
# Amount parsing (tolerant of OCR-mangled brackets)
# --------------------------------------------------------------------------- #
_PCT_RE = re.compile(r"%")


def to_number(value):
    """
    Brazilian amount string -> float, or None if not a clean figure.
    Normalizes OCR bracket artifacts '[1.234]' / '(2.138]' to '(2.138)'.
    Skips percentages and ratio-like tokens.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or _PCT_RE.search(s):
        return None
    # normalize stray brackets used as parentheses by the OCR
    if ("[" in s) or ("]" in s):
        s = s.replace("[", "(").replace("]", ")")
        if s.startswith("(") and not s.endswith(")"):
            s = s + ")"
        if s.endswith(")") and not s.startswith("("):
            s = "(" + s
    return parse_amount(s)


def form_pages(statements, doc_type):
    """ITR (CVM forms): the pages that carry CVM account codes, or None (no restriction) for any
    other document type.

    Every line of a CVM form (BP / DRE / DFC, Individual and Consolidado) carries its account code;
    note and commentary pages carry none — audit 2026-11-04: WEG 160/160 primary rows coded vs
    0/252 note rows, Unipar 172/172 vs 0/130. ITR ignores page_class (Stage 2 under-detects the
    forms) and the notes' own «Consolidado» headers pass the Consolidado filter, so without this
    restriction note tables (segments, roll-forwards, Controladora figures under a Consolidado
    header) reached the Excel and sbs_fields. The restriction is by PAGE, not by row: a code-less
    line of a form page (one whose code the OCR missed) is still a form line."""
    if doc_type != "ITR":
        return None
    return frozenset(r.get("page_nb") for rows in statements.values() for r in rows if r.get("code"))


def document_has_consolidado(statements, doc_type):
    """
    Whether the document PRESENTS consolidated figures on the rows the export considers.

    Business rule (user, 2026-11-04): when a document presents consolidated AND company
    figures, the consolidated ones are taken; when it presents NO consolidated figures, the
    company's own (Controladora / Individual) are taken. Decided for the WHOLE document —
    never per row (a row whose consolidated cell the OCR lost must not silently fall back to
    the company's figure: a different number, e.g. closing cash 6.886.721 vs 21.721.393).
    Before this rule a company-only DFP / ITR produced an EMPTY workbook.
    """
    fp = form_pages(statements, doc_type)
    for rows in statements.values():
        bypass = is_small_span(rows)
        for r in rows:
            if (r.get("sbs_derived") or r.get("serasa_analysis") or r.get("non_figure_line")
                    or r.get("other_statement")):
                continue
            if fp is not None and r.get("page_nb") not in fp:
                continue
            if _primary_only(doc_type) and not bypass \
                    and not str(r.get("page_class", "")).startswith("PRIMARY"):
                continue
            for c in r.get("columns", []):
                if (c.get("account_type") or "").lower().startswith("consolidad") \
                        and to_number(c.get("value")) is not None:
                    return True
    return False


def consolidado_filter(statements, doc_type):
    """The Consolidado-only switch actually applied to this document (see document_has_consolidado)."""
    return _consolidado_only(doc_type) and document_has_consolidado(statements, doc_type)


def pick_cells(columns, doc_type, cons_only=None):
    """
    From a row's columns, return the cells written as (cell_n_minus_1, cell_n) — the column
    dicts, or None. Honors the Consolidado-only rule; maps by `period`, not position
    (provider dumps reverse the column order). `cons_only` = the document-level
    decision (consolidado_filter); when omitted, the doc-type default applies.
    """
    if cons_only is None:
        cons_only = _consolidado_only(doc_type)
    n_cell = nm1_cell = None
    for c in columns:
        at = (c.get("account_type") or "").lower()
        if cons_only and not at.startswith("consolidad"):
            continue
        if to_number(c.get("value")) is None:
            continue
        period = c.get("period")
        if period == "AMOUNT_N" and n_cell is None:
            n_cell = c
        elif period == "AMOUNT_N-1" and nm1_cell is None:
            nm1_cell = c
    return nm1_cell, n_cell


def pick_amounts(columns, doc_type, cons_only=None):
    """(amount_n_minus_1, amount_n) as numbers — the values of pick_cells."""
    nm1_cell, n_cell = pick_cells(columns, doc_type, cons_only)
    return (to_number(nm1_cell["value"]) if nm1_cell else None,
            to_number(n_cell["value"]) if n_cell else None)


_EPS_CAPTION = re.compile(r"(por acao|por acoes|\blpa\b|^lucro basico|^lucro diluido|^basico e diluido|"
                          r"^resultado basico|^resultado diluido|^prejuizo basico|^prejuizo diluido)")
_SHARE_CLASSES = {"on", "pn", "pna", "pnb", "pnc", "unit", "units", "acoes ordinarias", "acoes preferenciais"}


def is_per_share(row):
    """An earnings-per-share line (R$ per share, NOT thousands): CVM code 3.99.*, an EPS caption
    («Lucro por ação», «Básico e diluído»), or a share-class line («ON» / «PN») carrying decimals.
    The workbook shows it with its decimals and tagged «(per share)» — the thousands format printed
    2,138 as «2» among amounts in R$ thousand (audit 2026-11-04, P13)."""
    from anchors_lib import normalize
    if str(row.get("code") or "").startswith("3.99"):
        return True
    lab = normalize(row.get("label") or "")
    if lab and _EPS_CAPTION.search(lab):
        return True
    if lab in _SHARE_CLASSES:
        vals = [to_number(c.get("value")) for c in row.get("columns", [])]
        return any(v is not None and v != int(v) and abs(v) < 1000 for v in vals)
    return False


def is_recovered(item):
    """A cell / row / label recovered from the OCR words layer (optional words_recovery step)."""
    return bool(item) and (item.get("source") == "ocr_words" or item.get("label_source") == "ocr_words")


def is_small_span(rows):
    """
    True if a statement group's LABELLED rows occupy at most a 1- or 2-page
    consecutive block. In that case there is no risk of confusing a primary
    statement with a segment copy / note repeat, so the page_class filter must
    NOT drop these rows — even if Stage-2 mislabelled the page (e.g. a balance
    whose 'TOTAL DO PASSIVO' wording broke anchor completeness -> NOTE).

    A short, self-contained statement is taken in full regardless of page_class;
    only multi-page groups keep the PRIMARY-only filter to weed out repeats.
    """
    pages = sorted({r.get("page_nb") for r in rows
                    if r.get("label") and r.get("page_nb") is not None})
    if len(pages) <= 1:
        return True
    if len(pages) == 2 and pages[1] == pages[0] + 1:
        return True
    return False


def row_passes(row, doc_type, bypass_primary=False, cons_only=None, form_pages=None):
    """
    Whether this row should appear. A row qualifies on its AMOUNTS, not its
    label: `label_null` rows (subtotals whose name the OCR dropped, e.g. a
    'Total do ativo circulante' that arithmetically sums its section) carry real
    ONLY_AMOUNT figures and MUST be kept — they are downstream SBS-label targets.
    `form_pages` (ITR only, see form_pages()): rows of pages outside it are notes.
    """
    # Stage-3 (точка 6) tags an exact duplicate (same name + account_type +
    # value, same page) with sbs_duplicate_of. Tag-in-JSON, drop-on-export:
    # this is the presentation layer, so the duplicate row is removed here.
    if row.get("sbs_duplicate_of") is not None:
        return False
    # Stage 1 tags the Serasa analysis sections (indices, evolucao, Serasa "fluxo de
    # caixa", indicadores): Serasa = only the balance and the income statement.
    if row.get("serasa_analysis"):
        return False
    # Stage 1 (step 4d) tags a column-header line the provider returned as figures
    # ('(9 meses)' -> '(9', 'meses)'): not a statement line.
    if row.get("non_figure_line"):
        return False
    # An ITR row of another form (DVA / DMPL / DRA, by its CVM code — Stage 1 step 4e).
    if row.get("other_statement"):
        return False
    # A Stage-3 synthesized total (the derived totalLiabilitiesAmount line) has no
    # source page, so the page_class filter would always drop it. It is a real
    # computed figure that must always show — bypass the primary-only filter.
    if row.get("sbs_derived"):
        nm1, n = pick_amounts(row.get("columns", []), doc_type, cons_only)
        return (nm1 is not None) or (n is not None)
    # ITR: a page without any CVM code is a note / commentary page, not a form.
    if form_pages is not None and row.get("page_nb") not in form_pages:
        return False
    if _primary_only(doc_type) and not bypass_primary:
        if not str(row.get("page_class", "")).startswith("PRIMARY"):
            return False
    nm1, n = pick_amounts(row.get("columns", []), doc_type, cons_only)
    return (nm1 is not None) or (n is not None)


def passing_rows(statements, doc_type, group_key, cons_only=None):
    """[(row, amount_n_minus_1, amount_n)] the workbook writes for one statement group.
    The SINGLE row-selection function — the Excel sheets and the Streamlit screen both call
    it, so they can never disagree. `cons_only` defaults to the document-level decision."""
    if cons_only is None:
        cons_only = consolidado_filter(statements, doc_type)
    grp = statements.get(group_key, []) or []
    bypass = is_small_span(grp)
    fp = form_pages(statements, doc_type)
    out = []
    for r in grp:
        if row_passes(r, doc_type, bypass, cons_only, fp):
            nm1, n = pick_amounts(r.get("columns", []), doc_type, cons_only)
            out.append((r, nm1, n))
    return out


# --------------------------------------------------------------------------- #
# Why a row is flagged (statement sheets of the Excel and of the Streamlit screen)
# --------------------------------------------------------------------------- #
def review_reason(row):
    """Why a row carries `needs_review`, in words — the flags the pipeline left on it. Shown in the
    «Review» column of the statement sheets (Excel) and of the Streamlit tables: a flagged row used to
    look like any other line there (audit 2026-11-05, m4 — Cibra 6051: two Consolidado N-1 figures in
    one cash-flow line, the first shown silently). '' for a row without `needs_review`."""
    if not row.get("needs_review"):
        return ""
    why = []
    if row.get("arith_review"):                      # Stage 1, step 5b (sign_proof): an equation fails
        why.append(row["arith_review"])
    if row.get("xband_mixed"):                       # Stage 1: figures of two side-by-side panels
        why.append("figures of two side-by-side panels merged on one line")
    per_slot = {}
    for c in row.get("columns", []):
        if c.get("account_type") and c.get("period"):
            k = (c["account_type"], c["period"])
            per_slot[k] = per_slot.get(k, 0) + 1
    twice = [f"{t} {p}" for (t, p), k in per_slot.items() if k > 1]
    if twice:                                        # Stage 1: two cells claim one column
        why.append("two figures for one column (" + ", ".join(twice) + "): the first is shown")
    if row.get("sbs_candidates"):                    # Stage 3: two candidate fields (e.g. current / non-current)
        cands = " / ".join(row["sbs_candidates"])
        why.append(f"SBS field chosen between {cands}" if row.get("sbs_label")
                   else f"SBS field not resolved: {cands}")
    if row.get("sbs_blocked_reason"):                # Stage 3: a label withdrawn
        why.append(f"SBS label withdrawn ({row['sbs_blocked_reason']})")
    if row.get("sbs_review_reason"):
        why.append(row["sbs_review_reason"])
    return "; ".join(dict.fromkeys(why)) or "needs review"
