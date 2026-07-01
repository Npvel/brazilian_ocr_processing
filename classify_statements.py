#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
classify_statements.py — Production classifier: primary statement vs. note vs. junk.

A four-layer pipeline (see docs/Primary_Statement_Classification.md):

  Layer 1  Negative filter      kill obvious junk (no amounts / too sparse)
  Layer 2  Detector             anchor-set COMPLETENESS per family (+ density, span)
  Layer 3  Disambiguator        primary vs. segment copy (columns, page order, repeats)
  Layer 4  Validator            accounting identity -> confidence boost / flag

Every verdict is transparent: each page carries `page_class`, `confidence`, and a
list of human-readable `reasons`. Nothing is deleted — this is a labelling layer.

Outputs a dict keyed by page number; intended to be embedded into the Stage-1
output by structure_ocr.py (top-level `page_classification`), and also runnable
standalone for diagnostics.
"""

from collections import defaultdict

from anchors_lib import (
    anchor_match, anchor_variants, parse_amount, x_bands, xc, yc, FUZZ_BACKEND,
)

LST_GROUPS = ["BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST",
              "INCOME_STATEMENT_LST", "CASH_FLOW_LST"]
AMOUNT_LABELS = ("AMOUNT_N", "AMOUNT_N-1")

# Canonical reading order — used by the disambiguator for sequence reasoning.
CANONICAL_ORDER = ["BALANCE", "INCOME", "CASH_FLOW"]

# Thresholds (page-level heuristics; column model proven on the reference corpus).
SPARSE_MAX_ROWS = 2
SPARSE_MAX_AMOUNTS = 3
PRIMARY_MAX_COLS = 4          # 2 or 4 columns = primary-like; more = segment note
Y_ROW_TOL = 0.012

# Positional safety-net (Layer 3b): if a real primary block exists (>=2 primary
# families) and a nearby page is a strong-but-incomplete candidate for the
# missing family, promote it as a flagged primary. Deliberately conservative —
# it requires partial anchor evidence, not just "has columns".
POSITIONAL_BLOCK_RADIUS = 4        # pages searched after the primary block
POSITIONAL_MIN_PRIMARY_FAMILIES = 2  # block must be solid before we infer
POSITIONAL_PROMOTE_CONFIDENCE = 0.55
# Content-based fallback (Layer 3b, second attempt): when a missing family has
# NO anchor evidence anywhere nearby (OCR mangled ALL its anchor lines), a page
# carrying a SUBSTANTIAL run of that family's line-items, adjacent to the block,
# with a primary-like column count, is promoted as a flagged primary. Requires
# real content (not one stray note line) and an even lower confidence than the
# anchor-backed promotion, since the evidence is purely positional + form-shape.
POSITIONAL_MIN_CONTENT_ROWS = 5
POSITIONAL_CONTENT_CONFIDENCE = 0.5

# Family -> the LST groups whose line-items constitute that family's content.
FAMILY_GROUPS = {
    "BALANCE": ("BALANCE_ASSETS_LST", "BALANCE_LIABILITIES_LST"),
    "INCOME": ("INCOME_STATEMENT_LST",),
    "CASH_FLOW": ("CASH_FLOW_LST",),
}


# --------------------------------------------------------------------------- #
# Page feature extraction
# --------------------------------------------------------------------------- #
def extract_page_features(data):
    """
    Returns page -> {names:[(group,raw,y)], amounts:[(y,x,period,value)],
                     ys:[...], n_rows, n_amounts, n_cols, vspan}.
    Only pages carrying LST line-items are included.
    """
    groups = {el.get("group_name"): el for el in data if el.get("group_name")}
    pages = defaultdict(lambda: {"names": [], "amounts": [], "ys": []})

    for g in LST_GROUPS:
        for sg in groups.get(g, {}).get("subgroups", []):
            pg = sg["page_nb"]
            for v in sg["values"]:
                loc = v["location"]
                pages[pg]["ys"].append(yc(loc))
                if v["label"] in AMOUNT_LABELS:
                    pages[pg]["amounts"].append((yc(loc), xc(loc), v["label"], v["value"]))
                elif v["label"] == g:
                    pages[pg]["names"].append((g, v["value"], yc(loc)))

    for pg, f in pages.items():
        f["n_rows"] = len(f["names"])
        f["n_amounts"] = len(f["amounts"])
        f["n_cols"] = len(x_bands([x for _, x, _, _ in f["amounts"]]))
        f["vspan"] = (max(f["ys"]) - min(f["ys"])) if f["ys"] else 0.0
    return dict(pages)


# --------------------------------------------------------------------------- #
# Anchor completeness per family
# --------------------------------------------------------------------------- #
def compute_anchor_hits(pages, cfg):
    """page -> {family: [hit anchor ids]} for required anchors only."""
    stop = set(cfg["stopwords"])
    benign = set(cfg["benign_insertions"])
    thr = cfg["match_threshold"]
    stmts = cfg["statements"]

    hits = defaultdict(dict)
    for pg, f in pages.items():
        names = f["names"]
        for fam, fdef in stmts.items():
            fam_hits = []
            for a in fdef.get("required_anchors", []):
                variants = anchor_variants(a)
                if any(anchor_match(variants, raw, stop, benign, thr)[0]
                       for _, raw, _ in names):
                    fam_hits.append(a["id"])
            if fam_hits:
                hits[pg][fam] = fam_hits
    return hits, stop, benign, thr


def family_complete(pg, fam, hits, cfg):
    """
    Completeness of a family's REQUIRED anchor set on page `pg`.
    BALANCE is two-page (Ativo | Passivo+PL): evaluated across pg, pg±1.
    """
    req_ids = {a["id"] for a in cfg["statements"][fam]["required_anchors"]}
    if not req_ids:
        return False
    if fam == "BALANCE":
        # this page must carry at least one balance side, set complete across neighbours
        if not hits.get(pg, {}).get("BALANCE"):
            return False
        have = set()
        for adj in (pg - 1, pg, pg + 1):
            have |= set(hits.get(adj, {}).get("BALANCE", []))
        return req_ids.issubset(have)
    return req_ids.issubset(set(hits.get(pg, {}).get(fam, [])))


# --------------------------------------------------------------------------- #
# Layer 4 — accounting identity
# --------------------------------------------------------------------------- #
def _anchor_row_amount_n(anchor_id, fdef, names, amounts, stop, benign, thr):
    """AMOUNT_N value on the same row (y) as the named anchor; None if absent."""
    target = next((a for a in fdef["required_anchors"] if a["id"] == anchor_id), None)
    if not target:
        return None
    variants = anchor_variants(target)
    for _, raw, y in names:
        if anchor_match(variants, raw, stop, benign, thr)[0]:
            same = [(abs(ay - y), parse_amount(val))
                    for ay, ax, per, val in amounts
                    if per == "AMOUNT_N" and abs(ay - y) <= Y_ROW_TOL]
            same = [(d, v) for d, v in same if v is not None]
            if same:
                return min(same)[1]
    return None


def check_balance_identity(pg, pages, cfg, stop, benign, thr):
    """
    Balance equation across the two balance pages: ativo == passivo+PL.
    The two totals usually live on different pages, so search pg and pg±1.
    Returns dict or None if not computable.
    """
    fdef = cfg["statements"]["BALANCE"]
    ativo = passpl = None
    for adj in (pg - 1, pg, pg + 1):
        if adj not in pages:
            continue
        f = pages[adj]
        if ativo is None:
            ativo = _anchor_row_amount_n("total_ativo", fdef, f["names"], f["amounts"],
                                         stop, benign, thr)
        if passpl is None:
            passpl = _anchor_row_amount_n("total_passivo_pl", fdef, f["names"], f["amounts"],
                                          stop, benign, thr)
    if ativo is None or passpl is None:
        return None
    tol = max(abs(ativo), 1.0) * fdef["identity"]["tolerance_pct"] / 100.0
    return {"ativo": ativo, "passivo_pl": passpl, "balances": abs(ativo - passpl) <= tol}


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def classify(data, cfg):
    pages = extract_page_features(data)
    hits, stop, benign, thr = compute_anchor_hits(pages, cfg)

    # First pass: per-page complete families + raw verdict, collect reasons.
    prelim = {}
    for pg, f in pages.items():
        reasons = []
        complete_fams = [fam for fam in cfg["statements"]
                         if family_complete(pg, fam, hits, cfg)]

        # ---- Layer 1: negative filter ----------------------------------- #
        if f["n_amounts"] == 0:
            prelim[pg] = _verdict("JUNK_no_amounts", 0.95,
                                  ["no amounts on page"], complete_fams, f)
            continue
        if f["n_rows"] <= SPARSE_MAX_ROWS and f["n_amounts"] <= SPARSE_MAX_AMOUNTS:
            prelim[pg] = _verdict("JUNK_sparse", 0.85,
                                  [f"sparse page (rows={f['n_rows']}, amounts={f['n_amounts']})"],
                                  complete_fams, f)
            continue

        # ---- Layer 2: detector (completeness) --------------------------- #
        if not complete_fams:
            partial = {fam: ah for fam, ah in hits.get(pg, {}).items()}
            r = ["no complete anchor set"]
            if partial:
                r.append("partial anchors: " +
                         ", ".join(f"{fam}={ids}" for fam, ids in partial.items()))
            prelim[pg] = _verdict("NOTE", 0.7, r, complete_fams, f)
            continue

        fam = _dominant_family(complete_fams, f, hits, pg)
        reasons.append(f"complete anchor set: {fam} ({hits.get(pg, {}).get(fam) or 'balance-2pg'})")
        reasons.append(f"density rows={f['n_rows']}, amounts={f['n_amounts']}, vspan={f['vspan']:.2f}")

        # ---- Layer 3: disambiguator (columns) --------------------------- #
        if f["n_cols"] > PRIMARY_MAX_COLS:
            reasons.append(f"{f['n_cols']} columns > {PRIMARY_MAX_COLS} -> segment/wide note")
            prelim[pg] = _verdict(f"NOTE_segment:{fam}", 0.75, reasons, complete_fams, f)
            continue
        reasons.append(f"{f['n_cols']} columns (primary-like)")

        prelim[pg] = _verdict(f"PRIMARY:{fam}", 0.8, reasons, complete_fams, f,
                              family=fam)

    # ---- Layer 3 (cross-page): order + repeated-set disambiguation ----- #
    _disambiguate_repeats(prelim, pages)

    # ---- Layer 3b: positional safety-net (sequence-based recovery) ----- #
    _positional_safety_net(prelim, pages, hits, cfg)

    # ---- Layer 4: validator (identity) -------------------------------- #
    for pg, v in prelim.items():
        if not v["page_class"].startswith("PRIMARY"):
            continue
        if v.get("family") == "BALANCE":
            idres = check_balance_identity(pg, pages, cfg, stop, benign, thr)
            if idres is not None:
                v["identity"] = idres
                if idres["balances"]:
                    v["confidence"] = min(1.0, v["confidence"] + 0.15)
                    v["reasons"].append("balance identity holds (ativo == passivo+PL)")
                else:
                    v["reasons"].append("balance identity OFF — flagged (OCR digit?)")
                    v["needs_review"] = True

    return prelim


def _verdict(page_class, confidence, reasons, complete_fams, f, family=None):
    v = {
        "page_class": page_class,
        "confidence": round(confidence, 2),
        "reasons": list(reasons),
        "complete_families": complete_fams,
        "n_rows": f["n_rows"],
        "n_amounts": f["n_amounts"],
        "n_cols": f["n_cols"],
        "vspan": round(f["vspan"], 3),
    }
    if family:
        v["family"] = family
    return v


def _dominant_family(complete_fams, f, hits, pg):
    """If several families complete on one page, pick by canonical priority."""
    for fam in CANONICAL_ORDER:
        if fam in complete_fams:
            return fam
    return complete_fams[0]


def _disambiguate_repeats(prelim, pages):
    """
    Among PRIMARY pages of the same family, the EARLIEST in page order is the
    primary; later complete copies are demoted to NOTE_segment UNLESS they look
    like a legitimate Individual-vs-Consolidado split (different column count
    -> kept primary, annotated). Mirrors the corpus case: income on 22 (4-col)
    vs 46/47 (2-col).
    """
    by_family = defaultdict(list)
    for pg, v in prelim.items():
        if v["page_class"].startswith("PRIMARY") and v.get("family"):
            by_family[v["family"]].append(pg)

    for fam, pgs in by_family.items():
        pgs.sort()
        first = pgs[0]
        first_cols = prelim[first]["n_cols"]
        prelim[first]["reasons"].append("first occurrence in canonical order -> primary")

        # BALANCE spans two facing pages (Ativo | Passivo+PL). A page adjacent to
        # an already-primary balance page is the SAME statement's continuation,
        # not a repeat — keep both primary.
        if fam == "BALANCE":
            primary_pages = {first}
            for pg in pgs[1:]:
                if any(abs(pg - pp) == 1 for pp in primary_pages):
                    prelim[pg]["reasons"].append(
                        "adjacent to primary balance page -> same statement (continuation)")
                    primary_pages.add(pg)
                else:
                    prelim[pg]["page_class"] = f"NOTE_segment:{fam}"
                    prelim[pg]["reasons"].append(
                        "non-adjacent repeat of BALANCE -> demoted to note")
                    prelim[pg]["confidence"] = 0.65
            continue

        for pg in pgs[1:]:
            cols = prelim[pg]["n_cols"]
            if cols != first_cols:
                # different column layout -> likely separate Individual/Consolidado view
                prelim[pg]["reasons"].append(
                    f"repeat of {fam} with different column count "
                    f"({cols} vs {first_cols}) -> kept primary (Individual/Consolidado split?)")
                prelim[pg]["confidence"] = round(min(prelim[pg]["confidence"], 0.7), 2)
                prelim[pg]["needs_review"] = True
            else:
                prelim[pg]["page_class"] = f"NOTE_segment:{fam}"
                prelim[pg]["reasons"].append(
                    f"later repeat of {fam} (same {cols}-col layout) -> demoted to note")
                prelim[pg]["confidence"] = 0.65


def _positional_safety_net(prelim, pages, hits, cfg):
    """
    Layer 3b — sequence-based recovery (SAFETY-NET, not a primary detector).

    Rationale: primary statements appear as a block in canonical order
    (Balance -> Income -> Cash-Flow). If a solid primary block exists but one
    family is missing — typically because OCR mangled one of its anchor lines so
    completeness (Layer 2) just barely failed — a nearby page that is a
    STRONG-BUT-INCOMPLETE candidate is very likely that missing statement.

    Strict guards keep this conservative (no "next page with columns" guessing):
      * a real block must exist: >= POSITIONAL_MIN_PRIMARY_FAMILIES primary families;
      * the family must not already have a primary page;
      * the candidate must carry PARTIAL anchor evidence (>=1 required anchor) —
        never promoted on column layout alone;
      * it must sit within POSITIONAL_BLOCK_RADIUS pages after the block and have
        a primary-like column count (<= PRIMARY_MAX_COLS);
      * promoted pages are flagged needs_review with low confidence.
    """
    primary_families = {v["family"] for v in prelim.values()
                        if v["page_class"].startswith("PRIMARY") and v.get("family")}
    if len(primary_families) < POSITIONAL_MIN_PRIMARY_FAMILIES:
        return  # no trustworthy block -> do not infer anything

    primary_pages = [pg for pg, v in prelim.items()
                     if v["page_class"].startswith("PRIMARY")]
    block_end = max(primary_pages)

    for fam in CANONICAL_ORDER:
        if fam in primary_families:
            continue  # this family already has a primary page
        req_ids = {a["id"] for a in cfg["statements"][fam].get("required_anchors", [])}
        if not req_ids:
            continue

        # Candidates: pages just after the block, currently NOTE-ish, with at
        # least one of the family's required anchors and a primary-like layout.
        best = None
        for pg in sorted(pages):
            if pg <= block_end or pg > block_end + POSITIONAL_BLOCK_RADIUS:
                continue
            v = prelim.get(pg)
            if v is None or v["page_class"].startswith("PRIMARY"):
                continue
            partial = set(hits.get(pg, {}).get(fam, []))
            if not partial:                       # require real anchor evidence
                continue
            if v["n_cols"] == 0 or v["n_cols"] > PRIMARY_MAX_COLS:
                continue
            score = (len(partial), -pg)           # most anchors, then closest
            if best is None or score > best[0]:
                best = (score, pg, partial, req_ids)

        if best is not None:
            _, pg, partial, req_ids = best
            v = prelim[pg]
            v["page_class"] = f"PRIMARY:{fam}"
            v["family"] = fam
            v["confidence"] = POSITIONAL_PROMOTE_CONFIDENCE
            v["needs_review"] = True
            v["reasons"].append(
                f"positional safety-net: partial {fam} anchors {sorted(partial)} "
                f"({len(partial)}/{len(req_ids)}) within {POSITIONAL_BLOCK_RADIUS}p of "
                f"primary block (pages<= {block_end}) -> promoted as flagged primary")
            primary_families.add(fam)
            continue

        # Second attempt — CONTENT-based, when the family has NO anchor evidence
        # at all nearby (OCR mangled every anchor line). Promote the adjacent page
        # carrying the largest substantial run of this family's line-items, with a
        # primary-like column count. Strictly weaker than the anchor path: gated on
        # a real content run and tagged at the lowest confidence.
        cbest = None
        for pg in sorted(pages):
            if pg <= block_end or pg > block_end + POSITIONAL_BLOCK_RADIUS:
                continue
            v = prelim.get(pg)
            if v is None or v["page_class"].startswith("PRIMARY"):
                continue
            if v["n_cols"] == 0 or v["n_cols"] > PRIMARY_MAX_COLS:
                continue
            content = sum(1 for g, _, _ in pages[pg]["names"]
                          if g in FAMILY_GROUPS[fam])
            if content < POSITIONAL_MIN_CONTENT_ROWS:
                continue
            score = (content, -pg)
            if cbest is None or score > cbest[0]:
                cbest = (score, pg, content)

        if cbest is not None:
            _, pg, content = cbest
            v = prelim[pg]
            v["page_class"] = f"PRIMARY:{fam}"
            v["family"] = fam
            v["confidence"] = POSITIONAL_CONTENT_CONFIDENCE
            v["needs_review"] = True
            v["reasons"].append(
                f"positional safety-net (content): {content} {fam} line-items, "
                f"primary-like columns, within {POSITIONAL_BLOCK_RADIUS}p of primary "
                f"block (pages<= {block_end}), no anchor evidence -> flagged primary")
            primary_families.add(fam)


# --------------------------------------------------------------------------- #
# Summary helper
# --------------------------------------------------------------------------- #
def summarize(classification):
    from collections import Counter
    dist = Counter(v["page_class"].split(":")[0] for v in classification.values())
    primaries = {fam: [] for fam in CANONICAL_ORDER}
    for pg, v in classification.items():
        if v["page_class"].startswith("PRIMARY") and v.get("family"):
            primaries[v["family"]].append(pg)
    for fam in primaries:
        primaries[fam].sort()
    return {
        "fuzzy_backend": FUZZ_BACKEND,
        "pages_examined": len(classification),
        "class_distribution": dict(dist),
        "primary_pages": {f: p for f, p in primaries.items() if p},
    }
