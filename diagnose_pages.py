#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
diagnose_pages.py  —  READ-ONLY diagnostic pass for primary-statement detection.

Does NOT modify any data. For every page that carries LST line-items it computes
the signals used to classify primary statements vs. notes/junk:

  * anchors found per statement family + whether the REQUIRED set is complete
  * row density (line-items) and vertical span (top..bottom coverage)
  * distinct amount columns (x-bands) - 2/4 = primary-like, many = segment note
  * accounting identity check (balance: ativo == passivo+PL) when computable

Output: a human-readable table to stdout + page_diagnostics.json for tooling.

Matching primitives are imported from anchors_lib (shared with the production
classifier) so the logic can never drift between the two tools.
"""

import json
import sys
from collections import defaultdict, Counter

import yaml

from anchors_lib import (
    anchor_match, anchor_variants, parse_amount, yc, xc, x_bands,
    FUZZ_BACKEND as _FUZZ,
)

LST_GROUPS = ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST",
              "INCOME_STATEMENT_LST", "CASH_FLOW_LST"]
AMOUNT_LABELS = ("AMOUNT_N", "AMOUNT_N-1")


# --------------------------------------------------------------------------- #
# Core diagnostic
# --------------------------------------------------------------------------- #
def diagnose(data, cfg):
    groups = {el.get("group_name"): el for el in data if el.get("group_name")}
    stop = set(cfg["stopwords"])
    benign = set(cfg["benign_insertions"])
    thr = cfg["match_threshold"]
    stmts = cfg["statements"]

    # Collect, per page: names (with statement origin), amount x-centers, y-span.
    page_names = defaultdict(list)        # page -> [(stmt_group, raw_name, y)]
    page_amount_x = defaultdict(list)     # page -> [x_center]
    page_amounts = defaultdict(list)      # page -> [(y, x, period, value)]
    page_y = defaultdict(list)            # page -> [y_center of every element]

    for g in LST_GROUPS:
        for sg in groups.get(g, {}).get("subgroups", []):
            pg = sg["page_nb"]
            for v in sg["values"]:
                loc = v["location"]
                page_y[pg].append(yc(loc))
                if v["label"] in AMOUNT_LABELS:
                    page_amount_x[pg].append(xc(loc))
                    page_amounts[pg].append((yc(loc), xc(loc), v["label"], v["value"]))
                elif v["label"] == g:
                    page_names[pg].append((g, v["value"], yc(loc)))

    # Pre-pass: which balance side does each page carry? Balance in BR reports
    # spans TWO facing pages (Ativo | Passivo+PL), so BALANCE completeness is
    # evaluated across ADJACENT pages, not within one page.
    bal_side = {}
    bal_defs = stmts["BALANCE"]["required_anchors"]
    for pg, names in page_names.items():
        sides = set()
        for a in bal_defs:
            variants = anchor_variants(a)
            if any(anchor_match(variants, raw, stop, benign, thr)[0] for _, raw, _ in names):
                sides.add(a["id"])
        if sides:
            bal_side[pg] = sides

    def balance_complete(pg):
        have = set(bal_side.get(pg, set()))
        for adj in (pg - 1, pg + 1):
            have |= bal_side.get(adj, set())
        need = {a["id"] for a in bal_defs}
        return need.issubset(have)

    results = []
    for pg in sorted(page_names):
        names = page_names[pg]
        ys = page_y[pg]
        n_rows = len(names)
        n_amounts = len(page_amount_x[pg])
        vspan = (max(ys) - min(ys)) if ys else 0.0
        n_cols = len(x_bands(page_amount_x[pg]))

        anchors_hit = {}
        completeness = {}
        for fam, fdef in stmts.items():
            req = fdef.get("required_anchors", [])
            hits = []
            for a in req:
                variants = anchor_variants(a)
                if any(anchor_match(variants, raw, stop, benign, thr)[0]
                       for _, raw, _ in names):
                    hits.append(a["id"])
            anchors_hit[fam] = hits
            completeness[fam] = (len(hits) == len(req) and len(req) > 0)

        # BALANCE is two-page: complete if this page carries a balance side AND
        # the full required set is present across this page and its neighbours.
        if pg in bal_side and balance_complete(pg):
            completeness["BALANCE"] = True
            anchors_hit["BALANCE"] = sorted(bal_side.get(pg, set()))

        # Balance identity (best-effort): AMOUNT_N on 'Total do ativo' row vs
        # 'Total do passivo e PL' row.
        identity = {}
        if completeness.get("BALANCE"):
            ativo = _anchor_row_value("total_ativo", stmts["BALANCE"], names,
                                      page_amounts[pg], stop, benign, thr)
            passpl = _anchor_row_value("total_passivo_pl", stmts["BALANCE"], names,
                                       page_amounts[pg], stop, benign, thr)
            if ativo is not None and passpl is not None:
                tol = max(abs(ativo), 1) * stmts["BALANCE"]["identity"]["tolerance_pct"] / 100.0
                identity["balance"] = {
                    "ativo": ativo, "passivo_pl": passpl,
                    "balances": abs(ativo - passpl) <= tol,
                }

        # Provisional class (heuristic preview - final logic in the classifier).
        primary_fams = [f for f, c in completeness.items() if c]
        if n_amounts == 0:
            pclass = "JUNK_no_amounts"
        elif n_rows <= 2 and n_amounts <= 3:
            pclass = "JUNK_sparse"
        elif primary_fams and n_cols <= 4:
            pclass = "PRIMARY:" + ",".join(primary_fams)
        elif primary_fams and n_cols > 4:
            pclass = "NOTE_segment(complete-but-wide):" + ",".join(primary_fams)
        else:
            pclass = "NOTE"

        results.append({
            "page": pg,
            "n_rows": n_rows,
            "n_amounts": n_amounts,
            "n_cols": n_cols,
            "vspan": round(vspan, 3),
            "anchors": {f: h for f, h in anchors_hit.items() if h},
            "complete": primary_fams,
            "identity": identity,
            "provisional_class": pclass,
        })
    return results


def _anchor_row_value(anchor_id, fdef, names, amounts, stop, benign, thr):
    """AMOUNT_N value on the same row (y) as the named anchor; None if absent."""
    target = next((a for a in fdef["required_anchors"] if a["id"] == anchor_id), None)
    if not target:
        return None
    variants = anchor_variants(target)
    for _, raw, y in names:
        if anchor_match(variants, raw, stop, benign, thr)[0]:
            same = [(abs(ay - y), parse_amount(val))
                    for ay, ax, per, val in amounts
                    if per == "AMOUNT_N" and abs(ay - y) <= 0.012]
            same = [(d, v) for d, v in same if v is not None]
            if same:
                return min(same)[1]
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    src = sys.argv[1] if len(sys.argv) > 1 else "comprehansive_sample.json"
    cfg = yaml.safe_load(open("anchors.yaml", encoding="utf-8"))
    data = json.load(open(src, encoding="utf-8"))

    results = diagnose(data, cfg)

    print(f"[fuzzy backend: {_FUZZ}]  pages with LST line-items: {len(results)}\n")
    hdr = f"{'pg':>4} {'rows':>4} {'amts':>5} {'cols':>4} {'vspan':>6}  {'complete':22} class"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        comp = ",".join(r["complete"]) if r["complete"] else "-"
        idflag = ""
        if r["identity"].get("balance"):
            idflag = "  [bal=" + ("OK" if r["identity"]["balance"]["balances"] else "X") + "]"
        print(f"{r['page']:>4} {r['n_rows']:>4} {r['n_amounts']:>5} {r['n_cols']:>4} "
              f"{r['vspan']:>6.3f}  {comp:22} {r['provisional_class']}{idflag}")

    print("\n=== SUMMARY ===")
    by_class = Counter(r["provisional_class"].split(":")[0].split("(")[0] for r in results)
    for k, c in by_class.most_common():
        print(f"  {k}: {c}")
    primaries = [r for r in results if r["provisional_class"].startswith("PRIMARY")]
    print(f"\n  PRIMARY candidate pages: {[r['page'] for r in primaries]}")
    for fam in ["BALANCE", "INCOME", "CASH_FLOW"]:
        pgs = [r["page"] for r in results if fam in r["complete"]]
        print(f"  complete {fam}: pages {pgs}")

    json.dump(results, open("page_diagnostics.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\nWritten: page_diagnostics.json")


if __name__ == "__main__":
    main()
