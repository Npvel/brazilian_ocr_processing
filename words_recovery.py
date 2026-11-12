# -*- coding: utf-8 -*-
"""
words_recovery.py — Stage-1 add-on, ON by default (user decision 2026-11-12): recover what the reciTAL
extraction lost, from the OCR words layer (`GET /files/{id}/words/`: every recognized word of every
page + its box; reciTAL confirmed `/production/files/{id}/words/` on 2026-11-05).

Pluggable by design (user decision 2026-11-04). The layer can still be missing (fetch failed, already
deleted after validation), so nothing in the pipeline depends on this module:
    * structure_ocr.process(data)              -> this module is never called; output unchanged
    * structure_ocr.process(data, words=words) -> recover(...) runs once, at the end of Stage 1
    * delete this file                         -> structure_ocr's guarded import turns it off
Every recovered item carries its provenance, so it can be audited, styled or filtered out later:
    cell  `source: "ocr_words"`        row  `source: "ocr_words"`        label  `label_source: "ocr_words"`

Why it is needed: the extraction (values + groups) misses ~8% of the figures printed in the
statement tables (1,024 of 12,568 on the 22-PDF corpus, up to 19% of a page), while the words
layer has all of them, with boxes identical to the extracted value boxes. Three kinds of loss:
  1. CELL  — a row is there but one of its figures is not (Raízen p26: the Consolidado closing
             cash 21.721.393 / 14.819.906; ALUBAR p10: the Consolidado column on ~17 lines).
  2. LINE  — the whole printed line is missing (Guabi p1: the four liability subtotals
             177.359 / 20.973 / 192.927 / 391.259 -> the balance did not close).
  3. LABEL — the figures are there, the name is not (RECONTEC p13: "Lucro líquido antes do
             imposto de renda e contribuição social" 63.964 / 65.095 came out unnamed).

What it NEVER does: change an existing figure, period or name; add a figure outside the page's
column grid (every recovered figure sits in a column the extraction itself populated — >= 3 figures,
or a column of a Stage-1 header zone, printed in the table header, whatever its count — and takes
that column's period); put a second figure into a column a row already fills; add a figure at
the spot of an extracted one. The only structural edit: a figure-less name line absorbed into
the line it continues (the printed name wrapped — same rule as Stage-1 step 4b), its text kept
in `label_parts`.
"""
import re
import statistics
from collections import Counter, defaultdict

# Numbers as printed in BR statements: 1.234 / (1.234.567) / 1.234,56 / 12 / (12) / -80.218.
_BR_AMOUNT = re.compile(r"^[\(\[]?[-−–]?\s?(?:\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d{1,3}(?:,\d{1,2})?)[\)\]]?$")
# US-style documents (number_format == "US": 193,081 / (1,234.5)); build_output rewrites them.
_US_AMOUNT = re.compile(r"^[\(\[]?[-−–]?\s?(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,3}(?:\.\d{1,2})?)[\)\]]?$")
_CODE = re.compile(r"^\d{1,2}(?:\.\d{1,3})+$")          # CVM/B3 row code: 1.01 / 2.02.01 / 6.01.02.01

COLUMN_GAP = 0.02          # x_max gap between two right-aligned columns (= Step 2c PERIOD_GRID_BAND_GAP)
COLUMN_MIN_CELLS = 3       # a column = >= 3 extracted figures with the same right edge
COLUMN_MIN_PURITY = 0.9    # the column's extracted figures agree on ONE period
COLUMN_MARGIN = 0.006      # right-edge jitter of a right-aligned figure around its column
SMALL_COLUMN_SHARE = 0.3   # a column of small figures (holding company: 8 / 6 / 14) admits 1-digit ones
ZONE_MARGIN_PITCHES = 1.5  # recovered figures stay inside the page's table zone (rows' y-extent)
LABEL_GAP = 0.03           # a wider horizontal gap separates label | note-ref | code columns
LABEL_MIN_LETTERS = 3      # recovered label must carry >= 3 letters
WRAP_MAX_PITCHES = 1.6     # a wrapped name continues on the adjacent visual line
LINE_MIN_DIGITS = 3        # a lost line with ONE figure needs a real amount, not '5' of "Acima de 5 anos"


def _yc(b):
    return (b["y_min"] + b["y_max"]) / 2.0


def _xc(b):
    return (b["x_min"] + b["x_max"]) / 2.0


def _inside(px, py, b):
    return b["x_min"] <= px <= b["x_max"] and b["y_min"] <= py <= b["y_max"]


def _same_spot(a, b):
    """The two boxes are the same printed item: the centre of one lies inside the other. A mere
    touch is NOT enough — boxes of adjacent lines overlap by a hair on dense pages (1Q26 p26:
    the name box of line k reached the words of line k+1)."""
    return _inside(_xc(a), _yc(a), b) or _inside(_xc(b), _yc(b), a)


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return {"x_min": min(a["x_min"], b["x_min"]), "x_max": max(a["x_max"], b["x_max"]),
            "y_min": min(a["y_min"], b["y_min"]), "y_max": max(a["y_max"], b["y_max"])}


def _digits(w):
    return len(re.sub(r"\D", "", w or ""))


def _balanced(w):
    """Both parentheses/brackets or none: '(9' of a '(9 meses)' header is not a figure."""
    return (w[:1] in "([") == (w[-1:] in ")]")


def is_figure(word, number_format="BR"):
    w = (word or "").strip()
    rx = _US_AMOUNT if number_format == "US" else _BR_AMOUNT
    return bool(w) and bool(rx.match(w)) and _balanced(w)


def _note_like(values):
    """A column of note references ('21', '8', '22'), not of amounts: 1-2 digit bare integers."""
    vals = [v.strip() for v in values]
    return bool(vals) and sum(1 for v in vals if re.fullmatch(r"\d{1,2}", v)) >= 0.8 * len(vals)


def _is_continuation(text):
    """A name fragment that continues the line above: lowercase start ('da contribuição social')
    or a bare parenthetical ('(Notas 11.2 e 17.4)')."""
    t = text.strip()
    first = next((ch for ch in t if ch.isalpha()), "")
    return first.islower() or t.startswith("(")


# --------------------------------------------------------------------------- page model
class _Page:
    """Geometry of one page: rows, panels, column grid per panel, tokens."""

    def __init__(self, S, page, rows, tokens, tol, panels, pitch, number_format, zones=None):
        self.S, self.page, self.rows, self.tol, self.panels, self.pitch = S, page, rows, tol, panels, pitch
        self.tokens = [t for t in tokens if t.get("coordinates") and (t.get("word") or "").strip()]
        self.number_format = number_format
        self.zones = zones
        self.figures = [a for r in rows for a in r["amounts"]]
        self.name_boxes = []
        for r in rows:
            if r.get("label") and r.get("name_box") and r.get("name_x_min") is not None:
                lo, hi = r["name_box"]
                self.name_boxes.append({"x_min": r["name_x_min"], "x_max": r.get("name_x_max") or r["name_x_min"],
                                        "y_min": lo, "y_max": hi})
        ys = [S.row_ref_y(r) for r in rows if S.row_ref_y(r) is not None]
        m = ZONE_MARGIN_PITCHES * pitch
        self.zone = (min(ys) - m, max(ys) + m) if ys else None
        self.columns = self._grid()

    def panel(self, x):
        return self.S.panel_of(self.page, x, self.panels)

    def _grid(self):
        """(panel) -> [column {lo, hi, period, small}] from the extracted figures' right edges, plus the
        columns of the Stage-1 header zones (step 2c'): a column the table header prints is a column
        even when the extraction left it almost empty (Cibra p7, audit 2026-11-05 M3: the Consolidado
        2024 column of the liabilities held 1-2 extracted figures -> 'off grid', never recovered); a
        zone column takes the zone's period."""
        by_panel = defaultdict(list)
        for a in self.figures:
            if is_figure(a["value"], self.number_format):     # not 'I' / '-' / '(9' / 'meses)'
                by_panel[self.panel(_xc(a["loc"]))].append(a)
        grid = {}
        for pa, figs in by_panel.items():
            figs.sort(key=lambda a: a["loc"]["x_max"])
            bands = []
            for a in figs:
                x = a["loc"]["x_max"]
                if bands and x - bands[-1][-1]["loc"]["x_max"] <= COLUMN_GAP:
                    bands[-1].append(a)
                else:
                    bands.append([a])
            cols = []
            for b in bands:
                if len(b) < COLUMN_MIN_CELLS or _note_like([a["value"] for a in b]):
                    continue
                cnt = Counter(a["period"] for a in b)
                per, n = cnt.most_common(1)[0]
                cols.append({"lo": b[0]["loc"]["x_max"], "hi": b[-1]["loc"]["x_max"],
                             "period": per if n / len(b) >= COLUMN_MIN_PURITY else None,
                             "small": sum(1 for a in b if _digits(a["value"]) <= 2) >= SMALL_COLUMN_SHARE * len(b)})
            grid[pa] = cols
        for (pg, pa), rec in ((self.zones or {}).get("table") or {}).items():
            if pg != self.page:
                continue
            cols = grid.setdefault(pa, [])
            for z in rec["zones"]:
                hit = [c for c in cols if c["lo"] - COLUMN_MARGIN <= z["xmax"] <= c["hi"] + COLUMN_MARGIN]
                for c in hit:
                    c["period"] = z["period"]
                if not hit:
                    cols.append({"lo": z["lo"], "hi": z["hi"], "period": z["period"], "small": False, "zone": True})
            cols.sort(key=lambda c: c["lo"])
        return grid

    def grid_of(self, pa):
        return self.columns.get(pa, [])

    def column_of(self, x_max, pa):
        for i, c in enumerate(self.grid_of(pa)):
            if c["lo"] - COLUMN_MARGIN <= x_max <= c["hi"] + COLUMN_MARGIN:
                return i
        return None

    def known(self, box):
        """The extraction already has a figure at this spot -> never add it again."""
        return any(_same_spot(box, a["loc"]) for a in self.figures)

    def in_name(self, box):
        return any(_same_spot(box, nb) for nb in self.name_boxes)

    def panel_x_range(self, pa):
        edges = self.panels.get(self.page)
        if not edges:
            return 0.0, 1.01
        lo = edges[pa] - 0.01
        hi = edges[pa + 1] - 0.01 if pa + 1 < len(edges) else 1.01
        return lo, hi

    def free_text(self, y, x_lo, x_hi):
        """Label words on one visual line between x_lo and x_hi: not an extracted figure, not part
        of an extracted name. -> (text, code, box) or (None, None, None)."""
        t = self.tol(self.page)
        toks = sorted((tk for tk in self.tokens
                       if abs(_yc(tk["coordinates"]) - y) <= t
                       and tk["coordinates"]["x_min"] >= x_lo and tk["coordinates"]["x_max"] <= x_hi
                       and not self.in_name(tk["coordinates"]) and not self.known(tk["coordinates"])),
                      key=lambda tk: tk["coordinates"]["x_min"])
        if not toks:
            return None, None, None
        clusters = [[toks[0]]]
        for tk in toks[1:]:
            if tk["coordinates"]["x_min"] - clusters[-1][-1]["coordinates"]["x_max"] > LABEL_GAP:
                clusters.append([tk])
            else:
                clusters[-1].append(tk)

        def letters(cl):
            return sum(ch.isalpha() for tk in cl for ch in tk["word"])
        best = max(clusters, key=letters)
        if letters(best) < LABEL_MIN_LETTERS:
            return None, None, None
        code = None
        for cl in clusters:
            if cl is best:
                break
            if len(cl) == 1 and _CODE.match(cl[0]["word"].strip()):
                code = cl[0]["word"].strip()
        box = None
        for tk in best:
            box = _union(box, tk["coordinates"])
        return " ".join(tk["word"].strip() for tk in best), code, box

    def _adjacent_text(self, y, direction, x_lo, x_hi):
        """Free text on the nearest visual line above (direction -1) / below (+1), within reach."""
        t, lim = self.tol(self.page), WRAP_MAX_PITCHES * self.pitch
        ys = [_yc(tk["coordinates"]) for tk in self.tokens
              if 0.6 * t < direction * (_yc(tk["coordinates"]) - y) <= lim
              and tk["coordinates"]["x_min"] >= x_lo and tk["coordinates"]["x_max"] <= x_hi]
        if not ys:
            return None, None, None
        yy = min(ys) if direction > 0 else max(ys)
        return self.free_text(yy, x_lo, x_hi)

    def line_label(self, y, pa, x_hi, name_only):
        """Label of a figures line = free text left of its first figure. A wrapped printed name is
        completed: a continuation fragment takes the line above (a figure-less name row of the
        extraction, absorbed, or free text); a name ending with a connector takes the lowercase
        line below. -> None or {text, code, box, parts, absorbed}."""
        x_lo, _ = self.panel_x_range(pa)
        text, code, box = self.free_text(y, x_lo, x_hi)
        if text is None:
            return None
        parts, absorbed = [text], []
        lim = WRAP_MAX_PITCHES * self.pitch
        if _is_continuation(text):
            above = [r for r in name_only if 0 < y - r["name_y"] <= lim]
            if above:
                a = max(above, key=lambda r: r["name_y"])
                parts.insert(0, a["label"])
                absorbed.append(a)
                code = code or a.get("code")
            else:
                head, code2, box2 = self._adjacent_text(y, -1, x_lo, x_hi)
                if head is not None:
                    parts.insert(0, head)
                    code, box = code or code2, _union(box, box2)
        if self.S._WRAP_CONNECTOR.search(parts[-1]):
            below = [r for r in name_only if 0 < r["name_y"] - y <= lim and r not in absorbed
                     and _is_continuation(r["label"])]
            if below:
                b = min(below, key=lambda r: r["name_y"])
                parts.append(b["label"])
                absorbed.append(b)
            else:
                tail, _c, box2 = self._adjacent_text(y, +1, x_lo, x_hi)
                if tail is not None and _is_continuation(tail):
                    parts.append(tail)
                    box = _union(box, box2)
        return {"text": " ".join(p.strip() for p in parts), "code": code, "box": box,
                "parts": parts if len(parts) > 1 else None, "absorbed": absorbed}


# --------------------------------------------------------------------------- recovery
def _cols_match(a, b):
    return a["lo"] - COLUMN_MARGIN <= b["hi"] + COLUMN_MARGIN and b["lo"] - COLUMN_MARGIN <= a["hi"] + COLUMN_MARGIN


def _borrow_columns(P, Q):
    """A statement continued on the next page with one of its columns lost there ENTIRELY has no
    column of its own on that page to recover into (serasa 92644954 p4: the DRE tail kept only the
    2023 column, the 2024 one — net income 3.213 — is in the words only). Take the column from the
    adjacent page Q when both pages print the same statement in the same layout: same statement
    rows, and EVERY column of P is a column of Q at the same x with the same period (P's grid is
    Q's grid minus the lost column). Note tables of neighbouring pages that merely share one
    column x are never mixed (their periods differ: book / fair value, cost / depreciation).
    Returns the number of columns borrowed."""
    if not ({r["statement"] for r in P.rows if r.get("label")} & {r["statement"] for r in Q.rows if r.get("label")}):
        return 0
    n = 0
    for pa, qcols in Q.columns.items():
        pcols = P.columns.get(pa, [])
        if not pcols or not all(p["period"] and any(_cols_match(p, q) and q["period"] == p["period"]
                                                    for q in qcols)
                                for p in pcols):
            continue
        for q in qcols:
            if q.get("borrowed") or q["period"] is None or any(_cols_match(p, q) for p in pcols):
                continue
            pcols.append(dict(q, borrowed=True))
            n += 1
        P.columns[pa] = pcols
    return n


def _apply_label(row, lab, y, c):
    row["label"] = lab["text"]
    row["label_source"] = "ocr_words"
    box = lab["box"]
    for a in lab["absorbed"]:
        if a.get("name_box") and a.get("name_x_min") is not None:
            lo, hi = a["name_box"]
            box = _union(box, {"x_min": a["name_x_min"], "x_max": a.get("name_x_max") or a["name_x_min"],
                               "y_min": lo, "y_max": hi})
    row["name_x_min"], row["name_x_max"] = box["x_min"], box["x_max"]
    row["name_y"] = y
    row["name_box"] = (box["y_min"], box["y_max"])
    if lab["code"] and not row.get("code"):
        row["code"] = lab["code"]
    if lab["parts"]:
        row["label_parts"] = lab["parts"]
        row["label_wrapped"] = "W"
        c["labels_wrapped"] += 1


def recover(rows, words, report, S, number_format="BR", zones=None):
    """
    Recover lost cells, lost lines and lost labels of the Stage-1 `rows` (in place) from the
    OCR words layer `words` (list per page of {word, coordinates}). `S` = the structure_ocr
    module (geometry helpers: tolerance, panels, statement routing). `zones` = Stage-1 header zones
    (step 2c', optional): their columns join the page grid. Counters -> report["words"].
    """
    c = report.setdefault("words", Counter())
    if not words:
        c["skipped_no_words"] += 1
        return
    tol = S.make_row_tol(rows)
    panels = S.build_panels(rows, tol)
    pitch, doc_pitch = S.page_line_pitch(rows)
    by_page = defaultdict(list)
    for r in rows:
        if r.get("y") is not None and r.get("page_nb") is not None:
            by_page[r["page_nb"]].append(r)

    pages = {}
    for page in sorted(by_page):
        if 1 <= page <= len(words):
            P = _Page(S, page, by_page[page], words[page - 1] or [], tol, panels,
                      pitch.get(page, doc_pitch), number_format, zones)
            if P.zone is not None:
                pages[page] = P
    for page, P in pages.items():
        for nb in (page - 1, page + 1):
            if nb in pages:
                c["columns_borrowed"] += _borrow_columns(P, pages[nb])

    new_rows, absorbed_ids = [], set()
    for page, P in pages.items():
        prow = by_page[page]
        t = tol(page)

        def name_only_rows(pa):
            return [r for r in prow if r.get("label") and not r["amounts"] and r.get("name_y") is not None
                    and id(r) not in absorbed_ids and S.row_panel(r, panels) == pa]

        # 1) every figure word of the page grid that the extraction does not have
        lost = []
        for tk in P.tokens:
            b = tk["coordinates"]
            w = tk["word"].strip()
            if not is_figure(w, number_format) or P.known(b) or P.in_name(b):
                continue
            if not (P.zone[0] <= _yc(b) <= P.zone[1]):
                continue
            pa = P.panel(_xc(b))
            col = P.column_of(b["x_max"], pa)
            if col is None:
                c["skip_off_grid"] += 1
                continue
            column = P.grid_of(pa)[col]
            if column["period"] is None:
                c["skip_column_without_period"] += 1
                continue
            if not column["small"] and (_digits(w) == 1 or re.fullmatch(r"\d{2}", w)):
                # in a column of large figures: a dash the OCR read as '1', a footnote marker
                # '(2)', a bare date / note number ('31', '12'). '(27)' / '-45' are kept.
                c["skip_small_in_large_column"] += 1
                continue
            lost.append({"value": w, "period": column["period"], "loc": dict(b), "src": "ocr_words",
                         "_panel": pa, "_col": col})

        # 2) CELL: the figure's visual line carries a row of its panel -> into that row
        orphans = []
        for f in lost:
            line = [r for r in prow if S.row_ref_y(r) is not None and not r.get("non_figure_line")
                    and abs(S.row_ref_y(r) - _yc(f["loc"])) <= t
                    and S.row_panel(r, panels) == f["_panel"]]
            if not line:
                orphans.append(f)
                continue
            r = min(line, key=lambda r: (abs(S.row_ref_y(r) - _yc(f["loc"])), r.get("label") is None))
            if any(P.column_of(a["loc"]["x_max"], f["_panel"]) == f["_col"] for a in r["amounts"]):
                c["skip_column_filled"] += 1
                continue
            r["amounts"].append({k: v for k, v in f.items() if not k.startswith("_")})
            c["cells_added"] += 1

        # 3) LINE: figures of a line the extraction lost entirely -> one new row per line
        by_panel = defaultdict(list)
        for f in orphans:
            by_panel[f["_panel"]].append(f)
        for pa, figs in by_panel.items():
            figs.sort(key=lambda f: _yc(f["loc"]))
            clusters = []
            for f in figs:
                last = clusters[-1] if clusters else None
                if (last is not None and _yc(f["loc"]) - _yc(last[0]["loc"]) <= t
                        and all(g["_col"] != f["_col"] for g in last)):
                    last.append(f)
                else:
                    clusters.append([f])
            for cl in clusters:
                cy = statistics.median(_yc(f["loc"]) for f in cl)
                lab = P.line_label(cy, pa, min(f["loc"]["x_min"] for f in cl) - 0.003, name_only_rows(pa))
                if len(cl) < 2 and (lab is None or _digits(cl[0]["value"]) < LINE_MIN_DIGITS):
                    c["skip_lone_figure"] += 1          # one figure: too little evidence of a line
                    continue
                stmt = S._statement_for_orphan(page, cl[0]["loc"], panels, prow, pitch.get(page, doc_pitch))
                if stmt is None:
                    c["skip_no_statement"] += 1
                    continue
                row = {"statement": stmt, "page_nb": page, "label": None, "y": cy,
                       "name_x_max": None, "name_x_min": None, "name_y": None, "name_box": None,
                       "amounts": [{k: v for k, v in f.items() if not k.startswith("_")} for f in cl],
                       "code": None, "source": "ocr_words"}
                if lab is not None:
                    _apply_label(row, lab, cy, c)
                    absorbed_ids.update(id(a) for a in lab["absorbed"])
                new_rows.append(row)
                c["rows_added"] += 1
                c["cells_added_in_new_rows"] += len(cl)

        # 4) LABEL: an unnamed row whose name is printed on its line
        for r in prow:
            if (r.get("label") is not None or not r["amounts"] or r.get("non_figure_line")
                    or id(r) in absorbed_ids):
                continue
            pa = S.row_panel(r, panels)
            y = S.row_ref_y(r)
            lab = P.line_label(y, pa, min(a["loc"]["x_min"] for a in r["amounts"]) - 0.003, name_only_rows(pa))
            if lab is None:
                continue
            _apply_label(r, lab, y, c)
            absorbed_ids.update(id(a) for a in lab["absorbed"])
            c["labels_added"] += 1

    if absorbed_ids:
        c["name_rows_absorbed"] += len(absorbed_ids)
        rows[:] = [r for r in rows if id(r) not in absorbed_ids]
    rows.extend(new_rows)


# --------------------------------------------------------------------------- metadata
_SERASA_MONTHS = re.compile(r"COMPREENDE\s+(\d{1,2})\s+MES", re.IGNORECASE)


def recover_metadata(metadata, words, report):
    """DURATION the extraction did not return, from the words: Serasa states the length of the
    income-statement period only in its «Observações» («A DEMONSTRACAO DE RESULTADOS COMPREENDE 09
    MES(ES)», serasa2 p4). Fills metadata["DURATION"] only when it is missing / empty."""
    c = report.setdefault("words", Counter())
    du = metadata.get("DURATION")
    if not words or (du and du.get("value")):
        return
    for pi, page in enumerate(words):
        text = " ".join((t.get("word") or "") for t in page or [])
        m = _SERASA_MONTHS.search(text)
        if m:
            n = int(m.group(1))
            metadata["DURATION"] = {"value": f"{n:02d} meses", "raw": m.group(0), "page_nb": pi + 1,
                                    "confidence": None, "normalized_months": n, "source": "ocr_words"}
            c["metadata_duration"] += 1
            return
