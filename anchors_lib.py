#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
anchors_lib.py — Shared primitives for anchor matching & geometry.

Single source of truth for:
  * text normalization (accents, note refs, punctuation, stopwords)
  * the corpus-validated anchor-matching rule (token coverage + benign
    insertions + fuzzy backstop)
  * Brazilian number parsing
  * geometry helpers (centers, overlap, x-band clustering)
  * fuzzy backend selection (rapidfuzz, with difflib fallback)

Used by both `classify_statements.py` and `diagnose_pages.py` so the matching
logic can never drift between them.
"""

import re
import unicodedata

# --------------------------------------------------------------------------- #
# Fuzzy backend: rapidfuzz if available, else stdlib difflib (portable).
# --------------------------------------------------------------------------- #
try:
    from rapidfuzz import fuzz as _rf_fuzz

    FUZZ_BACKEND = "rapidfuzz"

    def token_sort_ratio(a, b):
        return _rf_fuzz.token_sort_ratio(a, b)

except ImportError:  # pragma: no cover - environment-dependent
    from difflib import SequenceMatcher

    FUZZ_BACKEND = "difflib"

    def token_sort_ratio(a, b):
        sa = " ".join(sorted(a.split()))
        sb = " ".join(sorted(b.split()))
        return SequenceMatcher(None, sa, sb).ratio() * 100.0


# --------------------------------------------------------------------------- #
# Text normalization & token matching
# --------------------------------------------------------------------------- #
_NOTE_REF = re.compile(r"\(nota[^)]*\)")
_LEADING_SIGNS = re.compile(r"^[\s()+\-=.]+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")


def normalize(s):
    """
    Canonicalize a line name for comparison:
      strip accents -> lowercase -> drop "(Nota XX)" -> drop leading (+)/(-)/(=)
      -> drop punctuation -> collapse whitespace.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NOTE_REF.sub(" ", s)
    s = _LEADING_SIGNS.sub("", s)
    s = _NON_ALNUM.sub(" ", s)
    return _WS.sub(" ", s).strip()


def sig_tokens(s, stopwords):
    """Significant (non-stopword) tokens of a normalized string."""
    return set(normalize(s).split()) - stopwords


def _infl_prefix(a, b):
    """
    True if two single tokens share a long stem — catches Portuguese
    inflection / OCR spelling drift that exact set-equality misses:
      credito/creditos, aplicacao/aplicacoes, custo/custos, divida/dividas.
    Requires a common prefix of >=4 chars AND >=80% of the shorter token, so
    short or merely same-initial words (e.g. caixa/capital) do NOT pass.
    """
    if len(a) < 4 or len(b) < 4:
        return False
    k = 0
    for x, y in zip(a, b):
        if x == y:
            k += 1
        else:
            break
    return k >= 4 and k >= 0.8 * min(len(a), len(b))


def _token_covers(ct, line_tokens, token_fuzzy):
    """A canon token `ct` is covered by SOME line token iff equal, a stem match,
    or fuzzily close (>= token_fuzzy). Used only when token_fuzzy > 0."""
    for lt in line_tokens:
        if lt == ct or _infl_prefix(ct, lt) or token_sort_ratio(ct, lt) >= token_fuzzy:
            return True
    return False


def anchor_match(canon_variants, line, stopwords, benign, threshold, token_fuzzy=0):
    """
    Corpus-validated matching rule. A line matches an anchor iff SOME variant:
      1) coverage   — every significant canon token is present in the line,
      2) no foreign — every extra line token is a benign insertion (so a
         narrowing qualifier like 'circulante'/'segmento' blocks the match),
      3) backstop   — token_sort_ratio >= threshold (absorbs OCR typos).

    canon_variants: list[str]  (canonical phrase + aliases)

    token_fuzzy (default 0 == OFF): when > 0, coverage and the foreign-token
    test are evaluated at the TOKEN level with fuzzy/stem equality instead of
    exact set equality. This recalls inflected / abbreviated / OCR-drifted forms
    (`credito`~`creditos`, `custos`~`custo`) that the strict rule rejects before
    the phrase backstop is ever consulted. The default keeps the rule BYTE-
    IDENTICAL for callers that omit it (Stage-2 classifier, diagnostics); the
    SBS labeller (Stage-3, recall-oriented) opts in. The phrase backstop and the
    benign/foreign discipline are preserved, so a narrowing qualifier still
    blocks the match — the relaxation only spans spelling variants of tokens
    that ARE present.

    Returns (matched: bool, best_score: float).
    """
    ls = sig_tokens(line, stopwords)
    nline = normalize(line)
    best = 0.0
    lst = list(ls) if token_fuzzy else None
    for canon in canon_variants:
        cs = sig_tokens(canon, stopwords)
        if not cs:
            continue
        if token_fuzzy:
            # token-level fuzzy coverage + fuzzy-aware foreign-token test
            if any(not _token_covers(ct, lst, token_fuzzy) for ct in cs):
                continue
            foreign = False
            for lt in ls:
                if lt in benign:
                    continue
                if any(lt == ct or _infl_prefix(ct, lt)
                       or token_sort_ratio(ct, lt) >= token_fuzzy for ct in cs):
                    continue
                foreign = True
                break
            if foreign:
                continue
        else:
            if cs - ls:                  # a required canon token is missing
                continue
            if (ls - cs) - benign:       # a foreign (narrowing) extra token
                continue
        best = max(best, token_sort_ratio(normalize(canon), nline))
    return best >= threshold, best


def anchor_variants(anchor_def):
    """All acceptable phrases for an anchor entry: canon + aliases."""
    return [anchor_def["canon"]] + anchor_def.get("aliases", [])


# --------------------------------------------------------------------------- #
# Brazilian number parsing
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def parse_amount(s):
    """
    '6.886.721'      -> 6886721.0     (dot = thousands separator)
    '(106.057.746)'  -> -106057746.0  (parentheses = negative)
    Returns None if the token is not a parseable number.
    """
    if s is None:
        return None
    t = str(s).strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(".", "").replace(" ", "").replace(" ", "")
    t = t.replace(",", ".")          # rare decimal comma
    if not _NUM_RE.fullmatch(t):
        return None
    val = float(t)
    return -val if neg else val


# --------------------------------------------------------------------------- #
# Document number format (Brazilian "1.234,56" vs US "1,234.56")
# --------------------------------------------------------------------------- #
# A filing of a foreign-owned company may print US-style figures (Documento1 / Solae, an IFF
# company: "193,081"). parse_amount reads the comma as a DECIMAL comma -> 193.081, i.e. the whole
# document came out 1000x too small and sums mixing "596" with "16,091" broke. The format is a
# property of the DOCUMENT: detect it once (Stage 1), then rewrite US figures in the Brazilian
# notation so every downstream parser stays unchanged (the original string is kept as value_raw).
_BR_EVIDENCE = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?\)?$|\d,\d{1,2}\)?%?$")
_US_EVIDENCE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?$|\d\.\d{1,2}\)?%?$")
_US_GROUPED = re.compile(r"^([\(\[]?-?\s?)(\d{1,3}(?:,\d{3})+)(\.\d+)?([\)\]]?)$")
_US_DECIMAL = re.compile(r"^([\(\[]?-?\s?)(\d+)\.(\d{1,2})([\)\]]?)$")
US_FORMAT_MIN_EVIDENCE = 5


def detect_number_format(values):
    """'US' when US-style grouping clearly dominates the document's figures, else 'BR'."""
    br = us = 0
    for v in values:
        t = (v or "").strip()
        br += bool(_BR_EVIDENCE.search(t))
        us += bool(_US_EVIDENCE.search(t))
    return "US" if us >= US_FORMAT_MIN_EVIDENCE and us > 10 * br else "BR"


def to_br_number(s):
    """Rewrite one US-style figure in Brazilian notation ('193,081' -> '193.081',
    '(1,234.5)' -> '(1.234,5)', '7.95' -> '7,95'); anything else is returned unchanged."""
    if s is None:
        return s
    t = str(s).strip()
    m = _US_GROUPED.match(t)
    if m:
        dec = ("," + m.group(3)[1:]) if m.group(3) else ""
        return f"{m.group(1)}{m.group(2).replace(',', '.')}{dec}{m.group(4)}"
    m = _US_DECIMAL.match(t)
    if m:
        return f"{m.group(1)}{m.group(2)},{m.group(3)}{m.group(4)}"
    return s


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def yc(loc):
    return (loc["y_min"] + loc["y_max"]) / 2.0


def xc(loc):
    return (loc["x_min"] + loc["x_max"]) / 2.0


def x_overlap(a_min, a_max, b_min, b_max):
    return max(0.0, min(a_max, b_max) - max(a_min, b_min))


def x_bands(xcenters, gap=0.04):
    """Cluster x-centers into vertical bands (one band == one column)."""
    if not xcenters:
        return []
    xs = sorted(xcenters)
    bands = [[xs[0]]]
    for x in xs[1:]:
        if x - bands[-1][-1] <= gap:
            bands[-1].append(x)
        else:
            bands.append([x])
    return [sum(b) / len(b) for b in bands]
