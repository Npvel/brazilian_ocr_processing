# -*- coding: utf-8 -*-
"""Audit runner — one reciTAL file -> the pipeline in BOTH modes -> everything an auditor needs.

    python3.13 -E -P harness/audit_run.py <file_id> [<file_id> ...] [--prod] [--no-images] [--all-pages]

Per file, into _audit/<file_id>/ (confidential data, git-ignored):
    file.json values.json groups.json words.json   reciTAL extraction (READ-ONLY GETs)
    <id>_raw.json / <id>_words.json                the two uploads of the Streamlit app
    out_words.json   report_words.xlsx             pipeline WITH the OCR words layer (production mode,
                                                   the default since 2026-11-12)
    out_nowords.json report_nowords.xlsx           pipeline WITHOUT it (the fallback when the layer is
                                                   missing; audit folders made before 2026-11-12 call
                                                   this run out_prod.json / report_prod.xlsx)
    pages/page_<n>.png                             page images of every page the Excel uses + every
                                                   page Stage 2 called PRIMARY (--all-pages: all)
    summary.md                                     what the pipeline produced (facts, no judgement)

reciTAL guardrails (harness/recital_fetch.py): GET only, never /verify, /next/, /prepare/,
/version/; the token is never printed nor written. Uploading a PDF is done by the user in the
reciTAL web UI (agent Brazil_OCR202606_V4, doctype 5485), not here.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))

import structure_ocr as S  # noqa: E402
import export_excel as E  # noqa: E402
import recital_fetch as R  # noqa: E402

AUDIT_ROOT = PROJ / "_audit"
SHEETS = [("balance", ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"]),
          ("income", ["INCOME_STATEMENT_LST"]), ("cash_flow", ["CASH_FLOW_LST"])]


def _excel_pages(out):
    dt = E.resolve_doc_type(out)
    cons = E.consolidado_filter(out["statements"], dt)
    pages = {}
    for sheet, groups in SHEETS:
        pg = set()
        n = 0
        for g in groups:
            for r, _nm1, _n in E.passing_rows(out["statements"], dt, g, cons):
                n += 1
                if r.get("page_nb"):
                    pg.add(r["page_nb"])
        pages[sheet] = (n, sorted(pg))
    return dt, cons, pages


def _fmt(v):
    return "" if v is None else format(v, ",.0f").replace(",", ".") if abs(v) >= 1 else str(v)


def _summary(meta, out_w, rep_w, out_n, rep_n):
    L = [f"# Audit run — file {meta.get('id')}: {meta.get('name')}", "",
         f"- reciTAL status: `{meta.get('status')}`, pages: {len(meta.get('pages') or [])}"]
    for mode, out, rep in (("production — OCR words layer ON (out_words)", out_w, rep_w),
                           ("fallback — no words layer (out_nowords)", out_n, rep_n)):
        dt, cons, pages = _excel_pages(out)
        L += ["", f"## Mode: {mode}", "",
              f"- document type `{dt}`, number format `{out.get('number_format')}`, "
              f"columns kept: {'Consolidado only' if cons else 'all columns of the rows shown'}"]
        md = out.get("metadata") or {}
        L.append("- metadata: " + "; ".join(f"{k}={E.meta_display(k, v)}" for k, v in md.items()))
        for sheet, (n, pg) in pages.items():
            L.append(f"- Excel `{sheet}`: {n} rows from pages {pg}")
        prim = {}
        for pg, v in (out.get("page_classification") or {}).items():
            if str(v.get("page_class", "")).startswith("PRIMARY"):
                prim.setdefault(v["page_class"], []).append(int(pg))
        L.append("- Stage 2 PRIMARY pages: " + (", ".join(f"{k} {sorted(v)}" for k, v in sorted(prim.items())) or "none"))
        if rep.get("words"):
            w = rep["words"]
            L.append(f"- words recovery: cells {w.get('cells_added', 0)} + {w.get('cells_added_in_new_rows', 0)} "
                     f"in {w.get('rows_added', 0)} new lines, labels {w.get('labels_added', 0)}, columns borrowed "
                     f"{w.get('columns_borrowed', 0)}")
        sf = out.get("sbs_fields") or {}
        L += ["", "| SBS field | statement | N-1 | N | how |", "|---|---|---|---|---|"]
        for f, e in (sf.get("fields") or {}).items():
            L.append(f"| {f} | {E._STMT_SHORT.get(e.get('statement'), e.get('statement'))} | {_fmt(e.get('AMOUNT_N-1'))} "
                     f"| {_fmt(e.get('AMOUNT_N'))} | {E.sbs_method_text(e)}{' — REVIEW' if e.get('needs_review') else ''} |")
        checks = sf.get("checks") or []
        L += ["", "Accounting checks: " + (", ".join(
            f"{'OK' if c['ok'] else 'FAILED'} {c['period']} {c['identity'].replace('Amount', '')}" for c in checks) or "none")]
    return "\n".join(L) + "\n"


def run(c, fid, prod, images, all_pages):
    d = R.fetch(c, fid, prod, quiet=True, root=AUDIT_ROOT)
    raw_p, words_p = R.export(d)
    meta = json.loads((d / "file.json").read_text(encoding="utf-8"))
    data = json.loads(raw_p.read_text(encoding="utf-8"))
    words = json.loads(words_p.read_text(encoding="utf-8"))
    out_w, rep_w = S.process(data, words=words)
    out_n, rep_n = S.process(data)
    for tag, out, rep in (("words", out_w, rep_w), ("nowords", out_n, rep_n)):
        (d / f"out_{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        ser = {k: (dict(v) if hasattr(v, "items") else v) for k, v in rep.items()}
        (d / f"out_{tag}_report.json").write_text(json.dumps(ser, ensure_ascii=False, indent=1, default=str),
                                                  encoding="utf-8")
        wb, _dt, _counts = E.build_workbook(out)
        wb.save(d / f"report_{tag}.xlsx")
    pages = set()
    if all_pages:
        pages = set(range(1, len(meta.get("pages") or []) + 1))
    else:
        for out in (out_w, out_n):
            for _sheet, (_n, pg) in _excel_pages(out)[2].items():
                pages.update(pg)
            for pg, v in (out.get("page_classification") or {}).items():
                if str(v.get("page_class", "")).startswith("PRIMARY"):
                    pages.add(int(pg))
    got = 0
    if images:
        for pg in sorted(pages):
            if not any((d / "pages").glob(f"page_{pg}.*")):
                R.page_image(c, fid, pg, d / "pages")
                got += 1
    (d / "summary.md").write_text(_summary(meta, out_w, rep_w, out_n, rep_n), encoding="utf-8")
    print(f"  {fid}: {meta.get('name')!r} -> {d}  (pages fetched now: {got}, total wanted: {len(pages)})")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    c = R.RecitalClient()
    for a in args:
        run(c, int(a), "--prod" in sys.argv, "--no-images" not in sys.argv, "--all-pages" in sys.argv)


if __name__ == "__main__":
    main()
