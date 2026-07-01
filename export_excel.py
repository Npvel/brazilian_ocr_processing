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
    Code | SBS Label | Label | Page | AMOUNT_N-1 | AMOUNT_N

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
COLUMNS = ["Code", "SBS Label", "Label", "Page", "AMOUNT_N-1", "AMOUNT_N"]
COL_WIDTHS = [14, 30, 56, 8, 18, 18]

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


def pick_amounts(columns, doc_type):
    """
    From a row's columns, return (amount_n_minus_1, amount_n) as numbers.
    Honors the Consolidado-only rule; maps by `period`, not position
    (provider dumps reverse the column order).
    """
    cons_only = _consolidado_only(doc_type)
    n_val = nm1_val = None
    for c in columns:
        at = (c.get("account_type") or "").lower()
        if cons_only and not at.startswith("consolidad"):
            continue
        num = to_number(c.get("value"))
        if num is None:
            continue
        period = c.get("period")
        if period == "AMOUNT_N" and n_val is None:
            n_val = num
        elif period == "AMOUNT_N-1" and nm1_val is None:
            nm1_val = num
    return nm1_val, n_val


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


def row_passes(row, doc_type, bypass_primary=False):
    """
    Whether this row should appear. A row qualifies on its AMOUNTS, not its
    label: `label_null` rows (subtotals whose name the OCR dropped, e.g. a
    'Total do ativo circulante' that arithmetically sums its section) carry real
    ONLY_AMOUNT figures and MUST be kept — they are downstream SBS-label targets.
    """
    # Stage-3 (точка 6) tags an exact duplicate (same name + account_type +
    # value, same page) with sbs_duplicate_of. Tag-in-JSON, drop-on-export:
    # this is the presentation layer, so the duplicate row is removed here.
    if row.get("sbs_duplicate_of") is not None:
        return False
    # A Stage-3 synthesized total (the derived totalLiabilitiesAmount line) has no
    # source page, so the page_class filter would always drop it. It is a real
    # computed figure that must always show — bypass the primary-only filter.
    if row.get("sbs_derived"):
        nm1, n = pick_amounts(row.get("columns", []), doc_type)
        return (nm1 is not None) or (n is not None)
    if _primary_only(doc_type) and not bypass_primary:
        if not str(row.get("page_class", "")).startswith("PRIMARY"):
            return False
    nm1, n = pick_amounts(row.get("columns", []), doc_type)
    return (nm1 is not None) or (n is not None)


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
        val = clean_meta(entry.get("value")) if isinstance(entry, dict) else ""
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


def _write_statement_header(ws, r, ncols):
    for col, text in enumerate(COLUMNS, start=1):
        cell = ws.cell(r, col, text)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=ALLIANZ_BLUE_MID)
        cell.alignment = CENTER if col in (4,) else (RIGHT if col >= 5 else LEFT)
        cell.border = BORDER
    ws.row_dimensions[r].height = 22


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


def build_statement_sheet(ws, title, subtitle, group_specs, statements, doc_type):
    """group_specs: list of (group_key, statements-dict). Writes sections."""
    ws.sheet_view.showGridLines = False
    ncols = len(COLUMNS)
    for i, w in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    r = _brand_header(ws, ncols, f"Allianz Trade  ·  {title}", subtitle)
    header_row = r
    _write_statement_header(ws, r, ncols)
    r += 1
    ws.freeze_panes = f"A{header_row + 1}"

    zebra = 0
    n_written = 0
    for group_key in group_specs:
        group_rows = statements.get(group_key, [])
        # A short (<=2 consecutive pages) statement is taken in full regardless
        # of page_class — no segment-copy risk, and it rescues balances that
        # Stage-2 mislabelled as NOTE on wording alone.
        bypass = is_small_span(group_rows)
        rows = [rr for rr in group_rows if row_passes(rr, doc_type, bypass)]
        if not rows:
            continue
        # section divider only when more than one section on the sheet
        if len(group_specs) > 1:
            _write_section_divider(ws, r, SECTION_TITLES.get(group_key, group_key), ncols)
            r += 1
            zebra = 0

        for rr in rows:
            nm1, n = pick_amounts(rr.get("columns", []), doc_type)
            label = rr.get("label") or ""
            is_total = _looks_like_total(label)
            values = [
                rr.get("code") or "",
                rr.get("sbs_label") or "",
                label,
                rr.get("page_nb"),
                nm1,
                n,
            ]
            for col, val in enumerate(values, start=1):
                cell = ws.cell(r, col, val)
                cell.border = BORDER
                if col == 2:
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
                        cell.number_format = '#,##0;(#,##0)'
                else:
                    cell.font = CELL_FONT
                    cell.alignment = LEFT
                # fill: totals highlighted, else zebra
                if is_total:
                    cell.fill = PatternFill("solid", fgColor=ALLIANZ_LIGHT)
                else:
                    fill = WHITE if zebra % 2 == 0 else "F4F7FA"
                    cell.fill = PatternFill("solid", fgColor=fill)
            ws.row_dimensions[r].height = 18
            r += 1
            n_written += 1
            if not is_total:
                zebra += 1

    if n_written == 0:
        cell = ws.cell(r, 1, "No rows matched the filter for this statement.")
        cell.font = Font(name="Arial", italic=True, color=GREY_TEXT, size=10)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
    return n_written


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build_workbook(output):
    statements = output.get("statements", {})
    metadata = output.get("metadata", {})
    doc_type = resolve_doc_type(output)

    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = "metadata"
    build_metadata_sheet(ws_meta, metadata, doc_type)

    counts = {}
    for sheet_name, title, groups in STATEMENT_SHEETS:
        ws = wb.create_sheet(sheet_name)
        subtitle = f"{title}   —   document type: {doc_type}"
        counts[sheet_name] = build_statement_sheet(
            ws, title, subtitle, groups, statements, doc_type)

    # tab colors
    ws_meta.sheet_properties.tabColor = ALLIANZ_BLUE
    for sheet_name, *_ in STATEMENT_SHEETS:
        wb[sheet_name].sheet_properties.tabColor = ALLIANZ_BLUE_MID
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
    print(f"  consolidado-only filter: {_consolidado_only(doc_type)}")
    print(f"  primary-only filter    : {_primary_only(doc_type)}")
    print("  rows written per sheet :")
    for k, v in counts.items():
        print(f"      {k:12}: {v}")
    print(f"\nЗаписано: {out_path}")


if __name__ == "__main__":
    main()
