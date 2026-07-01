#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
doc_type.py — Single source of truth for the document-type decision.

The shape of a Brazilian financial filing dictates how every later stage treats
it (which columns to keep, whether page-classification is authoritative, whether
row codes can drive SBS labelling). That decision was historically made inside
the Excel exporter; it now lives here so Stage 1 can compute it ONCE, stamp it on
the output (`document_type`), and every downstream stage reads the SAME value.

Types (detection order — first match wins, most specific first):

    SERASA     provider tag 'SERASA EXPERIAN' present   (Serasa Experian dump)
    ITR        any row carries a CODE_VALUE              (hierarchical CVM codes)
    UPPERCASE  >= 50% of labels are ALL-CAPS             (generic provider dump)
    DFP_4COL   a column carries Consolidado/Controladora (annual, 4 columns)
    TWO_COL    none of the above                         (single-perspective)

Why SERASA precedes UPPERCASE: a Serasa report is also all-caps, so the generic
UPPERCASE rule would catch it — but the provider tag is an exact, robust signal
whereas ">= 50% caps" is incidental. Filter behaviour for the two is identical
today (take all columns, ignore page_class); naming it SERASA lets the
Serasa-specific labelling fixes target it precisely.

Filter policy per type (consumed by the Excel exporter and any later filter):
    consolidado_only  keep only Consolidado columns — ONLY where such a split
                      exists (ITR, DFP_4COL). On single-perspective docs there is
                      nothing to filter, so all columns are taken.
    primary_only      restrict to page_class PRIMARY:* (DFP_4COL, TWO_COL). On
                      ITR/UPPERCASE/SERASA the page classifier under-detects, so
                      page_class is ignored and rows are kept on their own merit.
"""

from anchors_lib import normalize

# Document-level provider/source tags (normalized). These appear in the
# ACCOUNT_TYPE group on a cover page and identify the data SOURCE, unlike the
# per-column Controladora/Consolidado headers that also live in that group.
PROVIDER_TAGS_NORMALIZED = frozenset({"serasa experian"})

# Document types this module can return. Exposed for callers that branch on type.
DOC_TYPES = ("SERASA", "ITR", "UPPERCASE", "DFP_4COL", "TWO_COL")


def is_uppercase_label(label):
    """True if every cased letter in the label is uppercase (an all-caps line)."""
    letters = [c for c in label if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def provider_tag(metadata):
    """The normalized provider tag from metadata.ACCOUNT_TYPE, or '' if none."""
    value = ((metadata or {}).get("ACCOUNT_TYPE") or {}).get("value") or ""
    return normalize(value)


def detect_doc_type(statements, metadata=None):
    """Classify the document. See the module docstring for type definitions and
    detection order. `metadata` carries the provider tag (Stage-1 output); when
    omitted, only the statement-shape signals (code / caps / account_type) apply,
    so a SERASA doc would fall through to UPPERCASE — behaviourally identical.
    """
    if provider_tag(metadata) in PROVIDER_TAGS_NORMALIZED:
        return "SERASA"

    has_code = False
    n_label = n_upper = 0
    has_consolidado = False
    for rows in statements.values():
        for r in rows:
            if r.get("code"):
                has_code = True
            label = r.get("label")
            if label:
                n_label += 1
                if is_uppercase_label(label):
                    n_upper += 1
            for c in r.get("columns", []):
                at = (c.get("account_type") or "").lower()
                if at.startswith("consolidad") or at.startswith("controladora"):
                    has_consolidado = True

    if has_code:
        return "ITR"
    if n_label and n_upper / n_label >= 0.5:
        return "UPPERCASE"
    if has_consolidado:
        return "DFP_4COL"
    return "TWO_COL"


def consolidado_only(doc_type):
    """Whether to keep only Consolidado columns for this doc type."""
    return doc_type in ("ITR", "DFP_4COL")


def primary_only(doc_type):
    """Whether to restrict to page_class PRIMARY:* for this doc type."""
    return doc_type in ("DFP_4COL", "TWO_COL")
