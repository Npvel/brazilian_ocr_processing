#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
structure_ocr.py
================
Восстановление табличных финансовых отчётностей из сырого OCR-JSON.

Цель: построчно и постранично (сверху вниз) собрать показатели наименований,
их значения AMOUNT_N / AMOUNT_N-1 и тип отчётности (ACCOUNT_TYPE:
Controladora / Consolidado / ...) для четырёх групп:
    BALANCE_ASSETS_LST, BALANCE_LIABILITIES_LST,
    INCOME_STATEMENT_LST, CASH_FLOW_LST

Источники данных (верхний уровень исходника — список):
    * 4 скаляра-метаданные: CLOSING_DATE, DURATION, CURRENCY, UNIT
    * группы (group_name + subgroups):
        - LST-группы:   1 subgroup == 1 визуальная строка (name + её суммы)
        - ONLY_AMOUNT:  все суммы; достраивает недостающие колонки (Consolidado)
        - ACCOUNT_TYPE: заголовки колонок (могут быть фрагментированы OCR)
        - CODE_VALUE:   коды строк (в этом файле пусто; ветка предусмотрена)

Ключевые факты, на которых построен алгоритм (проверены на образце):
    * subgroup LST = ровно одна строка (y-разброс ≈ 0) -> связка имя↔суммы готова.
    * ONLY_AMOUNT НЕ полное супермножество: ~6% сумм LST в нём отсутствуют,
      поэтому колонки строим как UNION (LST + ONLY_AMOUNT) с дедупом по локации.
    * Заголовок ACCOUNT_TYPE покрывает ПАРУ колонок (N, N-1), а не одну, и часто
      висит над зазором между ними -> сопоставляем по перекрытию x с парой.
    * На странице бывает несколько таблиц/типов отчёта -> заголовок выбираем
      ближайший СВЕРХУ (max y_center < y строки).

Scope: обрабатываем только страницы, где есть хотя бы одна LST-строка.
Суммы на не-LST страницах (примечания) отбрасываются (см. отчёт).
"""

import argparse
import json
import os
import sys
from collections import defaultdict, Counter

from anchors_lib import normalize
from doc_type import detect_doc_type

# Stage-2 classification is optional: it needs anchors.yaml + pyyaml. If either
# is missing the reconstruction (Stage 1) still runs; classification is skipped.
try:
    import yaml
    import classify_statements as clf
    _CLASSIFY_AVAILABLE = True
except Exception:  # pragma: no cover - environment-dependent
    _CLASSIFY_AVAILABLE = False

# Stage-3 SBS labelling is also optional: it needs sbs_label_mapping.json. If
# anything is missing, Stage 1/2 still run; sbs_label stamping is skipped.
try:
    import sbs_labeler
    _SBS_AVAILABLE = True
except Exception:  # pragma: no cover - environment-dependent
    _SBS_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #
LST_GROUPS = [
    "BALANCE_ASSETS_LST",
    "BALANCE_LIABILITIES_LST",
    "INCOME_STATEMENT_LST",
    "CASH_FLOW_LST",
]
AMOUNT_LABELS = ("AMOUNT_N", "AMOUNT_N-1")
METADATA_NAMES = ("CLOSING_DATE", "DURATION", "CURRENCY", "UNIT")

# Допуск по вертикали: насколько центры y считаются «той же строкой».
Y_ROW_TOL = 0.012
# Допуск при кластеризации безымянных (orphan) сумм в синтетические строки.
Y_ORPHAN_TOL = 0.010
# Минимальный горизонтальный зазор между двумя суммами ОДНОГО периода, при
# котором это считается слипанием двух таблиц «бок о бок» (а не легитимной
# многоколоночной строкой). Панели Ativo|Passivo расходятся на ~0.4+;
# соседние N/N-1 в 4-колоночном блоке — на ~0.1.
XBAND_GAP_MIN = 0.25


# --------------------------------------------------------------------------- #
# Геометрия
# --------------------------------------------------------------------------- #
def yc(loc):
    return (loc["y_min"] + loc["y_max"]) / 2.0


def xc(loc):
    return (loc["x_min"] + loc["x_max"]) / 2.0


def x_overlap(a_min, a_max, b_min, b_max):
    """Длина пересечения двух x-интервалов (0, если не пересекаются)."""
    return max(0.0, min(a_max, b_max) - max(a_min, b_min))


def loc_key(value, loc):
    """Ключ дедупликации суммы по содержимому + позиции."""
    return (value, round(loc["x_min"], 4), round(loc["y_min"], 4))


# --------------------------------------------------------------------------- #
# Нормализация типа отчётности
# --------------------------------------------------------------------------- #
def normalize_account_type(raw):
    """
    Приводит распознанный (возможно фрагментированный) заголовок к канону.
    Ключевые слова важнее регистра/окончаний:
        *controladora* -> Controladora
        *consolidad*   -> Consolidado   (consolidado/-a/-os/-dos)
    Иначе возвращаем Title-case исходной фразы.
    """
    if not raw:
        return None
    low = raw.lower()
    if "controladora" in low:
        return "Controladora"
    if "consolidad" in low:  # consolidado / consolidada / consolidados
        return "Consolidado"
    if "individual" in low:
        return "Individual"
    # Прочее (Resultado, Média ponderada, segmentado, ...) — оставляем читаемым.
    return raw.strip()


# --------------------------------------------------------------------------- #
# Загрузка и индексация исходника
# --------------------------------------------------------------------------- #
def load_groups(data):
    groups = {}
    for el in data:
        gn = el.get("group_name")
        if gn:
            groups[gn] = el
    return groups


def extract_metadata(data):
    """
    Метаданные (CLOSING_DATE, DURATION, CURRENCY, UNIT) -> компактный словарь.

    Каждое поле может прийти в ДВУХ формах:
      * скаляр   — top-level элемент с data_point_name + value:{...}
      * группа   — group_name + subgroups[].values[] (как в ITR-формах);
                   значение повторяется на нескольких страницах, берём с
                   максимальной confidence.
    """
    meta = {}

    def consider(name, value, raw, page, conf):
        """Записать кандидата, если его ещё нет или у него выше confidence."""
        if name not in METADATA_NAMES:
            return
        cur = meta.get(name)
        if cur is None or (conf or 0) > (cur.get("confidence") or 0):
            meta[name] = {"value": value, "raw": raw, "page_nb": page, "confidence": conf}

    for el in data:
        # Форма 1: скаляр.
        dpn = el.get("data_point_name")
        if dpn in METADATA_NAMES:
            v = el.get("value") or {}
            consider(dpn, v.get("normalized_value", v.get("value")), v.get("value"),
                     v.get("page_nb"), v.get("confidence"))

        # Форма 2: группа (берём лучший по confidence subgroup-value).
        gn = el.get("group_name")
        if gn in METADATA_NAMES:
            for sg in el.get("subgroups", []):
                for v in sg.get("values", []):
                    consider(gn, v.get("normalized_value", v.get("value")), v.get("value"),
                             v.get("page_nb", sg.get("page_nb")), v.get("confidence"))

    return meta


# Provider/source tags that identify the document SOURCE (e.g. a Serasa Experian
# dump), as opposed to the per-column Controladora/Consolidado headers that also
# live in the ACCOUNT_TYPE group. Compared on the normalized value.
PROVIDER_TAGS = ("serasa experian",)


def extract_account_type(data):
    """
    Surface a document-level provider tag from the ACCOUNT_TYPE group.

    The group serves two unrelated purposes in this corpus: per-column headers
    (Controladora / Consolidado / Resultado, on statement pages) AND a one-off
    source tag (SERASA EXPERIAN, on a cover page). Only the latter is a document
    metadatum, so we surface a value ONLY when it matches a known provider tag —
    that keeps DFP/ITR metadata free of a misleading "Consolidado" account type
    while giving Serasa reports an exact type signal.

    Returns a metadata-shaped dict (value/raw/page_nb/confidence) or None.
    """
    best = None  # (value, confidence, page)
    for el in data:
        if el.get("group_name") != "ACCOUNT_TYPE":
            continue
        for sg in el.get("subgroups", []):
            for v in sg.get("values", []):
                value = v.get("value")
                if not value or normalize(value) not in PROVIDER_TAGS:
                    continue
                conf = v.get("confidence") or 0
                if best is None or conf > best[1]:
                    best = (value, conf, v.get("page_nb", sg.get("page_nb")))
    if best is None:
        return None
    return {"value": best[0], "raw": best[0], "page_nb": best[2], "confidence": best[1]}


# --------------------------------------------------------------------------- #
# Шаг 1. Строки-якоря из LST-групп
# --------------------------------------------------------------------------- #
def build_row_anchors(groups):
    """
    Каждый subgroup LST -> строка-якорь:
        {statement, page_nb, label, y, name_x_max, amounts:[...], code:None}
    amount := {value, label(period), loc}
    Возвращает (rows, lst_pages, lst_amount_keys[page]->set)
    """
    rows = []
    lst_pages = set()
    lst_amount_keys = defaultdict(set)

    for gname in LST_GROUPS:
        grp = groups.get(gname)
        if not grp:
            continue
        for sg in grp.get("subgroups", []):
            page = sg.get("page_nb")
            lst_pages.add(page)

            name = None
            name_x_max = None
            amounts = []
            ys = []
            for v in sg.get("values", []):
                loc = v["location"]
                ys.append(yc(loc))
                if v["label"] in AMOUNT_LABELS:
                    amounts.append({"value": v["value"], "period": v["label"], "loc": loc})
                    lst_amount_keys[page].add(loc_key(v["value"], loc))
                else:
                    # Имя строки (label == имя группы, value == текст показателя).
                    name = v["value"]
                    name_x_max = loc["x_max"]

            row_y = sum(ys) / len(ys) if ys else None
            rows.append({
                "statement": gname,
                "page_nb": page,
                "label": name,            # может быть None для строк только с суммами
                "y": row_y,
                "name_x_max": name_x_max,
                "amounts": amounts,
                "code": None,
            })
    return rows, lst_pages, lst_amount_keys


# --------------------------------------------------------------------------- #
# Шаг 2. Достройка колонок из ONLY_AMOUNT + сбор orphan-сумм
# --------------------------------------------------------------------------- #
def augment_with_only_amount(groups, rows, lst_pages, lst_amount_keys, report):
    """
    Идём по ONLY_AMOUNT:
        - дубликат существующей LST-суммы (по локации) -> skip;
        - страница не LST -> drop (вне scope);
        - привязывается к строке по y -> добавляем колонку;
        - иначе -> orphan (кандидат в строку label:null).
    Возвращает orphans[page] -> list of {value, period, loc}.
    """
    only = groups.get("ONLY_AMOUNT")
    orphans = defaultdict(list)
    if not only:
        return orphans

    # Индекс строк по странице для быстрой привязки по y.
    rows_by_page = defaultdict(list)
    for r in rows:
        rows_by_page[r["page_nb"]].append(r)

    # X-полосы НАЗВАННЫХ колонок по (page, statement), построенные ДО привязки.
    # На двухпанельных страницах (Ativo слева / Passivo справа на ОДНИХ y) сумма
    # одного отчёта НЕ должна приклеиваться к строке другого только потому, что
    # они делят y — маршрутизируем по перекрытию x с полосой нужного отчёта.
    # Тот же принцип уже применяется в materialize_orphans (см. CLAUDE.md #11);
    # без него склейка ещё и портит сами полосы, ломая последующую маршрутизацию.
    xbands = build_statement_xbands(rows)

    counters = report["only_amount"]
    for sg in only.get("subgroups", []):
        page = sg.get("page_nb")
        for v in sg.get("values", []):
            if v["label"] not in AMOUNT_LABELS:
                continue
            loc = v["location"]
            k = loc_key(v["value"], loc)

            if k in lst_amount_keys[page]:
                counters["dup_skip"] += 1
                continue
            if page not in lst_pages:
                counters["drop_non_lst_page"] += 1
                continue

            y = yc(loc)
            x = xc(loc)
            # Кандидаты-строки на той же высоте.
            cands = [r for r in rows_by_page[page] if r["y"] is not None and abs(r["y"] - y) <= Y_ROW_TOL]
            if cands:
                # Statement-aware gate. Statements whose NAMED x-band overlaps this
                # sum's x are the only ones it can belong to. If any band overlaps,
                # keep candidates only from those statements; if that leaves none
                # (the sum sits in a statement with no row at this y — a subtotal on
                # its own line on the OTHER panel), drop to orphan rather than glue
                # it onto a wrong-statement neighbour. When the x overlaps no band
                # at all, keep the original y-only behaviour (conservative).
                overlapping = {
                    stmt for (pg, stmt), (bmin, bmax) in xbands.items()
                    if pg == page and bmin is not None
                    and x_overlap(loc["x_min"], loc["x_max"], bmin, bmax) > 0
                }
                if overlapping:
                    same_stmt = [r for r in cands if r["statement"] in overlapping]
                    if same_stmt:
                        cands = same_stmt
                    else:
                        orphans[page].append({"value": v["value"], "period": v["label"], "loc": loc})
                        counters["orphan_xband"] += 1
                        continue
                # Предпочесть строки, чьё имя левее этой суммы (логичная привязка).
                left = [r for r in cands if r["name_x_max"] is not None and r["name_x_max"] < x]
                pool = left or cands
                # Ближайшая по y.
                best = min(pool, key=lambda r: abs(r["y"] - y))
                # Occupancy guard: не ставим вторую сумму в уже занятую x-колонку строки
                # (на сложных notes-страницах одна y-полоса пересекает несколько таблиц).
                occupied = any(
                    x_overlap(loc["x_min"], loc["x_max"], a["loc"]["x_min"], a["loc"]["x_max"]) > 0
                    for a in best["amounts"]
                )
                if occupied:
                    orphans[page].append({"value": v["value"], "period": v["label"], "loc": loc})
                    counters["orphan_occupied"] += 1
                    continue
                if len({r["statement"] for r in pool}) > 1:
                    counters["ambiguous_statement"] += 1
                best["amounts"].append({"value": v["value"], "period": v["label"], "loc": loc})
                lst_amount_keys[page].add(k)  # чтобы не привязать дважды
                counters["augment_row"] += 1
            else:
                orphans[page].append({"value": v["value"], "period": v["label"], "loc": loc})
                counters["orphan"] += 1
    return orphans


def build_statement_xbands(rows):
    """
    (page, statement) -> (x_min, x_max) band covering that statement's NAMED
    amount columns. Used to route orphan sums by horizontal position, not just
    by y — essential for side-by-side layouts (Ativo left / Passivo right on the
    same rows) where a pure-y rule would merge two statements into one row.
    """
    spans = defaultdict(lambda: [None, None])
    for r in rows:
        if r["label"] is None:
            continue  # only named rows define a reliable band
        for a in r["amounts"]:
            lo, hi = a["loc"]["x_min"], a["loc"]["x_max"]
            key = (r["page_nb"], r["statement"])
            cur = spans[key]
            cur[0] = lo if cur[0] is None else min(cur[0], lo)
            cur[1] = hi if cur[1] is None else max(cur[1], hi)
    return spans


def _statement_for_orphan(page, loc, xbands, page_rows):
    """
    Pick the statement an orphan sum belongs to. Prefer the statement whose
    NAMED x-band overlaps the orphan's x (side-by-side disambiguation); fall
    back to the nearest named row by y when no band overlaps.
    """
    ox_min, ox_max = loc["x_min"], loc["x_max"]
    best_stmt, best_ov = None, 0.0
    for (pg, stmt), (bmin, bmax) in xbands.items():
        if pg != page or bmin is None:
            continue
        ov = x_overlap(ox_min, ox_max, bmin, bmax)
        if ov > best_ov:
            best_ov, best_stmt = ov, stmt
    if best_stmt is not None:
        return best_stmt
    # Fallback: nearest named row by y (original behaviour).
    if page_rows:
        oy = yc(loc)
        return min(page_rows, key=lambda r: abs(r["y"] - oy))["statement"]
    return None


def materialize_orphans(orphans, rows, report):
    """
    Безымянные суммы (orphan) на LST-страницах -> синтетические строки label:null.

    Кластеризуем по (statement-band, y): сначала каждую orphan-сумму относим к
    statement по перекрытию её x с x-band НАЗВАННЫХ колонок этого отчёта, затем
    кластеризуем по y ВНУТРИ statement. Это разделяет двухпанельные раскладки
    (Ativo слева / Passivo справа на одних y) — иначе суммы двух отчётов слиплись
    бы в одну строку с перепутанными колонками.
    """
    rows_by_page = defaultdict(list)
    for r in rows:
        if r["y"] is not None:
            rows_by_page[r["page_nb"]].append(r)

    xbands = build_statement_xbands(rows)

    new_rows = []
    for page, items in orphans.items():
        # 1) Route each orphan to its statement by x-band.
        by_stmt = defaultdict(list)
        for a in items:
            stmt = _statement_for_orphan(page, a["loc"], xbands, rows_by_page.get(page, []))
            by_stmt[stmt].append(a)

        # 2) Cluster by y WITHIN each statement.
        for stmt, group in by_stmt.items():
            group.sort(key=lambda a: yc(a["loc"]))
            clusters = []
            cur = []
            anchor = None
            for a in group:
                y = yc(a["loc"])
                if anchor is None or abs(y - anchor) <= Y_ORPHAN_TOL:
                    cur.append(a)
                    anchor = y if anchor is None else (anchor + y) / 2.0
                else:
                    clusters.append(cur)
                    cur = [a]
                    anchor = y
            if cur:
                clusters.append(cur)

            for cl in clusters:
                cy = sum(yc(a["loc"]) for a in cl) / len(cl)
                row = {
                    "statement": stmt,
                    "page_nb": page,
                    "label": None,
                    "y": cy,
                    "name_x_max": None,
                    "amounts": cl,
                    "code": None,
                }
                # Integrity guard: a genuine side-by-side collapse shows up as the
                # SAME period appearing twice with a WIDE horizontal gap between the
                # two amounts (two separate tables). A mere repeated period at
                # adjacent x is a legitimate multi-column row (e.g. a 4-column
                # Controladora|Consolidado block), so gap-test before flagging.
                by_period = defaultdict(list)
                for a in cl:
                    by_period[a["period"]].append(xc(a["loc"]))
                wide_dup = any(
                    len(xs) > 1 and (max(xs) - min(xs)) > XBAND_GAP_MIN
                    for xs in by_period.values()
                )
                if wide_dup:
                    row["xband_mixed"] = True
                    report["only_amount"]["xband_mixed_rows"] += 1
                new_rows.append(row)
                report["only_amount"]["label_null_rows"] += 1
    rows.extend(new_rows)


# --------------------------------------------------------------------------- #
# Шаг 3. Заголовки ACCOUNT_TYPE по страницам
# --------------------------------------------------------------------------- #
def build_account_headers(groups, lst_pages):
    """page -> list of {raw, x_min, x_max, y} (только LST-страницы)."""
    headers = defaultdict(list)
    at = groups.get("ACCOUNT_TYPE")
    if not at:
        return headers
    for sg in at.get("subgroups", []):
        page = sg.get("page_nb")
        if page not in lst_pages:
            continue
        for v in sg.get("values", []):
            loc = v["location"]
            headers[page].append({
                "raw": v["value"],
                "x_min": loc["x_min"],
                "x_max": loc["x_max"],
                "y": yc(loc),
            })
    return headers


# --------------------------------------------------------------------------- #
# Шаг 4. Коды строк CODE_VALUE (если присутствуют)
# --------------------------------------------------------------------------- #
def attach_codes(groups, rows, report):
    """
    Если CODE_VALUE непуст — привязываем код к строке по странице и y.
    В образце группа пуста, ветка предусмотрена на будущее.
    """
    cv = groups.get("CODE_VALUE")
    if not cv or not cv.get("subgroups"):
        return
    rows_by_page = defaultdict(list)
    for r in rows:
        if r["y"] is not None:
            rows_by_page[r["page_nb"]].append(r)

    for sg in cv["subgroups"]:
        page = sg.get("page_nb")
        for v in sg.get("values", []):
            loc = v["location"]
            y = yc(loc)
            cands = [r for r in rows_by_page.get(page, []) if abs(r["y"] - y) <= Y_ROW_TOL]
            if cands:
                best = min(cands, key=lambda r: abs(r["y"] - y))
                best["code"] = v["value"]
                report["code_value"]["attached"] += 1
            else:
                report["code_value"]["orphan"] += 1


# --------------------------------------------------------------------------- #
# Шаг 5. Колонки строки + сопоставление account_type
# --------------------------------------------------------------------------- #
def pair_columns(cols):
    """
    Группируем колонки (отсортированы по x) в пары (AMOUNT_N, AMOUNT_N-1).
    Непарные -> одиночные группы. Возвращает список групп (list[list[col]]).
    """
    pairs = []
    i = 0
    n = len(cols)
    while i < n:
        if (cols[i]["period"] == "AMOUNT_N"
                and i + 1 < n
                and cols[i + 1]["period"] == "AMOUNT_N-1"):
            pairs.append([cols[i], cols[i + 1]])
            i += 2
        else:
            pairs.append([cols[i]])
            i += 1
    return pairs


def assign_account_types(row, headers, report):
    """
    Для каждой пары колонок строки выбираем заголовок:
        кандидаты — заголовки ВЫШЕ строки (y_header < y_row);
        среди пересекающихся по x с диапазоном пары берём ближайший сверху;
        фрагменты на одной высоте, попавшие в пару, склеиваем.
    Возвращает список колонок с проставленным account_type.
    """
    cols = sorted(row["amounts"], key=lambda a: xc(a["loc"]))
    page_headers = headers.get(row["page_nb"], [])
    groups_of_cols = pair_columns(cols)

    out_columns = []
    for grp in groups_of_cols:
        span_min = min(c["loc"]["x_min"] for c in grp)
        span_max = max(c["loc"]["x_max"] for c in grp)

        acc_type = None
        confidence = None
        if page_headers and row["y"] is not None:
            above = [h for h in page_headers if h["y"] < row["y"] - 1e-9]
            # Заголовки, пересекающиеся по x с диапазоном пары.
            overlapped = [(h, x_overlap(span_min, span_max, h["x_min"], h["x_max"]))
                          for h in above]
            overlapped = [(h, ov) for h, ov in overlapped if ov > 0]

            if overlapped:
                # Ближайшая сверху строка заголовков (max y среди пересекающихся).
                top_y = max(h["y"] for h, _ in overlapped)
                same_row = [h for h, _ in overlapped if abs(h["y"] - top_y) <= Y_ROW_TOL]
                same_row.sort(key=lambda h: h["x_min"])
                raw = " ".join(h["raw"] for h in same_row)
                acc_type = normalize_account_type(raw)
                confidence = "overlap"
            elif above:
                # Fallback: ближайший по центру x среди верхних заголовков.
                pair_cx = (span_min + span_max) / 2.0
                h = min(above, key=lambda hh: abs((hh["x_min"] + hh["x_max"]) / 2.0 - pair_cx))
                acc_type = normalize_account_type(h["raw"])
                confidence = "nearest_x"
                report["account_type"]["fallback_nearest"] += 1
            else:
                report["account_type"]["no_header_above"] += 1
        elif not page_headers:
            report["account_type"]["page_without_header"] += 1

        for c in grp:
            out_columns.append({
                "account_type": acc_type,
                "period": c["period"],
                "value": c["value"],
                "x": round(xc(c["loc"]), 4),
                "_match": confidence,
            })
    return out_columns


# --------------------------------------------------------------------------- #
# Сборка результата
# --------------------------------------------------------------------------- #
def build_output(rows, headers, report, keep_debug, classification=None):
    result = {g: [] for g in LST_GROUPS}
    # Сортировка строк: страница -> y (сверху вниз).
    for r in sorted(rows, key=lambda r: (r["page_nb"], r["y"] if r["y"] is not None else 0)):
        stmt = r["statement"]
        if stmt not in result:
            # orphan-строка без определённого statement — пропускаем из 4 групп.
            report["rows"]["dropped_no_statement"] += 1
            continue
        columns = assign_account_types(r, headers, report)
        if not keep_debug:
            for c in columns:
                c.pop("x", None)
                c.pop("_match", None)
        row_obj = {
            "page_nb": r["page_nb"],
            "label": r["label"],
            "columns": columns,
        }
        # Stamp the page's Stage-2 class so downstream can filter rows directly.
        if classification is not None:
            verdict = classification.get(r["page_nb"])
            if verdict is not None:
                row_obj["page_class"] = verdict["page_class"]
                row_obj["page_confidence"] = verdict["confidence"]
        if r["code"] is not None:
            row_obj["code"] = r["code"]
        if keep_debug:
            row_obj["y"] = round(r["y"], 4) if r["y"] is not None else None
        if r["label"] is None:
            row_obj["label_missing"] = True
        # Two independent integrity signals can flag a row for review. Track each
        # cause separately, but count `needs_review` ONCE per flagged row (a row
        # can trip both), so the counter equals the number of rows actually
        # carrying needs_review=True.
        # 1) Orphan-merge corruption flag (set in materialize_orphans).
        if r.get("xband_mixed"):
            row_obj["xband_mixed"] = True
            row_obj["needs_review"] = True
            report["rows"]["xband_mixed"] += 1
        # 2) Duplicate (account_type, period) — the row was crossed by several
        #    tables (dense notes pages).
        named_dup = Counter((c["account_type"], c["period"]) for c in columns if c["account_type"])
        if any(v > 1 for v in named_dup.values()):
            row_obj["needs_review"] = True
            report["rows"]["column_collision"] += 1
        if row_obj.get("needs_review"):
            report["rows"]["needs_review"] += 1

        result[stmt].append(row_obj)

        report["rows"]["total"] += 1
        report["rows"]["with_label" if r["label"] else "label_null"] += 1
        report["columns"]["total"] += len(columns)
    return result


def new_report():
    return {
        "only_amount": Counter(),
        "account_type": Counter(),
        "code_value": Counter(),
        "rows": Counter(),
        "columns": Counter(),
        "classification": Counter(),
        "sbs": Counter(),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run_classification(data, report):
    """
    Stage-2: classify each page (PRIMARY / NOTE / JUNK). Returns page->verdict
    or None if the classifier/anchors.yaml are unavailable. Failures are soft —
    Stage-1 reconstruction is never blocked by classification.
    """
    if not _CLASSIFY_AVAILABLE:
        report["classification"]["skipped_no_module"] += 1
        return None
    anchors_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anchors.yaml")
    if not os.path.exists(anchors_path):
        report["classification"]["skipped_no_anchors"] += 1
        return None
    try:
        cfg = yaml.safe_load(open(anchors_path, encoding="utf-8"))
        classification = clf.classify(data, cfg)
        for v in classification.values():
            report["classification"][v["page_class"].split(":")[0]] += 1
        return classification
    except Exception as exc:  # pragma: no cover
        report["classification"]["error"] += 1
        report["classification_error"] = str(exc)
        return None


def run_sbs_labeling(statements, report, document_type=None):
    """
    Stage-3: stamp `sbs_label` (+ scope/method/score) on each row, using the
    aliases in sbs_label_mapping.json. Soft failure — Stage 1/2 are never
    blocked. Mutates `statements` in place; records a stats block in the report.
    `document_type` gates type-specific labelling stages (e.g. SERASA).
    """
    if not _SBS_AVAILABLE:
        report["sbs"]["skipped_no_module"] += 1
        return
    mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "sbs_label_mapping.json")
    if not os.path.exists(mapping_path):
        report["sbs"]["skipped_no_mapping"] += 1
        return
    try:
        mapping = sbs_labeler.load_mapping(mapping_path)
        match_cfg = sbs_labeler.load_match_config()
        stats = sbs_labeler.label_statements(statements, mapping, match_cfg,
                                             document_type=document_type)
        report["sbs"].update(stats)
    except Exception as exc:  # pragma: no cover
        report["sbs"]["error"] += 1
        report["sbs_error"] = str(exc)


def process(data, keep_debug=False):
    groups = load_groups(data)
    report = new_report()

    metadata = extract_metadata(data)
    account_type = extract_account_type(data)
    if account_type is not None:
        metadata["ACCOUNT_TYPE"] = account_type
    classification = run_classification(data, report)

    rows, lst_pages, lst_amount_keys = build_row_anchors(groups)
    orphans = augment_with_only_amount(groups, rows, lst_pages, lst_amount_keys, report)
    materialize_orphans(orphans, rows, report)
    attach_codes(groups, rows, report)
    headers = build_account_headers(groups, lst_pages)
    statements = build_output(rows, headers, report, keep_debug, classification)

    # Document type — computed ONCE here (single source of truth) from the
    # assembled statements + provider tag, then carried in the output so every
    # downstream stage (SBS labelling, Excel export) reads the same value.
    document_type = detect_doc_type(statements, metadata)
    report["document_type"] = document_type

    # Stage-3: SBS labelling (runs on the assembled statements, in place).
    run_sbs_labeling(statements, report, document_type)

    report["pages"] = {
        "lst_pages": len(lst_pages),
        "pages_list": sorted(p for p in lst_pages if p is not None),
    }

    output = {
        "metadata": metadata,
        "document_type": document_type,
        "statements": statements,
    }
    if classification is not None:
        # Top-level per-page classification + a compact summary for filtering.
        output["page_classification"] = {
            str(pg): _public_verdict(v) for pg, v in sorted(classification.items())
        }
        output["classification_summary"] = clf.summarize(classification)
    return output, report


def _public_verdict(v):
    """Trim internal fields from a verdict for the public output."""
    out = {
        "page_class": v["page_class"],
        "confidence": v["confidence"],
        "reasons": v["reasons"],
        "n_cols": v["n_cols"],
    }
    if v.get("complete_families"):
        out["complete_families"] = v["complete_families"]
    if v.get("identity"):
        out["identity"] = v["identity"]
    if v.get("needs_review"):
        out["needs_review"] = True
    return out


def print_report(report):
    def block(title, c):
        print(f"  [{title}]")
        for k, v in sorted(c.items()):
            print(f"      {k}: {v}")

    print("=" * 60)
    print("ОТЧЁТ О КАЧЕСТВЕ СТРУКТУРИЗАЦИИ")
    print("=" * 60)
    print(f"  LST-страниц обработано: {report['pages']['lst_pages']}")
    block("rows", report["rows"])
    block("columns", report["columns"])
    block("only_amount", report["only_amount"])
    block("account_type", report["account_type"])
    block("code_value", report["code_value"])
    if report["classification"]:
        block("classification (page_class)", report["classification"])
    if report["sbs"]:
        # Compact SBS summary: matched / scope / review, omit per-field noise.
        s = report["sbs"]
        print("  [sbs_label]")
        for k in ("row_matched", "row_unmatched", "scope_in", "scope_cross",
                  "row_sbs_needs_review"):
            if k in s:
                print(f"      {k}: {s[k]}")
    print("=" * 60)


def main():
    # Консоль Windows по умолчанию cp1252 — переключаем на UTF-8 для кириллицы.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Структуризация OCR-JSON финансовой отчётности.")
    ap.add_argument("input", nargs="?", default="comprehansive_sample.json",
                    help="входной OCR-JSON")
    ap.add_argument("-o", "--output", default="structured_output.json",
                    help="выходной JSON")
    ap.add_argument("--debug", action="store_true",
                    help="сохранять координаты (x/y) и метку сопоставления для сверки")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    output, report = process(data, keep_debug=args.debug)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Отчёт о качестве в JSON (Counter -> dict).
    report_path = args.output.rsplit(".", 1)[0] + "_report.json"
    serializable = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in report.items()}
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)

    print_report(report)
    print(f"Отчёт:    {report_path}")
    print(f"\nЗаписано: {args.output}")
    meta_pairs = ", ".join("{0}={1!r}".format(k, v["value"]) for k, v in output["metadata"].items())
    print("Метаданные: { " + meta_pairs + " }")


if __name__ == "__main__":
    main()
