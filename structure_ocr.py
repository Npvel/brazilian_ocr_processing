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
import calendar
import json
import os
import re
import statistics
import sys
from collections import defaultdict, Counter

from anchors_lib import normalize, detect_number_format, to_br_number, parse_amount
from doc_type import detect_doc_type, provider_tag, PROVIDER_TAGS_NORMALIZED

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

# Stage-3 end: ONE value per SBS field and period (sums / printed totals / identity-proven picks).
try:
    import sbs_fields
    _SBS_FIELDS_AVAILABLE = True
except Exception:  # pragma: no cover - environment-dependent
    _SBS_FIELDS_AVAILABLE = False

# Stage 1 step 5b: OCR sign / dash errors repaired where the code tree or a printed subtotal proves
# them (sign_proof.py, standard library). Soft dependency like the stages above.
try:
    import sign_proof
    _SIGN_PROOF_AVAILABLE = True
except Exception:  # pragma: no cover
    _SIGN_PROOF_AVAILABLE = False

# Add-on: recovery of lost cells / lines / labels from the reciTAL OCR words layer — ON by default
# (user decision 2026-11-12; reciTAL confirmed the layer in production 2026-11-05). Nothing depends
# on it: without `words` (layer missing, fetch failed) or without this module the pipeline runs as
# the words-free fallback, exactly. Deleting words_recovery.py removes the step entirely.
try:
    import words_recovery
    _WORDS_AVAILABLE = True
except Exception:  # pragma: no cover - optional module
    _WORDS_AVAILABLE = False

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

# Допуск по вертикали «та же визуальная строка» — ОТ ШАГА СТРОК СТРАНИЦЫ, не константа.
# Замер на 22 PDF reciTAL (2026-11): разброс y внутри одной строки p99 <= 0.0017, а шаг строк
# бывает 0.0079 (Raízen p26 — первичный ДДС). Прежний фикс. допуск 0.012 был БОЛЬШЕ строки:
# сумма безымянной соседней строки приклеивалась к именованной, а настоящая сумма той строки
# уходила в label_null (каскад); orphan-суммы соседних строк слипались цепочкой (16 сумм в одной
# строке на DVA p27). Теперь допуск = 0.45 * шаг страницы, в пределах [MIN, MAX].
ROW_TOL_PITCH_FRACTION = 0.45
ROW_TOL_MIN = 0.003
ROW_TOL_MAX = 0.0065
ROW_TOL_DEFAULT_PITCH = 0.012   # страница со слишком малым числом строк -> медиана документа / это
# Склейка фрагментов ОДНОГО заголовка ACCOUNT_TYPE ("resultado" + "consolidado") в одну строку.
HEADER_LINE_TOL = 0.012
# Совместимость: старое имя (для внешних проб). В логике строк больше не используется.
Y_ROW_TOL = 0.012
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


def page_line_pitch(rows):
    """
    Шаг строк по страницам: медиана расстояний между соседними НАЗВАНИЯМИ (y-центры).
    Возвращает (pitch[page], doc_pitch). Пары ближе 0.004 (две панели на одной строке) не считаются.
    """
    ys = defaultdict(list)
    for r in rows:
        if r.get("name_y") is not None:
            ys[r["page_nb"]].append(r["name_y"])
    pitch = {}
    for pg, v in ys.items():
        v = sorted(v)
        d = [b - a for a, b in zip(v, v[1:]) if b - a > 0.004]
        if len(d) >= 3:
            pitch[pg] = statistics.median(d)
    doc = statistics.median(pitch.values()) if pitch else ROW_TOL_DEFAULT_PITCH
    return pitch, doc


def make_row_tol(rows):
    """page -> допуск «та же визуальная строка» = 0.45 * шаг страницы в [ROW_TOL_MIN, ROW_TOL_MAX]."""
    pitch, doc = page_line_pitch(rows)

    def tol(page):
        return min(ROW_TOL_MAX, max(ROW_TOL_MIN, ROW_TOL_PITCH_FRACTION * pitch.get(page, doc)))
    return tol


def row_ref_y(r):
    """
    y-ориентир строки для привязки сумм: медиана y-центров её СУММ (суммы одной строки выровнены
    точнее всего), иначе y названия, иначе средний y. Название может сидеть на 0.002-0.004 ниже сумм
    (DUTRA), поэтому средний y по названию+суммам не годится для узкого допуска.
    """
    if r.get("amounts"):
        return statistics.median(yc(a["loc"]) for a in r["amounts"])
    if r.get("name_y") is not None:
        return r["name_y"]
    return r.get("y")


# --------------------------------------------------------------------------- #
# Панели страницы (двухпанельные балансы: Ativo | Passivo на одних строках)
# --------------------------------------------------------------------------- #
# Панель определяется ФИЗИЧЕСКИ — по колонке названий, а не по LST-группе провайдера:
#  * группа классифицирует СОДЕРЖАНИЕ (на странице ДДС бывают строки BALANCE_*), поэтому полосы
#    «отчётов» перекрываются и защита по ним отсекает правильную строку той же линии (Romagnole p11);
#  * x-полосы из ИМЕНОВАННЫХ LST-сумм покрывают только первую пару колонок (Controladora), колонки
#    Consolidado из ONLY_AMOUNT в них не попадают -> защита по полосам слепа к ним (ALUBAR p6).
# Панель = от левого края своей колонки названий до левого края следующей.
PANEL_GAP_MIN = 0.15          # колонки названий дальше друг от друга по x — разные панели
PANEL_MIN_NAMES = 3           # колонка названий из 1-2 строк — не панель (заголовок/шум)
PANEL_MIN_SHARED_LINES = 2    # панели «бок о бок» обязаны делить визуальные строки
PANEL_MIN_DISTINCT = 0.6      # доля РАЗНЫХ названий в колонке: «Comprado/Vendido» в каждой строке
                              # таблицы деривативов — колонка значений, а не панель (Raízen p64)
PANEL_MIN_AMOUNTS = 3         # у КАЖДОЙ панели свои суммы между её названиями и следующей панелью
NAME_PANEL_MARGIN = 0.01      # допуск только для НАЗВАНИЙ (отступ чуть левее края колонки названий).
                              # Числа — без допуска: число стоит правее названий СВОЕЙ панели, а
                              # короткие числа последней колонки левой панели кончаются вплотную к
                              # названиям правой (Delfia p5: 58 / 371 / 933, центр в 0.008–0.010 от края —
                              # с допуском они уходили в правую панель, DEL10, исправлено 2026-11-12)


def build_panels(rows, tol):
    """page -> [x левых краёв панелей] (только для страниц с >= 2 панелями бок о бок)."""
    names = defaultdict(list)
    amts = defaultdict(list)
    for r in rows:
        if r.get("label") and r.get("name_x_min") is not None and r.get("name_y") is not None:
            names[r["page_nb"]].append((r["name_x_min"], r["name_y"], r["label"]))
        for a in r.get("amounts", []):
            amts[r["page_nb"]].append(xc(a["loc"]))
    panels = {}
    for pg, v in names.items():
        xs = sorted(x for x, _, _ in v)
        cols = [[xs[0]]]
        for x in xs[1:]:
            (cols[-1].append(x) if x - cols[-1][-1] <= PANEL_GAP_MIN else cols.append([x]))
        cols = [c for c in cols if len(c) >= PANEL_MIN_NAMES]
        if len(cols) < 2:
            continue
        edges = [min(c) for c in cols]
        member = defaultdict(list)
        labels = defaultdict(list)
        for x, y, lab in v:
            k = _panel_index(x, edges, NAME_PANEL_MARGIN)
            member[k].append(y)
            labels[k].append(normalize(lab))
        shared_ok = all(
            sum(1 for ya in member[i] if any(abs(ya - yb) <= tol(pg) for yb in member[i + 1]))
            >= PANEL_MIN_SHARED_LINES
            for i in range(len(edges) - 1))
        distinct_ok = all(len(set(labels[k])) >= PANEL_MIN_NAMES
                          and len(set(labels[k])) >= PANEL_MIN_DISTINCT * len(labels[k])
                          for k in range(len(edges)))
        bounds = edges + [1.01]
        amounts_ok = all(sum(1 for x in amts[pg] if bounds[k] <= x < bounds[k + 1]) >= PANEL_MIN_AMOUNTS
                         for k in range(len(edges)))
        if shared_ok and distinct_ok and amounts_ok:
            panels[pg] = edges
    return panels


def _panel_index(x, edges, margin=0.0):
    """Последняя панель, левый край которой (минус `margin`) x достигает."""
    k = 0
    for i, e in enumerate(edges):
        if x >= e - margin:
            k = i
    return k


def panel_of(page, x, panels):
    """Панель ЧИСЛА (x = центр его рамки): без допуска (DEL10, см. NAME_PANEL_MARGIN)."""
    edges = panels.get(page)
    return _panel_index(x, edges) if edges else 0


def row_panel(r, panels):
    """Панель строки: по левому краю названия (с допуском названий); у безымянной — по x её сумм."""
    if r.get("name_x_min") is not None:
        edges = panels.get(r["page_nb"])
        return _panel_index(r["name_x_min"], edges, NAME_PANEL_MARGIN) if edges else 0
    if r.get("amounts"):
        return panel_of(r["page_nb"], statistics.median(xc(a["loc"]) for a in r["amounts"]), panels)
    return 0


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


# --------------------------------------------------------------------------- #
# Нормализация метаданных: OCR-токены -> значения для SBS (value/raw не меняются)
# --------------------------------------------------------------------------- #
# Провайдер отдаёт обрывки: CURRENCY '(Reais' / 'reais)' / 'Reais,' / 'REAL', UNIT 'Mil)' / 'MILHOES',
# CLOSING_DATE '2025' (только год) / 'C-31/12/2024' (буква колонки Serasa). Хуже: у Romagnole
# CLOSING_DATE = '2024' — год СРАВНИТЕЛЬНОЙ колонки, тогда как отчётность на 31/12/2025 (DURATION
# '31 de dezembro de 2025 e 2024'). Нормализованное значение кладём рядом: `normalized`.
_MONTHS_PT = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
              "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_DATE_NUM = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
# «31 de dezembro de 2024 e 2023»; also the document's own typo «31 DE DEZEMBRO E 2024 E 2023» (Cibra, audit
# 2026-11-05 m3: «e» for «de» between the month and the year — a month name directly followed by a year)
_DATE_TXT = re.compile(r"(\d{1,2})\s+de\s+([a-z]+)\s+(?:d?e\s+)?(\d{4})((?:\s*(?:e|,)\s*(?:de\s+)?\d{4})*)")


def _dates_in(text):
    """ISO dates written in a Portuguese period text; '31 de dezembro de 2025 e 2024' -> both."""
    t = normalize(text or "")
    out = []
    raw = (text or "")
    for d, m, y in _DATE_NUM.findall(raw):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            out.append(f"{y}-{int(m):02d}-{int(d):02d}")
    for d, mon, y, more in _DATE_TXT.findall(t):
        m = _MONTHS_PT.get(mon)
        if m:
            for yy in [y] + re.findall(r"\d{4}", more):
                out.append(f"{yy}-{m:02d}-{int(d):02d}")
    return out


_MONTHS_WORD = {"um": 1, "tres": 3, "seis": 6, "nove": 9, "doze": 12}
_MONTH_YEAR = re.compile(r"^\D{0,3}(\d{1,2})[./-](\d{4})\D*$")


def _duration_months(text):
    """Months of the reporting period stated by a DURATION text, or None. An explicit count wins
    («09 meses», «nove meses», «trimestre» / «semestre»); else two dates with the SAME day and month
    one year apart («31 de dezembro de 2024 e 2023», «31 de dezembro de 2024 e 31 de dezembro de
    2023») are the annual comparison of two fiscal years -> 12 (audit 2026-11-04, P14: Dom, Entrepay,
    São Salvador). A single date, or two dates of different day / month (an interim balance against the
    last year-end: «31 de março de 2026 e 31 de dezembro de 2025»), states no period length."""
    t = normalize(text or "")
    m = re.search(r"\b(\d{1,2})\s+mes(es)?\b", t)
    if m and 1 <= int(m.group(1)) <= 24:
        return int(m.group(1))
    m = re.search(r"\b(um|tres|seis|nove|doze)\s+mes(es)?\b", t)
    if m:
        return _MONTHS_WORD[m.group(1)]
    if "trimestr" in t:
        return 3
    if "semestr" in t:
        return 6
    ds = sorted(set(_dates_in(text)))
    if len(ds) == 2 and ds[0][4:] == ds[1][4:] and int(ds[1][:4]) - int(ds[0][:4]) == 1:
        return 12
    return None


def normalize_metadata(meta):
    """Add `normalized` to CLOSING_DATE (ISO date), CURRENCY (ISO code), UNIT (multiplier) and
    DURATION (months, when the text states a date range). Tag, never overwrite: `value` stays the
    provider's. CLOSING_DATE: a full date is taken as is; a bare year (or a year older than the
    period text) is completed / corrected from the LATEST date of DURATION (`normalized_from`)."""
    cd, du = meta.get("CLOSING_DATE"), meta.get("DURATION")
    du_dates = sorted(_dates_in(du.get("value") if du else None))
    if cd:
        own = _dates_in(cd.get("value"))
        year = re.search(r"\b(\d{4})\b", str(cd.get("value") or ""))
        my = _MONTH_YEAR.match(str(cd.get("value") or "").strip())
        if not own and my and 1 <= int(my.group(1)) <= 12:
            # «12.2024» (month.year — DIMENSIONAL, audit 2026-11-04 D6): the last day of that month
            mm, yy = int(my.group(1)), int(my.group(2))
            own = [f"{yy}-{mm:02d}-{calendar.monthrange(yy, mm)[1]:02d}"]
        if own:
            cd["normalized"] = max(own)
        elif du_dates and (year is None or du_dates[-1][:4] >= year.group(1)):
            cd["normalized"] = du_dates[-1]
            cd["normalized_from"] = "DURATION"
    cur = meta.get("CURRENCY")
    if cur and re.search(r"\b(real|reais|brl)\b|r\$", normalize(str(cur.get("value") or "")) + " "
                         + str(cur.get("value") or "").lower()):
        cur["normalized"] = "BRL"
    un = meta.get("UNIT")
    if un:
        u = normalize(str(un.get("value") or ""))
        mult = (1_000_000 if re.search(r"\bmilho(es)?\b|\bmi\b", u) else
                1_000 if re.search(r"\bmilhares\b|\bmil\b", u) else
                1 if re.search(r"\bunidades?\b|\breais\b", u) else None)
        if mult:
            un["normalized"] = mult
    if du and len(_DATE_NUM.findall(du.get("value") or "")) == 2:
        (d1, m1, y1), (d2, m2, y2) = _DATE_NUM.findall(du["value"])
        months = (int(y2) - int(y1)) * 12 + int(m2) - int(m1) + (1 if int(d2) >= 28 and int(d1) <= 3 else 0)
        if 1 <= months <= 24:
            du["normalized_months"] = months
    elif du and du.get("value"):
        months = _duration_months(du["value"])
        if months:
            du["normalized_months"] = months
    return meta


_CENTS = re.compile(r"\d,\d{2}\)?$")


def infer_unit_from_cents(meta, rows):
    """No UNIT printed (accounting-system dumps: Uniar, Benoit — audit 2026-11-04 UNI4 / BEN9) and the
    figures carry centavos («1.120.836.736,37»): the amounts are in units (reais) — a thousands
    statement never prints two decimals on almost every figure. Tag, never overwrite."""
    un = meta.get("UNIT")
    if un and un.get("normalized"):
        return
    figs = [str(a["value"]).strip() for r in rows for a in r["amounts"]
            if a.get("value") and re.search(r"\d", str(a["value"]))]
    cents = sum(1 for v in figs if _CENTS.search(_DC_SUFFIX.sub(lambda m: m.group(1), v)))
    if len(figs) >= 10 and cents >= 0.8 * len(figs):
        un = meta.setdefault("UNIT", {"value": None, "raw": None, "page_nb": None, "confidence": None})
        un["normalized"] = 1
        un["normalized_from"] = "figures with centavos"


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
            name_x_min = None
            name_y = None
            name_box = None
            amounts = []
            ys = []
            for v in sg.get("values", []):
                loc = v["location"]
                ys.append(yc(loc))
                if v["label"] in AMOUNT_LABELS:
                    amounts.append({"value": v["value"], "period": v["label"], "loc": loc, "src": "lst"})
                    lst_amount_keys[page].add(loc_key(v["value"], loc))
                else:
                    # Имя строки (label == имя группы, value == текст показателя).
                    name = v["value"]
                    name_x_max = loc["x_max"]
                    name_x_min = loc["x_min"]
                    name_y = yc(loc)
                    name_box = (loc["y_min"], loc["y_max"])

            row_y = sum(ys) / len(ys) if ys else None
            rows.append({
                "statement": gname,
                "page_nb": page,
                "label": name,            # может быть None для строк только с суммами
                "y": row_y,
                "name_x_max": name_x_max,
                "name_x_min": name_x_min,  # левый край названия -> колонка названий -> панель страницы
                "name_y": name_y,         # y-центр названия (debug / контроль геометрии)
                "name_box": name_box,     # (y_min, y_max) названия — для проверки «сумма на строке названия»
                "amounts": amounts,
                "code": None,
            })
    return rows, lst_pages, lst_amount_keys


# --------------------------------------------------------------------------- #
# Шаг 2. Достройка колонок из ONLY_AMOUNT + сбор orphan-сумм
# --------------------------------------------------------------------------- #
# Сумма «на строке названия», если её y-центр в пределах допуска от y названия ИЛИ внутри
# вертикальной рамки названия (+ поле): двухстрочное название держит суммы на любой своей строке.
NAME_BOX_MARGIN = 0.0015


def detach_offline_amounts(rows, report):
    """
    Шаг 1b. Проверка строк провайдера: «1 subgroup = 1 строка» верно НЕ всегда. На плотных
    страницах провайдер связывает название строки k с суммами строки k±1 (DUTRA p2: у
    «Serviços Prestados» суммы строки «Mercadorias Recebidas…»; Raízen p26: (339.177) строки
    «Aplicações em títulos…» отдана «Adições ao investimento»). Сумма, которая стоит НЕ на
    визуальной строке своего названия (или в другой панели двухпанельной страницы), отвязывается
    и размещается заново по общим правилам (своя строка / своя безымянная строка).
    Возвращает [(page, amount)] — отвязанные суммы. Их ключи остаются в lst_amount_keys, поэтому
    копия той же суммы в ONLY_AMOUNT по-прежнему отбрасывается как дубль (сохранность не страдает).
    """
    tol = make_row_tol(rows)
    panels = build_panels(rows, tol)
    loose = []
    for r in rows:
        if r.get("name_y") is None or not r["amounts"]:
            continue
        page = r["page_nb"]
        lo, hi = r["name_box"]
        keep = []
        for a in r["amounts"]:
            ay = yc(a["loc"])
            on_line = (abs(ay - r["name_y"]) <= tol(page)
                       or lo - NAME_BOX_MARGIN <= ay <= hi + NAME_BOX_MARGIN)
            same_panel = (page not in panels
                          or panel_of(page, xc(a["loc"]), panels) == row_panel(r, panels))
            if on_line and same_panel:
                keep.append(a)
            else:
                loose.append((page, a))
                report["only_amount"]["lst_detached"] += 1
        if len(keep) != len(r["amounts"]):
            r["amounts"] = keep
            ys = [r["name_y"]] + [yc(a["loc"]) for a in keep]
            r["y"] = sum(ys) / len(ys)
    return loose


def augment_with_only_amount(groups, rows, lst_pages, lst_amount_keys, report, loose=None):
    """
    Размещение «свободных» сумм на строках страницы — сначала отвязанные LST-суммы (шаг 1b),
    затем ONLY_AMOUNT:
        - дубликат существующей LST-суммы (по локации) -> skip;
        - страница не LST -> drop (вне scope);
        - на той же визуальной строке (и в той же панели) есть строка -> добавляем колонку;
        - иначе -> orphan (кандидат в строку label:null).
    Возвращает orphans[page] -> list of {value, period, loc}.
    """
    only = groups.get("ONLY_AMOUNT") or {}
    orphans = defaultdict(list)

    # Индекс строк по странице для быстрой привязки по y.
    rows_by_page = defaultdict(list)
    for r in rows:
        rows_by_page[r["page_nb"]].append(r)

    # Панели страницы (Ativo слева / Passivo справа на ОДНИХ строках), построенные ДО привязки
    # по колонке НАЗВАНИЙ: сумма одной панели не должна приклеиваться к строке другой только
    # потому, что они делят y. Та же панель применяется в materialize_orphans (CLAUDE.md #11).
    tol = make_row_tol(rows)
    panels = build_panels(rows, tol)

    def place(page, value, period, loc, src):
        """Put one figure on its visual line. Returns the outcome key."""
        y = yc(loc)
        x = xc(loc)
        # Кандидаты-строки на той же ВИЗУАЛЬНОЙ строке (допуск от шага страницы).
        tol_p = tol(page)
        cands = [r for r in rows_by_page[page]
                 if row_ref_y(r) is not None and abs(row_ref_y(r) - y) <= tol_p]
        fig = {"value": value, "period": period, "loc": loc, "src": src}
        if not cands:
            orphans[page].append(fig)
            return "orphan"
        # Panel gate. On a side-by-side page a figure can only belong to a row of ITS
        # OWN panel. If its panel has no row on this line (a subtotal printed alone on
        # the OTHER panel), drop it to an orphan rather than glue it onto the
        # neighbouring panel's row. Single-panel page -> plain visual-line match: there
        # the provider's statement tag is content, not location, and must not gate.
        if page in panels:
            pa = panel_of(page, x, panels)
            same_panel = [r for r in cands if row_panel(r, panels) == pa]
            if not same_panel:
                orphans[page].append(fig)
                return "orphan_xband"
            cands = same_panel
        # Предпочесть строки, чьё имя левее этой суммы (логичная привязка).
        left = [r for r in cands if r["name_x_max"] is not None and r["name_x_max"] < x]
        pool = left or cands
        # Ближайшая по y.
        best = min(pool, key=lambda r: abs(row_ref_y(r) - y))
        # Occupancy guard: не ставим вторую сумму в уже занятую x-колонку строки
        # (на сложных notes-страницах одна y-полоса пересекает несколько таблиц).
        occupied = any(
            x_overlap(loc["x_min"], loc["x_max"], a["loc"]["x_min"], a["loc"]["x_max"]) > 0
            for a in best["amounts"]
        )
        if occupied:
            orphans[page].append(fig)
            return "orphan_occupied"
        if len({r["statement"] for r in pool}) > 1:
            report["only_amount"]["ambiguous_statement"] += 1
        best["amounts"].append(fig)
        return "augment_row"

    counters = report["only_amount"]
    # 1) Re-place the provider amounts detached from a wrong line (step 1b).
    for page, a in loose or []:
        counters[f"lst_rebound_{place(page, a['value'], a['period'], a['loc'], 'lst')}"] += 1
    # 2) ONLY_AMOUNT.
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
            # A single-value ONLY_AMOUNT subgroup is ALWAYS tagged AMOUNT_N by the provider
            # (592/592 on Raízen): its tag carries no period evidence -> source "oa_single".
            src = "oa_pair" if len(sg.get("values", [])) >= 2 else "oa_single"
            outcome = place(page, v["value"], v["label"], loc, src)
            counters[outcome] += 1
            if outcome == "augment_row":
                lst_amount_keys[page].add(k)  # чтобы не привязать дважды
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


# Безымянная строка обычно — подитог, закрывающий блок НАД ней: её отчёт берём у ближайшей
# именованной строки выше (в своей панели), если она не дальше стольких строк и не дальше строки ниже.
ORPHAN_ABOVE_MAX_PITCHES = 4


def _statement_for_orphan(page, loc, panels, page_rows, pitch):
    """
    Statement of an unnamed figure. Candidates = named rows of ITS panel (side-by-side page) or of
    the page. The row ABOVE wins (an unnamed line is usually the subtotal closing the block above)
    unless it is far away and a row below is nearer; otherwise the row below. The neighbours' tags
    are the evidence: the provider's statement tag classifies content, so x-bands per statement are
    not reliable (they overlap on stacked pages: serasa2 p5 routed IS figures to BL by max overlap).
    """
    named = [r for r in page_rows if r.get("label")]
    pa = panel_of(page, xc(loc), panels)
    pool = [r for r in named if row_panel(r, panels) == pa] or named or page_rows
    if not pool:
        return None
    oy = yc(loc)
    above = [r for r in pool if row_ref_y(r) <= oy]
    below = [r for r in pool if row_ref_y(r) > oy]
    a = max(above, key=row_ref_y) if above else None
    b = min(below, key=row_ref_y) if below else None
    if a is not None:
        da = oy - row_ref_y(a)
        if b is None or da <= ORPHAN_ABOVE_MAX_PITCHES * pitch or da <= row_ref_y(b) - oy:
            return a["statement"]
    return (b or a)["statement"]


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

    tol = make_row_tol(rows)
    panels = build_panels(rows, tol)
    pitch, doc_pitch = page_line_pitch(rows)

    new_rows = []
    for page, items in orphans.items():
        # 1) Route each orphan to its statement: own panel, then the named neighbour above.
        by_stmt = defaultdict(list)
        for a in items:
            stmt = _statement_for_orphan(page, a["loc"], panels, rows_by_page.get(page, []),
                                         pitch.get(page, doc_pitch))
            by_stmt[(stmt, panel_of(page, xc(a["loc"]), panels))].append(a)

        # 2) Cluster by y WITHIN each statement — one cluster == one visual line.
        #    The cluster span is bounded from its FIRST (top-most) member: no drifting average, so
        #    adjacent lines can never chain into one row. A figure overlapping in x a figure already
        #    in the cluster is the same column twice -> necessarily another line -> new cluster.
        tol_p = tol(page)
        for (stmt, _panel), group in by_stmt.items():
            group.sort(key=lambda a: yc(a["loc"]))
            clusters = []   # [(y_top, [amounts])]
            for a in group:
                y = yc(a["loc"])
                last = clusters[-1] if clusters else None
                same_line = (last is not None and y - last[0] <= tol_p and not any(
                    x_overlap(a["loc"]["x_min"], a["loc"]["x_max"], b["loc"]["x_min"], b["loc"]["x_max"]) > 0
                    for b in last[1]))
                if same_line:
                    last[1].append(a)
                else:
                    clusters.append((y, [a]))

            for _, cl in clusters:
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
# Шаг 2c. Период по сетке колонок (метка провайдера ошибается в неполных строках)
# --------------------------------------------------------------------------- #
# Числа выровнены ВПРАВО: x_max одной колонки разбросан на 0.002-0.007, колонки разнесены на
# >= 0.06. Метка AMOUNT_N / AMOUNT_N-1 провайдера надёжна в ПОЛНЫХ строках (все колонки панели
# заполнены), но ломается в строках с пустыми ячейками: Raízen p26 — 15 значений колонки
# Controladora 2025 помечены N-1 (арифметика: (1.072.622)+(2.466.243)+9.974.302+37.238 =
# 6.472.675 = прирост 2025), Conso 19.587 — N-1 вместо N; одиночные subgroup-ы ONLY_AMOUNT
# всегда несут AMOUNT_N (ALUBAR p6: Conso 2024 = 192 помечено N). Решение пользователя 2026-11-04:
# колонка (x) приоритетнее метки; исправленная ячейка помечается `period_corrected`.
PERIOD_GRID_BAND_GAP = 0.02     # разрыв x_max, разделяющий две выровненные вправо колонки
PERIOD_GRID_MIN_REF = 3         # эталонных значений (из полных строк), чтобы доверять колонке
PERIOD_GRID_MIN_PURITY = 0.9    # согласие эталонов колонки об одном периоде


def correct_periods_by_column_grid(rows, report):
    tol = make_row_tol(rows)
    panels = build_panels(rows, tol)
    items_by = defaultdict(list)   # (page, panel) -> [(row, amount)]
    for r in rows:
        for a in r["amounts"]:
            items_by[(r["page_nb"], panel_of(r["page_nb"], xc(a["loc"]), panels))].append((r, a))
    for items in items_by.values():
        xs = sorted(a["loc"]["x_max"] for _, a in items)
        raw = []
        for x in xs:
            if raw and x - raw[-1][1] <= PERIOD_GRID_BAND_GAP:
                raw[-1][1] = x
                raw[-1][2] += 1
            else:
                raw.append([x, x, 1])
        bands = [(lo, hi) for lo, hi, n in raw if n >= PERIOD_GRID_MIN_REF]
        if not bands:
            continue

        def band_of(a):
            xm = a["loc"]["x_max"]
            for i, (lo, hi) in enumerate(bands):
                if lo - 1e-9 <= xm <= hi + 1e-9:
                    return i
            return None

        per_row = defaultdict(list)
        for r, a in items:
            per_row[id(r)].append(a)
        ref = Counter()
        for amts in per_row.values():
            bs = [band_of(a) for a in amts]
            # a FULL row: one figure in EVERY column of the panel, no column twice
            if None in bs or len(set(bs)) != len(bs) or len(bs) != len(bands):
                continue
            for b, a in zip(bs, amts):
                if a.get("src", "lst") != "oa_single":   # singleton tags carry no period evidence
                    ref[(b, a["period"])] += 1
        band_period = {}
        for b in range(len(bands)):
            cnt = {p: ref[(b, p)] for p in AMOUNT_LABELS}
            tot = sum(cnt.values())
            if tot >= PERIOD_GRID_MIN_REF:
                p, c = max(cnt.items(), key=lambda kv: kv[1])
                if c / tot >= PERIOD_GRID_MIN_PURITY:
                    band_period[b] = p
        for r, a in items:
            b = band_of(a)
            if b in band_period and a["period"] != band_period[b]:
                a["period_src"] = a["period"]
                a["period"] = band_period[b]
                report["only_amount"]["period_corrected"] += 1


# --------------------------------------------------------------------------- #
# Шаг 2c'. Зоны колонок по шапке таблицы: период и перспектива — из шапки
# --------------------------------------------------------------------------- #
# Метка AMOUNT_N / AMOUNT_N-1 провайдера ненадёжна у чисел без названия (ONLY_AMOUNT), а шаг 2c
# голосует только «полными» строками. Cibra 12.2024 (аудит 2026-11-05, K1 / K2 / K4): у агента 5485
# «полными» на пассиве p7 оказались ровно 3 строки, где пара ONLY_AMOUNT перепрыгнула пустую колонку
# Conso 2024 (Contr. 2023 с меткой N) -> колонка Contr. 2023 перекрашена в N, а правило пары (шаг 5)
# поставило Contr. 2023 в Conso 2024: TOTAL DO PASSIVO E PL 2024 = 2.433.930 вместо 4.392.444; у 6051
# одиночки Conso 2024 ДДС с меткой N-1 не исправлялись. Шапка печатает, чья колонка: reciTAL отдаёт
# рамки CLOSING_DATE («31/12/2024» над колонкой N) и ACCOUNT_TYPE («Controladora» / «Consolidado» над
# парой N | N-1). Где шапка ОДНОЗНАЧНА, число берёт период и перспективу зоны своей колонки, что бы ни
# говорила метка; иначе — прежнее поведение (шаг 2c + правило пары). Решение пользователя 2026-11-05
# по симуляции на 36 документах (_audit/ZONES_SIMULATION_2026-11-05.md): зоны — только DFP с
# Controladora / Consolidado над колонками; вне Cibra ни одного изменения Excel / sbs_fields / golden.
ZONE_HDR_MAX_PITCHES = 8.0      # дата шапки колонки — не дальше стольких строк над таблицей (не название)
ZONE_FIG_SHARE_MIN = 0.5        # колонка таблицы: >= половины ячеек — суммы или прочерки
ZONE_LOOKUP_MARGIN = 0.006      # число, добавленное позже (OCR words) -> зона колонки его правого края
_ZONE_DASHES = ("-", "–", "—")
_FRAME_MONTHS = {"jan": 1, "janeiro": 1, "fev": 2, "fevereiro": 2, "mar": 3, "marco": 3, "abr": 4,
                 "abril": 4, "mai": 5, "maio": 5, "jun": 6, "junho": 6, "jul": 7, "julho": 7,
                 "ago": 8, "agosto": 8, "set": 9, "setembro": 9, "out": 10, "outubro": 10,
                 "nov": 11, "novembro": 11, "dez": 12, "dezembro": 12}


def _header_frames(groups, name):
    """page -> frames {raw, x_min, x_max, y_min, y_max, y, xc} of a header group (CLOSING_DATE /
    ACCOUNT_TYPE) — every page, the zone decides itself which frames stand over the table."""
    out = defaultdict(list)
    for sg in (groups.get(name) or {}).get("subgroups", []):
        for v in sg.get("values", []):
            loc = v.get("location")
            if not loc or not (v.get("value") or "").strip():
                continue
            out[sg.get("page_nb")].append({"raw": v["value"], "x_min": loc["x_min"], "x_max": loc["x_max"],
                                           "y_min": loc["y_min"], "y_max": loc["y_max"],
                                           "y": yc(loc), "xc": xc(loc)})
    return out


def _frame_year(s):
    y = int(s)
    return y + 2000 if y < 100 else y


def parse_frame_date(raw):
    """A plain column date -> (year, month | None, day | None): '31/12/2024', 'C-31/12/2024' (Serasa
    column letter), '31/dez/24', '31 de dezembro de 2024', 'DEZEMBRO /2024', '12.2024', '2024'.
    Anything else (a period label '1T25', a phrase, a number) -> None."""
    t = re.sub(r"^[a-z] (?=\d)", "", normalize(raw or ""))
    m = re.fullmatch(r"(\d{1,2}) (\d{1,2}) (\d{2}|\d{4})", t)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        return (_frame_year(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2}) (?:de )?([a-z]+) (?:de )?(\d{2}|\d{4})", t)
    if m and m.group(2) in _FRAME_MONTHS:
        return (_frame_year(m.group(3)), _FRAME_MONTHS[m.group(2)], int(m.group(1)))
    m = re.fullmatch(r"([a-z]+) (?:de )?(\d{4})", t)
    if m and m.group(1) in _FRAME_MONTHS:
        return (int(m.group(2)), _FRAME_MONTHS[m.group(1)], None)
    m = re.fullmatch(r"(\d{1,2}) (\d{4})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return (int(m.group(2)), int(m.group(1)), None)
    m = re.fullmatch(r"((?:19|20)\d{2})", t)
    if m:
        return (int(m.group(1)), None, None)
    return None


def frame_kind(raw, closing):
    """'N' (the document's closing date), 'old' (an earlier date), 'unclear' (later, or no closing
    date known) or None (not a plain date) — what a CLOSING_DATE frame says about its column."""
    d = parse_frame_date(raw)
    if d is None:
        return None
    if closing is None:
        return "unclear"
    y, m, dd = d
    cy, cm, cd = closing
    if m is not None and dd is not None:
        return "N" if (y, m, dd) == (cy, cm, cd) else ("old" if (y, m, dd) < (cy, cm, cd) else "unclear")
    if m is not None:
        return "N" if (y, m) == (cy, cm) else ("old" if (y, m) < (cy, cm) else "unclear")
    return "N" if y == cy else ("old" if y < cy else "unclear")


def closing_date(metadata):
    """(year, month, day) of CLOSING_DATE.normalized, or None."""
    iso = ((metadata or {}).get("CLOSING_DATE") or {}).get("normalized")
    if iso and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(iso)):
        return (int(iso[:4]), int(iso[5:7]), int(iso[8:10]))
    return None


def _note_refs(values):
    """A column of note references ('21', '8', '13'), not of amounts: >= 80 % bare 1-2 digit integers."""
    vals = [str(v).strip() for v in values]
    return bool(vals) and sum(1 for v in vals if re.fullmatch(r"\d{1,2}", v)) >= 0.8 * len(vals)


def _figure_like(value):
    """A cell of a figure column: a printed amount or a dash (not '(ii)', '(a)', '(9')."""
    v = (value or "").strip()
    return is_wellformed_figure(v) or v in _ZONE_DASHES


def _zone_columns(items):
    """Right-edge clusters of one panel's figures [{lo, hi, xmax, members, table}] (x_max gap <= the
    Step-2c band gap, ANY count: a column the extraction left almost empty is still a column). `table`
    = the cluster holds figures: a column of note references or of category markers ('(i)', '(ii)' —
    1Q26 p68: such a column next to a lost one shifted every pair) is no column of the table."""
    cols = []
    for _r, a in sorted(items, key=lambda ra: ra[1]["loc"]["x_max"]):
        x = a["loc"]["x_max"]
        if cols and x - cols[-1]["hi"] <= PERIOD_GRID_BAND_GAP:
            cols[-1]["hi"] = x
            cols[-1]["members"].append(a)
        else:
            cols.append({"lo": x, "hi": x, "members": [a]})
    for c in cols:
        vals = [a["value"] for a in c["members"]]
        c["xmax"] = statistics.median(a["loc"]["x_max"] for a in c["members"])
        c["table"] = (not _note_refs(vals) and any(is_wellformed_figure(v) for v in vals)
                      and sum(1 for v in vals if _figure_like(v)) >= ZONE_FIG_SHARE_MIN * len(vals))
    return cols


def _panel_span(page, pa, panels):
    """x-range of panel `pa` of the page (the whole page when it has one panel)."""
    edges = panels.get(page)
    if not edges:
        return (-1.0, 2.0)
    return (edges[pa] - 0.01 if pa > 0 else -1.0, edges[pa + 1] - 0.01 if pa + 1 < len(edges) else 2.0)


def _merge_header_fragments(frames):
    """ACCOUNT_TYPE frames of one header line -> headers (fragments closer than
    PERSPECTIVE_HEADER_GAP_MIN are one header: 'resultado' + 'consolidado'). A header naming both
    perspectives ('Controladora e Consolidado') is `combined`: it types no pair on its own."""
    out = []
    for h in sorted(frames, key=lambda h: h["x_min"]):
        if out and h["x_min"] - out[-1]["x_max"] < PERSPECTIVE_HEADER_GAP_MIN:
            out[-1]["raw"] += " " + h["raw"]
            out[-1]["x_max"] = max(out[-1]["x_max"], h["x_max"])
        else:
            out.append(dict(h))
    for o in out:
        o["xc"] = (o["x_min"] + o["x_max"]) / 2
        low = normalize(o["raw"])
        o["combined"] = bool(re.search(r"(controladora|individua)", low)) and "consolidad" in low
        o["type"] = None if o["combined"] else normalize_account_type(o["raw"])
    return out


def build_header_zones(rows, panels, groups, closing):
    """(page, panel) -> {"status", "detail", "zones": [{type, period, lo, hi, xmax, members}]}.
    A panel is zoned only when its header is unambiguous:
      * perspective headers (ACCOUNT_TYPE) on ONE line, in the panel, above the first table row;
      * the table columns split in order into consecutive pairs, exactly 2 per header, each header
        standing over its own pair;
      * date frames (CLOSING_DATE) above the table, at most ZONE_HDR_MAX_PITCHES lines above it, each
        over ONE column (>= half of the frame over the column's width, the nearest column) — a date of
        the statement title stands over no column and is ignored;
      * in a pair exactly one column stands under a frame of the closing date -> N, the other -> N-1;
        no frame of an unclear / later date in the pair, no earlier date over the N column;
      * N is printed LEFT of N-1 in every pair (all 127 zoned pairs of the 36 documents of the
        simulation; the only exception was a shifted pairing, 1Q26 p68).
    A pair failing its date test is left out (the panel is 'partial'); anything else -> no zone."""
    types_by = _header_frames(groups, "ACCOUNT_TYPE")
    dates_by = _header_frames(groups, "CLOSING_DATE")
    pitch, doc_pitch = page_line_pitch(rows)
    items_by = defaultdict(list)
    for r in rows:
        for a in r["amounts"]:
            items_by[(r["page_nb"], panel_of(r["page_nb"], xc(a["loc"]), panels))].append((r, a))
    table = {}
    for (pg, pa), items in items_by.items():
        rec = {"status": None, "detail": "", "zones": []}
        table[(pg, pa)] = rec
        lo, hi = _panel_span(pg, pa, panels)
        cols = [c for c in _zone_columns(items) if c["table"]]
        figs = [a for _, a in items if is_wellformed_figure(a["value"])]
        if not figs:
            rec["status"] = "no_figures"
            continue
        first_y = min(a["loc"]["y_min"] for a in figs)
        types = [h for h in types_by.get(pg, []) if lo <= h["xc"] < hi]
        persp = [h for h in types if normalize_account_type(h["raw"]) in _PERSPECTIVES]
        dates = [d for d in dates_by.get(pg, []) if lo <= d["xc"] < hi]
        if not persp:
            rec["status"] = "no_type_header"
            continue
        if not dates:
            rec["status"] = "no_date_frame"
            continue
        if any(h["y_max"] > first_y + 1e-9 for h in persp):
            rec["status"] = "type_header_inside_table"   # a second table / a table continued
            continue
        ys = sorted(h["y"] for h in persp)
        if ys[-1] - ys[0] > HEADER_LINE_TOL:
            rec["status"] = "type_header_lines"
            continue
        line_y = statistics.median(ys)
        line = [h for h in types if h["y_max"] <= first_y + 1e-9 and abs(h["y"] - line_y) <= HEADER_LINE_TOL]
        heads = [h for h in _merge_header_fragments(line) if h["type"] in _PERSPECTIVES or h["combined"]]
        if any(h["combined"] for h in heads):
            rec["status"] = "combined_header"
            continue
        if len(cols) != 2 * len(heads):
            rec["status"] = "ncols_mismatch"
            rec["detail"] = f"{len(cols)} columns for {len(heads)} headers"
            continue
        pairs = [(2 * k, 2 * k + 1) for k in range(len(heads))]
        wmax = max(a["loc"]["x_max"] - a["loc"]["x_min"] for a in figs)   # a column is this wide
        spans = [(cols[i]["xmax"] - wmax, cols[j]["xmax"]) for i, j in pairs]
        centres = [(s0 + s1) / 2 for s0, s1 in spans]
        if any(x_overlap(spans[k][0], spans[k][1], h["x_min"], h["x_max"]) <= 0
               or min(range(len(centres)), key=lambda i: abs(centres[i] - h["xc"])) != k
               for k, h in enumerate(heads)):
            rec["status"] = "header_not_over_pair"
            continue
        over = defaultdict(list)            # column index -> what the date frames over it say
        for d in dates:
            if d["y_max"] > first_y + 1e-9 or first_y - d["y"] > ZONE_HDR_MAX_PITCHES * pitch.get(pg, doc_pitch):
                continue                     # inside the table / the statement title
            best, best_ov = None, 0.0
            for i, c in enumerate(cols):
                ov = x_overlap(c["xmax"] - wmax, c["xmax"], d["x_min"], d["x_max"])
                if ov > best_ov:
                    best, best_ov = i, ov
            if best is not None and best_ov >= 0.5 * (d["x_max"] - d["x_min"]):
                over[best].append(frame_kind(d["raw"], closing))
        zones, reasons = [], []
        for k, (i0, i1) in enumerate(pairs):
            k0, k1 = over.get(i0, []), over.get(i1, [])
            if ("N" in k0) == ("N" in k1) or any(x in ("unclear", None) for x in k0 + k1):
                reasons.append(f"pair {k}: dates {k0} | {k1}")
                continue
            i_n, i_o = (i0, i1) if "N" in k0 else (i1, i0)
            if "old" in over.get(i_n, []):
                reasons.append(f"pair {k}: an earlier date over the N column")
                continue
            if i_n > i_o:
                reasons.append(f"pair {k}: N right of N-1")
                zones = None
                break
            for i, per in ((i_n, "AMOUNT_N"), (i_o, "AMOUNT_N-1")):
                c = cols[i]
                zones.append({"type": heads[k]["type"], "period": per, "lo": c["lo"], "hi": c["hi"],
                              "xmax": c["xmax"], "members": c["members"]})
        rec["detail"] = "; ".join(reasons)
        if zones is None:
            rec["status"] = "n_right_of_n1"
            continue
        rec["zones"] = zones
        rec["status"] = "zoned" if zones and not reasons else ("partial" if zones else "pair_dates")
    return table


def apply_header_zones(rows, groups, metadata, report):
    """Step 2c' (runs right after Step 2c): every figure of a zone column takes the zone's period (and,
    in Step 5, its perspective) whatever the provider tag. Provenance as in Step 2c: `period_src` = the
    provider's tag whenever the period differs from it (a Step-2c correction the zone undoes is
    dropped). Returns the zone table for Step 5 and the words step."""
    panels = build_panels(rows, make_row_tol(rows))
    table = build_header_zones(rows, panels, groups, closing_date(metadata))
    for rec in table.values():
        if rec["zones"]:
            report["only_amount"]["zone_panels"] += 1
        for z in rec["zones"]:
            for a in z["members"]:
                provider = a.get("period_src", a["period"])
                a["zone"] = (z["type"], z["period"])
                a["provider_period"] = provider
                a["unzoned_period"] = a["period"]     # what Step 5 would have paired without the zone
                if a["period"] != z["period"]:
                    report["only_amount"]["zone_period_set"] += 1
                a["period"] = z["period"]
                if a["period"] != provider:
                    a["period_src"] = provider
                else:
                    a.pop("period_src", None)
    return {"panels": panels, "table": table}


def zone_of(zones, page, x_max, x_center):
    """(perspective, period) of the zone column a later-added figure (OCR words) stands in, or None."""
    if not zones:
        return None
    rec = zones["table"].get((page, panel_of(page, x_center, zones["panels"])))
    if not rec:
        return None
    hit = [z for z in rec["zones"] if z["lo"] - ZONE_LOOKUP_MARGIN <= x_max <= z["hi"] + ZONE_LOOKUP_MARGIN]
    return (hit[0]["type"], hit[0]["period"]) if len(hit) == 1 else None


# --------------------------------------------------------------------------- #
# Шаг 2d. Баланс в ТРИ колонки: N | N-1 (пересчитанный) | входящий баланс N-1
# --------------------------------------------------------------------------- #
# Рестейтмент сравнительного года (CPC 23) печатает баланс в 3 колонки: «31/12/2024 | 31/12/2023
# Reapresentado | 01/01/2023 Reapresentado» (Dom Atacarejo p6, аудит 2026-11-04). Макет — на 2
# периода, третья колонка должна быть просто опущена. Провайдер же метит СРЕДНЮЮ колонку AMOUNT_N
# (пары ONLY_AMOUNT), а входящий баланс — AMOUNT_N-1 (LST связывает название с 2024 и 01/01/2023):
# сетка колонок (шаг 2c) честно получает две полосы «N», pick_cells берёт первую N и первую N-1 ->
# N-1 актива = 01/01/2023 (итог 918.483 вместо 1.174.378, 8 полей sbs_fields неверны).
# Доказательство — тождество баланса со СМЕЖНОЙ стороной (p7 напечатана в 2 колонки N | N-1):
# максимум (= итог стороны) полосы N совпадает с итогом N соседней страницы, ровно одна из двух
# других полос совпадает с её итогом N-1 -> это N-1; оставшаяся — более старая колонка -> AMOUNT_N-2
# (в JSON остаётся с `period_src`, в Excel / SBS не идёт). Нет доказательства — ничего не меняется.
EXTRA_COLUMN_BANDS = 3


def _period_bands(items):
    """[(lo, hi, [(row, amount)])] — right-aligned columns (x_max clusters, >= PERIOD_GRID_MIN_REF)."""
    srt = sorted(items, key=lambda ra: ra[1]["loc"]["x_max"])
    raw = []
    for r, a in srt:
        x = a["loc"]["x_max"]
        if raw and x - raw[-1][1] <= PERIOD_GRID_BAND_GAP:
            raw[-1][1] = x
            raw[-1][2].append((r, a))
        else:
            raw.append([x, x, [(r, a)]])
    return [(lo, hi, its) for lo, hi, its in raw if len(its) >= PERIOD_GRID_MIN_REF]


def _band_total(its):
    """The side's grand total printed in this column = the largest figure of its balance rows."""
    vals = [abs(v) for r, a in its if r["statement"] in LST_GROUPS[:2]
            for v in [parse_amount(a["value"])] if v is not None]
    return max(vals) if vals else None


def resolve_extra_period_column(rows, report):
    tol = make_row_tol(rows)
    panels = build_panels(rows, tol)
    by = defaultdict(list)
    for r in rows:
        if r["statement"] not in LST_GROUPS[:2]:
            continue
        for a in r["amounts"]:
            by[(r["page_nb"], panel_of(r["page_nb"], xc(a["loc"]), panels))].append((r, a))
    info = {}
    for key, items in by.items():
        bands = []
        for lo, hi, its in _period_bands(items):
            per = Counter(a["period"] for _, a in its).most_common(1)[0][0]
            bands.append({"its": its, "period": per, "total": _band_total(its)})
        info[key] = bands
    for (pg, pa), bands in info.items():
        if len(bands) != EXTRA_COLUMN_BANDS or len({b["period"] for b in bands}) != 2:
            continue
        for (qp, qa), qb in info.items():
            if abs((qp or 0) - (pg or 0)) != 1 or len(qb) != 2 \
                    or {b["period"] for b in qb} != set(AMOUNT_LABELS):
                continue
            q = {b["period"]: b["total"] for b in qb}
            if None in q.values():
                continue
            n = [b for b in bands if b["total"] is not None and abs(b["total"] - q["AMOUNT_N"]) <= 0.5]
            rest = [b for b in bands if b not in n]
            n1 = [b for b in rest if b["total"] is not None and abs(b["total"] - q["AMOUNT_N-1"]) <= 0.5]
            if len(n) != 1 or len(n1) != 1:
                continue
            extra = [b for b in rest if b is not n1[0]]
            for band, period in ((n[0], "AMOUNT_N"), (n1[0], "AMOUNT_N-1"), (extra[0], "AMOUNT_N-2")):
                for _r, a in band["its"]:
                    if a["period"] != period:
                        a.setdefault("period_src", a["period"])
                        a["period"] = period
                        report["only_amount"]["extra_column_period_set"] += 1
            report["only_amount"]["extra_column_resolved"] += 1
            break


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
    tol = make_row_tol(rows)

    for sg in cv["subgroups"]:
        page = sg.get("page_nb")
        for v in sg.get("values", []):
            loc = v["location"]
            y = yc(loc)
            cands = [r for r in rows_by_page.get(page, []) if abs(row_ref_y(r) - y) <= tol(page)]
            if cands:
                best = min(cands, key=lambda r: abs(row_ref_y(r) - y))
                best["code"] = v["value"]
                report["code_value"]["attached"] += 1
            else:
                report["code_value"]["orphan"] += 1


# --------------------------------------------------------------------------- #
# Шаг 4b. Перенос названия на 2+ строки -> одна строка с полным названием
# --------------------------------------------------------------------------- #
# Провайдер отдаёт каждую физическую строку названия отдельным subgroup. Суммы стоят на ПОСЛЕДНЕЙ
# строке названия (DFP: «Imposto sobre a renda e contribuição» / «social a recuperar» 141.634)
# или на ПЕРВОЙ (ITR: «Captação de empréstimos e financiamentos, CRI e» 367.677 / «Debentures
# Liquido dos Custos»). Без склейки строка с суммами несёт обрывок названия, и SBS его не узнаёт
# или узнаёт неверно (Raízen p22: EBT «…antes do imposto sobre a renda e / da contribuição
# social» -> incomeTaxExpenseRefundAmount). Решение пользователя 2026-11-04: склеивать.
WRAP_MAX_PITCHES = 1.6          # продолжение — на соседней визуальной строке
_WRAP_CONNECTOR = re.compile(
    r"(\b(de|do|da|dos|das|e|a|o|as|os|em|no|na|nos|nas|com|sobre|para|por|pelo|pela|pelos|pelas|"
    r"ao|aos|à|às|não|nao|ou|que|entre)|[-–,(/])\s*$", re.IGNORECASE)


def _starts_lowercase(label):
    for ch in label or "":
        if ch.isalpha():
            return ch.islower()
    return False


_CAPS_CONTINUATION = re.compile(r"^\s*E\s+\S")


def _continues(label):
    """The line continues the name above: lowercase start, or — in an ALL-CAPS heading — the
    conjunction «E» (ALUBAR p7: «RESULTADO ANTES DO RESULTADO FINANCEIRO, IMPOSTO DE RENDA» /
    «E CONTRIBUIÇÃO SOCIAL»: the fragment matched the income-tax alias). Only «E»: «A vencer…»,
    «De 1 a 3 anos» are line items of their own."""
    return _starts_lowercase(label) or bool(_CAPS_CONTINUATION.match(label or ""))


# Type C: an ALL-CAPS name cut after a connector, continued on the next line in capitals too (Cibra p8,
# audit 2026-11-05 K5: «LUCRO (PREJUÍZO) ANTES DO IMPOSTO DE RENDA E» / «  DA CONTRIBUIÇÃO SOCIAL»
# with the figures — the fragment matched the income-tax alias and the pre-tax line stayed unlabelled).
# The second line must open with a preposition / conjunction no line item starts with («DA», «DO», …),
# or be printed indented under the first.
_CAPS_CONT_START = re.compile(r"^\s*(DA|DO|DE|DOS|DAS|E)\s+\S")
WRAP_INDENT_MIN = 0.005         # x of the continuation's name right of the first line's (≈ 1 character)


def _all_caps(label):
    letters = [ch for ch in label or "" if ch.isalpha()]
    return bool(letters) and all(ch.isupper() for ch in letters)


def _caps_continuation(a, b):
    """b continues the ALL-CAPS name-only line a (type C), see _CAPS_CONT_START."""
    if not (_all_caps(a["label"]) and _all_caps(b["label"]) and _WRAP_CONNECTOR.search(a["label"])):
        return False
    if _CAPS_CONT_START.match(b["label"]):
        return True
    xa, xb = a.get("name_x_min"), b.get("name_x_min")
    return xa is not None and xb is not None and xb - xa >= WRAP_INDENT_MIN


def merge_wrapped_labels(rows, report):
    """
    Merge the physical lines of a wrapped line-item name into ONE row (in place).
      A  name-only line + next line STARTING LOWERCASE        -> label A+B, amounts of the 2nd line
      B  name-only line ENDING WITH A CONNECTOR + next line
         WITHOUT a name (label_null)                          -> label A, amounts of the 2nd line
      C  ALL-CAPS name-only line ending with a connector + next
         ALL-CAPS line with a name AND amounts, opening with
         DA / DO / DE / DOS / DAS / E or indented              -> label A+B, amounts of the 2nd line
      D  line WITH amounts ending with a connector + next
         NAME-ONLY line (lowercase start, or no code on a
         coded page)                                          -> label A+B, amounts of the 1st line
    Only consecutive rows of the same statement, page and panel, on adjacent visual lines.
    Nothing is lost: the parts are kept in `label_parts`.
    """
    tol = make_row_tol(rows)
    panels = build_panels(rows, tol)
    pitch, doc_pitch = page_line_pitch(rows)
    coded_pages = {r["page_nb"] for r in rows if r.get("code")}

    def line_y(r):
        return r["name_y"] if r.get("name_y") is not None else row_ref_y(r)

    groups = defaultdict(list)
    for r in rows:
        if r.get("y") is not None:
            groups[(r["statement"], r["page_nb"], row_panel(r, panels))].append(r)
    removed = set()
    for (stmt, page, _pa), grp in groups.items():
        grp.sort(key=line_y)
        max_dy = WRAP_MAX_PITCHES * pitch.get(page, doc_pitch)
        i = 0
        while i < len(grp) - 1:
            a, b = grp[i], grp[i + 1]
            dy = line_y(b) - line_y(a)
            kind = None
            if 0 < dy <= max_dy and a.get("label"):
                if not a["amounts"] and b.get("label") and _continues(b["label"]):
                    kind = "A"
                elif not a["amounts"] and b.get("label") is None and b["amounts"] \
                        and _WRAP_CONNECTOR.search(a["label"]):
                    kind = "B"
                elif not a["amounts"] and b.get("label") and b["amounts"] and _caps_continuation(a, b):
                    kind = "C"
                elif a["amounts"] and b.get("label") and not b["amounts"] and not b.get("code") \
                        and ((_WRAP_CONNECTOR.search(a["label"])
                              and (_starts_lowercase(b["label"]) or page in coded_pages))
                             # coded page: a code-less lowercase line is the tail of the coded
                             # line above even when OCR cut its last word ("...CRI e direit" / "de uso")
                             or (page in coded_pages and _starts_lowercase(b["label"]))):
                    kind = "D"
            if kind is None:
                i += 1
                continue
            parts = (a.get("label_parts") or [a["label"]]) + \
                    ((b.get("label_parts") or [b["label"]]) if b.get("label") else [])
            keep, drop = (b, a) if kind in ("A", "B", "C") else (a, b)
            keep["label"] = " ".join(p.strip() for p in parts if p)
            keep["label_parts"] = parts
            keep["label_wrapped"] = kind
            keep["code"] = keep.get("code") or drop.get("code")
            boxes = [x["name_box"] for x in (a, b) if x.get("name_box")]
            if boxes:
                keep["name_box"] = (min(lo for lo, _ in boxes), max(hi for _, hi in boxes))
            keep["name_x_min"] = keep.get("name_x_min") if keep.get("name_x_min") is not None else drop.get("name_x_min")
            keep["name_x_max"] = keep.get("name_x_max") if keep.get("name_x_max") is not None else drop.get("name_x_max")
            if keep.get("name_y") is None:
                # type B: the name is attached to the line that carries the amounts
                keep["name_y"] = row_ref_y(keep) if keep["amounts"] else drop.get("name_y")
            removed.add(id(drop))
            report["rows"][f"wrapped_label_{kind}"] += 1
            grp[i:i + 2] = [keep]          # the merged row can chain with the next line
    if removed:
        rows[:] = [r for r in rows if id(r) not in removed]


# --------------------------------------------------------------------------- #
# Шаг 4d. Строки-заголовки колонок, принятые провайдером за суммы
# --------------------------------------------------------------------------- #
# Провайдер иногда отдаёт заголовок колонок как безымянную строку «сумм»: Moreno p6 — «(9 meses)
# (9 meses) (3 meses) (3 meses)» -> значения '(9', 'meses)', '(9', '(3'; в Excel выходила строка
# «9 / 9». Безымянная строка, в которой НЕТ НИ ОДНОЙ правильно записанной суммы (только текст,
# прочерки, обрывки со скобкой без пары), — это заголовок/мусор: помечаем `non_figure_line`, в JSON
# остаётся, в Excel и SBS не идёт. Строка с хотя бы одной нормальной суммой не трогается
# ('I' вместо прочерка рядом с суммами — обычная строка).
_FIGURE_RE = re.compile(r"^[\(\[]?[-−–]?\s?(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d{1,3}(?:[.,]\d{1,2})?)[\)\]]?$")


def is_wellformed_figure(value):
    """A printed amount: digits with thousands/decimal separators, parentheses balanced — or such
    an amount with a debit / credit suffix (`1.234,56D`, see dc_signed_value)."""
    v = _DC_SUFFIX.sub(lambda m: m.group(1), (value or "").strip())
    return bool(_FIGURE_RE.match(v)) and ((v[:1] in "([") == (v[-1:] in ")]"))


# Accounting-system dumps (balancete layout, Uniar / Benoit — audit 2026-11-04) print every balance
# figure with its DEBIT / CREDIT nature: «1.120.836.736,37D», «102.178.388,07 C». parse_amount reads
# none of them -> the whole Uniar balance (44 lines) and 3 Benoit contra-accounts vanished from the
# Excel. The sign is the side's normal balance: assets are debit-natured (D = +, C = − a contra
# account such as accumulated depreciation), liabilities / equity and the income statement are
# credit-natured (C = +, D = −: «AJUSTES DE AVALIAÇÃO PATRIMONIAL» D on the equity side). Rewritten in
# Stage 1 (like the US number format) so every downstream parser stays unchanged; the token as
# recognized stays in `value_raw`. A cash-flow statement has no D/C convention: left untouched.
_DC_SUFFIX = re.compile(r"^(.*\d)\s?([DC])$")
_DC_DEBIT_NORMAL = {"BALANCE_ASSETS_LST"}
_DC_CREDIT_NORMAL = {"BALANCE_LIABILITIES_LST", "INCOME_STATEMENT_LST"}


def dc_signed_value(value, statement):
    """'1.234,56D' -> '1.234,56' / '(1.234,56)' by the statement side's normal balance; any other
    value (or a cash-flow figure) is returned unchanged."""
    m = _DC_SUFFIX.match((value or "").strip())
    if not m or statement not in (_DC_DEBIT_NORMAL | _DC_CREDIT_NORMAL):
        return value
    body, nature = m.group(1).strip(), m.group(2)
    if not is_wellformed_figure(body) or body[:1] in "([-−–":
        return value
    normal = "D" if statement in _DC_DEBIT_NORMAL else "C"
    return body if nature == normal else f"({body})"


def tag_non_figure_lines(rows, report):
    for r in rows:
        if r.get("label") is None and r["amounts"] \
                and not any(is_wellformed_figure(a["value"]) for a in r["amounts"]):
            r["non_figure_line"] = True
            report["rows"]["non_figure_line"] += 1


# --------------------------------------------------------------------------- #
# Шаг 4e. ITR: строки других форм (ДВА / DMPL / DRA) — не отчёты, которые нужны
# --------------------------------------------------------------------------- #
# Коды CVM/B3 называют форму: 1/2 баланс, 3 DRE, 6 DFC — нужные; 4 DRA (совокупный доход), 5 DMPL
# (изменения капитала), 7 DVA (добавленная стоимость) — нет. Провайдер кладёт строки DVA в
# INCOME_/CASH_FLOW_LST, ITR игнорирует page_class -> в Excel Eztec / Copasa шли 29 / 36 строк DVA,
# а «Receitas» / «Receitas Financeiras» / «Dividendos» DVA получали SBS-поля выручки, финансовых
# доходов и дивидендов наравне с DRE / DFC. Страница, где большинство кодов 4 / 5 / 7, и любая
# строка с таким кодом помечаются `other_statement` (DRA / DMPL / DVA): в JSON остаются, в SBS и
# Excel не идут.
OTHER_STATEMENT_CODES = {"4": "DRA", "5": "DMPL", "7": "DVA"}


def tag_other_statements(rows, report):
    prefix = lambda r: str(r.get("code") or "").split(".")[0]
    by_page = defaultdict(Counter)
    for r in rows:
        if r.get("code"):
            by_page[r["page_nb"]][prefix(r)] += 1
    page_form = {pg: OTHER_STATEMENT_CODES.get(c.most_common(1)[0][0]) for pg, c in by_page.items()}
    for r in rows:
        form = OTHER_STATEMENT_CODES.get(prefix(r)) if r.get("code") else page_form.get(r["page_nb"])
        if form:
            r["other_statement"] = form
            report["rows"][f"other_statement_{form}"] += 1


# --------------------------------------------------------------------------- #
# Шаг 4c. SERASA: только Баланс и ОПУ (правило пользователя 2026-11-04)
# --------------------------------------------------------------------------- #
# Отчёт Serasa Experian после двух отчётов («BALANÇOS», «DEMONSTRAÇÕES DOS RESULTADOS») печатает
# аналитику (индексы, Evolução PL / Imobilizado, собственный «FLUXO DE CAIXA», Indicadores), строки
# которой провайдер тоже помечает как BALANCE_* / INCOME_*: в Excel serasa2 42 из 61 строки пассива
# были аналитикой. Шаблон фиксирован (проверено на 7 отчётах, зоны по заголовкам в OCR words —
# harness/ground_truth.yaml): ОПУ закрывается строкой «GIR DA ATIVIDADE (EBITDA)». Зона отчётов =
# от первой строки баланса до этой строки (+ строки сразу за ней). Слой words для этого не нужен.
SERASA_DRE_END = ("gir da atividade ebitda",)
SERASA_DRE_END_FALLBACK = ("resultado liquido",)
SERASA_TAIL_MAX_PITCHES = 2.0


def tag_serasa_analysis(rows, report):
    """Tag (never delete) every row outside the Serasa statement zone: `serasa_analysis = True`."""
    pitch, doc_pitch = page_line_pitch(rows)
    order = sorted((r for r in rows if r.get("page_nb") is not None and r.get("y") is not None),
                   key=lambda r: (r["page_nb"], r["y"]))
    start = next((i for i, r in enumerate(order) if r["statement"] in LST_GROUPS[:2]), None)
    if start is None:
        return
    first_inc = next((i for i in range(start, len(order))
                      if order[i]["statement"] == "INCOME_STATEMENT_LST"), None)
    end = None
    if first_inc is not None:
        for names in (SERASA_DRE_END, SERASA_DRE_END_FALLBACK):
            end = next((i for i in range(first_inc, len(order))
                        if normalize(order[i].get("label") or "") in names), None)
            if end is not None:
                break
    if end is None:
        end = len(order) - 1          # no closing line found -> cut nothing at the end
    else:
        # rows printed right after the closing line (same page, adjacent) still belong to the DRE
        while end + 1 < len(order):
            a, b = order[end], order[end + 1]
            lim = SERASA_TAIL_MAX_PITCHES * pitch.get(a["page_nb"], doc_pitch)
            if b["page_nb"] == a["page_nb"] and 0 <= b["y"] - a["y"] <= lim:
                end += 1
            else:
                break
    for i, r in enumerate(order):
        if i < start or i > end:
            r["serasa_analysis"] = True
            report["rows"]["serasa_analysis"] += 1


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


_PERSPECTIVES = ("Controladora", "Consolidado", "Individual")
PERSPECTIVE_HEADER_GAP_MIN = 0.03   # two perspective headers closer than this = one split phrase


def _perspectives_over(grp, page_headers, row_y):
    """How many DIFFERENT known perspectives the headers of the closest header line above the
    columns span (fragments of one header — «resultado» + «consolidado» — count once)."""
    if not page_headers or row_y is None:
        return 0
    lo = min(c["loc"]["x_min"] for c in grp)
    hi = max(c["loc"]["x_max"] for c in grp)
    over = [h for h in page_headers if h["y"] < row_y - 1e-9 and x_overlap(lo, hi, h["x_min"], h["x_max"]) > 0]
    if not over:
        return 0
    top = max(h["y"] for h in over)
    line = sorted((h for h in over if abs(h["y"] - top) <= HEADER_LINE_TOL), key=lambda h: h["x_min"])
    # ONE header naming both perspectives («Controladora e Consolidado» over 2025 | 2024, ALUBAR p47)
    # comes back as fragments — with or without the «e» — printed side by side; two real headers
    # stand over their own column pairs, a column width apart (São Salvador p25: 0.118)
    if re.search(r"(controladora|individual)\w*\s+e\s+consolidad", normalize(" ".join(h["raw"] for h in line))):
        return 1
    persp = [(h, normalize_account_type(h["raw"])) for h in line
             if normalize_account_type(h["raw"]) in _PERSPECTIVES]
    if len({k for _, k in persp}) >= 2 and all(
            b["x_min"] - a["x_max"] < PERSPECTIVE_HEADER_GAP_MIN
            for (a, ka), (b, kb) in zip(persp, persp[1:]) if ka != kb):
        return 1
    return len({k for _, k in persp})


def _pair_types(cols, page_headers, row_y, report, periods=None):
    """
    Правило пары: для каждой пары колонок строки выбираем заголовок:
        кандидаты — заголовки ВЫШЕ строки (y_header < y_row);
        среди пересекающихся по x с диапазоном пары берём ближайший сверху;
        фрагменты на одной высоте, попавшие в пару, склеиваем.
    `cols` — суммы строки по x. Возвращает [(account_type, match)] в том же порядке. `periods`
    подменяет периоды при спаривании (след зоны: какой тип дало бы правило без зоны); `report` None —
    без счётчиков.
    """
    items = [{"period": p, "loc": c["loc"], "_i": i}
             for i, (c, p) in enumerate(zip(cols, periods if periods is not None else [c["period"] for c in cols]))]
    groups_of_cols = []
    for grp in pair_columns(items):
        # A «pair» spanning the headers of two DIFFERENT perspectives is not a pair: São Salvador p25
        # (audit 2026-11-04, SSA6) — the Controladora 2024 dash read as «1» (x 0.616) paired with the
        # Consolidado 2023 (20) (x 0.874): the joined header «Controladora Consolidado» typed both
        # Controladora and the Consolidado filter dropped the line. Each cell then takes its own header.
        if len(grp) == 2 and _perspectives_over(grp, page_headers, row_y) >= 2:
            groups_of_cols += [[grp[0]], [grp[1]]]
        else:
            groups_of_cols.append(grp)

    out = [None] * len(cols)
    for grp in groups_of_cols:
        span_min = min(c["loc"]["x_min"] for c in grp)
        span_max = max(c["loc"]["x_max"] for c in grp)

        acc_type = None
        confidence = None
        if page_headers and row_y is not None:
            above = [h for h in page_headers if h["y"] < row_y - 1e-9]
            # Заголовки, пересекающиеся по x с диапазоном пары.
            overlapped = [(h, x_overlap(span_min, span_max, h["x_min"], h["x_max"]))
                          for h in above]
            overlapped = [(h, ov) for h, ov in overlapped if ov > 0]

            if overlapped:
                # Ближайшая сверху строка заголовков (max y среди пересекающихся).
                top_y = max(h["y"] for h, _ in overlapped)
                same_row = [h for h, _ in overlapped if abs(h["y"] - top_y) <= HEADER_LINE_TOL]
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
                if report is not None:
                    report["account_type"]["fallback_nearest"] += 1
            elif report is not None:
                report["account_type"]["no_header_above"] += 1
        elif not page_headers and report is not None:
            report["account_type"]["page_without_header"] += 1
        for c in grp:
            out[c["_i"]] = (acc_type, confidence)
    return out


def assign_account_types(row, headers, report, zones=None):
    """
    account_type and period of every figure of the row -> its output columns (sorted by x).
    A figure of a header-zone column (Step 2c'; a figure the OCR-words step added later is looked up by
    its right edge) takes the perspective and period of its zone; every other figure keeps the pair rule
    (`_pair_types`). A zone perspective that differs from what the pair rule gives without the zones
    is traced like a period: `account_type_corrected` + `account_type_src`.
    """
    cols = sorted(row["amounts"], key=lambda a: xc(a["loc"]))
    page_headers = headers.get(row["page_nb"], [])
    zone = []
    for c in cols:
        z = c.get("zone")
        if z is None and zones and c.get("src") == "ocr_words":
            z = zone_of(zones, row["page_nb"], c["loc"]["x_max"], xc(c["loc"]))
        zone.append(z)
    rest = [c for c, z in zip(cols, zone) if z is None]
    typed = dict(zip(map(id, rest), _pair_types(rest, page_headers, row["y"], report)))
    prior = None
    if len(rest) < len(cols):
        # the perspective the pair rule gives WITHOUT the header zones (periods as Step 2c left them)
        prior = _pair_types(cols, page_headers, row["y"], None,
                            [c.get("unzoned_period", c["period"]) for c in cols])

    out_columns = []
    for i, c in enumerate(cols):
        if zone[i] is not None:
            (acc_type, period), confidence = zone[i], "zone"
            report["account_type"]["zone"] += 1
        else:
            (acc_type, confidence), period = typed[id(c)], c["period"]
        col = {
            "account_type": acc_type,
            "period": period,
            "value": c["value"],
            "x": round(xc(c["loc"]), 4),
            # debug-only geometry (removed from normal output in build_output)
            "x_max": round(c["loc"]["x_max"], 4),
            "y": round(yc(c["loc"]), 4),
            "_match": confidence,
        }
        provider = c.get("provider_period") if zone[i] is not None else None
        if provider is not None:
            if period != provider:
                # the header zone overrode the provider's period tag (kept for traceability)
                col["period_corrected"] = True
                col["period_src"] = provider
        elif c.get("period_src"):
            # the column grid overrode the provider's period tag (kept for traceability)
            col["period_corrected"] = True
            col["period_src"] = c["period_src"]
        if zone[i] is not None and c.get("src") != "ocr_words" and prior[i][0] != acc_type:
            # the header zone overrode the perspective of the pair rule (kept for traceability)
            col["account_type_corrected"] = True
            col["account_type_src"] = prior[i][0]
            report["account_type"]["zone_type_changed"] += 1
        if c.get("src") == "ocr_words":
            # recovered from the OCR words layer (words_recovery step, on by default)
            col["source"] = "ocr_words"
        out_columns.append(col)
    return out_columns


# --------------------------------------------------------------------------- #
# Сборка результата
# --------------------------------------------------------------------------- #
def build_output(rows, headers, report, keep_debug, classification=None, number_format="BR", zones=None):
    result = {g: [] for g in LST_GROUPS}
    # Сортировка строк: страница -> y (сверху вниз).
    for r in sorted(rows, key=lambda r: (r["page_nb"], r["y"] if r["y"] is not None else 0)):
        stmt = r["statement"]
        if stmt not in result:
            # orphan-строка без определённого statement — пропускаем из 4 групп.
            report["rows"]["dropped_no_statement"] += 1
            continue
        columns = assign_account_types(r, headers, report, zones)
        for c in columns:
            # debit / credit suffix (accounting-system dump): signed by the statement side
            nv = dc_signed_value(c["value"], stmt)
            if nv != c["value"]:
                c["value_raw"] = c["value"]
                c["value"] = nv
                report["columns"]["dc_suffix_signed"] += 1
        if number_format == "US":
            # US-style document ('193,081'): rewrite in Brazilian notation for every downstream
            # parser; the string exactly as recognized stays in value_raw.
            for c in columns:
                nv = to_br_number(c["value"])
                if nv != c["value"]:
                    c["value_raw"] = c["value"]
                    c["value"] = nv
        if not keep_debug:
            for c in columns:
                c.pop("x", None)
                c.pop("x_max", None)
                c.pop("y", None)
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
        if r.get("serasa_analysis"):
            # outside the Serasa statement zone (analysis section): kept, never exported
            row_obj["serasa_analysis"] = True
        if r.get("other_statement"):
            # an ITR row of another form (DVA / DMPL / DRA): kept, never labelled nor exported
            row_obj["other_statement"] = r["other_statement"]
        if r.get("label_parts"):
            # the printed name ran over several lines: label = joined parts (see merge_wrapped_labels)
            row_obj["label_parts"] = r["label_parts"]
        if r.get("non_figure_line"):
            # a column-header line the provider returned as figures: kept, never exported
            row_obj["non_figure_line"] = True
        for k in ("source", "label_source"):
            if r.get(k):
                # the row / its label was recovered from the OCR words layer (words_recovery)
                row_obj[k] = r[k]
        if keep_debug:
            row_obj["y"] = round(r["y"], 4) if r["y"] is not None else None
            if r.get("name_y") is not None:
                row_obj["name_y"] = round(r["name_y"], 4)
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
def run_classification(data, report, panel_of=None):
    """
    Stage-2: classify each page (PRIMARY / NOTE / JUNK). Returns page->verdict
    or None if the classifier/anchors.yaml are unavailable. Failures are soft —
    Stage-1 reconstruction is never blocked by classification.
    `panel_of(page, x)`: Stage 1's side-by-side panels — the column model counts per panel.
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
        classification = clf.classify(data, cfg, panel_of=panel_of)
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


def run_sbs_fields(output, report):
    """Stage-3 end: the per-field SBS values (sbs_fields.compute) on the assembled output.
    Soft failure — never blocks the statements."""
    if not (_SBS_AVAILABLE and _SBS_FIELDS_AVAILABLE):
        return None
    mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbs_label_mapping.json")
    try:
        with open(mapping_path, encoding="utf-8") as f:
            fields_cfg = json.load(f).get("fields", {})
        res = sbs_fields.compute(output, fields_cfg)
        report["sbs"]["fields_valued"] = len(res["fields"])
        report["sbs"]["identity_ok"] = sum(1 for c in res["checks"] if c["ok"])
        report["sbs"]["identity_failed"] = sum(1 for c in res["checks"] if not c["ok"])
        return res
    except Exception as exc:  # pragma: no cover
        report["sbs"]["fields_error"] += 1
        report["sbs_fields_error"] = str(exc)
        return None


def process(data, keep_debug=False, words=None):
    """Stages 1-3. `words` = the reciTAL OCR words layer (list per page of {word, coordinates}),
    OPTIONAL: when given, words_recovery restores lost cells / lines / labels (flagged
    `source`/`label_source: ocr_words`); when None the output is exactly the words-free one."""
    groups = load_groups(data)
    report = new_report()

    metadata = normalize_metadata(extract_metadata(data))
    if words is not None and _WORDS_AVAILABLE:
        words_recovery.recover_metadata(metadata, words, report)   # OPTIONAL (words layer)
    account_type = extract_account_type(data)
    if account_type is not None:
        metadata["ACCOUNT_TYPE"] = account_type
    rows, lst_pages, lst_amount_keys = build_row_anchors(groups)
    # Stage 2 counts columns per physical PANEL of a side-by-side page (Ativo | Passivo): the
    # panels come from the provider's row anchors, before any Stage-1 re-placement.
    panels0 = build_panels(rows, make_row_tol(rows))
    classification = run_classification(data, report, lambda pg, x: panel_of(pg, x, panels0))
    loose = detach_offline_amounts(rows, report)
    orphans = augment_with_only_amount(groups, rows, lst_pages, lst_amount_keys, report, loose)
    materialize_orphans(orphans, rows, report)
    correct_periods_by_column_grid(rows, report)
    # step 2c': where the table header is unambiguous, the period and perspective of every figure come
    # from the zone (column) it stands in — CLOSING_DATE + ACCOUNT_TYPE frames; elsewhere Step 2c stands
    zones = apply_header_zones(rows, groups, metadata, report)
    resolve_extra_period_column(rows, report)
    attach_codes(groups, rows, report)
    merge_wrapped_labels(rows, report)
    tag_non_figure_lines(rows, report)
    tag_other_statements(rows, report)
    number_format = detect_number_format(a["value"] for r in rows for a in r["amounts"])
    report["number_format"] = number_format
    infer_unit_from_cents(metadata, rows)
    if words is not None:
        # words layer (on by default; absent -> the words-free fallback) — see words_recovery.py
        if _WORDS_AVAILABLE:
            # the header-zone columns join the words grid: a column the header prints is a column
            # even when the extraction left it almost empty (Cibra p7 Consolidado 2024)
            words_recovery.recover(rows, words, report, sys.modules[__name__], number_format, zones=zones)
        else:
            report["words"] = Counter({"skipped_no_module": 1})
    if provider_tag(metadata) in PROVIDER_TAGS_NORMALIZED:
        tag_serasa_analysis(rows, report)
    headers = build_account_headers(groups, lst_pages)
    statements = build_output(rows, headers, report, keep_debug, classification, number_format, zones)
    if _SIGN_PROOF_AVAILABLE:
        # step 5b: a lost minus / a dash read as «1», repaired only where the arithmetic proves it
        sign_proof.repair(statements, report)

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
        "number_format": number_format,
        "statements": statements,
    }
    if classification is not None:
        # Top-level per-page classification + a compact summary for filtering.
        output["page_classification"] = {
            str(pg): _public_verdict(v) for pg, v in sorted(classification.items())
        }
        output["classification_summary"] = clf.summarize(classification)
    fields = run_sbs_fields(output, report)
    if fields is not None:
        output["sbs_fields"] = fields
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
    if report.get("words"):
        block("words (OCR words-layer recovery)", report["words"])
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


def _input_base(input_path):
    """`dir/<id>_raw.json` -> `dir/<id>`; `dir/<name>.json` -> `dir/<name>` (CLI defaults below)."""
    folder, name = os.path.split(input_path)
    stem = os.path.splitext(name)[0]
    if stem.endswith("_raw"):
        stem = stem[:-len("_raw")]
    return os.path.join(folder, stem)


def default_words_path(input_path):
    """The words layer that goes with an input file (CLI default, words on by default since
    2026-11-12): `<id>_raw.json` -> `<id>_words.json` (the pair `harness/recital_fetch.py export`
    writes), `<name>.json` -> `<name>_words.json`. Returns (path or None if absent, expected path)."""
    expected = _input_base(input_path) + "_words.json"
    return (expected if os.path.isfile(expected) else None), expected


def default_output_path(input_path):
    """CLI default output, next to the input: `<id>_raw.json` -> `<id>_structured.json`, `<name>.json`
    -> `<name>_structured.json`. Never a golden: the goldens (`structured_output*.json`) are written
    only with an explicit -o — the old default `structured_output.json` silently overwrote the main
    golden on any run without -o (fixed 2026-11-12)."""
    return _input_base(input_path) + "_structured.json"


def main():
    # Консоль Windows по умолчанию cp1252 — переключаем на UTF-8 для кириллицы.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")   # argparse errors are in Russian too
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Структуризация OCR-JSON финансовой отчётности.")
    ap.add_argument("input", nargs="?", default="comprehansive_sample.json",
                    help="входной OCR-JSON")
    ap.add_argument("-o", "--output", default=None,
                    help="выходной JSON (по умолчанию <имя входа>_structured.json рядом со входом; "
                         "эталоны structured_output*.json пишутся только явным -o)")
    ap.add_argument("--debug", action="store_true",
                    help="сохранять координаты (x/y) и метку сопоставления для сверки")
    ap.add_argument("--words", metavar="WORDS_JSON",
                    help="слой OCR words reciTAL (GET /files/{id}/words/) — восстановление потерянных "
                         "ячеек / строк / названий (words_recovery.py). По умолчанию берётся сам: "
                         "<имя>_words.json рядом со входом (<id>_raw.json -> <id>_words.json)")
    ap.add_argument("--no-words", action="store_true",
                    help="запасной режим без слоя words (как если бы reciTAL его не отдал)")
    args = ap.parse_args()
    if args.words and args.no_words:
        ap.error("--words и --no-words несовместимы")
    if args.output is None:
        args.output = default_output_path(args.input)

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    words = None
    words_path, expected = (args.words, args.words) if args.words else default_words_path(args.input)
    if words_path and not args.no_words:
        with open(words_path, encoding="utf-8") as f:
            words = json.load(f)
        n_pages = len(words) if isinstance(words, list) else "?"
        print(f"Слой OCR words: {words_path} ({n_pages} стр.)")
    else:
        why = "--no-words" if args.no_words else f"нет файла {expected}"
        print(f"Слой OCR words: не используется ({why}) -> запасной режим без words")

    output, report = process(data, keep_debug=args.debug, words=words)

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
