#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
export_excel.py — Stage-4: render a structured_output JSON into a polished,
Allianz-styled Excel workbook.

Sheets:
    1. metadata    — document metadata (key / value)
    2. balance     — BALANCE_ASSETS_LST + BALANCE_LIABILITIES_LST
    3. income      — INCOME_STATEMENT_LST
    4. cash_flow   — CASH_FLOW_LST

Statement sheet columns:
    Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N | Review
    (a row flagged needs_review is filled light red, its reason in «Review» — selection.review_reason)

Filtering — the document type decides what to keep. The type is computed once in
Stage 1 (see doc_type.py) and carried on the output as `document_type`; the
exporter reads it (recomputing only as a legacy fallback). Detection order and
filter policy (defined in doc_type.py):

    detection order (first match wins):
      1) provider tag 'SERASA EXPERIAN'       -> SERASA      (Serasa Experian dump)
      2) CODE_VALUE present on any row        -> ITR         (hierarchical codes)
      3) >=50% of labels are ALL-UPPERCASE    -> UPPERCASE    (provider dump)
      4) any column carries a Consolidado/    -> DFP_4COL     (annual, 4 columns)
         Controladora account_type
      5) otherwise                            -> TWO_COL      (2-col standalone)

    rows kept:
      SERASA     : ignore page_class; ALL columns
      ITR        : ignore page_class; Consolidado columns only
      UPPERCASE  : ignore page_class; ALL columns
      DFP_4COL   : page_class PRIMARY:* only; Consolidado columns only
      TWO_COL    : page_class PRIMARY:* only; ALL columns

    The "Consolidado only" rule applies ONLY where such a split exists (ITR,
    DFP_4COL). Where the statement is single-perspective (TWO_COL, UPPERCASE,
    SERASA) every column is taken — there is nothing to filter.

CLI:
    python3.13 export_excel.py structured_output.json -o report.xlsx
    python3.13 export_excel.py structured_output_itr.json          # -> *_report.xlsx
"""

import argparse
import json
import os
import re
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from anchors_lib import parse_amount
from doc_type import detect_doc_type, consolidado_only, primary_only

# --------------------------------------------------------------------------- #
# Allianz brand palette
# --------------------------------------------------------------------------- #
ALLIANZ_BLUE = "003781"      # primary corporate blue
ALLIANZ_BLUE_MID = "006192"  # accent
ALLIANZ_LIGHT = "E8EEF4"     # zebra / light fill
ALLIANZ_ACCENT = "DAA900"    # corporate gold (for subtotals / accents)
WHITE = "FFFFFF"
GREY_TEXT = "53565A"         # Allianz neutral grey
SECTION_FILL = "C8D5E3"      # section-divider fill (assets / liabilities)

THIN = Side(style="thin", color="B7C2CE")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

HEADER_FONT = Font(name="Arial", bold=True, color=WHITE, size=11)
TITLE_FONT = Font(name="Arial", bold=True, color=WHITE, size=16)
SUBTITLE_FONT = Font(name="Arial", bold=False, color="D5DEE9", size=10)
CELL_FONT = Font(name="Arial", color="1A1A1A", size=10)
SBS_FONT = Font(name="Arial", color=ALLIANZ_BLUE, size=10, bold=True)
# Items recovered from the OCR words layer (optional step): italic dark gold on light yellow.
RECOVERED_COLOR = "8A6100"
RECOVERED_FILL = "FFF4CC"
# Rows flagged needs_review: light red (as on the sbs_fields sheet), the reason in the «Review» column.
REVIEW_FILL = "FDECEA"
REVIEW_COLOR = "B42318"
SECTION_FONT = Font(name="Arial", bold=True, color=ALLIANZ_BLUE, size=11)
META_KEY_FONT = Font(name="Arial", bold=True, color=ALLIANZ_BLUE, size=11)
META_VAL_FONT = Font(name="Arial", color="1A1A1A", size=11)

LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")

# --------------------------------------------------------------------------- #
# Statement layout
# --------------------------------------------------------------------------- #
STATEMENT_SHEETS = [
    ("balance", "Balance Sheet", ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"]),
    ("income", "Income Statement", ["INCOME_STATEMENT_LST"]),
    ("cash_flow", "Cash Flow", ["CASH_FLOW_LST"]),
]
SECTION_TITLES = {
    "BALANCE_ASSETS_LST": "ASSETS  ·  Ativo",
    "BALANCE_LIABILITIES_LST": "LIABILITIES & EQUITY  ·  Passivo e Patrimônio Líquido",
    "INCOME_STATEMENT_LST": "INCOME STATEMENT  ·  Demonstração do Resultado",
    "CASH_FLOW_LST": "CASH FLOW  ·  Fluxo de Caixa",
}
COLUMNS = ["Code", "SBS Label", "Label", "Page", "AMOUNT_N-1", "AMOUNT_N", "Review"]
NUMBER_FORMAT = '#,##0;(#,##0)'
NUMBER_FORMAT_DECIMALS = '#,##0.00##;(#,##0.00##)'
COL_WIDTHS = [14, 30, 56, 8, 18, 18, 60]

META_ORDER = ["CLOSING_DATE", "DURATION", "CURRENCY", "UNIT", "ACCOUNT_TYPE"]
META_PRETTY = {
    "CLOSING_DATE": "Closing date  ·  Data de encerramento",
    "DURATION": "Period  ·  Período",
    "CURRENCY": "Currency  ·  Moeda",
    "UNIT": "Unit  ·  Unidade",
    "ACCOUNT_TYPE": "Account type  ·  Tipo de conta",
}


# Document-type detection + filter policy live in doc_type.py (single source of
# truth). The exporter prefers the type Stage-1 already stamped on the output and
# only recomputes as a fallback for legacy JSON that predates the field.
_consolidado_only = consolidado_only
_primary_only = primary_only


# Row / figure selection lives in selection.py (standard library only) — re-exported here so
# `export_excel.passing_rows` & co. keep working for app.py, the harness and older callers.
from selection import (  # noqa: E402,F401
    resolve_doc_type,
    to_number,
    document_has_consolidado,
    consolidado_filter,
    pick_cells,
    pick_amounts,
    is_recovered,
    is_per_share,
    is_small_span,
    row_passes,
    passing_rows,
    review_reason,
    _PCT_RE,
)



# --------------------------------------------------------------------------- #
# Metadata normalization (truncated OCR tokens like '(Reais', 'Mil)')
# --------------------------------------------------------------------------- #
def clean_meta(value):
    if value is None:
        return ""
    s = str(value).strip()
    s = s.strip("()[]").strip()
    s = re.sub(r"^[Cc]-", "", s)  # 'C-31/12/2024' -> '31/12/2024'
    return s


def meta_display(key, entry):
    """Metadata value as shown: the Stage-1 `normalized` value when present (ISO date -> dd/mm/yyyy,
    BRL, unit multiplier, months), with the OCR token kept alongside when it differs."""
    if not isinstance(entry, dict):
        return ""
    raw = clean_meta(entry.get("value"))
    norm = entry.get("normalized")
    if key == "CLOSING_DATE" and norm:
        y, m, d = norm.split("-")
        shown = f"{d}/{m}/{y}"
        return shown if raw == shown else f"{shown}   (OCR: {raw})"
    if key == "CURRENCY" and norm:
        return f"{norm}   ({raw})" if raw else norm
    if key == "UNIT" and norm:
        if entry.get("normalized_from") and not raw:
            return f"(× {norm:,})   — not printed; inferred from the {entry['normalized_from']}".replace(",", ".")
        return f"{raw}   (× {norm:,})".replace(",", ".")
    if key == "DURATION":
        months = entry.get("normalized_months")
        out = raw + (f"   ({months} meses)" if months and "mes" not in raw.lower() else "")
        return out + ("   — from the OCR words layer" if entry.get("source") == "ocr_words" else "")
    return raw


# --------------------------------------------------------------------------- #
# Sheet builders
# --------------------------------------------------------------------------- #
def _brand_header(ws, ncols, title, subtitle):
    """Two-row branded banner spanning ncols; returns next free row index."""
    last_col = get_column_letter(ncols)
    ws.merge_cells(f"A1:{last_col}1")
    ws.merge_cells(f"A2:{last_col}2")
    c1 = ws["A1"]
    c1.value = title
    c1.font = TITLE_FONT
    c1.fill = PatternFill("solid", fgColor=ALLIANZ_BLUE)
    c1.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    c2 = ws["A2"]
    c2.value = subtitle
    c2.font = SUBTITLE_FONT
    c2.fill = PatternFill("solid", fgColor=ALLIANZ_BLUE)
    c2.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 16
    return 4  # leave row 3 as a spacer


def build_metadata_sheet(ws, metadata, doc_type):
    ws.sheet_view.showGridLines = False
    _brand_header(ws, 2, "Allianz Trade  ·  Financial Statement Extract",
                  f"Document metadata   —   detected type: {doc_type}")
    for i, w in enumerate([42, 46], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    r = 4
    # column headers
    for col, text in enumerate(["Field", "Value"], start=1):
        cell = ws.cell(r, col, text)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=ALLIANZ_BLUE_MID)
        cell.alignment = LEFT
        cell.border = BORDER
    ws.row_dimensions[r].height = 20
    r += 1

    shown = 0
    for key in META_ORDER:
        entry = metadata.get(key)
        if key == "ACCOUNT_TYPE" and not entry:
            continue
        val = meta_display(key, entry)
        kcell = ws.cell(r, 1, META_PRETTY.get(key, key))
        vcell = ws.cell(r, 2, val)
        kcell.font = META_KEY_FONT
        vcell.font = META_VAL_FONT
        kcell.alignment = LEFT
        vcell.alignment = LEFT
        fill = ALLIANZ_LIGHT if shown % 2 == 0 else WHITE
        kcell.fill = PatternFill("solid", fgColor=fill)
        vcell.fill = PatternFill("solid", fgColor=fill)
        kcell.border = BORDER
        vcell.border = BORDER
        ws.row_dimensions[r].height = 22
        r += 1
        shown += 1

    ws.freeze_panes = "A5"


def _dmy(iso):
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def _minus_year(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    if m == 2 and d == 29:
        d = 28
    return f"{y - 1:04d}-{m:02d}-{d:02d}"


def period_labels(metadata, doc_type, sheet):
    """(N-1 label, N label) for the statement sheet's column headers — the dates of the two
    columns, only where they follow from the metadata without guessing (audit 2026-11-04, X1: an ITR
    balance compares with 31/12 of the previous year while its DRE / DFC compare with the same period
    of the previous year; a Serasa N may be 9 months against a 12-month N-1)."""
    cd = ((metadata or {}).get("CLOSING_DATE") or {}).get("normalized")
    if not cd:
        return None, None
    du = (metadata or {}).get("DURATION") or {}
    months = du.get("normalized_months")
    year_end_before = f"{int(cd[:4]) - 1}-12-31"
    if sheet == "balance":
        if doc_type in ("ITR", "SERASA"):
            return _dmy(year_end_before), _dmy(cd)
        if months == 12:
            return _dmy(_minus_year(cd)), _dmy(cd)
        return None, _dmy(cd)
    rng = re.findall(r"(\d{1,2})/(\d{1,2})/(\d{4})", du.get("value") or "")
    if doc_type == "ITR" and len(rng) == 2:
        (d1, m1, y1), (d2, m2, y2) = rng
        cur = f"{int(d1):02d}/{int(m1):02d}/{y1}–{int(d2):02d}/{int(m2):02d}/{y2}"
        prev = f"{int(d1):02d}/{int(m1):02d}/{int(y1) - 1}–{int(d2):02d}/{int(m2):02d}/{int(y2) - 1}"
        return prev, cur
    if months == 12:
        return f"12m to {_dmy(_minus_year(cd))}", f"12m to {_dmy(cd)}"
    n = f"{months}m to {_dmy(cd)}" if months else f"to {_dmy(cd)}"
    if doc_type == "SERASA":
        return f"12m to {_dmy(year_end_before)}", n
    return None, n


def _write_statement_header(ws, r, ncols, labels=(None, None)):
    for col, text in enumerate(COLUMNS, start=1):
        if col in (5, 6) and labels[col - 5]:
            text = f"{text}\n{labels[col - 5]}"
        cell = ws.cell(r, col, text)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=ALLIANZ_BLUE_MID)
        cell.alignment = (CENTER if col in (4,) else
                          Alignment(horizontal="right", vertical="center", wrap_text=True) if col in (5, 6)
                          else LEFT)
        cell.border = BORDER
    ws.row_dimensions[r].height = 34 if any(labels) else 22


def _write_section_divider(ws, r, title, ncols):
    last_col = get_column_letter(ncols)
    ws.merge_cells(f"A{r}:{last_col}{r}")
    cell = ws[f"A{r}"]
    cell.value = title
    cell.font = SECTION_FONT
    cell.fill = PatternFill("solid", fgColor=SECTION_FILL)
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for col in range(1, ncols + 1):
        ws.cell(r, col).border = BORDER
    ws.row_dimensions[r].height = 20


def _looks_like_total(label):
    n = label.lower()
    return n.startswith("total") or n.startswith("(=)") or "total do" in n


def build_statement_sheet(ws, title, subtitle, group_specs, statements, doc_type, cons_only=None,
                          period_header=(None, None)):
    """group_specs: list of (group_key, statements-dict). Writes sections. `period_header` = the
    dates of the two columns (period_labels)."""
    if cons_only is None:
        cons_only = consolidado_filter(statements, doc_type)
    ws.sheet_view.showGridLines = False
    ncols = len(COLUMNS)
    for i, w in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    r = _brand_header(ws, ncols, f"Allianz Trade  ·  {title}", subtitle)
    header_row = r
    _write_statement_header(ws, r, ncols, period_header)
    r += 1
    ws.freeze_panes = f"A{header_row + 1}"

    zebra = 0
    n_written = 0
    n_recovered = 0
    n_review = 0
    for group_key in group_specs:
        # A short (<=2 consecutive pages) statement is taken in full regardless
        # of page_class — no segment-copy risk, and it rescues balances that
        # Stage-2 mislabelled as NOTE on wording alone (inside passing_rows).
        selected = passing_rows(statements, doc_type, group_key, cons_only)
        if not selected:
            continue
        # section divider only when more than one section on the sheet
        if len(group_specs) > 1:
            _write_section_divider(ws, r, SECTION_TITLES.get(group_key, group_key), ncols)
            r += 1
            zebra = 0

        for rr, nm1, n in selected:
            label = rr.get("label") or ""
            per_share = is_per_share(rr)
            if per_share:
                label = f"{label}  (per share)" if label else "(per share)"
            is_total = _looks_like_total(label)
            c_nm1, c_n = pick_cells(rr.get("columns", []), doc_type, cons_only)
            # columns 3 (label) / 5 / 6 (figures) recovered from the OCR words layer
            recovered = {3: rr.get("label_source") == "ocr_words",
                         5: is_recovered(c_nm1), 6: is_recovered(c_n)}
            # a row flagged needs_review: light red, the reason in the «Review» column (audit 2026-11-05 m4)
            reason = review_reason(rr)
            n_review += bool(reason)
            values = [
                rr.get("code") or "",
                rr.get("sbs_label") or "",
                label,
                rr.get("page_nb"),
                nm1,
                n,
                reason,
            ]
            for col, val in enumerate(values, start=1):
                cell = ws.cell(r, col, val)
                cell.border = BORDER
                if col == 7:
                    cell.font = Font(name="Arial", size=9, color=REVIEW_COLOR)
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
                elif col == 2:
                    cell.font = SBS_FONT
                    cell.alignment = LEFT
                elif col == 3:
                    cell.font = Font(name="Arial", size=10, bold=is_total,
                                     color="1A1A1A")
                    cell.alignment = LEFT
                elif col == 4:
                    cell.font = CELL_FONT
                    cell.alignment = CENTER
                elif col >= 5:
                    cell.font = Font(name="Arial", size=10, bold=is_total,
                                     color="1A1A1A")
                    cell.alignment = RIGHT
                    if isinstance(val, (int, float)):
                        # figures with decimals (EPS in R$ per share, amounts with centavos) keep them
                        cell.number_format = (NUMBER_FORMAT_DECIMALS if per_share or val != int(val)
                                              else NUMBER_FORMAT)
                else:
                    cell.font = CELL_FONT
                    cell.alignment = LEFT
                # fill: review rows light red, totals highlighted, else zebra
                if reason:
                    cell.fill = PatternFill("solid", fgColor=REVIEW_FILL)
                elif is_total:
                    cell.fill = PatternFill("solid", fgColor=ALLIANZ_LIGHT)
                else:
                    fill = WHITE if zebra % 2 == 0 else "F4F7FA"
                    cell.fill = PatternFill("solid", fgColor=fill)
                if recovered.get(col):
                    cell.font = Font(name="Arial", size=10, bold=is_total, italic=True,
                                     color=RECOVERED_COLOR)
                    cell.fill = PatternFill("solid", fgColor=RECOVERED_FILL)
                    n_recovered += 1
            ws.row_dimensions[r].height = 18
            r += 1
            n_written += 1
            if not is_total:
                zebra += 1

    if n_written == 0:
        cell = ws.cell(r, 1, "No rows matched the filter for this statement.")
        cell.font = Font(name="Arial", italic=True, color=GREY_TEXT, size=10)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
    else:
        r += 1
        if n_recovered:
            _write_note(ws, r, ncols, RECOVERED_NOTE, RECOVERED_COLOR, RECOVERED_FILL)
            r += 1
        if n_review:
            _write_note(ws, r, ncols, REVIEW_NOTE, REVIEW_COLOR, REVIEW_FILL)
    return n_written


def _write_note(ws, r, ncols, text, color, fill):
    cell = ws.cell(r, 1, text)
    cell.font = Font(name="Arial", italic=True, color=color, size=9)
    cell.fill = PatternFill("solid", fgColor=fill)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)


RECOVERED_NOTE = ("Italic on light yellow = recovered from the OCR words layer (the provider "
                  "extraction lost it) · recuperado da camada de palavras do OCR")
REVIEW_NOTE = ("Light red = the pipeline flagged the line for review, the reason is in the Review column "
               "· linha marcada para revisão")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# SBS field values — ONE value per field and period (sbs_fields.py, Stage-3 end)
# --------------------------------------------------------------------------- #
SBS_METHOD_TEXT = {
    "single": "one line",
    "same_value": "same figure on several lines",
    "sum": "sum of the lines",
    "total_of_components": "printed total of its components",
    "first_of_several": "first of several different lines — REVIEW",
    "before_financial_result": "the line before the financial result",
    "identity": "the line the accounting identity proves",
    "sum+identity": "sum of the lines (proven by the identity)",
    "subset+identity": "part of the lines (proven by the identity)",
    "derived = current + non-current": "derived: current + non-current liabilities",
    "zero_by_identity": "0 — the identity leaves nothing for it",
    "held_for_sale+identity": "printed total + the held-for-sale line printed outside it (proven by the identity)",
    "figures_on_next_line": "figures on the line below the caption — REVIEW",
}
SBS_COLUMNS = ["SBS Field", "Statement", "AMOUNT_N-1", "AMOUNT_N", "How", "Rows (page · label · value N)",
               "Verified by"]
SBS_COL_WIDTHS = [34, 12, 18, 18, 40, 70, 46]
_STMT_SHORT = {"BALANCE_ASSETS_LST": "Assets", "BALANCE_LIABILITIES_LST": "Liabilities",
               "INCOME_STATEMENT_LST": "Income", "CASH_FLOW_LST": "Cash flow"}


def sbs_fields_of(output):
    """The output's `sbs_fields` (Stage 3), or computed now for a JSON that predates it."""
    if output.get("sbs_fields"):
        return output["sbs_fields"]
    try:
        import sbs_fields
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbs_label_mapping.json")
        with open(path, encoding="utf-8") as f:
            return sbs_fields.compute(output, json.load(f).get("fields", {}))
    except Exception:  # pragma: no cover
        return None


def sbs_method_text(entry):
    m = entry.get("method") or {}
    texts = {p: SBS_METHOD_TEXT.get(v, v) for p, v in m.items()}
    if len(set(texts.values())) <= 1:
        return next(iter(texts.values()), "")
    return "; ".join(f"{p[7:] or 'N'}: {t}" for p, t in texts.items())


def verified_text(entry):
    """The identities that verify the field — per period when the periods differ (an identity can
    hold for N and fail for N-1: Dom's N-1 assets, audit 2026-11-04 DOM11)."""
    ver = {p: sorted(v.replace("Amount", "") for v in vs) for p, vs in (entry.get("verified") or {}).items()}
    if not ver:
        return ""
    if len({tuple(v) for v in ver.values()}) == 1 and len(ver) == 2:
        return "; ".join(next(iter(ver.values())))
    return " | ".join(f"{'N' if p == 'AMOUNT_N' else 'N-1'}: {'; '.join(v)}"
                      for p, v in sorted(ver.items(), key=lambda kv: kv[0] != "AMOUNT_N"))


def build_sbs_fields_sheet(ws, sf, doc_type):
    ws.sheet_view.showGridLines = False
    ncols = len(SBS_COLUMNS)
    for i, w in enumerate(SBS_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    persp = (sf or {}).get("perspective") or ""
    r = _brand_header(ws, ncols, "Allianz Trade  ·  SBS Fields",
                      f"One value per SBS field and period   —   {persp}   —   document type: {doc_type}")
    for col, text in enumerate(SBS_COLUMNS, start=1):
        cell = ws.cell(r, col, text)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=ALLIANZ_BLUE_MID)
        cell.alignment = RIGHT if col in (3, 4) else LEFT
        cell.border = BORDER
    ws.freeze_panes = f"A{r + 1}"
    r += 1
    fields = (sf or {}).get("fields") or {}
    if not fields:
        cell = ws.cell(r, 1, "No SBS field could be valued for this document.")
        cell.font = Font(name="Arial", italic=True, color=GREY_TEXT, size=10)
        return 0
    for i, (f, e) in enumerate(fields.items()):
        review = bool(e.get("needs_review"))
        rows_txt = " | ".join(
            f"p{x.get('page')} · {(x.get('label') or 'unnamed')[:40]} · "
            f"{'' if x.get('AMOUNT_N') is None else format(x['AMOUNT_N'], ',.0f').replace(',', '.')}"
            f"{'' if x.get('used') else '  (not counted)'}"
            for x in e.get("rows", []))
        if e.get("note"):
            rows_txt = (rows_txt + "  —  " if rows_txt else "") + e["note"]
        if e.get("review_reason"):
            rows_txt = (rows_txt + "  —  " if rows_txt else "") + "REVIEW: " + e["review_reason"].replace("Amount", "")
        values = [f, _STMT_SHORT.get(e.get("statement"), e.get("statement") or ""), e.get("AMOUNT_N-1"),
                  e.get("AMOUNT_N"), sbs_method_text(e), rows_txt, verified_text(e)]
        fill = "FDECEA" if review else (WHITE if i % 2 == 0 else "F4F7FA")
        for col, val in enumerate(values, start=1):
            cell = ws.cell(r, col, val)
            cell.border = BORDER
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.font = SBS_FONT if col == 1 else CELL_FONT
            if col in (3, 4):
                cell.alignment = RIGHT
                if isinstance(val, (int, float)):
                    cell.number_format = '#,##0;(#,##0)'
            else:
                cell.alignment = LEFT
        ws.row_dimensions[r].height = 30 if len(rows_txt) > 70 else 18
        r += 1
    # accounting identities, per period
    r += 1
    _write_section_divider(ws, r, "ACCOUNTING CHECKS  ·  Verificações contábeis", ncols)
    r += 1
    for c in (sf or {}).get("checks", []):
        ok = c.get("ok")
        cell = ws.cell(r, 1, ("✓  " if ok else "✗  ") + c["identity"].replace("Amount", ""))
        cell.font = Font(name="Arial", size=10, bold=not ok, color="1A7F37" if ok else "B42318")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ws.cell(r, 3, c["period"]).font = CELL_FONT
        ws.cell(r, 4, c.get("lhs")).font = CELL_FONT
        ws.cell(r, 4).number_format = '#,##0;(#,##0)'
        if c.get("resolved"):
            note = "resolved: " + ", ".join(x.replace("Amount", "") for x in c["resolved"])
        else:
            note = c.get("note") or ""
        ws.cell(r, 5, note).font = CELL_FONT
        r += 1
    return len(fields)


def build_workbook(output):
    statements = output.get("statements", {})
    metadata = output.get("metadata", {})
    doc_type = resolve_doc_type(output)

    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = "metadata"
    build_metadata_sheet(ws_meta, metadata, doc_type)

    counts = {}
    cons_only = consolidado_filter(statements, doc_type)   # decided ONCE per document
    for sheet_name, title, groups in STATEMENT_SHEETS:
        ws = wb.create_sheet(sheet_name)
        subtitle = f"{title}   —   document type: {doc_type}"
        counts[sheet_name] = build_statement_sheet(
            ws, title, subtitle, groups, statements, doc_type, cons_only,
            period_labels(metadata, doc_type, sheet_name))

    ws_sbs = wb.create_sheet("sbs_fields")
    counts["sbs_fields"] = build_sbs_fields_sheet(ws_sbs, sbs_fields_of(output), doc_type)

    # tab colors
    ws_meta.sheet_properties.tabColor = ALLIANZ_BLUE
    for sheet_name, *_ in STATEMENT_SHEETS:
        wb[sheet_name].sheet_properties.tabColor = ALLIANZ_BLUE_MID
    ws_sbs.sheet_properties.tabColor = ALLIANZ_ACCENT
    return wb, doc_type, counts


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Render structured_output JSON to Allianz Excel.")
    ap.add_argument("input", help="structured_output JSON")
    ap.add_argument("-o", "--output", help="output .xlsx (default: <input>_report.xlsx)")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        output = json.load(f)

    wb, doc_type, counts = build_workbook(output)

    out_path = args.output or (os.path.splitext(args.input)[0] + "_report.xlsx")
    wb.save(out_path)

    print("=" * 60)
    print("EXCEL EXPORT")
    print("=" * 60)
    print(f"  detected document type : {doc_type}")
    print(f"  consolidado-only filter: {consolidado_filter(output.get('statements', {}), doc_type)}"
          f"  (doc type default {_consolidado_only(doc_type)}; no Consolidado figures -> company figures)")
    print(f"  primary-only filter    : {_primary_only(doc_type)}")
    print("  rows written per sheet :")
    for k, v in counts.items():
        print(f"      {k:12}: {v}")
    print(f"\nЗаписано: {out_path}")


if __name__ == "__main__":
    main()
