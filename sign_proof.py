# -*- coding: utf-8 -*-
"""
sign_proof.py — Stage 1, step 5b: OCR sign / dash errors repaired ONLY where the statement's own
arithmetic proves them. Standard library only. Runs on the assembled statements (structure_ocr).

The reciTAL extraction loses a printed minus in BOTH layers (values and words) — WEG 2.03.02.07 /
3.04 / 6.01.02 / 6.01.02.10, Unipar 2.03.08.02 / 6.01.02.04, ZAMP 3.07 / 2.03.05 / 6.01.02.08 / .14 /
.17, Dom «Lucro (prejuízos) acumulados» / «IRPJ e CSLL diferidos» (audit 2026-11-04, P7) — and reads
a printed dash as «1» (Entrepay «Demais provisões», P8). Nothing in the cell itself tells; the
arithmetic does:

  * ITR (CVM codes): every form line is printed and the code tree is exact — a parent is the sum of
    its direct children (1 = 1.01 + 1.02, 6.01.02 = Σ 6.01.02.xx …), plus the fixed CVM chains
    3.03 = 3.01 + 3.02, 3.05 = 3.03 + 3.04, 3.07 = 3.05 + 3.06, 3.09 = 3.07 + 3.08, 3.11 = 3.09 + 3.10,
    6.05 = 6.01 + … + 6.04, 6.05.02 = 6.05.01 + 6.05. Not sums: 3.99.* (per share), 6.05.0x (cash
    balances), the top codes 3 / 6.
  * documents without codes: a printed subtotal closes the block above it (the Stage-3 subtotal-tree
    rule), in the balance and the cash-flow statement — income-statement templates print costs
    without a minus BY CONVENTION (Serasa: COGS positive, the sign decision S5 = «as printed»), so in
    the income statement only the DASH test runs, never a sign flip (Cibra p8, audit 2026-11-05 K6:
    «Resultado de equivalência patrimonial» Consolidado 2024 printed «–», read «1»; 627.476 − 54.935 −
    88.252 − 10.784 − 12.363 = 461.142 = «LUCRO ANTES DO RESULTADO FINANCEIRO» exactly without it).

A figure is repaired only when exactly ONE explanation exists:
  sign   flipping one positive figure (or, in the code tree, one pair) makes the failing equation
         hold, and no equation that holds today breaks;
  dash   a «1» whose removal makes the equation hold EXACTLY while with it it is off by exactly 1.
The repaired cell keeps the token as recognized in `value_raw` and carries `sign_corrected` /
`dash_corrected` + `proof` (the equation). An ITR equation that still fails after the repairs flags
its parent row `needs_review` (`arith_review`): the reviewer is pointed at the line that does not add
up instead of a silent wrong sign. Rows labelled «(-) …» (a contra line printed positive by the
template) are never flipped.
"""
import re
from collections import defaultdict
from itertools import combinations

from anchors_lib import normalize, parse_amount

CHAINS = [("3.03", ("3.01", "3.02")), ("3.05", ("3.03", "3.04")), ("3.07", ("3.05", "3.06")),
          ("3.09", ("3.07", "3.08")), ("3.11", ("3.09", "3.10")),
          ("6.05", ("6.01", "6.02", "6.03", "6.04")), ("6.05.02", ("6.05.01", "6.05"))]
NOT_SUM_PARENTS = ("3", "6", "4", "5", "7", "6.05")
NOT_SUM_PREFIXES = ("3.99", "6.05.")
SUBTOTAL_BUCKETS = ("BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST", "CASH_FLOW_LST")
DASH_ONLY_BUCKETS = ("INCOME_STATEMENT_LST",)      # dash test only: signs stay as printed (S5)
TOTAL_CAPTION = re.compile(r"^(total|caixa liquido|fluxo de caixa liquido|aumento|reducao|saldo)\b")
# A SIGN is repaired only under a section total («Total …», «Caixa / Fluxo de caixa líquido …»), never
# under the cash reconciliation lines («Aumento…», «Saldo final…»): there a document's own error (São
# Salvador p25: the FX effect printed with the wrong sign) or a figure the extraction lost (Delfia p9 in
# production) makes a flip «fit» by coincidence.
FLIP_CAPTION = re.compile(r"^(total|caixa liquido|fluxo de caixa liquido)\b")
MAX_ITER = 6


def _value(c):
    """Cell value as a number (OCR bracket repair as in selection.to_number), or None."""
    v = c.get("value")
    if v is None:
        return None
    s = str(v).strip()
    if not s or "%" in s:
        return None
    if "[" in s or "]" in s:
        s = s.replace("[", "(").replace("]", ")")
        if s.startswith("(") and not s.endswith(")"):
            s += ")"
        if s.endswith(")") and not s.startswith("("):
            s = "(" + s
    return parse_amount(s)


def _tol(n_terms):
    return 0.5 + 0.5 * n_terms


def _flip(cell, proof, report):
    v = _value(cell)
    body = str(cell["value"]).strip().strip("()[]").lstrip("-−– ")
    cell.setdefault("value_raw", cell["value"])
    cell["value"] = "-" + body
    cell["sign_corrected"] = True
    cell["proof"] = proof
    report["columns"]["sign_corrected"] += 1
    return -v


def _dash(cell, proof, report):
    cell.setdefault("value_raw", cell["value"])
    cell["value"] = "-"
    cell["dash_corrected"] = True
    cell["proof"] = proof
    report["columns"]["dash_corrected"] += 1


def _cells(rows):
    """(account_type, period) -> [(row, cell)]."""
    out = defaultdict(list)
    for r in rows:
        for c in r.get("columns", []):
            if c.get("period") in ("AMOUNT_N", "AMOUNT_N-1"):
                out[(c.get("account_type") or "", c["period"])].append((r, c))
    return out


# --------------------------------------------------------------------------- ITR code tree
def _form_instances(rows):
    """Coded rows split into form instances: consecutive pages of one bucket, a new instance when a
    code repeats (Individual and Consolidado forms carry the same codes)."""
    inst, cur, seen, last_pg = [], [], set(), None
    for r in rows:
        code = r.get("code")
        if not code or r.get("other_statement"):
            continue
        pg = r.get("page_nb")
        if cur and (code in seen or (last_pg is not None and pg is not None and pg - last_pg > 1)):
            inst.append(cur)
            cur, seen = [], set()
        cur.append(r)
        seen.add(code)
        last_pg = pg
    if cur:
        inst.append(cur)
    return inst


def _equations(codes):
    """[(target, (terms...), label)] for one form instance's code set."""
    eqs = []
    children = defaultdict(list)
    for c in codes:
        if "." in c:
            children[c.rsplit(".", 1)[0]].append(c)
    for p, kids in children.items():
        if p not in codes or p in NOT_SUM_PARENTS or p.startswith(NOT_SUM_PREFIXES) or len(kids) < 2:
            continue
        eqs.append((p, tuple(sorted(kids)), f"{p} = Σ {p}.xx"))
    for t, terms in CHAINS:
        present = [x for x in terms if x in codes]
        if t in codes and len(present) >= 2:
            eqs.append((t, tuple(present), f"{t} = {' + '.join(present)}"))
    return eqs


def _repair_code_tree(rows, report):
    for inst in _form_instances(rows):
        by_code = {r["code"]: r for r in inst}
        eqs = _equations(set(by_code))
        if not eqs:
            continue
        for key, rcs in _cells(inst).items():
            cell = {r["code"]: c for r, c in rcs if r.get("code") in by_code and r is by_code[r["code"]]}
            val = {k: _value(c) for k, c in cell.items()}
            for _ in range(MAX_ITER):
                changed = False
                for t, terms, label in eqs:
                    if any(val.get(x) is None for x in (t,) + terms):
                        continue
                    if _holds(val, t, terms):
                        _maybe_dash(val, cell, t, terms, label, report)
                        continue
                    fix = _unique_flip(val, eqs, t, terms)
                    if fix:
                        for x in fix:
                            val[x] = _flip(cell[x], label, report)
                        changed = True
                if not changed:
                    break
            for t, terms, label in eqs:
                if all(val.get(x) is not None for x in (t,) + terms) and not _holds(val, t, terms):
                    row = by_code[t]
                    row["needs_review"] = True
                    row.setdefault("arith_review", f"{label} does not add up ({key[1]})")
                    report["rows"]["arith_review"] += 1


def _holds(val, t, terms, exact=False):
    diff = val[t] - sum(val[x] for x in terms)
    return abs(diff) <= (0.5 if exact else _tol(len(terms) + 1))


def _unique_flip(val, eqs, t, terms):
    """The ONE set of positive figures (a single one, else a pair of terms) whose sign flip makes
    (t = Σ terms) hold without breaking an equation that holds now; None if not unique."""
    def ok_after(flips):
        trial = dict(val)
        for x in flips:
            trial[x] = -trial[x]
        if not _holds(trial, t, terms):
            return False
        for t2, terms2, _ in eqs:
            names = (t2,) + terms2
            if not any(x in names for x in flips) or any(val.get(x) is None for x in names):
                continue
            if _holds(val, t2, terms2) and not _holds(trial, t2, terms2):
                return False
        return True
    single = [x for x in (t,) + terms if val[x] > 0 and ok_after((x,))]
    if len(single) == 1:
        return single
    if single:
        return None
    pos = [x for x in terms if val[x] > 0]
    pairs = [p for p in combinations(pos, 2) if ok_after(p)]
    return list(pairs[0]) if len(pairs) == 1 else None


def _maybe_dash(val, cell, t, terms, label, report):
    """A «1» term in an equation that holds only within the rounding tolerance, exact without it."""
    if _holds(val, t, terms, exact=True):
        return
    ones = [x for x in terms if val[x] == 1 and str(cell[x].get("value")).strip() in ("1", "I", "l")]
    if len(ones) != 1:
        return
    trial = dict(val)
    trial[ones[0]] = 0.0
    if _holds(trial, t, terms, exact=True):
        _dash(cell[ones[0]], label, report)
        val[ones[0]] = 0.0


# --------------------------------------------------------------------------- printed subtotals
def _repair_subtotals(rows, report, flip=True):
    """Printed subtotals of the statement lines the export shows: a PRIMARY page, or a short
    (<= 2 consecutive pages) statement — the Stage-3 `shown` rule. Note tables are never touched.
    flip=False (income statement): only a «1» read for a dash is repaired, never a sign."""
    small = _small_span(rows)
    pages = defaultdict(list)
    for r in rows:
        if r.get("serasa_analysis") or r.get("non_figure_line") or r.get("other_statement"):
            continue
        if small or str(r.get("page_class") or "").startswith("PRIMARY"):
            pages[r.get("page_nb")].append(r)
    for pg, prs in pages.items():
        for key, rcs in _cells(prs).items():
            cell_of = {id(r): c for r, c in rcs}
            stack = []          # [(value, row, cell, is_node)]
            for r in prs:
                c = cell_of.get(id(r))
                v = _value(c) if c else None
                if v is None or v == 0:
                    continue
                closed = _close(stack, v)
                if closed is None and stack and abs(v - stack[-1][0]) < 0.5 and (
                        not r.get("label") or TOTAL_CAPTION.match(normalize(r.get("label")))):
                    # the subtotal of a ONE-line block repeats that line (São Salvador p20: «Investimentos
                    # em controladas» 1.063.668 / unnamed 1.063.668 / «Total do ativo não circulante»
                    # 1.063.668) — a 1-child node, else the repeats pile up and fake a «sign error»
                    closed = 1
                if closed is None and flip and FLIP_CAPTION.match(normalize(r.get("label") or "")):
                    closed = _close_with_flip(stack, v, r, report)
                if closed is not None:
                    kids = stack[-closed:]
                    del stack[-closed:]
                    _dash_in_block(kids, v, r, report)
                    stack.append((v, r, c, True))
                else:
                    stack.append((v, r, c, False))


def _small_span(rows):
    pages = sorted({r.get("page_nb") for r in rows if r.get("label") and r.get("page_nb") is not None})
    return len(pages) <= 1 or (len(pages) == 2 and pages[1] == pages[0] + 1)


def _close(stack, v):
    """Largest k >= 2 whose last-k sum equals v (the Stage-3 subtotal-tree rule), or None."""
    s, best = 0.0, None
    for k in range(1, len(stack) + 1):
        s += stack[-k][0]
        if k >= 2 and abs(v - s) <= _tol(k):
            best = k
    return best


def _close_with_flip(stack, v, total_row, report):
    sols = {}
    s = 0.0
    for k in range(1, len(stack) + 1):
        s += stack[-k][0]
        if k < 2:
            continue
        for j in range(1, k + 1):
            val, r, c, node = stack[-j]
            lab = normalize(r.get("label") or "")
            if node or val <= 0 or not lab or lab.startswith("total") \
                    or str(r.get("label") or "").lstrip().startswith(("(-)", "(−)", "(–)")):
                continue
            if abs(v - (s - 2 * val)) <= _tol(k):
                sols.setdefault(id(c), (k, j))
    if len(sols) != 1:
        return None
    k, j = next(iter(sols.values()))
    val, r, c, node = stack[-j]
    label = total_row.get("label") or "unnamed subtotal"
    stack[-j] = (_flip(c, f"subtotal {label!r}", report), r, c, node)
    return k


def _dash_in_block(kids, v, total_row, report):
    s = sum(k[0] for k in kids)
    if abs(v - s) < 0.5:
        return
    ones = [k for k in kids if k[0] == 1 and not k[3] and str(k[2].get("value")).strip() in ("1", "I", "l")]
    if len(ones) == 1 and abs(v - (s - 1)) < 0.5:
        _dash(ones[0][2], f"subtotal {total_row.get('label') or 'unnamed subtotal'!r}", report)


# --------------------------------------------------------------------------- public
def repair(statements, report):
    """Repair proven sign / dash errors in place (see the module docstring)."""
    coded = any(r.get("code") for rows in statements.values() for r in rows)
    for bucket, rows in statements.items():
        if coded:
            _repair_code_tree(rows, report)
        elif bucket in SUBTOTAL_BUCKETS:
            _repair_subtotals(rows, report)
        elif bucket in DASH_ONLY_BUCKETS:
            _repair_subtotals(rows, report, flip=False)
