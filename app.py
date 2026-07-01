#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
app.py — Streamlit front-end for the BR OCR JSON pipeline.

Upload a raw OCR-provider JSON (root = list) OR an already-structured output
(root = dict with `statements`); the app runs Stages 1-3 when needed, then
renders the balance / income / cash-flow statements with a metadata ribbon and
a per-section expand/collapse control, and offers the polished Allianz Excel as
a download.

DESIGN CONTRACT — the screen MUST agree with the Excel:
  * which rows show      -> export_excel.row_passes / is_small_span
  * which amounts show   -> export_excel.pick_amounts  (period-mapped, doc-type
                            aware Consolidado filter)
  * the .xlsx download    -> export_excel.build_workbook  (the exact CLI function,
                            so the file is byte-identical to `export_excel.py`)

Run:
    cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
    python3.13 -m streamlit run app.py
"""

import html
import json
import os
import sys
from io import BytesIO

import streamlit as st

# The pipeline modules live next to this file. streamlit sets cwd to wherever
# it is launched, so make the import explicit and cwd-independent. (The pipeline
# itself reads anchors.yaml / sbs_label_mapping.json via __file__, so it is
# already cwd-safe once imported.)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import structure_ocr
import export_excel

# --------------------------------------------------------------------------- #
# Brand palette — mirrors export_excel.py so screen and workbook share a look.
# --------------------------------------------------------------------------- #
ALLIANZ_BLUE = "#003781"
ALLIANZ_BLUE_MID = "#006192"
ALLIANZ_LIGHT = "#E8EEF4"
ALLIANZ_GOLD = "#DAA900"
SECTION_FILL = "#C8D5E3"
GREY_TEXT = "#53565A"
NEG_RED = "#B00020"

# Three statement tabs (bilingual EN · PT), each mapping to one or more
# statement groups in the structured output.
TABS = [
    ("balance", "Balance Sheet · Balanço",
     ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"]),
    ("income", "Income Statement · DRE",
     ["INCOME_STATEMENT_LST"]),
    ("cash_flow", "Cash Flow · Fluxo de Caixa",
     ["CASH_FLOW_LST"]),
]

DOC_TYPE_DESC = {
    "SERASA": "Serasa Experian dump · todas as colunas",
    "ITR": "Quarterly · hierarchical CVM codes · Consolidado only",
    "UPPERCASE": "Generic provider dump · all-caps · all columns",
    "DFP_4COL": "Annual DFP · Controladora + Consolidado · Consolidado only",
    "TWO_COL": "Single-perspective · 2 columns",
}

st.set_page_config(
    page_title="Allianz Trade · Financial Statement Extract",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --------------------------------------------------------------------------- #
# Global stylesheet — kept as a plain (non-f) string to avoid brace escaping.
# --------------------------------------------------------------------------- #
def inject_css():
    st.markdown(
        """
<style>
  /* ---- page chrome ---- */
  .block-container { padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1500px; }
  #MainMenu, footer { visibility: hidden; }
  html, body, [class*="css"] { font-family: Arial, "Segoe UI", sans-serif; }

  /* ---- hero banner ---- */
  .hero {
    background: linear-gradient(95deg, #003781 0%, #00407e 55%, #006192 100%);
    border-radius: 14px; padding: 20px 26px; color: #fff;
    box-shadow: 0 6px 22px rgba(0,55,129,.22);
    border-bottom: 3px solid #DAA900; margin-bottom: 18px;
  }
  .hero h1 { font-size: 26px; font-weight: 800; margin: 0; letter-spacing:.2px; }
  .hero .sub { color: #cdddf0; font-size: 14px; margin-top: 4px; }
  .hero .file { color:#fff; font-size:13px; margin-top:10px; opacity:.95;}
  .hero .file b { color:#FFE08A; }

  /* ---- source filename line (above the metadata card) ---- */
  .src-line { color:#5b6672; font-size:13px; margin: 2px 0 8px 2px; }
  .src-line b { color:#003781; }

  /* ---- metadata ribbon ---- */
  .ribbon {
    display:flex; justify-content:space-between; align-items:stretch; gap:18px;
    background:#fff; border:1px solid #d7e0ea; border-left:5px solid #003781;
    border-radius:12px; padding:14px 20px; margin: 4px 0 18px 0;
    box-shadow: 0 2px 10px rgba(0,55,129,.06); flex-wrap:wrap;
  }
  .ribbon .side { display:flex; gap:26px; flex-wrap:wrap; align-items:center; }
  .ribbon .side.right { justify-content:flex-end; }
  .meta-item { display:flex; flex-direction:column; line-height:1.25; }
  .meta-item.r { text-align:right; }
  .meta-k { font-size:10.5px; text-transform:uppercase; letter-spacing:.4px;
            color:#7c8896; font-weight:700; }
  .meta-v { font-size:16px; color:#10243f; font-weight:700; }
  .doc-badge {
    background:#003781; color:#fff; border-radius:999px; padding:7px 16px;
    font-weight:800; font-size:14px; letter-spacing:.5px; align-self:center;
    box-shadow: inset 0 0 0 2px rgba(218,169,0,.55);
  }

  /* ---- section bar (rich Allianz blue, gold left accent) ---- */
  .sect-bar {
    background: linear-gradient(90deg, #003781 0%, #0a4a94 100%);
    color:#ffffff; font-weight:800; font-size:15px; letter-spacing:.3px;
    padding:11px 18px; border-radius:9px 9px 0 0;
    border-left:5px solid #DAA900; box-shadow:0 2px 8px rgba(0,55,129,.15);
  }

  /* ---- statement table ---- */
  .stmt-wrap {
    max-height: 74vh; overflow:auto; border:1px solid #b7c7d8;
    border-radius:0 0 11px 11px; box-shadow:0 3px 14px rgba(0,55,129,.07);
    margin-bottom: 6px;
  }
  table.stmt { border-collapse:collapse; width:100%; font-size:13px; background:#fff; }
  table.stmt thead th {
    position:sticky; top:0; z-index:3; background:#006192; color:#fff;
    font-weight:700; font-size:11.5px; text-transform:uppercase; letter-spacing:.4px;
    padding:9px 12px; text-align:left; white-space:nowrap;
  }
  table.stmt thead th .pt { display:block; font-size:9.5px; font-weight:500;
                            color:#bfe0ee; letter-spacing:.2px; text-transform:none; }
  table.stmt thead th.num { text-align:right; }
  table.stmt thead th.ctr { text-align:center; }
  table.stmt td {
    padding:7px 12px; border-bottom:1px solid #eef2f6; vertical-align:middle;
    color:#1a1a1a;
  }
  table.stmt td.num { text-align:right; white-space:nowrap; font-variant-numeric: tabular-nums; }
  table.stmt td.ctr { text-align:center; color:#5b6672; }
  table.stmt td.code { color:#5b6672; font-size:12px; white-space:nowrap; }
  table.stmt tr:hover td { background:#f1f6fb !important; }

  tr.detail:nth-child(even) td { background:#f7fafd; }
  tr.sbs td { background:#fbfcf4; }
  tr.sbs:nth-child(even) td { background:#f6f8ec; }
  tr.total td {
    background:#E8EEF4 !important; font-weight:800; color:#10243f;
    border-top:1px solid #c2d0e0; border-bottom:1px solid #c2d0e0;
  }
  tr.derived td { background:#fff6e0 !important; font-style:italic; }

  .dot { color:#DAA900; font-size:15px; margin-right:7px; vertical-align:-1px; }
  .sbs-name { color:#003781; font-weight:700; }
  .lbl-missing { color:#98a3b0; font-style:italic; }
  .neg { color:#B00020; }
  .badge {
    display:inline-block; font-size:9.5px; font-weight:800; letter-spacing:.3px;
    padding:1px 7px; border-radius:999px; margin-left:8px; vertical-align:1px;
  }
  .b-review { background:#FFF1D6; color:#9a6b00; border:1px solid #f0d49a; }
  .b-cross  { background:#eef0ff; color:#3a44a0; border:1px solid #c9cdf0; }
  .b-derived{ background:#fde9c8; color:#8a5a00; border:1px solid #f0cf9a; }

  .empty-note { color:#7c8896; font-size:13px; padding:14px 4px; font-style:italic; }

  /* ---- sidebar ---- */
  section[data-testid="stSidebar"] { background:#0f2647; }
  section[data-testid="stSidebar"] * { color:#e9eef6; }
  section[data-testid="stSidebar"] .side-logo { font-size:20px; font-weight:800;
     color:#fff; letter-spacing:.3px; }
  section[data-testid="stSidebar"] .side-logo .gold { color:#FFD24D; }
  section[data-testid="stSidebar"] .side-tag { font-size:11.5px; color:#9fb2cf;
     margin-bottom:6px; }
  section[data-testid="stSidebar"] [data-testid="stMetricValue"] { color:#fff; font-size:22px; }
  section[data-testid="stSidebar"] [data-testid="stMetricLabel"] { color:#9fb2cf; }

  /* segmented control: light-blue theme, nudged down off the section bar */
  div[data-testid="stSegmentedControl"] { margin-top:8px; }
  div[data-testid="stSegmentedControl"] [role="group"] { gap:4px; }
  div[data-testid="stSegmentedControl"] button {
    background:#eef4fb; border:1px solid #c4d6ea; color:#1a4a86; font-weight:700;
  }
  div[data-testid="stSegmentedControl"] button:hover {
    background:#dfeafa; color:#003781;
  }
  div[data-testid="stSegmentedControl"] button[aria-checked="true"],
  div[data-testid="stSegmentedControl"] button[aria-selected="true"] {
    background:#bcdcff !important; border-color:#5b9be0 !important;
    color:#003781 !important; box-shadow:inset 0 0 0 1px #5b9be0;
  }
  div[data-testid="stSegmentedControl"] button[aria-checked="true"] *,
  div[data-testid="stSegmentedControl"] button[aria-selected="true"] * {
    color:#003781 !important; font-weight:800;
  }

  .foot { color:#9aa6b3; font-size:11.5px; text-align:center; margin-top:26px;
          border-top:1px solid #e6ebf1; padding-top:12px; }

  /* ---- file uploader: refined navy card, gold accent (not a solid fill) ---- */
  section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
    background:#1a3b66; border:1.5px dashed #caa63a; border-radius:10px;
    transition:border-color .15s ease, background .15s ease;
  }
  section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"]:hover {
    background:#1f477a; border-color:#FFD24D;
  }
  section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * {
    color:#dbe4f1 !important;
  }
  section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] svg {
    fill:#FFD24D !important; color:#FFD24D !important;
  }
  section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
    background:#FFD24D; color:#0f2647 !important; border:none; font-weight:800;
    border-radius:7px;
  }
  section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button:hover {
    background:#FFDC6B; color:#0f2647 !important;
  }

  /* ---- download buttons: gold (matches the 'Trade' wordmark) ---- */
  [data-testid="stDownloadButton"] button {
    background:#FFD24D !important; color:#3a2e00 !important;
    border:1px solid #d9a400 !important; font-weight:800;
  }
  [data-testid="stDownloadButton"] button:hover {
    background:#FFDC6B !important; border-color:#c99400 !important; color:#2a2200 !important;
  }
  [data-testid="stDownloadButton"] button * { color:#3a2e00 !important; }

  /* ---- tabs: styled, active clearly highlighted ---- */
  [data-testid="stTabs"] [role="tablist"] { gap:8px; border-bottom:2px solid #d7e0ea; }
  [data-testid="stTabs"] [role="tab"] {
    background:#eef2f6; padding:10px 22px; border-radius:10px 10px 0 0;
    border:1px solid #e0e7ef; border-bottom:none;
  }
  [data-testid="stTabs"] [role="tab"] p { color:#5b6672; font-weight:700; font-size:15px; }
  [data-testid="stTabs"] [role="tab"]:hover { background:#e2eaf3; }
  [data-testid="stTabs"] [role="tab"]:hover p { color:#003781; }
  [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    background:#003781; box-shadow: inset 0 -4px 0 #DAA900;
  }
  [data-testid="stTabs"] [role="tab"][aria-selected="true"] p { color:#fff !important; font-weight:800; }
  [data-testid="stTabs"] [data-baseweb="tab-highlight"] { background:transparent !important; }
</style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def esc(s):
    return html.escape("" if s is None else str(s))


def fmt_amount(num):
    """Brazilian-style figure: '1.234.567' thousands sep, '(1.234)' negatives,
    decimals only when present. Returns '' for None."""
    if num is None:
        return ""
    neg = num < 0
    a = abs(num)
    if a == int(a):
        body = f"{int(a):,}".replace(",", ".")
    else:
        body = f"{a:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"({body})" if neg else body


def fmt_amount_html(num):
    if num is None:
        return ""
    txt = esc(fmt_amount(num))
    return f'<span class="neg">{txt}</span>' if (num is not None and num < 0) else txt


# --------------------------------------------------------------------------- #
# Cached pipeline + workbook build
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_and_process(file_bytes, filename):
    """Parse the upload and return (output, report, mode).
    mode: 'raw' (ran Stages 1-3) or 'structured' (used as-is)."""
    data = json.loads(file_bytes.decode("utf-8"))
    if isinstance(data, list):
        output, report = structure_ocr.process(data)
        return output, report, "raw"
    if isinstance(data, dict) and "statements" in data:
        return data, None, "structured"
    raise ValueError(
        "Unrecognized JSON. Expected a raw OCR list (root = list) or a "
        "structured output (root = dict with a 'statements' key)."
    )


@st.cache_data(show_spinner=False)
def build_excel_bytes(_output, cache_key):
    """Byte-for-byte the same workbook export_excel.py writes from the CLI.
    `_output` is excluded from the cache key (leading underscore); `cache_key`
    (filename + size) keys the cache."""
    wb, _doc_type, _counts = export_excel.build_workbook(_output)
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# --------------------------------------------------------------------------- #
# Row selection — the single place that decides what the screen shows. Delegates
# entirely to export_excel so the screen and the workbook never disagree.
# --------------------------------------------------------------------------- #
def passing_rows(statements, doc_type, group_key):
    """List of (row, amount_n_minus_1, amount_n) the exporter would write."""
    grp = statements.get(group_key, []) or []
    bypass = export_excel.is_small_span(grp)
    out = []
    for r in grp:
        if export_excel.row_passes(r, doc_type, bypass):
            nm1, n = export_excel.pick_amounts(r.get("columns", []), doc_type)
            out.append((r, nm1, n))
    return out


def doc_kpis(statements, doc_type):
    rows = sbs = review = 0
    pages = set()
    has_code = False
    for _key, _t, groups in TABS:
        for gk in groups:
            for r, _nm1, _n in passing_rows(statements, doc_type, gk):
                rows += 1
                if r.get("sbs_label"):
                    sbs += 1
                if r.get("needs_review"):
                    review += 1
                if r.get("code"):
                    has_code = True
                if r.get("page_nb") is not None:
                    pages.add(r.get("page_nb"))
    return {"rows": rows, "sbs": sbs, "review": review,
            "pages": len(pages), "has_code": has_code}


# --------------------------------------------------------------------------- #
# HTML builders
# --------------------------------------------------------------------------- #
def metadata_ribbon(metadata, doc_type):
    def val(key):
        entry = metadata.get(key)
        if isinstance(entry, dict):
            return export_excel.clean_meta(entry.get("value"))
        return ""

    pretty = export_excel.META_PRETTY

    def item(key, right=False):
        v = val(key)
        if not v:
            return ""
        cls = "meta-item r" if right else "meta-item"
        return (f'<div class="{cls}"><span class="meta-k">{esc(pretty.get(key, key))}'
                f'</span><span class="meta-v">{esc(v)}</span></div>')

    badge = (f'<span class="doc-badge" title="{esc(DOC_TYPE_DESC.get(doc_type, ""))}">'
             f'{esc(doc_type)}</span>')
    left = badge + item("CLOSING_DATE") + item("DURATION") + item("ACCOUNT_TYPE")
    right = item("CURRENCY", True) + item("UNIT", True)
    if not right:  # always anchor something on the right for balance
        right = ('<div class="meta-item r"><span class="meta-k">Currency · Moeda</span>'
                 '<span class="meta-v">—</span></div>')
    return (f'<div class="ribbon"><div class="side left">{left}</div>'
            f'<div class="side right">{right}</div></div>')


def _row_label_cell(r):
    """The 'Label · Conta' cell content, with SBS dot + badges."""
    label = r.get("label")
    if label:
        body = esc(label)
    elif r.get("label_missing"):
        body = '<span class="lbl-missing">— empty label · sem rótulo —</span>'
    else:
        body = ""
    return body


def _sbs_cell(r):
    sbs = r.get("sbs_label")
    if not sbs:
        return ""
    badges = ""
    if r.get("needs_review"):
        reason = esc(r.get("sbs_review_reason") or "needs review")
        badges += f'<span class="badge b-review" title="{reason}">REVIEW</span>'
    if r.get("sbs_scope") == "cross_scope":
        badges += '<span class="badge b-cross" title="match recovered out of its home statement">CROSS</span>'
    if r.get("sbs_derived"):
        badges += '<span class="badge b-derived" title="computed value (no source page)">DERIVED Σ</span>'
    return f'<span class="dot">●</span><span class="sbs-name">{esc(sbs)}</span>{badges}'


def statement_table(disp_rows, show_code):
    """disp_rows: list of (row, nm1, n). Returns a full HTML table string."""
    th = ['<th>SBS Label</th>',
          '<th>Label<span class="pt">Conta</span></th>',
          '<th class="ctr">Page<span class="pt">Pág.</span></th>',
          '<th class="num">AMOUNT_N-1<span class="pt">anterior</span></th>',
          '<th class="num">AMOUNT_N<span class="pt">atual</span></th>']
    if show_code:
        th.insert(0, '<th>Code<span class="pt">Código</span></th>')
    head = "<thead><tr>" + "".join(th) + "</tr></thead>"

    body = []
    for r, nm1, n in disp_rows:
        label = r.get("label") or ""
        is_total = export_excel._looks_like_total(label)
        is_sbs = bool(r.get("sbs_label"))
        is_derived = bool(r.get("sbs_derived"))
        cls = "derived" if is_derived else ("total" if is_total else ("sbs" if is_sbs else "detail"))

        page = r.get("page_nb")
        page_txt = "" if page is None else esc(page)
        cells = []
        if show_code:
            cells.append(f'<td class="code">{esc(r.get("code") or "")}</td>')
        cells.append(f'<td>{_sbs_cell(r)}</td>')
        cells.append(f'<td>{_row_label_cell(r)}</td>')
        cells.append(f'<td class="ctr">{page_txt}</td>')
        cells.append(f'<td class="num">{fmt_amount_html(nm1)}</td>')
        cells.append(f'<td class="num">{fmt_amount_html(n)}</td>')
        body.append(f'<tr class="{cls}">' + "".join(cells) + "</tr>")

    return (f'<div class="stmt-wrap"><table class="stmt">{head}'
            f'<tbody>{"".join(body)}</tbody></table></div>')


# --------------------------------------------------------------------------- #
# Section renderer (with the per-section +/- expand control)
# --------------------------------------------------------------------------- #
def render_section(statements, doc_type, group_key, show_code, show_title):
    rows = passing_rows(statements, doc_type, group_key)
    if not rows:
        return False  # nothing to draw for this group

    sbs_rows = [t for t in rows if t[0].get("sbs_label")]
    n_all, n_sbs = len(rows), len(sbs_rows)

    title = export_excel.SECTION_TITLES.get(group_key, group_key)
    if show_title:
        st.markdown(f'<div class="sect-bar">{esc(title)}</div>', unsafe_allow_html=True)

    opt_matched = f"− SBS Matched · {n_sbs}"
    opt_all = f"+ All · {n_all}"
    spacer, ctrl = st.columns([0.64, 0.36])
    with ctrl:
        choice = st.segmented_control(
            "view", [opt_matched, opt_all], default=opt_matched,
            key=f"seg_{group_key}", label_visibility="collapsed",
        )
    expanded = (choice is not None and choice.startswith("+"))

    disp = rows if expanded else sbs_rows
    if not disp:
        st.markdown(
            f'<div class="empty-note">No SBS-labelled rows in this section · '
            f'nenhuma linha rotulada SBS. Press <b>+ All · {n_all}</b> to show every row.</div>',
            unsafe_allow_html=True)
        return True

    st.markdown(statement_table(disp, show_code), unsafe_allow_html=True)
    return True


def render_tab(output, doc_type, groups):
    statements = output.get("statements", {})

    # Show the Code column only when codes exist anywhere in this tab.
    show_code = any(
        r.get("code")
        for gk in groups
        for r, _a, _b in passing_rows(statements, doc_type, gk)
    )
    multi = len([gk for gk in groups if (statements.get(gk) or [])]) > 1
    drew_any = False
    for gk in groups:
        drew = render_section(statements, doc_type, gk, show_code, show_title=multi)
        drew_any = drew_any or drew
        if drew and multi:
            st.write("")  # breathing room between Assets / Liabilities
    if not drew_any:
        st.markdown(
            '<div class="empty-note">No rows for this statement in the uploaded '
            'document · sem linhas para esta demonstração.</div>',
            unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def sidebar():
    with st.sidebar:
        st.markdown(
            '<div class="side-logo">Allianz <span class="gold">Trade</span></div>'
            '<div class="side-tag">OCR Financial Statement Extractor</div>',
            unsafe_allow_html=True)
        st.markdown("---")
        up = st.file_uploader(
            "Upload OCR JSON · Carregar JSON", type=["json"],
            help="Raw OCR provider JSON (root = list) or a structured output "
                 "(root = dict with 'statements').")
        return up


def sidebar_summary(output, report, mode, doc_type, kpis, up_name):
    with st.sidebar:
        tag = "Processed" if mode == "raw" else "Already structured · já estruturado"
        st.success(tag, icon="✅")
        st.markdown(
            f'<div class="side-tag">Detected type · tipo</div>'
            f'<div class="side-logo" style="font-size:17px">{esc(doc_type)}</div>'
            f'<div class="side-tag">{esc(DOC_TYPE_DESC.get(doc_type, ""))}</div>',
            unsafe_allow_html=True)
        st.write("")
        c1, c2 = st.columns(2)
        c1.metric("LST pages", kpis["pages"])
        c2.metric("Rows shown", kpis["rows"])
        c3, c4 = st.columns(2)
        c3.metric("SBS matched", kpis["sbs"])
        c4.metric("Needs review", kpis["review"])
        st.markdown("---")

        stem = os.path.splitext(up_name)[0]
        xlsx = build_excel_bytes(output, f"{up_name}:{len(json.dumps(output))}")
        st.download_button(
            "⬇  Download Excel · Baixar Excel", data=xlsx,
            file_name=f"{stem}_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, type="primary")
        st.caption("Identical to the export_excel.py output.")

        if mode == "raw":
            js = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")
            st.download_button(
                "⬇  Download structured JSON", data=js,
                file_name=f"{stem}_structured.json",
                mime="application/json", use_container_width=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    inject_css()
    up = sidebar()

    if up is None:
        st.markdown(
            '<div class="hero"><h1>Allianz Trade · Financial Statement Extract</h1>'
            '<div class="sub">Brazilian OCR reports → structured, page-classified, '
            'SBS-labelled financial statements.</div></div>',
            unsafe_allow_html=True)
        st.info("⬅  Upload an OCR JSON file in the sidebar to begin · "
                "Carregue um arquivo JSON na barra lateral para começar.")
        with st.container():
            a, b, c = st.columns(3)
            a.markdown("##### 1 · Upload\nRaw OCR JSON or a structured output — "
                       "the app detects which.")
            b.markdown("##### 2 · Review\nBalance, Income and Cash Flow tabs with "
                       "expandable rows and document metadata.")
            c.markdown("##### 3 · Export\nDownload the polished Allianz Excel — "
                       "identical to the pipeline output.")
        st.markdown('<div class="foot">Allianz Trade · OCR Financial Statement '
                    'Extractor · Stages 1-4</div>', unsafe_allow_html=True)
        return

    file_bytes = up.getvalue()
    try:
        with st.spinner("Processing · processando…"):
            output, report, mode = load_and_process(file_bytes, up.name)
    except Exception as exc:
        st.error(f"Could not read this file · não foi possível ler o arquivo:\n\n{exc}")
        return

    doc_type = export_excel.resolve_doc_type(output)
    statements = output.get("statements", {})
    metadata = output.get("metadata", {})
    kpis = doc_kpis(statements, doc_type)

    sidebar_summary(output, report, mode, doc_type, kpis, up.name)

    # Document-level metadata card, on top of the tables (one card for all tabs).
    st.markdown(f'<div class="src-line">Source · fonte: <b>{esc(up.name)}</b></div>',
                unsafe_allow_html=True)
    st.markdown(metadata_ribbon(metadata, doc_type), unsafe_allow_html=True)

    tabs = st.tabs([t[1] for t in TABS])
    for tab, (_key, _title, groups) in zip(tabs, TABS):
        with tab:
            render_tab(output, doc_type, groups)

    # Download also available below the tables, as requested.
    st.markdown("---")
    stem = os.path.splitext(up.name)[0]
    xlsx = build_excel_bytes(output, f"{up.name}:{len(json.dumps(output))}")
    st.download_button(
        "⬇  Download Excel · Baixar relatório", data=xlsx,
        file_name=f"{stem}_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary")

    st.markdown('<div class="foot">Allianz Trade · OCR Financial Statement '
                'Extractor · Stages 1-4 · screen and Excel share one filter</div>',
                unsafe_allow_html=True)


if __name__ == "__main__":
    main()
