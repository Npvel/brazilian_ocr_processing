# -*- coding: utf-8 -*-
"""Regression harness CLI (corpus = reciTAL agent 5485 exports in _recital/, see ground_truth.yaml).

    python3.13 -E -P harness/run.py snapshot <tag> [--words] [ids...]
                                                              run the pipeline on the corpus, save + score
                                                              (--words: optional OCR-words recovery ON;
                                                              default = production mode, no words layer)
    python3.13 -E -P harness/run.py score <tag>               scoreboard of a saved snapshot
    python3.13 -E -P harness/run.py diff <tagA> <tagB> [-v]   cell-level + Excel-level differences
    python3.13 -E -P harness/run.py goldens                   8 local structured_output*.json in sync?
    python3.13 -E -P harness/run.py selftest                  synthetic rule checks (company-only docs)
    python3.13 -E -P harness/recital_fetch.py ...             read-only reciTAL API (new PDFs, page images)
Snapshots: harness/snapshots/<tag>/<file_id>.json (confidential data -> git-ignored via *.json).
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hcore as H  # noqa: E402

SNAP = Path(__file__).resolve().parent / "snapshots"
COLS = ["cols", "conservation_diff", "offline", "null_multiline", "period_grid", "fragments",
        "missed_words", "words_cells", "words_rows", "words_labels",
        "xl_balance", "xl_income", "xl_cash_flow", "xl_missing_statements", "serasa_extra",
        "id_ok", "id_bad", "sbs_review"]
SHORT = {"cols": "cols", "conservation_diff": "cons", "offline": "offln", "null_multiline": "nullML",
         "period_grid": "perGr", "fragments": "frag", "missed_words": "missW",
         "words_cells": "wCell", "words_rows": "wRow", "words_labels": "wLab", "id_ok": "idOK", "id_bad": "idBad", "sbs_review": "sbsRv", "xl_balance": "xlB",
         "xl_income": "xlI", "xl_cash_flow": "xlC", "xl_missing_statements": "MISS", "serasa_extra": "srsX"}


def snapshot(tag, ids, use_words=False):
    d = SNAP / tag
    d.mkdir(parents=True, exist_ok=True)
    print(f"mode: {'OCR words recovery ON' if use_words else 'production (no words layer)'}")
    for fid in ids:
        res = H.evaluate(fid, use_words)
        (d / f"{fid}.json").write_text(json.dumps(res, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"  {fid} done", flush=True)
    score(tag)


def _load(tag):
    return {p.stem: json.load(open(p, encoding="utf-8")) for p in sorted((SNAP / tag).glob("*.json"))}


def score(tag):
    snaps = _load(tag)
    mode = sorted({s.get("words_mode", False) for s in snaps.values()})
    print(f"\n=== scoreboard [{tag}]  ({len(snaps)} docs, words mode {mode})")
    print(f"{'file':8} {'name':28} {'type':9} " + " ".join(f"{SHORT[c]:>6}" for c in COLS) + "  golden")
    tot = Counter()
    gtot = Counter()
    for fid, s in snaps.items():
        m = s["metrics"]
        g_ok = sum(1 for g in s["golden"] if g["json_ok"])
        g_n = len(s["golden"])
        x_ok = sum(1 for g in s["golden"] if g.get("excel_ok"))
        x_n = sum(1 for g in s["golden"] if "excel_ok" in g)
        for c in COLS:
            tot[c] += m.get(c, 0)
        gtot["ok"] += g_ok; gtot["n"] += g_n; gtot["xok"] += x_ok; gtot["xn"] += x_n
        gs = f"{g_ok}/{g_n}" + (f" xl {x_ok}/{x_n}" if x_n else "") if g_n else ""
        print(f"{fid:8} {(s['name'] or '')[:28]:28} {s['doc_type']:9} "
              + " ".join(f"{m.get(c, 0):>6}" for c in COLS) + f"  {gs}")
    print(f"{'TOTAL':8} {'':28} {'':9} " + " ".join(f"{tot[c]:>6}" for c in COLS)
          + f"  {gtot['ok']}/{gtot['n']} xl {gtot['xok']}/{gtot['xn']}")
    # golden by expected fix step
    by = Counter()
    for s in snaps.values():
        for g in s["golden"]:
            by[(g["g"].get("fix"), g["json_ok"])] += 1
    print("golden by fix step (pass/total):",
          {k: f"{by[(k, True)]}/{by[(k, True)] + by[(k, False)]}" for k in sorted({k for k, _ in by})})
    fails = [(s["fid"], g["g"]) for s in snaps.values() for g in s["golden"] if g["g"].get("fix") == "now" and not g["json_ok"]]
    if fails:
        print("!! golden marked fix=now FAILING:", fails)


def diff(a, b, verbose=False):
    A, B = _load(a), _load(b)
    print(f"=== diff {a} -> {b}")
    for fid in sorted(set(A) | set(B)):
        sa, sb = A.get(fid), B.get(fid)
        if not sa or not sb:
            print(f"  {fid}: only in {'B' if sb else 'A'}"); continue
        key = lambda r: (r["stmt"], r["page"], r["label"], r["code"], tuple(map(tuple, r["cols"])))
        ca, cb = Counter(map(key, sa["rows"])), Counter(map(key, sb["rows"]))
        xk = lambda v: (v["sheet"], v["page"], v["label"], v["nm1"], v["n"], v["sbs"])
        xa, xb = Counter(map(xk, sa["excel"])), Counter(map(xk, sb["excel"]))
        rm, ad = ca - cb, cb - ca
        xrm, xad = xa - xb, xb - xa
        ma, mb = sa["metrics"], sb["metrics"]
        dm = {k: (ma.get(k, 0), mb.get(k, 0)) for k in COLS if ma.get(k, 0) != mb.get(k, 0)}
        ga = sum(1 for g in sa["golden"] if g["json_ok"]); gb = sum(1 for g in sb["golden"] if g["json_ok"])
        fa, fb = sa.get("sbs_fields") or {}, sb.get("sbs_fields") or {}
        fdiff = {f: (fa.get(f), fb.get(f)) for f in sorted(set(fa) | set(fb))
                 if (fa.get(f) or [None, None])[:2] != (fb.get(f) or [None, None])[:2]}
        if not (rm or ad or xrm or xad or dm or ga != gb or fdiff):
            print(f"  {fid}: identical"); continue
        print(f"  {fid} {sa['name'][:30]}: rows -{sum(rm.values())} +{sum(ad.values())} | excel -{sum(xrm.values())} "
              f"+{sum(xad.values())} | metrics {dm} | golden {ga}->{gb}")
        for f, (va, vb) in fdiff.items():
            print(f"       SBS {f}: {va} -> {vb}")
        if verbose:
            for k in list(xrm.elements())[:25]:
                print(f"       XL- {k}")
            for k in list(xad.elements())[:25]:
                print(f"       XL+ {k}")
            for k in list(rm.elements())[:15]:
                print(f"       J-  {k}")
            for k in list(ad.elements())[:15]:
                print(f"       J+  {k}")


LOCAL_GOLDENS = [
    ("comprehansive_sample.json", "structured_output.json"),
    ("sample_2.json", "structured_output_sample2.json"),
    ("itr_samples.json", "structured_output_itr.json"),
    ("provider_sample.json", "structured_output_provider.json"),
    ("complicated_empty_labels_sample.json", "structured_output_complicated.json"),
    ("complicated_sample_2 (Demonstrativos 2023 Alvorada Pneus Distribuidora).json",
     "structured_output_complicated_sample2.json"),
    ("serasa_report_1.json", "structured_output_serasa1.json"),
    ("serasa_report_2.json", "structured_output_serasa2.json"),
]


def goldens():
    """Are the 8 local structured_output*.json byte-identical to what the current code produces?"""
    for inp, gold in LOCAL_GOLDENS:
        out, rep = H.S.process(json.load(open(H.PROJ / inp, encoding="utf-8")))
        same = (json.dumps(out, ensure_ascii=False, sort_keys=True)
                == json.dumps(json.load(open(H.PROJ / gold, encoding="utf-8")), ensure_ascii=False, sort_keys=True))
        print(f"  {gold:46} {'in sync' if same else 'OUT OF SYNC -> regenerate with structure_ocr.py'}")


def selftest():
    """Synthetic checks of rules the corpus cannot exercise (user rule 2026-11-04: consolidated figures
    if the document presents them, else the company's) — a company-only document must NOT be empty."""
    import copy
    from doc_type import detect_doc_type
    ok = True
    data, words, meta, out, rep = H.run("2844779")          # ITR: Individuais p1-8, Consolidadas p9-18
    o = copy.deepcopy(out)
    for k in o["statements"]:
        o["statements"][k] = [r for r in o["statements"][k] if (r.get("page_nb") or 0) < 9]
    o["document_type"] = detect_doc_type(o["statements"], o["metadata"])
    _, _, c1 = H.E.build_workbook(o)
    _, _, c2 = H.E.build_workbook(out)
    for name, cond, counts in (
            ("ITR company-only -> company figures exported", min(c1.values()) > 0, c1),
            ("ITR with both -> Consolidado only (DVA / DMPL rows excluded since 2026-11-04; coded lines are "
             "never deduplicated since the audit fix 2026-11-04: + 2.01.02.01 Fornecedores Nacionais)",
             {k: c2[k] for k in ("balance", "income", "cash_flow")} == {"balance": 76, "income": 26, "cash_flow": 36},
             c2)):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}  {counts}")
        ok &= cond
    data, words, meta, out, rep = H.run("2834911")          # DFP 4-col: strip the Consolidado columns
    o = copy.deepcopy(out)
    for k in o["statements"]:
        for r in o["statements"][k]:
            r["columns"] = [c for c in r["columns"] if not (c.get("account_type") or "").lower().startswith("consolidad")]
    o["document_type"] = detect_doc_type(o["statements"], o["metadata"])
    _, _, c3 = H.E.build_workbook(o)
    cond = min(c3.values()) > 0
    print(f"  {'PASS' if cond else 'FAIL'}  DFP Controladora-only -> company figures exported  {c3}")
    ok &= cond
    # The OCR-words step is OPTIONAL (production may have no words layer): without words, with an
    # empty layer, or with the module switched off, the output must be the words-free one exactly.
    data, words, meta = H.load("3029574")
    dump = lambda o: json.dumps(o, ensure_ascii=False, sort_keys=True)
    base = dump(H.S.process(data, keep_debug=True)[0])
    saved = H.S._WORDS_AVAILABLE
    try:
        H.S._WORDS_AVAILABLE = False
        off = dump(H.S.process(data, keep_debug=True, words=words)[0])
    finally:
        H.S._WORDS_AVAILABLE = saved
    empty = dump(H.S.process(data, keep_debug=True, words=[])[0])
    on = dump(H.S.process(data, keep_debug=True, words=words)[0])
    for name, cond in (("words module off -> words-free output", off == base),
                       ("empty words layer -> words-free output", empty == base),
                       ("words layer on -> recovery happens (Guabi liability subtotals)", on != base)):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= cond
    print("selftest", "OK" if ok else "FAILED")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1]
    if cmd == "snapshot":
        args = [a for a in sys.argv[3:] if a != "--words"]
        snapshot(sys.argv[2], args or H.file_ids(), "--words" in sys.argv[3:])
    elif cmd == "score":
        score(sys.argv[2])
    elif cmd == "diff":
        diff(sys.argv[2], sys.argv[3], "-v" in sys.argv)
    elif cmd == "goldens":
        goldens()
    elif cmd == "selftest":
        selftest()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
