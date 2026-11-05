# -*- coding: utf-8 -*-
"""
sbs_fields.py — ONE value per SBS field and period: the contract handed downstream.

Rows carry `sbs_label` = WHAT a printed line is. The SBS system needs ONE figure per field and
period. When several exported rows share a field (cash + marketable securities, current + deferred
tax, a cost of sales printed on two lines, a non-current block printed without its own total), this
module decides — deterministically and with provenance — whether to SUM them, take their printed
TOTAL, or PICK one, and proves the choice with the statements' accounting identities whenever
their figures are present:

    totalAssets   = totalCurrentAssets + totalFixedAssets
    totalAssets   = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm + shareholdersFunds
    totalLiabilities = financialLiabilitiesShortTerm + financialLiabilitiesLongTerm
        (USER MAPPING 2026-11-04: financialLiabilitiesShortTerm / LongTerm = the TOTAL current /
        non-current liabilities; shortTermLiabilitiesAmount / longTermLiabilitiesAmount = the short- /
        long-term credits and loans)
    grossProfit   = turnover ± rawMaterialsAndConsumables      (costs printed negative or positive)
    posttaxProfit = pretaxProfit ± incomeTaxExpenseRefund

A held-for-sale line printed OUTSIDE the current / non-current subtotals (IFRS 5: current) joins the
current total where the first two identities prove it (user decision 2026-11-05, HELD_FOR_SALE_FIELD).

Field kinds:
    TOTAL    one printed line expected (totals and results). Several different candidates: their
             printed total if one is the sum of the others, else the alternative an identity proves
             (one of them, or their sum — MBP's non-current assets printed as two block subtotals
             178.256 + 285.932 = total assets − current assets), else the first printed (review).
             Operating profit: the candidate printed BEFORE the financial result (Serasa «RESULT
             ATIVIDADE», not «RESULT OPERACIONAL», which comes after it).
    CURRENT  current-section components (the SBS definition says *_CURRENT): trade debtors / trade
             creditors / stock — a non-current «Contas a receber» is not summed in.
    (other)  components, summed (unless one of them is the printed total of the others); an
             identity may keep a subset (Copasa: the DRE cost line, not its detail line).

Uses the export's own row selection (selection.passing_rows), so the values are those of the
rows the Excel shows. Standard library only. Never changes a row.
"""
from collections import defaultdict
from itertools import combinations, product

from selection import (consolidado_filter, form_pages, passing_rows, pick_amounts, resolve_doc_type, row_passes,
                       is_small_span)

GROUPS = ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST", "INCOME_STATEMENT_LST", "CASH_FLOW_LST"]
PERIODS = ("AMOUNT_N", "AMOUNT_N-1")

TOTAL_FIELDS = {
    "totalAssetsAmount", "totalCurrentAssetsAmount", "totalFixedAssetsAmount",
    "financialLiabilitiesShortTerm", "financialLiabilitiesLongTerm", "totalLiabilitiesAmount",
    "shareholdersFundsAmount", "turnoverAmount", "grossProfitAmount", "operatingProfitAmount",
    "ebitdaAmount", "pretaxProfitAmount", "posttaxProfitAmount", "netGroupProfitAmount",
    "operatingCashFlowAmount", "netCashInvestmentAmount",
}
CURRENT_FIELDS = {"tradeDebtorsAmount", "tradeCreditorsAmount", "stockAmount"}
FINANCIAL_RESULT_FIELDS = {"interestIncomeAndSimilarAmount", "interestPaidAmount"}
FINANCIAL_RESULT_CAPTIONS = ("resultado financeiro", "result financeiro", "receitas financeiras",
                             "despesas financeiras")

# (target, terms, mode): "sum" = target == Σ terms; "signed" = target == a + b or a − b
IDENTITIES = [
    ("totalAssetsAmount", ("totalCurrentAssetsAmount", "totalFixedAssetsAmount"), "sum"),
    ("totalAssetsAmount", ("financialLiabilitiesShortTerm", "financialLiabilitiesLongTerm",
                           "shareholdersFundsAmount"), "sum"),
    ("totalLiabilitiesAmount", ("financialLiabilitiesShortTerm", "financialLiabilitiesLongTerm"), "sum"),
    ("grossProfitAmount", ("turnoverAmount", "rawMaterialsAndConsumablesAmount"), "signed"),
    ("posttaxProfitAmount", ("pretaxProfitAmount", "incomeTaxExpenseRefundAmount"), "signed"),
]
MAX_SUBSET_ROWS = 5

# Statements whose figure IS the field's value, when narrower than the labelling scope. Income tax
# is labelled in the DRE and in the DFC (double-home), but the DFC line is an indirect-method
# ADD-BACK (the expense with the opposite sign: +147.177 for a (147.177) expense) or the tax PAID —
# never the expense of the period. São Salvador (audit 2026-11-04, P9): the pre-tax line was not
# labelled, so its Correntes / Diferidos components were not either, and the field silently took
# the DFC adjustment; the «±» net-income identity cannot see a sign.
VALUE_STATEMENTS = {"incomeTaxExpenseRefundAmount": ["INCOME_STATEMENT_LST"]}

# Assets held for sale are CURRENT (IFRS 5, IAS 1.66) — and so are the liabilities associated with them. A
# document may print the line OUTSIDE both subtotals (Cibra p7: «Ativo mantido para venda» 30.000 between
# «Total do ativo circulante» and «ATIVO NÃO CIRCULANTE» — audit 2026-11-05 O1, user decision 2026-11-05).
# The printed current total + that line is then offered as an ALTERNATIVE (held_for_sale+identity), which
# _resolve takes only where the balance identity proves the line stands outside: total assets = current +
# the line + non-current (assets = ST + LT + equity for the liabilities), the printed total alone failing it.
# A line printed inside the current section leaves the identity holding with the printed total: nothing
# changes, nothing is counted twice.
HELD_FOR_SALE_FIELD = {"BALANCE_ASSETS_LST": "totalCurrentAssetsAmount",
                       "BALANCE_LIABILITIES_LST": "financialLiabilitiesShortTerm"}


def _is_held_for_sale(group, label):
    """«Ativo(s) (não circulante(s)) mantido(s) / disponível(is) para venda» on the assets side (not the IAS 39
    «ativos financeiros disponíveis para venda» investments); «Passivo(s) … venda» on the liabilities side."""
    lab = _norm(label)
    if group == "BALANCE_ASSETS_LST":
        return (lab.startswith(("ativo ", "ativos ")) and "para venda" in lab and "financeir" not in lab
                and ("mantid" in lab or "disponive" in lab))
    if group == "BALANCE_LIABILITIES_LST":
        return lab.startswith(("passivo ", "passivos ")) and "venda" in lab
    return False


def _tol(target, n_terms):
    """Printed statements round each figure: ± 1 unit per term — an ABSOLUTE error, whatever the
    size of the total (a relative tolerance let RECONTEC's two equity totals 99.103 / 99.124 both
    «satisfy» total assets 141.720)."""
    return n_terms + 0.5


def _close(a, b, n_terms=1):
    return abs(a - b) <= _tol(b, n_terms)


def _norm(label):
    try:
        from anchors_lib import normalize
        return normalize(label or "")
    except Exception:  # pragma: no cover
        return (label or "").lower()


# --------------------------------------------------------------------------- candidates
def _candidates(output, mapping_fields):
    statements = output.get("statements", {})
    dt = resolve_doc_type(output)
    cons = consolidado_filter(statements, dt)
    cand = defaultdict(list)
    fin_order = {}           # group -> print order of its first financial-result line
    seq = defaultdict(list)  # group -> the exported rows in print order (for the hierarchy test)
    held = defaultdict(list)  # current-total field -> its held-for-sale lines (HELD_FOR_SALE_FIELD)
    order = 0
    for g in GROUPS:
        for r, nm1, n in passing_rows(statements, dt, g, cons):
            order += 1
            seq[g].append((r, {"AMOUNT_N": n, "AMOUNT_N-1": nm1}))
            f = r.get("sbs_label")
            if not f and g in HELD_FOR_SALE_FIELD and _is_held_for_sale(g, r.get("label")):
                held[HELD_FOR_SALE_FIELD[g]].append({"group": g, "order": order, "row": r,
                                                     "values": {"AMOUNT_N": n, "AMOUNT_N-1": nm1}})
            lab = _norm(r.get("label"))
            if g not in fin_order and (f in FINANCIAL_RESULT_FIELDS
                                       or any(lab.startswith(x) for x in FINANCIAL_RESULT_CAPTIONS)):
                fin_order[g] = order
            if f:
                cand[f].append({"group": g, "order": order, "row": r,
                                "values": {"AMOUNT_N": n, "AMOUNT_N-1": nm1}})
    _figures_on_next_line(statements, dt, cons, cand)
    out = {}
    for f, cs in cand.items():
        allowed = VALUE_STATEMENTS.get(f) or (mapping_fields.get(f) or {}).get("allowed_statements") or []
        home = next((s for s in allowed if any(c["group"] == s for c in cs)), None)
        if home is None and allowed:
            # only lines OUTSIDE the field's statements (a cash-flow movement «Empréstimos e
            # financiamentos» for a balance field, a balance «Salários e encargos» payable for
            # the personnel EXPENSE): a flow / a balance is not the other — no value
            continue
        home = home or cs[0]["group"]
        cs = [c for c in cs if c["group"] == home]
        # summed fields only: a TOTAL field picks ONE line (and the cumulative result lines of an
        # income statement — EBIT inside the block that sums to pre-tax — are not components)
        cs, dropped, notes = (cs, [], []) if f in TOTAL_FIELDS else _drop_components(cs, seq[home])
        if f in CURRENT_FIELDS:
            cs, note = _current_only(cs, cand, home)
            notes += [note] if note else []
        out[f] = {"statement": home, "cands": cs, "note": "; ".join(notes) or None, "dropped": dropped}
    for f, hs in held.items():
        if f in out:
            out[f]["held_for_sale"] = hs
    return out, cons, fin_order


# --------------------------------------------------------------------------- hierarchy
# A line and its own components can carry the same SBS field: the CVM code tree of an ITR (ZAMP
# 1.01.02 Aplicações Financeiras + its 1.01.02.01.03 Títulos e valores mobiliários, WEG 1.01.01 +
# 1.01.01.01 + 1.01.01.02, Unipar 2.01.04 + 2.01.04.01 — audit 2026-11-04) or, without codes, a
# group printed above its detail lines (Uniar DISPONÍVEL = CAIXA + BANCOS + APLICAÇÕES, Benoit
# INTANGÍVEL 465.137,12 = INTANGÍVEL 2.065.303,75 + AMORTIZAÇÃO −1.600.166,63). Summing them counts
# the parent twice («sum of the lines»), and no identity sees it (cash / creditors are in none). A
# candidate that is a COMPONENT of another candidate of the same field is not counted:
#   coded rows — its code descends from the other's (1.01.02.01.03 under 1.01.02);
#   code-less rows — it lies inside a contiguous block (>= 2 lines, same page, right below or right
#   above the other line) whose figures sum to the other line's, in EVERY period the line prints.
MAX_BLOCK_ROWS = 40


def _block_members(seq, i, period):
    """Index sets of the contiguous blocks (>= 2 lines, same page) right below / right above seq[i]
    whose figures sum to seq[i]'s figure for `period`."""
    v = seq[i][1][period]
    if not v:
        return []
    page = seq[i][0].get("page_nb")
    found = []
    for step in (1, -1):
        s, idxs, j = 0.0, [], i + step
        while 0 <= j < len(seq) and seq[j][0].get("page_nb") == page and len(idxs) < MAX_BLOCK_ROWS:
            s += seq[j][1][period] or 0.0
            idxs.append(j)
            if len(idxs) >= 2 and _close(s, v, len(idxs)):
                found.append(set(idxs))
                break
            j += step
    return found


def _drop_components(cs, seq):
    """(candidates without the components of another candidate, the dropped ones, [note])."""
    if len(cs) < 2:
        return cs, [], []
    pos = {id(r): k for k, (r, _) in enumerate(seq)}
    dropped = set()
    for p in sorted(cs, key=lambda c: c["order"]):
        if id(p) in dropped:
            continue
        pcode = p["row"].get("code")
        blocks = None
        for c in cs:
            if c is p or id(c) in dropped:
                continue
            ccode = c["row"].get("code")
            if pcode and ccode:
                if ccode.startswith(pcode + "."):
                    dropped.add(id(c))
                    p["has_components"] = True
                continue
            if pcode or ccode or id(p["row"]) not in pos or id(c["row"]) not in pos:
                continue
            if blocks is None:
                i = pos[id(p["row"])]
                blocks = {per: _block_members(seq, i, per) for per in PERIODS
                          if seq[i][1][per] is not None}
            k = pos[id(c["row"])]
            if blocks and all(any(k in b for b in bl) for bl in blocks.values()):
                dropped.add(id(c))
                p["has_components"] = True
    if not dropped:
        return cs, [], []
    kept = [c for c in cs if id(c) not in dropped]
    # one line left: its method («printed total of its components») already says it
    note = ([] if len(kept) == 1 else
            [f"{len(cs) - len(kept)} component row(s) of another line not counted (hierarchy)"])
    return kept, [c for c in cs if id(c) in dropped], note


def _figures_on_next_line(statements, dt, cons, cand):
    """A TOTAL caption printed WITHOUT figures whose figures sit on the unnamed line right below
    it (Documento1 p2: «Lucro Operacional antes do Resultado Financeiro» / unnamed 74.692 /
    93.662 — EBIT + financial result = pre-tax). Used only when the field has no other candidate;
    flagged for review (`figures_on_next_line`)."""
    fp = form_pages(statements, dt)
    for g in GROUPS:
        rows = statements.get(g) or []
        bypass = is_small_span(rows)
        for a, b in zip(rows, rows[1:]):
            f = a.get("sbs_label")
            if (f not in TOTAL_FIELDS or f in cand or a.get("columns") or b.get("label")
                    or b.get("sbs_label") or b.get("page_nb") != a.get("page_nb")
                    or not row_passes(b, dt, bypass, cons, fp)):
                continue
            nm1, n = pick_amounts(b.get("columns", []), dt, cons)
            cand[f].append({"group": g, "order": 10 ** 6, "row": b, "caption": a.get("label"),
                            "values": {"AMOUNT_N": n, "AMOUNT_N-1": nm1}, "next_line": True})


def _current_only(cs, cand, group):
    """Keep the current-section rows. The Stage-3 `sbs_section` decides wherever it is known (codes,
    section headers / subtotals, arithmetic). Only rows WITHOUT a section fall back to the position
    test «printed before the section's current total on their page» — a total-last layout. The
    fallback used to run over every row as soon as none was non-current, so on a header-led page
    (ITR: 2.01 Passivo Circulante printed ABOVE 2.01.02 Fornecedores) it dropped the CURRENT rows
    and kept a note's line instead (Unipar UNP1, audit 2026-11-04)."""
    kept = [c for c in cs if c["row"].get("sbs_section") not in ("noncurrent", "equity")]
    totals = [t for f in ("totalCurrentAssetsAmount", "financialLiabilitiesShortTerm")
              for t in cand.get(f, []) if t["group"] == group]
    kept = [c for c in kept if c["row"].get("sbs_section") is not None
            or not any(t["row"].get("page_nb") == c["row"].get("page_nb")
                       and t["order"] < c["order"] for t in totals)]
    if kept and len(kept) < len(cs):
        return kept, f"{len(cs) - len(kept)} non-current row(s) not counted (SBS: current only)"
    return cs, None


# --------------------------------------------------------------------------- per-field options
def _option(value, method, rows):
    return {"value": value, "method": method, "rows": rows}


def _total_of_components(vals):
    """(cand, value) whose value is the printed total of all the others, or None."""
    if len(vals) < 3:
        return None
    for i, (c, v) in enumerate(vals):
        rest = [w for j, (_, w) in enumerate(vals) if j != i]
        if _close(sum(rest), v, len(rest)):
            return c, v
    return None


def _options(field, cands, period, fin_order):
    """[default option, alternatives...] for one field and period (None if no value)."""
    vals = [(c, c["values"][period]) for c in cands if c["values"][period] is not None]
    if not vals:
        return None
    if len(vals) == 1:
        c, v = vals[0]
        method = ("figures_on_next_line" if c.get("next_line")
                  else "total_of_components" if c.get("has_components") else "single")
        return [_option(v, method, [c])]
    alts = []
    total = _total_of_components(vals)
    if field in TOTAL_FIELDS:
        distinct = []
        for c, v in vals:
            if not any(_close(v, d) for d, _ in distinct):
                distinct.append((v, c))
        if len(distinct) == 1:
            return [_option(vals[0][1], "same_value", [c for c, _ in vals])]
        if total:
            default = _option(total[1], "total_of_components", [total[0]])
        else:
            first = min(vals, key=lambda cv: cv[0]["order"])
            default = _option(first[1], "first_of_several", [first[0]])
            if field == "operatingProfitAmount" and cands[0]["group"] in fin_order:
                before = [cv for cv in vals if cv[0]["order"] < fin_order[cands[0]["group"]]]
                if len(before) == 1:
                    default = _option(before[0][1], "before_financial_result", [before[0][0]])
        alts = [_option(v, "identity", [c]) for v, c in distinct]
        alts.append(_option(sum(v for _, v in vals), "sum+identity", [c for c, _ in vals]))
    else:
        if total:
            default = _option(total[1], "total_of_components", [total[0]])
        else:
            default = _option(sum(v for _, v in vals), "sum", [c for c, _ in vals])
        if len(vals) <= MAX_SUBSET_ROWS:
            for k in range(1, len(vals)):
                for sub in combinations(vals, k):
                    alts.append(_option(sum(v for _, v in sub), "subset+identity", [c for c, _ in sub]))
            alts.append(_option(sum(v for _, v in vals), "sum+identity", [c for c, _ in vals]))
    return [default] + [a for a in alts if not _close(a["value"], default["value"])]


def _held_for_sale_option(opts, held, period):
    """Append «printed current total + the held-for-sale line(s)» to a field's options for `period`
    (see HELD_FOR_SALE_FIELD): only an ALTERNATIVE, so _resolve takes it where the balance identity proves
    it and the printed total alone fails — never by default."""
    if not opts:
        return
    lines = [h for h in held if h["values"][period]]
    if not lines:
        return
    base = opts[0]
    alt = _option(base["value"] + sum(h["values"][period] for h in lines), "held_for_sale+identity",
                  base["rows"] + lines)
    if not any(_close(alt["value"], o["value"]) for o in opts):
        opts.append(alt)


# --------------------------------------------------------------------------- identities
def _holds(mode, target, terms):
    if mode == "sum":
        return _close(sum(terms), target, len(terms))
    a, b = terms
    return _close(a + b, target, 2) or _close(a - b, target, 2)


def _resolve(choice, opts, period, checks, skip=()):
    """Apply the identities in order: keep the defaults when they hold; otherwise switch the
    fewest unlocked fields to the alternatives that make the identity hold (unique minimum)."""
    locked = set()
    for target, terms, mode in IDENTITIES:
        if target in skip:
            continue
        names = (target,) + terms
        missing = [f for f in names if opts.get(f) is None]
        if len(missing) == 1 and missing[0] != target:
            # the one block nobody printed is ZERO when the identity leaves nothing for it
            # (Serasa 105563483: no long-term liabilities — assets = current + equity; MBP: no
            # income tax — net income = pre-tax profit)
            need = choice[target]["value"] - sum(choice[f]["value"] for f in terms if f != missing[0])
            if abs(need) <= _tol(choice[target]["value"], len(terms)):
                choice[missing[0]] = _option(0.0, "zero_by_identity", [])
                opts[missing[0]] = [choice[missing[0]]]
                missing = []
        if missing:
            continue
        cur = [choice[f]["value"] for f in names]
        label = f"{target} = " + (" + ".join(terms) if mode == "sum" else " ± ".join(terms))
        if _holds(mode, cur[0], cur[1:]):
            checks.append({"identity": label, "period": period, "ok": True, "lhs": cur[0],
                           "rhs": sum(cur[1:]) if mode == "sum" else None})
            for f in names:
                choice[f].setdefault("verified", []).append(label)
            locked.update(names)
            continue
        spaces = [[choice[f]] if f in locked else [choice[f]] + opts[f][1:] for f in names]
        best, best_n, answers = None, None, set()
        for combo in product(*spaces):
            if not _holds(mode, combo[0]["value"], [o["value"] for o in combo[1:]]):
                continue
            changed = sum(1 for f, o in zip(names, combo) if o is not choice[f])
            # two alternatives giving the SAME figures are one answer (two lines printing the same
            # cost of sales are not an ambiguity — WEG9 «ambiguous» on −13.534.197 twice)
            answer = tuple(round(o["value"], 2) for o in combo)
            if best_n is None or changed < best_n:
                best, best_n, answers = combo, changed, {answer}
            elif changed == best_n:
                answers.add(answer)
        n_best = len(answers)
        if best is not None and n_best == 1:
            for f, o in zip(names, best):
                if o is not choice[f]:
                    choice[f] = dict(o)
                choice[f].setdefault("verified", []).append(label)
            locked.update(names)
            checks.append({"identity": label, "period": period, "ok": True, "lhs": best[0]["value"],
                           "rhs": sum(o["value"] for o in best[1:]) if mode == "sum" else None,
                           "resolved": [f for f, o in zip(names, best) if o["method"].endswith("identity")]})
        else:
            checks.append({"identity": label, "period": period, "ok": False, "lhs": cur[0],
                           "rhs": sum(cur[1:]) if mode == "sum" else None,
                           "note": "ambiguous" if n_best > 1 else "no alternative satisfies it"})


# --------------------------------------------------------------------------- public
def compute(output, mapping_fields):
    """{"perspective", "fields": {field: {...}}, "checks": [...]} for a structured output."""
    cands, cons, fin_order = _candidates(output, mapping_fields or {})
    per_period = {}
    checks = []
    for p in PERIODS:
        opts = {f: _options(f, d["cands"], p, fin_order) for f, d in cands.items()}
        for f, d in cands.items():
            if d.get("held_for_sale"):
                _held_for_sale_option(opts.get(f), d["held_for_sale"], p)
        choice = {f: dict(o[0]) for f, o in opts.items() if o}
        derived = _derive_total_liabilities(choice, opts, cands, after=False)
        _resolve(choice, opts, p, checks, skip=("totalLiabilitiesAmount",) if derived else ())
        _derive_total_liabilities(choice, opts, cands, after=True)
        per_period[p] = choice
    # a field taking part in an identity that FAILS in a period is flagged for review in that period —
    # the reviewer is pointed at the figures that do not add up (audit 2026-11-04, P12: ZAMP's failed
    # «posttax = pretax ± tax» left pre-tax unflagged; Dom's wrong N-1 assets carried no REVIEW)
    failed = defaultdict(list)
    for c in checks:
        if not c["ok"]:
            for target, terms, mode in IDENTITIES:
                if c["identity"].startswith(f"{target} = ") and all(t in c["identity"] for t in terms):
                    for f in (target,) + terms:
                        failed[f].append(f"{c['period']}: {c['identity']} does not hold")
    fields = {}
    for f in sorted(set(cands) | {f for ch in per_period.values() for f in ch}):
        d = cands.get(f) or {"statement": ((mapping_fields or {}).get(f, {}).get("allowed_statements") or [None])[0],
                             "cands": [], "note": None}
        entry = {"statement": d["statement"]}
        used = set()
        review = False
        for p in PERIODS:
            ch = per_period[p].get(f)
            if ch is None:
                entry[p] = None
                continue
            entry[p] = ch["value"]
            entry.setdefault("method", {})[p] = ch["method"]
            if ch.get("verified"):
                entry.setdefault("verified", {})[p] = ch["verified"]
            review |= ch["method"] in ("first_of_several", "figures_on_next_line")
            used.update(id(c["row"]) for c in ch["rows"])
        # a held-for-sale line is listed where an identity counted it in (held_for_sale+identity)
        hfs = [h for h in d.get("held_for_sale") or [] if id(h["row"]) in used]
        entry["rows"] = [{"page": c["row"].get("page_nb"), "label": c["row"].get("label") or c.get("caption"),
                          "AMOUNT_N": c["values"]["AMOUNT_N"], "AMOUNT_N-1": c["values"]["AMOUNT_N-1"],
                          "used": id(c["row"]) in used}
                         for c in sorted(d["cands"] + (d.get("dropped") or []) + hfs, key=lambda c: c["order"])]
        if d["note"]:
            entry["note"] = d["note"]
        if failed.get(f) and any(entry.get(p) is not None for p in PERIODS):
            review = True
            entry["review_reason"] = "; ".join(failed[f])
        if review:
            entry["needs_review"] = True
        fields[f] = entry
    _ebit_not_printed(output, fields)
    return {"perspective": "Consolidado" if cons else "all columns", "fields": dict(sorted(fields.items())),
            "checks": checks}


def _ebit_not_printed(output, fields):
    """EBIT = the operating result BEFORE the financial result (user decision 2026-11-04). When the
    only «resultado operacional» is printed after it (Uniar: it includes the financial income; Stage 3
    withdrew its label, `after_financial_result`), the field stays EMPTY and is flagged for review —
    never the pre-tax-like figure."""
    if "operatingProfitAmount" in fields:
        return
    statements = output.get("statements", {})
    dt = resolve_doc_type(output)
    cons = consolidado_filter(statements, dt)
    rows = [(r, nm1, n) for r, nm1, n in passing_rows(statements, dt, "INCOME_STATEMENT_LST", cons)
            if r.get("sbs_blocked_reason") == "after_financial_result"]
    if not rows:
        return
    fields["operatingProfitAmount"] = {
        "statement": "INCOME_STATEMENT_LST", "AMOUNT_N": None, "AMOUNT_N-1": None, "needs_review": True,
        "note": "no operating result printed BEFORE the financial result — EBIT left empty",
        "rows": [{"page": r.get("page_nb"), "label": r.get("label"), "AMOUNT_N": n, "AMOUNT_N-1": nm1,
                  "used": False} for r, nm1, n in rows]}


def _derive_total_liabilities(choice, opts, cands, after):
    """totalLiabilities = current + non-current liabilities by definition (Stage 3 derives a row per balance cell). When no
    printed line holds it, keep it consistent with the FIELD values of ST and LT chosen here (after
    the identities). Returns True when the field is derived."""
    tl = cands.get("totalLiabilitiesAmount")
    if not tl or not all(c["row"].get("sbs_derived") for c in tl["cands"]):
        return False
    st, lt = choice.get("financialLiabilitiesShortTerm"), choice.get("financialLiabilitiesLongTerm")
    if after and st and lt and "totalLiabilitiesAmount" in choice:
        choice["totalLiabilitiesAmount"] = _option(st["value"] + lt["value"], "derived = current + non-current",
                                                   choice["totalLiabilitiesAmount"]["rows"])
        opts["totalLiabilitiesAmount"] = [choice["totalLiabilitiesAmount"]]
    return True
