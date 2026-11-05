# -*- coding: utf-8 -*-
"""Regression harness core: corpus (reciTAL agent 5485 exports in _recital/), pipeline run, canonical
cell view, Excel view, quality metrics and golden-cell checks.

Metrics (per document) — all computed on the PIPELINE OUTPUT, never on the provider groups alone:
  conservation    output columns == unique in-scope amounts (LST u ONLY_AMOUNT, dedup by box)
  offline         amounts whose y is not on the visual line of their row's name (row-binding errors)
  null_multiline  label_null rows whose amounts span more than one visual line (orphan chain merges)
  period_grid     amounts whose period disagrees with their x_max column's majority (period-tag errors)
  fragments       rows whose label is a wrapped-line fragment (lowercase start after a name-only row)
  missed_words    numeric OCR words inside the table zone/columns that are absent from the output
  excel           rows per sheet; statements of the ground truth that the Excel misses entirely
  serasa_extra    Excel rows outside the Serasa statement zone (analysis sections)
  golden          eye-verified cells (ground_truth.yaml) pass / fail
"""
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
REC = PROJ / "_recital"
sys.path.insert(0, str(PROJ))

import yaml  # noqa: E402

import structure_ocr as S  # noqa: E402
import export_excel as E  # noqa: E402
from anchors_lib import normalize  # noqa: E402

AMOUNT_RE = re.compile(r"^[\(\[]?[-−–]?\s?\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?[\)\]]?$"
                       r"|^[\(\[]?[-−–]?\d{1,3}(?:,\d{1,2})?[\)\]]?$")
SHEETS = [("balance", ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"]),
          ("income", ["INCOME_STATEMENT_LST"]),
          ("cash_flow", ["CASH_FLOW_LST"])]
GT = yaml.safe_load(open(Path(__file__).with_name("ground_truth.yaml"), encoding="utf-8"))


# --------------------------------------------------------------------------- corpus / run
def file_ids():
    """The corpus = the documents of ground_truth.yaml present in _recital/ (a file fetched there
    for another purpose never silently joins the scoreboard)."""
    return sorted(str(f) for f in GT["docs"] if (REC / str(f) / "groups.json").exists())


def load(fid):
    d = REC / str(fid)
    vals = json.load(open(d / "values.json", encoding="utf-8"))
    groups = json.load(open(d / "groups.json", encoding="utf-8"))
    words = json.load(open(d / "words.json", encoding="utf-8")) if (d / "words.json").exists() else None
    meta = json.load(open(d / "file.json", encoding="utf-8"))
    return vals + groups, words, meta


def run(fid, use_words=False):
    """Pipeline on one corpus file. use_words=False = PRODUCTION mode (no words layer: the words
    are loaded only for the metrics); True = the optional words_recovery step is on."""
    data, words, meta = load(fid)
    out, rep = S.process(data, keep_debug=True, words=words if use_words else None)
    return data, words, meta, out, rep


# --------------------------------------------------------------------------- views
def canon_rows(out):
    """Every output row as a hashable signature (stmt, page, label, code, cells sorted by x)."""
    rows = []
    for stmt, rs in out["statements"].items():
        for r in rs:
            # a cell recovered from the OCR words layer carries a 4th marker "w" (absent otherwise,
            # so words-free snapshots keep their exact signatures)
            cols = tuple((c.get("account_type") or "", c.get("period"), c.get("value"))
                         + (("w",) if c.get("source") == "ocr_words" else ())
                         for c in sorted(r.get("columns", []), key=lambda c: c.get("x", 0)))
            rows.append({"stmt": stmt, "page": r.get("page_nb"),
                         "label": r.get("label") if not r.get("label_source") else f"[w] {r['label']}",
                         "code": r.get("code"), "cols": cols, "page_class": r.get("page_class"),
                         "sbs": r.get("sbs_label"), "y": r.get("y")})
    return rows


def excel_view(out):
    """What the Allianz Excel shows: (sheet, stmt, page, label, sbs, N-1, N) per written row."""
    dt = E.resolve_doc_type(out)
    cons = E.consolidado_filter(out["statements"], dt) if hasattr(E, "consolidado_filter") else None
    view = []
    for sheet, groups in SHEETS:
        for g in groups:
            for r, nm1, n in E.passing_rows(out["statements"], dt, g, cons):
                view.append({"sheet": sheet, "stmt": g, "page": r.get("page_nb"), "label": r.get("label"),
                             "sbs": r.get("sbs_label"), "nm1": nm1, "n": n, "y": r.get("y")})
    return dt, view


# --------------------------------------------------------------------------- geometry helpers
def _page_pitch(out):
    """Median vertical distance between consecutive named rows, per page (names the extraction
    returned — labels recovered from the OCR words would shift the estimate between modes)."""
    ys = defaultdict(list)
    for rs in out["statements"].values():
        for r in rs:
            if r.get("name_y") is not None and not r.get("label_source"):
                ys[r["page_nb"]].append(r["name_y"])
    pitch = {}
    for pg, v in ys.items():
        v = sorted(v)
        d = [b - a for a, b in zip(v, v[1:]) if b - a > 0.004]
        if len(d) >= 3:
            pitch[pg] = statistics.median(d)
    return pitch


def _theta(pitch):
    return min(0.0065, max(0.003, 0.45 * pitch)) if pitch else 0.0045


def _bands(xs, gap=0.02, min_n=4):
    xs = sorted(xs)
    if not xs:
        return []
    out = [[xs[0]]]
    for x in xs[1:]:
        (out[-1].append(x) if x - out[-1][-1] <= gap else out.append([x]))
    return [(b[0], b[-1]) for b in out if len(b) >= min_n]


# --------------------------------------------------------------------------- metrics
def metrics(fid, data, words, out, rep):
    m = Counter()
    G = S.load_groups(data)
    # conservation
    keys = defaultdict(set)
    lst_pages = set()
    for g in S.LST_GROUPS:
        for sg in (G.get(g) or {}).get("subgroups", []):
            lst_pages.add(sg.get("page_nb"))
    for g in S.LST_GROUPS + ["ONLY_AMOUNT"]:
        for sg in (G.get(g) or {}).get("subgroups", []):
            if sg.get("page_nb") not in lst_pages:
                continue
            for v in sg.get("values", []):
                if v.get("label") in S.AMOUNT_LABELS and v.get("location"):
                    keys[sg["page_nb"]].add(S.loc_key(v["value"], v["location"]))
    n_in = sum(len(v) for v in keys.values())
    # conservation is about the EXTRACTION: cells recovered from the words layer are counted apart
    n_out = sum(1 for rs in out["statements"].values() for r in rs if not r.get("sbs_derived")
                for c in r.get("columns", []) if c.get("source") != "ocr_words")
    m["cols"] = n_out
    m["conservation_diff"] = n_out - n_in
    m["words_cells"] = sum(1 for rs in out["statements"].values() for r in rs
                           for c in r.get("columns", []) if c.get("source") == "ocr_words")
    m["words_rows"] = sum(1 for rs in out["statements"].values() for r in rs if r.get("source") == "ocr_words")
    m["words_labels"] = sum(1 for rs in out["statements"].values() for r in rs
                            if r.get("label_source") == "ocr_words" and r.get("source") != "ocr_words")
    # offline / null multiline / fragments
    pitch = _page_pitch(out)
    for stmt, rs in out["statements"].items():
        prev = None
        for r in rs:
            if r.get("sbs_derived"):
                continue
            th = _theta(pitch.get(r["page_nb"]))
            cols = r.get("columns", [])
            if r.get("name_y") is not None:
                m["offline"] += sum(1 for c in cols if c.get("y") is not None and abs(c["y"] - r["name_y"]) > th)
            elif r.get("label") is None and len(cols) > 1:
                ys = [c["y"] for c in cols if c.get("y") is not None]
                if ys and max(ys) - min(ys) > th:
                    m["null_multiline"] += 1
            lab = r.get("label") or ""
            if lab[:1].islower() and prev is not None and prev.get("page_nb") == r.get("page_nb") \
                    and prev.get("label") and not prev.get("columns"):
                m["fragments"] += 1
            prev = r
    # period vs column grid (x_max bands per page)
    per_page = defaultdict(list)
    for rs in out["statements"].values():
        for r in rs:
            for c in r.get("columns", []):
                if c.get("x_max") is not None:
                    per_page[r["page_nb"]].append((c["x_max"], c.get("period")))
    for pg, items in per_page.items():
        for lo, hi in _bands([x for x, _ in items]):
            inb = [p for x, p in items if lo - 1e-6 <= x <= hi + 1e-6]
            maj, mc = Counter(inb).most_common(1)[0]
            if mc / len(inb) >= 0.6:
                m["period_grid"] += sum(1 for p in inb if p != maj)
    # missed numeric words (table zone x column bands of the OUTPUT)
    if words:
        outcells = defaultdict(list)
        rowys = defaultdict(list)
        for rs in out["statements"].values():
            for r in rs:
                if r.get("y") is not None and not r.get("sbs_derived"):
                    rowys[r["page_nb"]].append(r["y"])
                for c in r.get("columns", []):
                    if c.get("x_max") is not None:
                        # the token AS RECOGNIZED: a repaired cell (sign / dash, D/C suffix, US
                        # notation) keeps it in value_raw and is still the word on the page
                        outcells[r["page_nb"]].append((c.get("value_raw") or c["value"], c["x_max"], c["y"]))
        for pg, cells in outcells.items():
            if pg is None or pg < 1 or pg > len(words):
                continue
            bands = _bands([x for _, x, _ in cells], min_n=3)
            y0, y1 = min(rowys[pg]) - 0.012, max(rowys[pg]) + 0.012
            have = defaultdict(list)
            for v, x, y in cells:
                have[v].append((x, y))
            for t in words[pg - 1]:
                c = t.get("coordinates") or {}
                w = (t.get("word") or "").strip()
                if not c or not AMOUNT_RE.match(w):
                    continue
                yy = (c["y_min"] + c["y_max"]) / 2
                if not (y0 <= yy <= y1) or not any(lo - 0.006 <= c["x_max"] <= hi + 0.006 for lo, hi in bands):
                    continue
                m["words_in_table"] += 1
                if not any(abs(x - c["x_max"]) < 0.0006 and abs(y - yy) < 0.0006 for x, y in have.get(w, ())):
                    m["missed_words"] += 1
    return m


def excel_checks(fid, out, view):
    """Statements of the ground truth that are missing from the Excel; Serasa analysis rows that leak in."""
    res = Counter()
    gt = GT["docs"].get(str(fid)) or {}
    stm = gt.get("statements") or {}
    for sheet, _ in SHEETS:
        rows = [v for v in view if v["sheet"] == sheet]
        res[f"xl_{sheet}"] = len(rows)
        if sheet in stm:
            on_gt = [v for v in rows if v["page"] in set(stm[sheet])]
            if not on_gt:
                res["xl_missing_statements"] += 1
                res[f"MISSING_{sheet}"] = 1
    zone = gt.get("serasa_zone")
    if zone:
        (p0, y0), (p1, y1) = zone
        for v in view:
            if v["page"] is None or v["y"] is None:
                continue
            pos = (v["page"], v["y"])
            if pos < (p0, y0) or pos > (p1, y1):
                res["serasa_extra"] += 1
    return res


def _cell_ok(r, g):
    want = {k: g[k] for k in ("N", "N-1") if k in g}
    at = (g.get("at") or "").lower()
    for per, val in want.items():
        period = "AMOUNT_N" if per == "N" else "AMOUNT_N-1"
        ok = any(c.get("period") == period and c.get("value") == val
                 and (not at or (c.get("account_type") or "").lower().startswith(at))
                 for c in r.get("columns", []))
        if not ok:
            return False
    return True


def golden(fid, out, view):
    res = []
    for g in GT.get("golden", []):
        if g["doc"] != str(fid):
            continue
        cands = []
        for rs in out["statements"].values():
            for r in rs:
                if r.get("page_nb") != g["page"]:
                    continue
                nl = normalize(r.get("label") or "")
                if ("label_exact" in g and nl == g["label_exact"]) or ("label" in g and g["label"] in nl):
                    cands.append(r)
        ok = any(_cell_ok(r, g) for r in cands)
        item = {"g": g, "json_ok": ok}
        if g.get("excel"):
            want_n = E.to_number(g.get("N")) if "N" in g else None
            want_nm1 = E.to_number(g.get("N-1")) if "N-1" in g else None
            lab = g.get("label") or g.get("label_exact")
            item["excel_ok"] = any(v["sheet"] == g["excel"] and v["page"] == g["page"]
                                   and lab in normalize(v["label"] or "")
                                   and (want_n is None or v["n"] == want_n) and (want_nm1 is None or v["nm1"] == want_nm1)
                                   for v in view)
        res.append(item)
    return res


def evaluate(fid, use_words=False):
    data, words, meta, out, rep = run(fid, use_words)
    dt, view = excel_view(out)
    m = metrics(fid, data, words, out, rep)
    m.update(excel_checks(fid, out, view))
    gold = golden(fid, out, view)
    sf = out.get("sbs_fields") or {}
    fields = {f: [e.get("AMOUNT_N-1"), e.get("AMOUNT_N"), e.get("method")] for f, e in sf.get("fields", {}).items()}
    m["id_ok"] = sum(1 for c in sf.get("checks", []) if c["ok"])
    m["id_bad"] = sum(1 for c in sf.get("checks", []) if not c["ok"])
    m["sbs_review"] = sum(1 for e in sf.get("fields", {}).values() if e.get("needs_review"))
    return {"fid": str(fid), "name": meta.get("name"), "doc_type": dt, "metrics": dict(m), "words_mode": use_words,
            "sbs_fields": fields, "checks": sf.get("checks", []),
            "rows": canon_rows(out), "excel": view, "golden": gold}
