#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
sbs_labeler.py — Stamp every structured row with its SBS field (`sbs_label`).

Maps each reconstructed line-item `label` to a new SBS Field Name
(primary_sbs_label) using the aliases in `sbs_label_mapping.json`.

Design — a small-stage pipeline over two passes (see CLAUDE.md "Stage-3" for the
authoritative description; this is a summary):

  PASS 1 (per statement bucket):
    0a) CODE-FIRST  — if the row carries a standardized CVM/B3 code (level 1..3,
        mapping.code_map), label it straight from the code (source=code, 100). An
        OCR-robust signal that does not depend on the line text; the text stages
        then skip the row. Codes are present only in ITR/DFP filings.
    0)  MATCH       — DICT-FIRST: exact lookup in the sbs_normalize alias index
        (a clean hit wins, score 100); on a miss, fall back to anchors_lib.
        anchor_match with token_fuzzy=SBS_TOKEN_FUZZY (recall-oriented). A
        before-tax guard (_is_pretax_label) drops the income-tax field from a
        "...antes do imposto..." line (it is token-identical to the tax alias).
    1)  TWIN/SECTION — resolve current/non-current twins matched via a SHARED
        caption (qualifier -> code -> positional section -> needs_review).
    2)  EMPTY-LABEL — recover an SBS field for nameless balance subtotals.

  PASS 2 (document-level — needs every bucket labelled first):
    3)  SCOPE-GATE  — scope is a FILTER, not a tie-breaker: an out-of-statement
        match is BLOCKED if the field is captured in-home elsewhere (no loss),
        else RECOVERED (kept + cross_scope + needs_review). Double-home fields
        (depreciation / dividend / posttax / income-tax) are allowed in two
        statements. (This REPLACED the older "scope is only a tie-breaker" rule.)
    4)  STRUCT-VALIDATE — FLAG-ONLY income-tier order + balance side checks.
    SERASA) NET-INCOME — doc-type-gated: the Serasa template's second net-income
        line ("RESULTADO LIQUIDO" after dividends) is cleared (post-tax profit is
        the before-dividends figure).
    5)  DEDUP       — tag duplicates two ways (both within one bucket, both on the
        full amount signature): (a) same page + name; (b) same sbs_label when the
        rows share a page or a page-kind (catches the same figure re-worded under a
        different caption that (a) misses). The cluster row KEPT is the one best
        respecting the statement's canonical order (income_tier sequence), not
        blindly the first — so a total printed both at the top and at its proper
        bottom keeps the bottom copy; ties / non-income buckets keep the earliest.

Tagging philosophy: "tag, never delete". Coverage = all matched rows regardless
of page_class; downstream (the Excel exporter) filters on page_class / doc type.

Public API:
    load_mapping(path) -> dict
    label_statements(statements, mapping, cfg, document_type=None) -> stats dict
    annotate_output(output, mapping, cfg) -> stats dict        (operates on full output)

CLI (post-process an existing structured_output without re-running Stage 1):
    python3.13 sbs_labeler.py structured_output.json -o structured_output.json
    python3.13 sbs_labeler.py structured_output.json --dry-run
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

from anchors_lib import normalize, anchor_match, parse_amount

# --------------------------------------------------------------------------- #
# Paths & defaults
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
MAPPING_PATH = os.path.join(_HERE, "sbs_label_mapping.json")
ANCHORS_PATH = os.path.join(_HERE, "anchors.yaml")

# Fallback matching params if anchors.yaml can't be read (keeps the module
# usable standalone). These mirror the committed anchors.yaml values.
_DEFAULT_STOPWORDS = {
    "de", "do", "da", "dos", "das", "e", "na", "o", "a", "os", "as",
    "para", "com", "em", "ao", "aos",
}
_DEFAULT_BENIGN = {
    "utilizado", "gerado", "consumido", "aplicado", "prejuizo", "prejuizos",
    "liquido", "liquidas", "antes", "pelas", "pela", "pelos", "pelo",
    "nas", "nos",
}
_DEFAULT_THRESHOLD = 85

# Stage-3 is RECALL-oriented (open vocabulary: each company phrases line items
# differently), unlike Stage-2's precision-oriented closed anchor lexicon. So
# the labeller opts the shared matcher into TOKEN-LEVEL fuzzy coverage: a canon
# token counts as present when a line token is an inflection / abbreviation /
# OCR variant of it (credito~creditos, custos~custo), instead of requiring exact
# token equality. The phrase backstop (token_sort_ratio >= threshold) and the
# benign/foreign discipline still apply, so narrowing qualifiers ('circulante')
# keep blocking. 0 would restore Stage-2's strict behaviour. Measured on all 6
# golden outputs: lifts matched rows with no observed false positives.
SBS_TOKEN_FUZZY = 88

# --------------------------------------------------------------------------- #
# SBS-only normalization layer (Stage-3 dict lookup).
# --------------------------------------------------------------------------- #
# The shared normalize() in anchors_lib MUST stay byte-identical (Stage-2 relies
# on it). For the SBS exact-dictionary lookup we add ONE surgical token drop:
# {prejuizo, prejuizos}. Brazilian P&L writes every profit line as
# "Lucro (prejuízo)" = "Profit (loss)" — a dual-sign notation that never
# distinguishes two SBS fields, yet (left in an alias) it BLOCKS the token-
# coverage match for a label that omits it. Measured on the goldens: dropping
# only these two lifts exact-dict reproduction 263->268 and recovers 3 true
# positives the fuzzy engine missed, with zero new wrong hits / conflicts.
# We drop ONLY these two — the rest of the benign set is meaningful
# (e.g. 'liquido' separates receita líquida from bruta).
_SBS_DROP_TOKENS = {"prejuizo", "prejuizos"}


def sbs_normalize(s):
    """normalize() + drop SBS-safe dual-sign tokens. Used for dict lookup only."""
    n = normalize(s)
    if not n:
        return n
    return " ".join(t for t in n.split() if t not in _SBS_DROP_TOKENS)

# --------------------------------------------------------------------------- #
# Current / non-current ambiguity
# --------------------------------------------------------------------------- #
# Some captions are shared by a current/non-current TWIN PAIR of SBS fields:
#   * debt       — financialLiabilitiesShortTerm vs ...LongTerm
#                  ("emprestimos e financiamentos")
#   * securities — cashAndSecuritiesAmount (current) vs financialAssetsAmount
#                  (non-current) ("titulos e valores mobiliarios",
#                  "aplicacoes financeiras", "investimentos")
# The twin pairs and their shared captions are declared in
# sbs_label_mapping.json -> ambiguity_groups (built by build_sbs_mapping.py);
# this module just consumes them, so adding a new pair needs no code change.

# Explicit current / non-current qualifiers carried inside a row's own label.
# A row WITH one of these is trusted as-is; a row matching a shared caption
# WITHOUT any qualifier is inherently ambiguous and MUST go through
# code/positional resolution (never silently take one twin).
_QUAL_NONCURRENT = ("nao circulante", "nao corrente", "longo prazo")
_QUAL_CURRENT = ("circulante", "corrente", "curto prazo")


def _section_qualifier(label_norm):
    """'current' / 'noncurrent' / None from the row's own normalized label."""
    # non-current must be tested first ("nao circulante" contains "circulante")
    if any(q in label_norm for q in _QUAL_NONCURRENT):
        return "noncurrent"
    if any(q in label_norm for q in _QUAL_CURRENT):
        return "current"
    return None


# Section markers for positional resolution, per statement scope. HEADERS lead a
# section (items BELOW belong to it -> look UP); SUBTOTALS close a section
# (items ABOVE belong to it -> look DOWN to the next one). The two layouts
# coexist in real data: ITR is header-led, the annual DFP sample is
# subtotal-led with no headers — so both directions must be handled.
# Keyed by the statement bucket so assets and liabilities use the right words.
_SECTION_MARKERS = {
    "BALANCE_LIABILITIES_LST": {
        "header_current": {
            "passivo circulante", "circulante", "passivo corrente",
            "exigivel a curto prazo",
        },
        "header_noncurrent": {
            "passivo nao circulante", "nao circulante", "passivo nao corrente",
            "exigivel a longo prazo",
        },
        "subtotal_current": {
            "total do passivo circulante", "total do circulante",
            "total do passivo corrente", "total do exigivel a curto prazo",
        },
        "subtotal_noncurrent": {
            "total do passivo nao circulante", "total do nao circulante",
            "total do passivo nao corrente", "total do exigivel a longo prazo",
        },
    },
    "BALANCE_ASSETS_LST": {
        "header_current": {
            "ativo circulante", "circulante", "ativo corrente",
            "realizavel a curto prazo",
        },
        "header_noncurrent": {
            "ativo nao circulante", "nao circulante", "ativo nao corrente",
            "ativo realizavel a longo prazo", "realizavel a longo prazo",
        },
        "subtotal_current": {
            "total do ativo circulante", "total do circulante",
            "total do ativo corrente", "total do realizavel a curto prazo",
        },
        "subtotal_noncurrent": {
            "total do ativo nao circulante", "total do nao circulante",
            "total do ativo nao corrente", "total do realizavel a longo prazo",
        },
    },
}


# --------------------------------------------------------------------------- #
# Config / mapping loaders
# --------------------------------------------------------------------------- #
def load_match_config(anchors_path=ANCHORS_PATH):
    """(stopwords, benign, threshold) from anchors.yaml, with safe fallback."""
    try:
        import yaml
        cfg = yaml.safe_load(open(anchors_path, encoding="utf-8"))
        stop = {x for x in cfg.get("stopwords", []) if isinstance(x, str)}
        benign = set(cfg.get("benign_insertions", []))
        thr = cfg.get("match_threshold", _DEFAULT_THRESHOLD)
        return stop or _DEFAULT_STOPWORDS, benign or _DEFAULT_BENIGN, thr
    except Exception:
        return _DEFAULT_STOPWORDS, _DEFAULT_BENIGN, _DEFAULT_THRESHOLD


def load_mapping(path=MAPPING_PATH):
    """Load sbs_label_mapping.json and pre-index the matchable fields."""
    with open(path, encoding="utf-8") as f:
        mapping = json.load(f)
    # Pre-extract the fields that actually have aliases (only those can match).
    variants = {}
    scope = {}
    for fname, spec in mapping["fields"].items():
        al = spec.get("aliases") or []
        if al:
            variants[fname] = al
            scope[fname] = spec.get("statement_scope")
    mapping["_variants"] = variants
    mapping["_scope"] = scope

    # Index ambiguity groups for fast lookup. Per group we keep the twin fields,
    # the home scope, and the set of normalized shared captions.
    groups = mapping.get("ambiguity_groups", [])
    twin_fields = set()
    groups_by_scope = defaultdict(list)
    for g in groups:
        g = {
            "name": g["name"],
            "scope": g["scope"],
            "current": g["current"],
            "noncurrent": g["noncurrent"],
            "shared": set(g.get("shared_aliases_normalized", [])),
        }
        twin_fields.add(g["current"])
        twin_fields.add(g["noncurrent"])
        groups_by_scope[g["scope"]].append(g)
    mapping["_ambiguity_by_scope"] = groups_by_scope
    mapping["_twin_fields"] = twin_fields

    # Exact dictionary index on the SBS-only normalization (Stage-3 primary
    # engine). normalized-alias -> set(fields); split into clean (1 field) and
    # conflict (>1 field, e.g. shared twin captions resolved structurally).
    # Both the fuzzy aliases AND any exact-only aliases feed the dict; exact-only
    # aliases (e.g. 'passivo total') are deliberately absent from `variants`, so
    # they match verbatim here but never via the order-insensitive fuzzy stage.
    dict_idx = defaultdict(set)
    for fname, vlist in variants.items():
        for raw in vlist:
            k = sbs_normalize(raw)
            if k:
                dict_idx[k].add(fname)
    for fname, spec in mapping["fields"].items():
        for raw in spec.get("exact_aliases") or []:
            k = sbs_normalize(raw)
            if k:
                dict_idx[k].add(fname)
    mapping["_dict_clean"] = {k: next(iter(v)) for k, v in dict_idx.items() if len(v) == 1}
    mapping["_dict_conflict"] = {k: sorted(v) for k, v in dict_idx.items() if len(v) > 1}

    # Per-field structural address (canonical_ordinal / income_tier /
    # home_statement / allowed_statements / home_section) — used by the scope-gate
    # and structural-validation stages. Read straight from the mapping fields.
    fc = {}
    for fname, spec in mapping["fields"].items():
        fc[fname] = {
            "canonical_ordinal": spec.get("canonical_ordinal"),
            "income_tier": spec.get("income_tier"),
            "home_statement": spec.get("home_statement"),
            "allowed_statements": spec.get("allowed_statements") or [],
            "home_section": spec.get("home_section"),
            "category": spec.get("category"),
        }
    mapping["_field_config"] = fc

    # CVM/B3 code -> SBS field (ITR/DFP filings). Only levels 1..3 (standardized);
    # consumed by the code-first stage when a row carries a CODE_VALUE.
    mapping["_code_map"] = dict(mapping.get("code_map") or {})
    return mapping


# Income-tax expense must never be assigned to a "before-tax" line. A pre-tax
# profit caption ("Lucro/Resultado antes do imposto ... e contribuição social")
# is token-set-IDENTICAL to the income-tax alias ("imposto de renda e
# contribuição social ...") — the only distinguishing word is "antes" ("before"),
# which is a whitelisted benign insertion, so a bag-of-tokens match silently
# flips the pre-tax profit line into the tax field (verified on the DFP cash-flow
# EBT line). This semantic guard drops the tax field from the candidate set
# whenever the label carries a pre-tax marker. Validated on all goldens: the only
# tax-labelled row carrying a marker is exactly the mislabel being corrected.
_PRETAX_MARKERS = (
    "antes do imposto", "antes dos impostos", "antes dos tributos",
    "antes da provisao", "antes do ir", "antes da contribuicao",
    "antes da cs", "antes ir",
)
_INCOME_TAX_FIELD = "incomeTaxExpenseRefundAmount"


def _is_pretax_label(label):
    """True if the label is a 'before-tax' caption (never the tax-expense line)."""
    n = normalize(label)
    return any(mk in n for mk in _PRETAX_MARKERS)


# --------------------------------------------------------------------------- #
# Core matching — DICT-FIRST, then fuzzy fallback.
# --------------------------------------------------------------------------- #
# Primary engine: an exact lookup in the SBS dictionary (built on sbs_normalize).
# A clean hit short-circuits with score 100 (the dictionary carries ~86% of all
# labels and never drifts). A dict CONFLICT (a caption shared by a twin pair)
# returns all conflict fields so the twin stage can resolve them structurally.
# Only on a dict miss do we run the (unchanged) fuzzy anchor_match — measured as
# the right fallback (IDF / stemmer add nothing on this corpus).
def _match_all(label, variants, stop, benign, thr,
               dict_clean=None, dict_conflict=None):
    """
    All SBS fields matching `label`, sorted by score desc, plus the match source.
    Returns (hits, source) with source in {dict, dict_conflict, fuzzy, none}.
    dict_clean / dict_conflict are the prebuilt sbs_normalize indices (optional;
    when omitted the function is pure fuzzy, preserving older call sites).
    """
    pretax = _is_pretax_label(label)

    def _guard(hits, source):
        # Semantic guard: a before-tax line is never the income-tax expense.
        if pretax and any(f == _INCOME_TAX_FIELD for f, _ in hits):
            hits = [(f, s) for f, s in hits if f != _INCOME_TAX_FIELD]
        return (hits, source) if hits else ([], "none")

    if dict_clean is not None:
        n = sbs_normalize(label)
        if n in dict_clean:
            return _guard([(dict_clean[n], 100.0)], "dict")
        if dict_conflict and n in dict_conflict:
            return _guard([(f, 100.0) for f in dict_conflict[n]], "dict_conflict")
    hits = []
    for fname, vlist in variants.items():
        ok, score = anchor_match(vlist, label, stop, benign, thr,
                                 token_fuzzy=SBS_TOKEN_FUZZY)
        if ok:
            hits.append((fname, score))
    hits.sort(key=lambda t: -t[1])
    return _guard(hits, "fuzzy")


def _resolve_twin(group, label_qualifier, code, positional_section):
    """
    Resolve a current/non-current twin ambiguity for one group. Returns
        (field | None, method, reason)
    field is None when undecidable (caller flags needs_review). Priority:
    explicit label qualifier -> CODE_VALUE prefix -> positional section.
    """
    cur, non = group["current"], group["noncurrent"]
    # 1) Explicit qualifier in the row's own label — strongest, self-describing.
    if label_qualifier == "current":
        return cur, "qualifier", "label says current (circulante / curto prazo)"
    if label_qualifier == "noncurrent":
        return non, "qualifier", "label says non-current (nao circulante / longo prazo)"
    # 2) CODE_VALUE prefix (ITR hierarchical codes): x.01 = current, x.02 = non-current.
    if code:
        seg = code.split(".")
        if len(seg) >= 2:
            two = ".".join(seg[:2])
            if two.endswith(".01"):
                return cur, "code", f"code {code} -> current (x.01)"
            if two.endswith(".02"):
                return non, "code", f"code {code} -> non-current (x.02)"
    # 3) Positional section (two-pass scan over the statement's rows).
    if positional_section == "current":
        return cur, "positional", "within current section"
    if positional_section == "noncurrent":
        return non, "positional", "within non-current section"
    # 4) Undecidable.
    return None, "unresolved", "no qualifier, code, or section marker resolvable"


# The section-total SBS field for each (scope, section). A label_null row that
# arithmetically equals the sum of its section's detail rows is that subtotal.
# The liabilities side has THREE blocks (current / non-current / equity), not two
# — a nameless equity subtotal (Total do Patrimônio Líquido) must map to
# shareholdersFundsAmount, never to longTermLiabilitiesAmount.
_SECTION_TOTAL_FIELD = {
    ("BALANCE_ASSETS_LST", "current"): "totalCurrentAssetsAmount",
    ("BALANCE_ASSETS_LST", "noncurrent"): "totalFixedAssetsAmount",
    ("BALANCE_LIABILITIES_LST", "current"): "shortTermLiabilitiesAmount",
    ("BALANCE_LIABILITIES_LST", "noncurrent"): "longTermLiabilitiesAmount",
    ("BALANCE_LIABILITIES_LST", "equity"): "shareholdersFundsAmount",
}

# Equity (Patrimônio Líquido) line-item keywords. A liabilities subtotal whose
# feeding detail rows are equity items is the equity total, not a liability one.
# Validated present across all sample documents.
_EQUITY_KEYWORDS = (
    "capital social", "reserva", "reservas",
    "lucros acumulados", "prejuizos acumulados", "lucros prejuizos acumulados",
    "patrimonio liquido", "acoes em tesouraria", "ajuste de avaliacao",
    "ajustes de avaliacao patrimonial", "participacao dos acionistas nao control",
    "participacao dos nao controladores", "adiantamento para futuro aumento de capital",
    # Result-of-the-period and its appropriations live INSIDE the equity block
    # (they move retained earnings). Without these, an equity subtotal whose
    # detail rows include them fails the all-equity content test and is wrongly
    # ordinal-classified non-current. Only consulted in BALANCE_LIABILITIES scope.
    "resultado do exercicio", "ajustes de exercicios anteriores",
    "distribuicao de lucros", "dividendos distribuidos", "dividendos propostos",
    "lucro liquido do exercicio", "prejuizo do exercicio",
)


def _is_equity_label(label_norm):
    return any(kw in label_norm for kw in _EQUITY_KEYWORDS)


def _is_small_span(rows):
    """True if a group's labelled rows span <=2 consecutive pages — then it is a
    short, self-contained statement (no segment-copy risk). Mirrors the Excel
    exporter's rule so empty-label resolution runs on the same rows the report
    will actually show, and never on multi-page NOTE-table noise."""
    pages = sorted({r.get("page_nb") for r in rows
                    if r.get("label") and r.get("page_nb") is not None})
    if len(pages) <= 1:
        return True
    return len(pages) == 2 and pages[1] == pages[0] + 1


def _resolve_empty_label(rows, idx, scope, sections):
    """
    Try to assign an SBS field to a label_null row (a subtotal whose name the OCR
    dropped) — balance scopes only, conservative. Returns (field|None, method,
    reason). Never guesses: only an arithmetic section-subtotal match counts.

    Signal: the row's AMOUNT_N equals the running sum of the immediately
    preceding *detail* rows (named, non-subtotal) of the same positional section.
    That marks it as the section total -> the matching totalCurrent/Fixed or
    short/longTerm field. A value equal to an adjacent row (OCR duplicate/shift)
    is explicitly NOT assigned.
    """
    field_for = _SECTION_TOTAL_FIELD
    if scope not in {"BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"}:
        return None, None, None

    def amt_n(r):
        for c in r.get("columns", []):
            if c.get("period") == "AMOUNT_N":
                return parse_amount(c.get("value"))
        return None

    target = amt_n(rows[idx])
    if target is None:
        return None, "empty_label_unresolved", "no AMOUNT_N value"

    # OCR duplicate/shift guard: identical to an immediate neighbour's value.
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(rows) and rows[j].get("label"):
            if amt_n(rows[j]) == target:
                return (None, "empty_label_unresolved",
                        f"value {target:g} duplicates neighbour — likely OCR shift")

    # Running sum of preceding contiguous DETAIL rows in the same section,
    # stopping at the previous subtotal/section boundary or a section change.
    # Keep their normalized labels too — their CONTENT identifies the section.
    sect = sections[idx] if idx < len(sections) else None
    run = []
    run_labels = []
    for j in range(idx - 1, -1, -1):
        rj = rows[j]
        if not rj.get("label"):
            break  # hit a previous subtotal (also label_null) -> boundary
        if sections[j] != sect:
            break  # crossed into another section
        v = amt_n(rj)
        if v is not None:
            run.append(v)
            run_labels.append(normalize(rj.get("label") or ""))
    if not run:
        return (None, "empty_label_unresolved",
                "no preceding detail rows to sum")
    s = sum(run)
    tol = max(2.0, 0.01 * abs(target))
    if abs(s - target) > tol:
        return (None, "empty_label_unresolved",
                f"value {target:g} != sum of preceding items ({s:g})")

    # Arithmetic confirms a section subtotal. Identify WHICH section:
    #   1) CONTENT — if the feeding detail rows are equity items (capital social,
    #      reservas, lucros/prejuizos acumulados, ...), this is the equity total
    #      -> shareholdersFundsAmount. (The liabilities side has 3 blocks, so a
    #      pure current/non-current ordinal would wrongly call equity 'non-current'.)
    #   2) positional marker, if Stage-1 found section headers;
    #   3) ordinal fallback — for docs that lost ALL subheadings, the Nth NON-EQUITY
    #      balance subtotal maps current-then-non-current.
    chosen, note = None, ""
    if scope == "BALANCE_LIABILITIES_LST" and run_labels and \
            all(_is_equity_label(lbl) for lbl in run_labels):
        chosen = "equity"
        note = f"; equity block by content ({len(run_labels)} equity items)"
    elif sect is not None:
        chosen = sect
    else:
        # ordinal among NON-equity subtotals only (equity ones are content-tagged)
        n_before = 0
        for j in range(idx):
            rj = rows[j]
            if rj.get("label") or amt_n(rj) is None:
                continue
            # was that earlier subtotal an equity one? (re-derive cheaply)
            pj = []
            for k in range(j - 1, -1, -1):
                if not rows[k].get("label"):
                    break
                if rows[k].get("label"):
                    pj.append(normalize(rows[k]["label"]))
            if pj and all(_is_equity_label(x) for x in pj):
                continue  # skip equity subtotals in the ordinal count
            n_before += 1
        chosen = "current" if n_before == 0 else "noncurrent"
        note = f"; ordinal fallback (non-equity subtotal #{n_before + 1}, no section markers)"

    field = field_for.get((scope, chosen))
    if field:
        return (field, "empty_label_arith",
                f"= sum of {len(run)} preceding items ({s:g}){note}")
    return (None, "empty_label_unresolved",
            f"subtotal of section {chosen!r} has no SBS total field")


def _amount_n(row):
    """AMOUNT_N value of a row as a float, or None."""
    for c in row.get("columns", []):
        if c.get("period") == "AMOUNT_N":
            return parse_amount(c.get("value"))
    return None


def _fill_sections_by_arithmetic(rows, idxs, scope, sect):
    """
    Page-local, GAP-FILL-ONLY section assignment from the numeric structure,
    for layouts whose section boundaries the marker vocabulary can't read
    (abbreviated subtotals like `PASSIVO CIRC` / `EXIGIVEL L PRAZO` in the
    SERASA/provider dump). A row whose AMOUNT_N equals the running sum of the
    preceding contiguous detail rows is a SUBTOTAL closing a block; blocks are
    labelled in order (1st non-equity = current, next = non-current) with an
    equity block recognised by content (so the liabilities equity total is not
    mislabelled non-current). NEVER overrides a section the marker passes set —
    only fills `None` gaps, so working docs (ITR, annual DFP) are untouched.
    Mutates `sect` in place for the given page's `idxs`.
    """
    vals = [_amount_n(rows[i]) for i in idxs]
    norms = [normalize(rows[i].get("label") or "") for i in idxs]
    run, run_pos, nonequity = [], [], 0
    for pos, i in enumerate(idxs):
        v = vals[pos]
        if v is None:
            run, run_pos = [], []
            continue
        s = sum(run)
        if run and abs(s - v) <= max(2.0, 0.01 * abs(v)):
            # this row closes a block: classify it + its feeding detail rows
            block_labels = [norms[p] for p in run_pos]
            if (scope == "BALANCE_LIABILITIES_LST" and block_labels
                    and all(_is_equity_label(l) for l in block_labels)):
                block = "equity"
            else:
                block = "current" if nonequity == 0 else "noncurrent"
                nonequity += 1
            for p in run_pos + [pos]:
                if sect[idxs[p]] is None:        # gap-fill only — never override
                    sect[idxs[p]] = block
            run, run_pos = [], []
        else:
            run.append(v)
            run_pos.append(pos)


# Document types whose balance is a SERASA/provider-style dump: all-caps, no
# CVM codes, abbreviated section markers, frequently split across pages. On these
# the section logic switches to bucket-wide arithmetic (see _compute_sections).
_SERASA_SHAPE_DOC_TYPES = {"SERASA", "UPPERCASE"}


def _has_total_do_markers(rows, scope):
    """True if any row's normalized label is a lexical `total do ...` section
    subtotal (the marker vocabulary the bucket-wide arithmetic replaces). Present
    in DFP / sample2 (full `Total do ativo circulante` wording), absent in the
    SERASA/provider dump (abbreviated `ATIVO CIRCULANTE` / `PASSIVO CIRC`). Used
    as a second, shape-based guard so a code-less DFP-style doc can never slip
    into the bucket-wide branch."""
    markers = _SECTION_MARKERS.get(scope)
    if not markers:
        return False
    total_markers = markers["subtotal_current"] | markers["subtotal_noncurrent"]
    return any(normalize(r.get("label") or "") in total_markers for r in rows)


def _is_serasa_shape(rows, scope, document_type):
    """Whether `_compute_sections` should run BUCKET-WIDE arithmetic for this
    balance bucket instead of the page-local marker passes.

    Combined gate (must satisfy ALL): the doc type is a SERASA/provider dump
    (clean Stage-1 signal), AND no row carries a CVM/B3 `code` (ITR/DFP are
    code-driven and never this shape), AND no lexical `total do ...` subtotal is
    present (DFP/sample2 carry these; their sections must stay page-local so the
    annual DFP's repeated balance copies are never summed across pages). The
    doc-type clause alone would suffice today, but the code/marker clauses are a
    cheap defence-in-depth that keeps DFP/ITR structurally out of this branch."""
    if document_type not in _SERASA_SHAPE_DOC_TYPES:
        return False
    if scope not in _BALANCE_SCOPES:
        return False
    if any(r.get("code") for r in rows):
        return False
    if _has_total_do_markers(rows, scope):
        return False
    return True


def _compute_sections(rows, scope, serasa_shape=False):
    """
    Assign each row a positional balance section ('current'/'noncurrent'/None)
    using BOTH header-led and subtotal-led layouts, page-locally. `scope`
    selects the asset vs liability marker vocabulary.

    Three passes per page:
      * forward    — a HEADER sets the section for itself and rows below it.
      * backward   — a SUBTOTAL claims itself and rows above it that the forward
                     pass left unset (subtotal-led layout, e.g. annual DFP).
      * arithmetic — gap-fill ONLY: for rows still None, infer the section from
                     the numeric subtotal structure (handles abbreviated /
                     OCR-mangled boundary wording the markers can't read, e.g.
                     provider's `PASSIVO CIRC` / `EXIGIVEL L PRAZO`). Never
                     overrides a marker-derived section.

    `serasa_shape` (see _is_serasa_shape) switches to a single, BUCKET-WIDE
    arithmetic pass: on that shape the page-local passes are unsafe — the logical
    balance is split across pages (so a block's detail rows and its subtotal land
    on different pages, defeating page-local arithmetic) and a bare `ativo
    circulante` line is a CLOSING subtotal carrying the block sum, not an opening
    header, so the forward pass would wrongly stamp `current` on the non-current
    rows below it. Summing the whole bucket in document order sidesteps both.
    Returns list[str|None] aligned to `rows`.
    """
    markers = _SECTION_MARKERS.get(scope)
    n = len(rows)
    sect = [None] * n
    if not markers:
        return sect

    # SERASA/provider shape: bucket-wide arithmetic is the AUTHORITATIVE (not
    # gap-fill) section source. Blocks close at their running-sum subtotal,
    # ordered current -> non-current, with an equity block recognised by content.
    if serasa_shape:
        _fill_sections_by_arithmetic(rows, list(range(n)), scope, sect)
        return sect

    h_cur = markers["header_current"]
    h_non = markers["header_noncurrent"]
    s_cur = markers["subtotal_current"]
    s_non = markers["subtotal_noncurrent"]

    norms = [normalize(r.get("label") or "") for r in rows]
    pages = [r.get("page_nb") for r in rows]

    # group row indices by page (sections never cross a page break)
    page_spans = defaultdict(list)
    for i in range(n):
        page_spans[pages[i]].append(i)

    for _, idxs in page_spans.items():
        # forward: headers lead
        cur = None
        for i in idxs:
            nm = norms[i]
            if nm in h_cur:
                cur = "current"
            elif nm in h_non:
                cur = "noncurrent"
            sect[i] = cur
        # backward: subtotals close (fill gaps the forward pass left None)
        cur = None
        for i in reversed(idxs):
            nm = norms[i]
            if nm in s_cur:
                cur = "current"
            elif nm in s_non:
                cur = "noncurrent"
            if sect[i] is None:
                sect[i] = cur
        # arithmetic: fill any remaining None from the numeric subtotal structure
        if scope in ("BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST") \
                and any(sect[i] is None for i in idxs):
            _fill_sections_by_arithmetic(rows, idxs, scope, sect)
    return sect


# --------------------------------------------------------------------------- #
# Stage-3 pipeline — six small, independently-testable stages.
# --------------------------------------------------------------------------- #
# label_statements orchestrates them in two passes:
#   PASS 1 (per statement bucket)
#     _stage_code_match      code-first labelling (CVM codes, ITR/DFP)  [pt.ITR]
#     _stage_match           dict-first match + scope tie-break (non-twin path)
#     _stage_twin_section    current/non-current twin resolution
#     _stage_empty_label     nameless balance subtotal recovery
#   PASS 2 (document-level — needs every bucket labelled first)
#     _stage_scope_gate      block out-of-scope (keep if no in-home copy)  [pt.1]
#     _stage_struct_validate income tier order + balance side check (flag) [pt.5]
#     _stage_balance_liability_totals  arithmetic totalLiabilities (ST+LT) +
#                            equity-in-long-term fix, per balance identity  [pt.TL]
#     _stage_dedup           tag same name+account_type+value duplicates   [pt.6]
_BALANCE_SCOPES = {"BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"}


def _row_amounts_key(row):
    """(account_type, period, value) signature of a row's columns — dedup key."""
    return tuple(sorted(
        (c.get("account_type") or "", c.get("period") or "", c.get("value") or "")
        for c in row.get("columns", [])))


def _stage_code_match(rows, stmt_name, mapping, stats):
    """Stage 0a — code-first labelling for filings that carry CVM/B3 codes.

    A row whose `code` is in the standardized level-1..3 map (mapping._code_map)
    is labelled directly from the code: an authoritative, OCR-robust signal that
    does not depend on the line text. Sets sbs_match_source='code', score 100,
    and marks the row resolved so the downstream text stages leave it alone. Rows
    without a mapped code are untouched and flow on to text matching. The code is
    trusted as-is even where its home statement differs from the current bucket
    (cross_scope is annotated); the document-level scope-gate still applies.
    """
    code_map = mapping.get("_code_map") or {}
    if not code_map:
        return stats
    scope_of = mapping["_scope"]
    fc = mapping.get("_field_config", {})
    for row in rows:
        field = code_map.get(row.get("code"))
        if not field:
            continue
        row["sbs_label"] = field
        row["sbs_match_method"] = "code"
        row["sbs_match_source"] = "code"
        row["sbs_match_score"] = 100.0
        row["_sbs_code_locked"] = True  # internal: text stages skip this row
        allowed = fc.get(field, {}).get("allowed_statements") or []
        home = scope_of.get(field)
        in_scope = (home is None or home == stmt_name or stmt_name in allowed)
        row["sbs_scope"] = "in_scope" if in_scope else "cross_scope"
        stats["scope_in" if in_scope else "scope_cross"] += 1
        stats["row_matched"] += 1
        stats["method_code"] += 1
        stats[f"field_{field}"] += 1
    return stats


def _stage_match(rows, stmt_name, mapping, match_cfg, stats):
    """Stage 0 — dict-first match + the non-twin scope tie-break. Sets sbs_label,
    sbs_match_method, sbs_match_score, sbs_match_source, sbs_scope."""
    variants = mapping["_variants"]
    scope_of = mapping["_scope"]
    dict_clean = mapping.get("_dict_clean")
    dict_conflict = mapping.get("_dict_conflict")
    stop, benign, thr = match_cfg

    for row in rows:
        if row.get("_sbs_code_locked"):
            continue  # already labelled by the code-first stage
        label = row.get("label")
        if not label:
            continue
        hits, source = _match_all(label, variants, stop, benign, thr,
                                  dict_clean, dict_conflict)
        if not hits:
            stats["row_unmatched"] += 1
            continue

        decided = method = candidates = None
        review = False
        if len(hits) == 1:
            decided, method = hits[0][0], ("dict" if source == "dict" else "unique")
        else:
            # Tie: statement scope first, then a clear top score, else flag.
            scoped = [(f, s) for f, s in hits if scope_of.get(f) == stmt_name]
            if len(scoped) == 1:
                decided, method, hits = scoped[0][0], "scope", scoped
            else:
                pool = scoped or hits
                top = pool[0][1]
                leaders = [f for f, s in pool if s >= top - 1e-9]
                if len(leaders) == 1:
                    decided, method, hits = leaders[0], "scope", pool
                else:
                    review = True
                    method = "unresolved_ambiguous"
                    candidates = sorted({f for f, _ in pool})
                    hits = pool

        row["sbs_label"] = decided
        row["sbs_match_method"] = method
        row["sbs_match_source"] = source
        if decided is not None:
            best = max((s for f, s in hits if f == decided), default=None)
            if best is not None:
                row["sbs_match_score"] = round(best, 1)
            fsc = scope_of.get(decided)
            in_scope = (fsc is None or fsc == stmt_name)
            row["sbs_scope"] = "in_scope" if in_scope else "cross_scope"
            stats["scope_in" if in_scope else "scope_cross"] += 1
            stats["row_matched"] += 1
            stats[f"method_{method}"] += 1
            stats[f"field_{decided}"] += 1
        if candidates is not None:
            row["sbs_candidates"] = candidates
        if review:
            row["needs_review"] = True
            stats["row_sbs_needs_review"] += 1
    return stats


def _stage_twin_section(rows, stmt_name, mapping, match_cfg, sections, stats,
                        clear_review=False):
    """Stage 1 — resolve current/non-current twins that matched via a SHARED
    caption (qualifier -> code -> positional section -> needs_review).

    `clear_review` (SERASA-shape buckets only): when a twin is resolved by the
    positional section, the section came from deterministic bucket-wide
    arithmetic, so the resolution is NOT a guess. Clear the ambiguity
    needs_review the match stage raised on a shared-caption (dict_conflict) row
    and drop its now-stale sbs_candidates. The only deliberate exception to the
    project's "needs_review is never cleared" rule, and the reason it is gated to
    the SERASA document type (the lone UPPERCASE golden keeps its flags)."""
    groups_here = mapping.get("_ambiguity_by_scope", {}).get(stmt_name, [])
    if not groups_here:
        return stats
    variants = mapping["_variants"]
    scope_of = mapping["_scope"]
    dict_clean = mapping.get("_dict_clean")
    dict_conflict = mapping.get("_dict_conflict")
    stop, benign, thr = match_cfg

    def shared_match(group, label):
        return anchor_match(sorted(group["shared"]), label, stop, benign, thr,
                            token_fuzzy=SBS_TOKEN_FUZZY)[0]

    for idx, row in enumerate(rows):
        if row.get("_sbs_code_locked"):
            continue  # the code already resolved which twin (e.g. 2.01.04=ST)
        label = row.get("label")
        if not label:
            continue
        hits, _src = _match_all(label, variants, stop, benign, thr,
                                dict_clean, dict_conflict)
        hit_fields = {f for f, _ in hits}
        twin_group = None
        for g in groups_here:
            if hit_fields & {g["current"], g["noncurrent"]} and shared_match(g, label):
                twin_group = g
                break
        if twin_group is None:
            continue

        qual = _section_qualifier(normalize(label))
        decided, method, reason = _resolve_twin(
            twin_group, qual, row.get("code"), sections[idx])
        row["sbs_review_reason"] = reason
        if decided is None:
            row["sbs_label"] = None
            row["sbs_match_method"] = "unresolved_ambiguous"
            row["sbs_candidates"] = sorted({twin_group["current"], twin_group["noncurrent"]})
            row["needs_review"] = True
            row.pop("sbs_scope", None)
            stats["twin_unresolved"] += 1
        else:
            row["sbs_label"] = decided
            row["sbs_match_method"] = method
            fsc = scope_of.get(decided)
            row["sbs_scope"] = "in_scope" if (fsc is None or fsc == stmt_name) else "cross_scope"
            # Deterministic positional resolution on a SERASA-shape bucket is not
            # a guess: retract the ambiguity flag/candidates the match stage set.
            if clear_review and method == "positional":
                if row.pop("sbs_candidates", None) is not None and row.get("needs_review"):
                    row["needs_review"] = False
                    stats["twin_review_cleared"] += 1
            stats["twin_resolved"] += 1
    return stats


def _stage_empty_label(rows, stmt_name, sections, stats):
    """Stage 2 — recover an SBS field for nameless balance subtotals (label
    dropped by OCR), conservatively and arithmetically."""
    if stmt_name not in _BALANCE_SCOPES:
        return stats
    small_span = _is_small_span(rows)
    for idx, row in enumerate(rows):
        if row.get("label"):
            continue
        stats["row_no_label"] += 1
        shown = small_span or str(row.get("page_class", "")).startswith("PRIMARY")
        if not shown:
            continue
        ef, em, er = _resolve_empty_label(rows, idx, stmt_name, sections)
        if em is None:
            continue
        row["sbs_match_method"] = em
        if er:
            row["sbs_review_reason"] = er
        if ef is not None:
            row["sbs_label"] = ef
            row["sbs_scope"] = "in_scope"
            stats["empty_label_resolved"] += 1
            stats[f"field_{ef}"] += 1
        else:
            row["needs_review"] = True
            stats["empty_label_unresolved"] += 1
    return stats


def _stage_scope_gate(statements, mapping, stats):
    """Stage 3 (точка 1) — block out-of-scope matches. A match whose bucket is
    not in the field's allowed_statements is BLOCKED (sbs_label -> None,
    sbs_blocked_reason set) ONLY if that field is also captured in-home elsewhere
    in this document (no data lost); otherwise the label is KEPT and flagged
    needs_review (recover). Double-home fields are allowed in two statements."""
    fc = mapping.get("_field_config", {})
    # in-home index: field -> set of buckets where it sits in an allowed statement
    in_home = defaultdict(set)
    for bucket, rows in statements.items():
        for r in rows:
            f = r.get("sbs_label")
            if f and f in fc and bucket in fc[f]["allowed_statements"]:
                in_home[f].add(bucket)

    for bucket, rows in statements.items():
        for r in rows:
            f = r.get("sbs_label")
            if not f or f not in fc:
                continue
            allowed = fc[f]["allowed_statements"]
            if not allowed or bucket in allowed:
                continue
            if in_home.get(f):
                r["sbs_blocked_reason"] = "out_of_scope"
                r["sbs_label"] = None
                r.pop("sbs_scope", None)
                stats["scope_blocked"] += 1
            else:
                r["sbs_scope"] = "cross_scope"
                r["needs_review"] = True
                r["sbs_review_reason"] = (
                    f"out-of-scope ({bucket}); no in-home copy — kept for review")
                stats["scope_recovered"] += 1
    return stats


def _stage_struct_validate(statements, mapping, stats):
    """Stage 4 (точка 5) — FLAG-ONLY structural checks. Income tier order
    (a later tier above an earlier one) and balance asset/liability side
    mismatch raise needs_review; a label is NEVER stripped (order anomalies are
    often OCR row-shuffle, not a wrong label)."""
    fc = mapping.get("_field_config", {})
    side_of = {"BALANCE_ASSETS_LST": "asset", "BALANCE_LIABILITIES_LST": "liab"}

    # income tier monotonicity, per page-run
    inc = statements.get("INCOME_STATEMENT_LST", [])
    by_page = defaultdict(list)
    for idx, r in enumerate(inc):
        f = r.get("sbs_label")
        if f and f in fc and fc[f].get("income_tier") \
                and fc[f]["home_statement"] == "INCOME_STATEMENT_LST":
            by_page[r.get("page_nb")].append((idx, fc[f]["income_tier"]))
    for _pg, seq in by_page.items():
        for i in range(len(seq) - 1):
            if seq[i][1] > seq[i + 1][1]:
                for j in (seq[i][0], seq[i + 1][0]):
                    inc[j]["needs_review"] = True
                    inc[j].setdefault("sbs_review_reason",
                                      "income tier order anomaly (possible OCR row shuffle)")
                stats["tier_anomaly"] += 1

    # balance asset/liability side mismatch
    for bucket, rows in statements.items():
        if bucket not in side_of:
            continue
        for r in rows:
            f = r.get("sbs_label")
            if not f or f not in fc:
                continue
            if not fc[f].get("home_section"):
                continue
            home = fc[f].get("home_statement")
            if home in side_of and home != bucket:
                r["needs_review"] = True
                r.setdefault("sbs_review_reason",
                             f"{fc[f].get('category')} field in {bucket} (section mismatch)")
                stats["section_anomaly"] += 1
    return stats


def _stage_serasa_net_income(statements, stats):
    """Stage 5b (SERASA template) — the income statement carries TWO net-income
    lines: 'RESULT EXERCICIO' (profit for the period) followed by 'DIVIDENDOS'
    and then 'RESULTADO LIQUIDO' (= the period profit minus dividends, i.e.
    retained). Both match posttaxProfitAmount, doubling the field. Per the agreed
    semantics, post-tax profit IS the profit for the period (before dividends),
    so the SECOND posttax line — the one appearing AFTER a dividendAmount row —
    is the after-dividend retained figure and is cleared (it has no SBS home).

    Gated to the SERASA document type by the caller, and further guarded to fire
    only on the posttax-after-dividend ordering, so a single net-income statement
    is never touched. The kept (first) posttax line is left untouched.
    """
    inc = statements.get("INCOME_STATEMENT_LST", [])
    seen_dividend = False
    kept_posttax = False
    for r in inc:
        field = r.get("sbs_label")
        if field == "dividendAmount":
            seen_dividend = True
        elif field == "posttaxProfitAmount":
            if seen_dividend and kept_posttax:
                # after-dividend retained profit — not an SBS field.
                r["sbs_label"] = None
                r["sbs_blocked_reason"] = "net_after_dividends"
                r.pop("sbs_scope", None)
                r["needs_review"] = True
                stats["serasa_net_after_dividends"] += 1
            else:
                kept_posttax = True
    return stats


# --------------------------------------------------------------------------- #
# Balance liability totals — arithmetic resolution of totalLiabilitiesAmount.
# --------------------------------------------------------------------------- #
# totalLiabilitiesAmount (SBS #23) is "Total liabilities (short-term + long-term
# + provisions)" — liabilities ONLY, EXCLUDING equity. It is therefore NOT the
# Portuguese balance bottom line ("Total do passivo e patrimônio líquido" /
# "Passivo Total"), which is the GRAND TOTAL (liabilities + equity == total
# assets). Text-aliasing the bottom line mislabels the grand total as total
# liabilities, so this field carries NO aliases (see build_sbs_mapping.py) and is
# resolved here, arithmetically, from the identity
#       total assets  ==  short-term + long-term liabilities  +  equity.
# This stage does two related things per balance "cell" (one account_type +
# period — each is its own self-balancing statement):
#   RC#2 fix — some templates (notably Serasa) print a "PASSIVO NÃO CIRCULANTE"
#     subtotal that ALREADY INCLUDES equity. The marker/arithmetic section logic
#     then labels that inflated figure longTermLiabilitiesAmount and leaves the
#     real long-term line ("EXIGÍVEL A LONGO PRAZO") unlabelled, double-counting
#     equity and breaking the balance identity by exactly the equity amount. When
#     the labelled long-term value equals (assets − short-term) AND a non-zero
#     equity exists AND it differs from (assets − short-term − equity), the long-
#     term label is on the wrong (equity-inclusive) line: demote it and promote
#     the unlabelled line whose value equals the true long-term (assets − ST − EQ).
#   totalLiabilities — relabel a real printed "Total do passivo" line to
#     totalLiabilitiesAmount IFF its value equals short-term + long-term (the true
#     liabilities total); never the grand total. If no such line is printed (the
#     common case — most templates print only the grand total), SYNTHESIZE a
#     derived row carrying ST + LT per cell (sbs_match_method='derived').
# Grand-total captions: liabilities + equity == total assets. A row carrying one
# of these is NEVER total liabilities — used as an explicit exclusion guard so the
# relabel step can never promote a grand-total line even if its value coincides
# with ST + LT (e.g. when long-term liabilities are zero).
_GRAND_TOTAL_CAPS = {
    "total do passivo e patrimonio liquido",
    "total do passivo e do patrimonio liquido",
    "total passivo e patrimonio liquido",
    "total do passivo e patrimonio liquido consolidado",
    "passivo total",
    "passivo e patrimonio liquido",
}
# Captions a true "total liabilities" (ST + LT, equity excluded) line can carry.
_TOTAL_LIAB_CAPS = {
    "total do passivo",
    "total do exigivel",
    "total do passivo exigivel",
    "passivo exigivel",
    "total das obrigacoes",
}


def _cell_value(row, account_type, period):
    """The row's value for one (account_type, period) cell, or None."""
    for c in row.get("columns", []):
        if (c.get("account_type") or "") == account_type and c.get("period") == period:
            return parse_amount(c.get("value"))
    return None


def _cell_keys(row):
    """The (account_type, period) keys a row carries a parseable value for."""
    out = []
    for c in row.get("columns", []):
        if parse_amount(c.get("value")) is not None:
            out.append((c.get("account_type") or "", c.get("period")))
    return out


def _fmt_amount(value):
    """Render a float back to the project's amount string (dot = thousands), so a
    synthesized row round-trips through parse_amount. Integers get dotted
    thousands; a fractional value keeps two decimals with a comma (BR style)."""
    neg = value < 0
    av = abs(value)
    if abs(av - round(av)) < 1e-6:
        s = f"{int(round(av)):,}".replace(",", ".")
    else:
        s = f"{av:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"({s})" if neg else s


def _stage_balance_liability_totals(statements, mapping, stats):
    """Stage 5c — arithmetic totalLiabilitiesAmount + the equity-in-long-term fix.

    Runs over the liabilities bucket using the balance identity
    (assets == short-term + long-term + equity) per (account_type, period) cell.
    See the module-level note above for the two corrections it makes. Pure
    arithmetic, self-gating: a no-op on a balance that is already internally
    consistent (DFP/ITR/2-col), active only where a figure double-counts equity
    or where the true liabilities total is not printed as its own line.
    """
    liab = statements.get("BALANCE_LIABILITIES_LST")
    assets = statements.get("BALANCE_ASSETS_LST")
    if not liab:
        return stats

    def tol(v):
        return max(2.0, 0.01 * abs(v))

    def shown(row):
        # mirror the exporter's visibility: a PRIMARY page, else any (SERASA/ITR/
        # UPPERCASE keep NOTE rows) — used to prefer the primary balance instance
        # over NOTE segment copies when several rows carry the same cell.
        return str(row.get("page_class") or "").startswith("PRIMARY")

    def pick(rows, field, key):
        """Value of `field` for cell `key`: the PRIMARY instance if any, else the
        first in-bucket occurrence (so segment-copy NOTE rows never override the
        real balance)."""
        chosen = None
        for r in rows:
            if r.get("sbs_label") != field:
                continue
            v = _cell_value(r, key[0], key[1])
            if v is None:
                continue
            if shown(r):
                return v
            if chosen is None:
                chosen = v
        return chosen

    # Every cell that the balance is expressed in (from the short-term line — it
    # is always printed, even in the Serasa shape).
    keys = set()
    for r in liab:
        if r.get("sbs_label") == "shortTermLiabilitiesAmount":
            keys.update(_cell_keys(r))
    if not keys:
        return stats

    derived_cols = []  # (key, total_liab_value) to synthesize if not printed
    for key in sorted(keys):
        at, period = key
        ST = pick(liab, "shortTermLiabilitiesAmount", key)
        LT = pick(liab, "longTermLiabilitiesAmount", key)
        EQ = pick(liab, "shareholdersFundsAmount", key)
        G = pick(assets or [], "totalAssetsAmount", key)

        # RC#2: the labelled long-term value equals (assets − ST) — i.e. it is the
        # equity-inclusive non-current subtotal, not the real long-term liability.
        if (LT is not None and EQ not in (None, 0) and G is not None
                and ST is not None
                and abs(LT - (G - ST)) <= tol(G - ST)
                and abs(LT - (G - ST - EQ)) > tol(EQ)):
            lt_true = G - ST - EQ
            # demote the equity-inclusive long-term row for this cell
            for r in liab:
                if (r.get("sbs_label") == "longTermLiabilitiesAmount"
                        and _cell_value(r, at, period) is not None
                        and abs(_cell_value(r, at, period) - LT) <= tol(LT)):
                    r["sbs_label"] = None
                    r["sbs_blocked_reason"] = "includes_equity"
                    r["sbs_review_reason"] = (
                        "non-current subtotal includes equity (assets-ST); "
                        "true long-term liabilities relabelled separately")
                    r["needs_review"] = True
                    r.pop("sbs_scope", None)
                    stats["liab_longterm_demoted"] += 1
            # promote the unlabelled line whose value == true long-term
            for r in liab:
                if (r.get("sbs_label") is None and not r.get("sbs_blocked_reason")
                        and _cell_value(r, at, period) is not None
                        and abs(_cell_value(r, at, period) - lt_true) <= tol(lt_true)):
                    r["sbs_label"] = "longTermLiabilitiesAmount"
                    r["sbs_match_method"] = "balance_identity"
                    r["sbs_match_source"] = "arithmetic"
                    r["sbs_match_score"] = 100.0
                    r["sbs_scope"] = "in_scope"
                    r["sbs_review_reason"] = (
                        "long-term liabilities = assets - short-term - equity")
                    stats["liab_longterm_promoted"] += 1
                    break
            LT = lt_true

        # totalLiabilitiesAmount = ST + LT (liabilities only, equity excluded).
        if ST is None and LT is None:
            continue
        total_liab = (ST or 0.0) + (LT or 0.0)
        # If a long-term line was never labelled for this cell, the total may be
        # incomplete (the doc may simply have no non-current liabilities, or OCR
        # dropped it) — carry it but flag for a human rather than assert it.
        partial = LT is None

        # relabel a printed "Total do passivo"-type line whose value matches.
        relabelled = False
        for r in liab:
            if r.get("sbs_label") not in (None, "totalLiabilitiesAmount"):
                continue
            if r.get("sbs_blocked_reason"):
                continue
            nlab = normalize(r.get("label") or "")
            v = _cell_value(r, at, period)
            if v is None or nlab not in _TOTAL_LIAB_CAPS or nlab in _GRAND_TOTAL_CAPS:
                continue
            if abs(v - total_liab) <= tol(total_liab):
                if r.get("sbs_label") != "totalLiabilitiesAmount":
                    r["sbs_label"] = "totalLiabilitiesAmount"
                    r["sbs_match_method"] = "balance_identity"
                    r["sbs_match_source"] = "arithmetic"
                    r["sbs_match_score"] = 100.0
                    r["sbs_scope"] = "in_scope"
                    r["sbs_review_reason"] = "total liabilities = short-term + long-term"
                    if partial:
                        r["needs_review"] = True
                    stats["total_liab_relabelled"] += 1
                relabelled = True
                break
        if not relabelled:
            derived_cols.append((key, total_liab, partial))

    # Synthesize a single derived totalLiabilitiesAmount row carrying every cell
    # that had no printed total-liabilities line (the common case). One row holds
    # all (account_type, period) cells, mirroring how real total rows are shaped.
    if derived_cols:
        columns = [{"account_type": (at or None), "period": period,
                    "value": _fmt_amount(v)} for (at, period), v, _p in derived_cols]
        partial_any = any(p for _k, _v, p in derived_cols)
        liab.append({
            "label": "Total liabilities (derived = current + non-current)",
            "page_nb": None,
            "page_class": None,
            "columns": columns,
            "needs_review": partial_any,
            "sbs_label": "totalLiabilitiesAmount",
            "sbs_match_method": "derived",
            "sbs_match_source": "arithmetic",
            "sbs_match_score": 100.0,
            "sbs_scope": "in_scope",
            "sbs_review_reason": (
                "derived total liabilities (short-term + long-term); "
                "no explicit liabilities-total line in the source"),
            "sbs_derived": True,
        })
        stats["total_liab_derived"] += 1
    return stats


def _page_kind(row):
    """Coarse page class of a row — PRIMARY | NOTE | JUNK | '' — for cross-page
    duplicate matching. NOTE_segment collapses to NOTE; a PRIMARY copy and a NOTE
    copy of the same line therefore keep DIFFERENT kinds, so the SBS dedup never
    merges a note copy onto a primary line (that separation is what the exporter's
    page_class filter relies on). Empty when page_class is missing/unknown."""
    pc = str(row.get("page_class") or "")
    for k in ("PRIMARY", "NOTE", "JUNK"):
        if pc.startswith(k):
            return k
    return ""


def _income_tier_positions(rows, fc):
    """[(idx, tier_float)] for rows labelled with an income field that carries an
    income_tier whose home IS the income statement — the canonical top-to-bottom
    sequence (turnover=1 ... posttaxProfit=9). Used to pick the duplicate-cluster
    survivor that best respects that order. Empty for non-income buckets / fields
    without a tier, so dedup there keeps its first-occurrence behaviour."""
    out = []
    for idx, r in enumerate(rows):
        f = r.get("sbs_label")
        if not f:
            continue
        c = fc.get(f)
        if not c or c.get("home_statement") != "INCOME_STATEMENT_LST":
            continue
        t = c.get("income_tier")
        if t is None:
            continue
        try:
            out.append((idx, float(t)))
        except (TypeError, ValueError):
            continue
    return out


def _seq_inversions(idx, tier, tier_positions):
    """How many other tier-bearing rows sit on the WRONG side of `idx` for its
    tier: an earlier row with a higher tier, or a later row with a lower tier. A
    row in its canonical place (e.g. net income LAST) scores 0; an OCR-displaced
    copy of the same field (net income FIRST) scores many. Lower = better-placed.
    Returns 0 when the row has no tier (so it never biases a non-income cluster)."""
    if tier is None:
        return 0
    bad = 0
    for q, u in tier_positions:
        if q == idx:
            continue
        if (q < idx and u > tier) or (q > idx and u < tier):
            bad += 1
    return bad


def _stage_dedup(statements, mapping, stats):
    """Stage 5 (точка 6) — tag duplicate rows; physical removal is the exporter's
    job (sbs_duplicate_of -> the kept row's index). Two complementary keys, both
    scoped to one statement bucket and both keyed on the same
    (account_type, period, value) amount signature — so Individual vs Consolidado
    perspectives are NEVER merged (they share a name + value but are distinct).

      (a) NAME duplicate — same page + same normalized label + same amounts.
          Catches an OCR re-print of an identical line (incl. one differing only
          by a stripped '(-)' sign); rows the OCR left unlabelled are skipped.

      (b) SBS duplicate  — same sbs_label + same amounts, when the two rows sit on
          the SAME page OR share the SAME page kind (PRIMARY/NOTE/JUNK). Catches
          the same figure RE-WORDED under a different caption that (a) misses
          (e.g. 'CUSTOS DE REVENDA DE MERCADORIAS' vs 'CUSTOS DE MERCADORIAS
          REVENDIDAS', both rawMaterialsAndConsumablesAmount, same amount). The
          page-OR-kind scope extends dedup across the pages of ONE statement copy
          (a note re-stating its own bottom line two pages on) while still NEVER
          merging a NOTE copy onto a PRIMARY line (different kind) — preserving
          the primary-vs-segment-copy separation the exporter depends on.

    SURVIVOR CHOICE — within every duplicate cluster the row KEPT (and shown in
    Excel) is the one BEST RESPECTING the statement's canonical order, not blindly
    the first in document order: the income field's `income_tier` gives that order
    (turnover -> ... -> net income), and we keep the row with the fewest sequence
    inversions (_seq_inversions); ties (and every non-income bucket, where no tier
    exists) fall back to the earliest index, preserving prior behaviour. This fixes
    OCR layouts that print a total (e.g. net income) BOTH at the top and in its
    correct place at the bottom — the bottom copy now survives. The two passes
    share one `survivor()` so removal targets are consistent.

    The SBS pass skips rows already removed by (a) so it never builds chains."""
    fc = mapping.get("_field_config", {})
    for bucket, rows in statements.items():
        tier_positions = _income_tier_positions(rows, fc)
        tier_by_idx = dict(tier_positions)

        def survivor(idxs):
            """Cluster member to KEEP: fewest sequence inversions, then earliest."""
            return min(idxs, key=lambda i: (
                _seq_inversions(i, tier_by_idx.get(i), tier_positions), i))

        dupes = {}  # idx -> kept idx (so the SBS pass skips already-removed rows)

        def tag_cluster(idxs):
            rep = survivor(idxs)
            for i in idxs:
                if i == rep:
                    continue
                rows[i]["sbs_duplicate_of"] = rep
                rows[i]["needs_review"] = True
                dupes[i] = rep
            return rep

        # (a) name-based, page-scoped: cluster by (page, name, amounts).
        name_clusters = defaultdict(list)
        for idx, r in enumerate(rows):
            label = r.get("label")
            if not label:
                continue
            amt = _row_amounts_key(r)
            if not amt or all(v == "" for _, _, v in amt):
                continue
            name_clusters[(r.get("page_nb"), normalize(label), amt)].append(idx)
        for _k, idxs in name_clusters.items():
            if len(idxs) < 2:
                continue
            tag_cluster(idxs)
            stats["duplicate_tagged"] += len(idxs) - 1

        # (b) SBS-based, same-page-or-same-kind, within the bucket. First split
        # each (sbs_label, amounts) group into page-OR-kind connected clusters
        # (skipping rows already removed by (a)), THEN pick each cluster's survivor
        # by canonical order — so the kept row is the best-placed one, not a seed.
        groups = defaultdict(list)
        for idx, r in enumerate(rows):
            f = r.get("sbs_label")
            if not f:
                continue
            amt = _row_amounts_key(r)
            if not amt or all(v == "" for _, _, v in amt):
                continue
            groups[(f, amt)].append(idx)
        for _grp_key, idxs in groups.items():
            members = [i for i in idxs if i not in dupes]
            if len(members) < 2:
                continue
            # group members into clusters joined by same page OR same kind
            clusters = []  # list of {"members": [...], "sigs": [(pg, kind), ...]}
            for idx in members:
                pg, kind = rows[idx].get("page_nb"), _page_kind(rows[idx])
                hit = None
                for cl in clusters:
                    if any(pg == cpg or (kind and kind == ck)
                           for cpg, ck in cl["sigs"]):
                        hit = cl
                        break
                if hit is None:
                    clusters.append({"members": [idx], "sigs": [(pg, kind)]})
                else:
                    hit["members"].append(idx)
                    hit["sigs"].append((pg, kind))
            for cl in clusters:
                if len(cl["members"]) < 2:
                    continue
                tag_cluster(cl["members"])
                stats["duplicate_tagged"] += len(cl["members"]) - 1
                stats["duplicate_tagged_sbs"] += len(cl["members"]) - 1
    return stats


# --------------------------------------------------------------------------- #
# Statement-level labelling (orchestrator)
# --------------------------------------------------------------------------- #
def label_statements(statements, mapping, match_cfg, stats=None, document_type=None):
    """
    Stamp `sbs_label` (+ scope / method / score / structural flags) on every row
    in `statements` (mutates in place), via the Stage-3 pipeline. `document_type`
    (from Stage 1) gates type-specific stages; when None, only the generic stages
    run (so older callers keep working unchanged).

    Row fields produced:
      * sbs_label          : resolved SBS Field Name, or None
      * sbs_match_method   : code | dict | unique | scope | qualifier |
                             positional | unresolved_ambiguous |
                             empty_label_arith | empty_label_unresolved |
                             balance_identity | derived
      * sbs_derived        : true on a Stage-3-synthesized total row (no source line)
      * sbs_match_source   : code | dict | dict_conflict | fuzzy  (matched engine)
      * sbs_match_score    : fuzzy score (100 for an exact dict hit)
      * sbs_scope          : in_scope | cross_scope
      * sbs_candidates     : competing fields, only when left unresolved
      * sbs_review_reason  : human-readable note
      * sbs_blocked_reason : 'out_of_scope' when the scope-gate cleared a label
      * sbs_duplicate_of   : index of the kept row this one duplicates (точка 6)
      * needs_review       : OR-ed true on any uncertainty (never cleared)

    Returns a stats Counter-dict.
    """
    if stats is None:
        stats = Counter()
    balance_or_twin = mapping.get("_ambiguity_by_scope", {})

    # Drop any rows a PRIOR run of this stage synthesized (the derived
    # totalLiabilitiesAmount line). They are not source data, so re-labelling an
    # already-labelled output must not accumulate them — keeps the stage idempotent.
    for rows in statements.values():
        rows[:] = [r for r in rows if not r.get("sbs_derived")]

    # PASS 1 — per-bucket match + structural resolution.
    for stmt_name, rows in statements.items():
        groups_here = balance_or_twin.get(stmt_name, [])
        serasa_shape = _is_serasa_shape(rows, stmt_name, document_type)
        sections = (_compute_sections(rows, stmt_name, serasa_shape=serasa_shape)
                    if (groups_here or stmt_name in _BALANCE_SCOPES)
                    else [None] * len(rows))
        # On a SERASA-shape bucket the section comes from deterministic
        # arithmetic (the running-sum subtotal proves it), so a positionally
        # resolved twin is NOT a guess -> clear the ambiguity review flag.
        # Restricted to document_type SERASA: the lone UPPERCASE golden keeps
        # its existing positional-twin flags (no regression), while still
        # benefiting from the same bucket-wide sections.
        clear_twin = serasa_shape and document_type == "SERASA"
        _stage_code_match(rows, stmt_name, mapping, stats)
        _stage_match(rows, stmt_name, mapping, match_cfg, stats)
        _stage_twin_section(rows, stmt_name, mapping, match_cfg, sections,
                            stats, clear_review=clear_twin)
        _stage_empty_label(rows, stmt_name, sections, stats)

    # PASS 2 — document-level stages (need all buckets labelled first).
    _stage_scope_gate(statements, mapping, stats)
    _stage_struct_validate(statements, mapping, stats)
    if document_type == "SERASA":
        _stage_serasa_net_income(statements, stats)
    # Arithmetic totalLiabilitiesAmount + equity-in-long-term fix. Runs after the
    # ST/LT/EQ labels are stable (scope-gate) and before dedup, so any relabelled
    # or synthesized total participates in deduplication consistently.
    _stage_balance_liability_totals(statements, mapping, stats)
    _stage_dedup(statements, mapping, stats)

    # Strip internal bookkeeping that must not reach the public output.
    for rows in statements.values():
        for row in rows:
            row.pop("_sbs_code_locked", None)
    return stats


def annotate_output(output, mapping=None, match_cfg=None):
    """
    Annotate a full structure_ocr output dict (with a 'statements' key).
    Reads `document_type` from the output to gate type-specific stages.
    Returns stats. Mutates output in place.
    """
    if mapping is None:
        mapping = load_mapping()
    if match_cfg is None:
        match_cfg = load_match_config()
    statements = output.get("statements", {})
    return label_statements(statements, mapping, match_cfg,
                            document_type=output.get("document_type"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_stats(stats, total_rows):
    print("=" * 60)
    print("SBS LABELLING")
    print("=" * 60)
    matched = stats.get("row_matched", 0)
    print(f"  rows total (labeled)   : {total_rows}")
    print(f"  rows matched -> sbs    : {matched}")
    print(f"  rows unmatched         : {stats.get('row_unmatched', 0)}")
    print(f"  rows without a label   : {stats.get('row_no_label', 0)}")
    print(f"  in_scope / cross_scope : {stats.get('scope_in', 0)} / {stats.get('scope_cross', 0)}")
    print(f"  sbs needs_review       : {stats.get('row_sbs_needs_review', 0)}")
    print("  pipeline stages:")
    print(f"      twin resolved/open    : {stats.get('twin_resolved', 0)} / {stats.get('twin_unresolved', 0)}")
    print(f"      empty-label res/open  : {stats.get('empty_label_resolved', 0)} / {stats.get('empty_label_unresolved', 0)}")
    print(f"      scope-gate block/keep : {stats.get('scope_blocked', 0)} / {stats.get('scope_recovered', 0)}")
    print(f"      struct flags tier/side: {stats.get('tier_anomaly', 0)} / {stats.get('section_anomaly', 0)}")
    print(f"      totalLiab relabel/deriv: {stats.get('total_liab_relabelled', 0)} / {stats.get('total_liab_derived', 0)}")
    print(f"      LT promote/demote (RC2): {stats.get('liab_longterm_promoted', 0)} / {stats.get('liab_longterm_demoted', 0)}")
    print(f"      duplicates tagged     : {stats.get('duplicate_tagged', 0)}")
    print("  resolution methods:")
    for k in sorted(stats):
        if k.startswith("method_"):
            print(f"      {k[7:]:22}: {stats[k]}")
    print("  per SBS field:")
    fld = sorted(((k[6:], v) for k, v in stats.items() if k.startswith("field_")),
                 key=lambda t: -t[1])
    for name, n in fld:
        print(f"      {name:34}: {n}")
    print("=" * 60)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="Stamp sbs_label on a structured_output JSON.")
    ap.add_argument("input", help="structured_output JSON (from structure_ocr.py)")
    ap.add_argument("-o", "--output",
                    help="output path (default: overwrite input). Ignored with --dry-run.")
    ap.add_argument("--mapping", default=MAPPING_PATH, help="sbs_label_mapping.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; do not write")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        output = json.load(f)

    mapping = load_mapping(args.mapping)
    match_cfg = load_match_config()

    total_rows = sum(
        1 for rows in output.get("statements", {}).values()
        for r in rows if r.get("label")
    )
    stats = annotate_output(output, mapping, match_cfg)
    _print_stats(stats, total_rows)

    if args.dry_run:
        print("\n(dry-run: nothing written)")
        return

    out_path = args.output or args.input
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nЗаписано: {out_path}")


if __name__ == "__main__":
    main()
