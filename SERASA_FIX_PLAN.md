# SERASA Fix Plan — 5 issues found on serasa_report_1/2

> Рабочий план для будущей сессии. Всё необходимое для исполнения — здесь:
> симптом, доказательства (с цифрами), корневая причина, точечная правка
> (файл + функция + строки), регрессионные риски и протокол проверки.
> Связанные docs: `CLAUDE.md`, `RUNBOOK.md`, память `[[br-sbs-labelling]]`,
> `[[br-excel-export]]`, `[[br-ocr-json-structure]]`.

## 0. Контекст

Два новых документа провайдера SERASA:

```
serasa_report_1.json  -> structured_output_serasa1.json + serasa_report_1.xlsx
serasa_report_2.json  -> structured_output_serasa2.json + serasa_report_2.xlsx
```

Прогон (Stage 1+2+3) и Excel уже сделаны. Оба сейчас детектятся как **UPPERCASE**.
Структура входа — стандартная (root = list of 11, см. `CLAUDE.md` п.1).

**Регрессионный baseline снят** до правок: `_baseline.json` (скрипт
`_baseline_snapshot.py`) — по каждой строке всех 8 голденов зафиксированы
`label / sbs_label / method / scope / blocked / needs_review`.

Текущие счётчики baseline (rows / sbs / needs_review):

| Golden | rows | sbs | NR |
|---|---|---|---|
| structured_output.json (DFP) | 1468 | 74 | 136 |
| structured_output_sample2.json | 253 | 32 | 8 |
| structured_output_itr.json | 302 | 69 | 3 |
| structured_output_provider.json | 53 | 27 | 6 |
| structured_output_complicated.json | 58 | 22 | 4 |
| structured_output_complicated_sample2.json | 90 | 25 | 9 |
| structured_output_serasa1.json | 70 | 25 | 5 |
| structured_output_serasa2.json | 98 | 23 | 6 |

---

## Сводка: 5 пунктов

| # | Что | Файл(ы) | Корень | Связь |
|---|---|---|---|---|
| 1 | Заменить UPPERCASE на тип **SERASA** | `structure_ocr.py`, `export_excel.py` | сигнал `ACCOUNT_TYPE="SERASA EXPERIAN"` теряется | самостоятельный |
| 2 | Долг ST/LT не разрешается при разрыве таблицы по страницам | `sbs_labeler.py` | `_compute_sections` группирует по страницам | общий корень с 3,4 |
| 3 | `APLICACOES FINANCEIRAS` (non-current) → cash вместо financialAssets | `sbs_labeler.py` | bare `circulante` трактуется как header, не subtotal | общий корень с 2,4 |
| 4 | Долг ST/LT «раз и навсегда», без under-review | `sbs_labeler.py` | то же, что 2 (+ детерминированный фоллбэк) | общий корень с 2,3 |
| 5 | `RESULT OPERACIONAL` (= pretax по значению) ложно → operatingProfit | `sbs_labeler.py` (+ опц. mapping) | fuzzy-алиас `resultado operacional`; коллизия значений с pretax | самостоятельный |

> **Ключевой инсайт:** пункты **2, 3, 4 — один корень** в `_compute_sections`
> (`sbs_labeler.py:520`). Разумно чинить их одной правкой секционной логики.

---

## Пункт 1 — детекция типа SERASA (вместо UPPERCASE)

### Симптом
Документы SERASA попадают в ветку UPPERCASE. Это работает по совпадению (правила
обработки те же), но тип назван неверно и зависит от хрупкого признака «≥50% меток
капсом».

### Доказательства
В сыром входе есть группа `ACCOUNT_TYPE`, элемент списка `[3]`:

```json
{ "group_name": "ACCOUNT_TYPE",
  "subgroups": [{ "page_nb": 1,
    "values": [{ "value": "SERASA EXPERIAN", "confidence": 0.9996,
      "location": { "x_min": 0.119, "x_max": 0.291, "y_min": 0.178, "y_max": 0.193 } }] }] }
```

Скан `ACCOUNT_TYPE` по всем входам (скрипт-проба) — признак специфичен:

| Вход | ACCOUNT_TYPE values |
|---|---|
| comprehansive_sample (DFP) | Controladora / Consolidado / Resultado / segmentado … |
| sample_2 | Consolidado / Controladora / Resultado |
| itr_samples | Individuais / Consolidadas |
| provider_sample | **SERASA EXPERIAN** |
| complicated_* | (пусто) |
| **serasa_report_1 / _2** | **SERASA EXPERIAN** |

То есть `"SERASA EXPERIAN"` встречается ровно у `provider_sample` + двух serasa.

### Почему сигнал теряется сейчас
- `structure_ocr.py:68` — `METADATA_NAMES = ("CLOSING_DATE","DURATION","CURRENCY","UNIT")`
  — `ACCOUNT_TYPE` не входит → `extract_metadata` (строка 138) его не поднимает.
- `build_account_headers` (`structure_ocr.py:416`) берёт ACCOUNT_TYPE-subgroups
  **только на LST-страницах** (строки 423-425); `SERASA EXPERIAN` сидит на стр. 1
  (не LST) → отбрасывается.
- В итоге в `structured_output*.json → metadata` нет ни `ACCOUNT_TYPE`, ни провайдера.

### Что уже готово в export_excel.py
- `META_ORDER` / `META_PRETTY` (строки 96-103) **уже** содержат `ACCOUNT_TYPE`.
- `build_metadata_sheet` (строка 296) уже умеет показать `ACCOUNT_TYPE`, если он есть
  в metadata.
- Значит достаточно довести значение до metadata и добавить ветку детекции.

### Правка

**A. `structure_ocr.py` — поднять провайдера в metadata.**
Не расширять `METADATA_NAMES` (оно завязано на форму scalar/group и x-геометрию
заголовков). Вместо этого добавить отдельную мелкую функцию извлечения, например:

```python
PROVIDER_VALUES = ("serasa experian",)  # расширяемо

def extract_account_type(data):
    """ACCOUNT_TYPE как метку источника (provider), независимо от страницы.
    Возвращает лучший по confidence value или None."""
    best = None
    for el in data:
        if el.get("group_name") != "ACCOUNT_TYPE":
            continue
        for sg in el.get("subgroups", []):
            for v in sg.get("values", []):
                conf = v.get("confidence") or 0
                if best is None or conf > best[1]:
                    best = (v.get("value"), conf, v.get("page_nb", sg.get("page_nb")))
    return best  # (value, conf, page) | None
```

Положить результат в `metadata["ACCOUNT_TYPE"] = {"value", "raw", "page_nb", "confidence"}`
(тот же формат, что прочие поля — `build_metadata_sheet` это ждёт). Вызвать рядом с
`extract_metadata(...)` в основном orchestration-блоке (см. `main`/сборку output).

**B. `export_excel.py` — новый тип SERASA.**
В `detect_doc_type` (строка 114) — но сигнатуры там читаются из `statements`, а
провайдер живёт в `metadata`. Поэтому сменить сигнатуру на
`detect_doc_type(statements, metadata)` и в `build_workbook` (строка 434) передать
`metadata`. Порядок детекции (первое совпадение): **SERASA первым**, т.к. он точный:

```python
def detect_doc_type(statements, metadata=None):
    at = ((metadata or {}).get("ACCOUNT_TYPE") or {}).get("value") or ""
    if "serasa" in at.lower():
        return "SERASA"
    ... (далее ITR / UPPERCASE / DFP_4COL / TWO_COL как сейчас)
```

Обработка SERASA = идентична UPPERCASE:
- `_consolidado_only`: `doc_type in ("ITR","DFP_4COL")` — SERASA НЕ входит → все колонки.
- `_primary_only`: `doc_type in ("DFP_4COL","TWO_COL")` — SERASA НЕ входит → page_class игнор.

То есть достаточно НЕ добавлять SERASA в эти два множества — поведение «все колонки,
без фильтра page_class» получится автоматически. Обновить docstring/таблицу вверху
файла (строки 16-33) и комментарии.

> Альтернатива (если не хотим менять сигнатуру `detect_doc_type`): прокинуть provider
> в `statements` не получится чисто; менять сигнатуру правильнее.

### Регрессионный риск
- `provider_sample.json` сейчас UPPERCASE → станет SERASA. Поведение фильтров
  идентично, но **проверить**, что Excel-вывод provider не изменился по содержимому
  (тип в шапке поменяется — это ОК).
- Прочие голдены провайдера не имеют → их тип не меняется.

### Проверка
- `detect_doc_type` на serasa1/serasa2/provider → `SERASA`; на DFP/ITR/sample2/
  complicated → как раньше.
- В `structured_output_serasa*.json → metadata.ACCOUNT_TYPE.value == "SERASA EXPERIAN"`.
- В metadata-листе Excel появляется строка «Account type · Tipo de conta = SERASA EXPERIAN».

---

## Пункт 2 + 4 — долг ST/LT раз и навсегда (page-split + детерминизм)

### Симптом
`EMPREST E FINANCIAMENTOS` встречается дважды (текущий + долгосрочный) — это twin
`financialLiabilities{Short,Long}Term`.
- **SERASA-1**: разрешается корректно (`positional`): ST=50.158, LT=34.535.
- **SERASA-2**: обе строки `sbs_label=null`, `unresolved_ambiguous`, `needs_review`.

### Доказательства
SERASA-2 пассивы разорваны OCR между страницами:

```
[0] p3 FORNECEDORES ... [1] p3 EMPREST E FINANCIAMENTOS (curr)
[2] p3 SAL TR CONTR ...                       <-- разрыв страницы
[5] p4 OUTROS DEBITOS [6] p4 PASSIVO CIRC  [7] p4 EMPREST E FINANCIAMENTOS (LT)
[10] p4 EXIGIVEL L PRAZO ...
```

`_compute_sections` трасса по SERASA-2 → **все** строки `sect=None` (в SERASA-1 — всё
проставлено). Причина — постраничная группировка.

Арифметика блоков сходится идеально (значит данных достаточно для детерминизма):

```
PASSIVO CIRC (N)  = 75.248+166.946+8.429+0+0+74.848 = 325.471 == 325.471  ✓
EXIGIVEL L PRAZO  = 268.333+0+279.138               = 547.471 == 547.471  ✓
```

### Корневая причина
`_compute_sections` (`sbs_labeler.py:520`) группирует строки по `page_nb`:

```python
# строки 551-553
page_spans = defaultdict(list)
for i in range(n):
    page_spans[pages[i]].append(i)
for _, idxs in page_spans.items():
    ... forward / backward / arithmetic — всё в пределах одной страницы
```

Когда одна логическая таблица (бакет `BALANCE_LIABILITIES_LST`) разбита OCR на стр.
3/4/5, арифметический gap-fill (`_fill_sections_by_arithmetic`, строка 480) не может
сложить детальные строки одного блока, т.к. они на разных страницах. Маркеры
(`PASSIVO CIRC`, `EXIGIVEL L PRAZO`) у SERASA сокращённые → лексические сеты
`_SECTION_MARKERS` их не ловят как subtotal → секция остаётся None → `_resolve_twin`
доходит до пункта (4) и отдаёт `None` + needs_review.

### Правка (общая с пунктом 3) — стратегия

> **Рекомендуемый подход: ветка по форме документа.**
> Для семейства SERASA/provider (UPPERCASE-дамп, без CODE_VALUE, сокращённые маркеры)
> арифметика — более надёжный источник секций, чем лексические маркеры, и она уже
> доказанно корректна. Для ITR/DFP оставить текущую логику (маркеры + коды) без
> изменений.

В `_compute_sections`:
1. Определить «SERASA/provider-shape бакет»: ни у одной строки нет `code`, и
   маркер-проходы (forward+backward) оставили **все** строки `None` ИЛИ бакет
   охватывает >1 страницы при отсутствии `total do …`-subtotal'ов.
2. Для такого бакета считать секции **по всему бакету целиком** (объединить страницы,
   сохранив порядок) и пустить `_fill_sections_by_arithmetic` бакет-широко.
3. Иначе — текущее постраничное поведение (НЕ трогаем ITR/DFP/sample2).

Минимальная реализация: вынести выбор «span» в начале цикла —

```python
serasa_shape = (
    scope in ("BALANCE_ASSETS_LST","BALANCE_LIABILITIES_LST")
    and all(not r.get("code") for r in rows)
    and not _has_total_do_markers(rows, scope)   # нет лексических subtotal'ов
)
spans = [list(range(n))] if serasa_shape else list(page_spans.values())
```

(`_has_total_do_markers` — проверка пересечения нормализованных меток со
`subtotal_*`-сетами; для SERASA вернёт False, для DFP/sample2 — True → постранично.)

### Пункт 4 «без under-review» — детерминированный финал twin
После того как секции считаются бакет-широко, SERASA-2 получит `current/noncurrent`
по арифметике, и `_resolve_twin` (`sbs_labeler.py:299`) дойдёт до пункта (3)
`positional` → разрешит ST/LT детерминированно (как SERASA-1).

Дополнительно, чтобы «раз и навсегда» (никогда `unresolved` на SERASA-долге):
- В twin-стадии для SERASA-shape ввести **детерминированный фоллбэк по порядку**:
  если секция всё ещё None (крайний случай), первая встреченная пара
  `EMPREST E FINANCIAMENTOS` на/над `PASSIVO CIRC` = ST, последующая (над
  `EXIGIVEL L PRAZO`) = LT (порядок current→non-current). Это уже частично заложено в
  `_fill_sections_by_arithmetic` (nonequity counter), нужно лишь гарантировать, что
  twin не остаётся `unresolved_ambiguous` на этом семействе.
- Снять `needs_review` для так разрешённого долга (детерминированный кейс, не гадание).

### Регрессионный риск (высокий — общий код!)
- DFP balance — много страниц-копий (стр. 20/49/51/65), segment-копии. Если по ошибке
  включить «бакет-широко» для DFP → арифметика может сложить через копии. **Защита:**
  `_has_total_do_markers` у DFP = True → остаётся постранично. **Обязательно проверить**
  diff DFP против baseline (74 sbs, 136 NR не должны деградировать).
- ITR — есть коды → `serasa_shape=False` → не трогается. Проверить diff (69 sbs / 3 NR).
- provider_sample — SERASA-shape, сейчас ST/LT уже резолвятся positional. Проверить, что
  по-прежнему резолвятся (27 sbs / 6 NR).

### Проверка
- SERASA-2: `BALANCE_LIABILITIES_LST` — обе `EMPREST E FINANCIAMENTOS` → ST/LT,
  method=positional, `needs_review` снят.
- SERASA-1: без изменений (ST=50.158, LT=34.535).
- diff всех 8 голденов против `_baseline.json` — изменения ТОЛЬКО в serasa1/serasa2
  (и осознанные в serasa), DFP/ITR/sample2/provider/complicated — без регресса.

---

## Пункт 3 — реклассификация cash → financialAssets в non-current

### Симптом
SERASA-1 `BALANCE_ASSETS_LST[6] APLICACOES FINANCEIRAS` (343/493) →
`cashAndSecuritiesAmount` (current), `needs_review`, candidates обе. По смыслу это
non-current → должно быть `financialAssetsAmount`.

### Доказательства
Арифметика относит строку к non-current блоку:

```
APLICACOES FINANCEIRAS + OUTROS CREDITOS = REALIZAVEL L PRAZO (non-current subtotal)
  N-1: 343 + 33.938 = 34.281 == 34.281  ✓
  N  : 493 + 33.938 = 34.431 == 34.431  ✓
```

twin `securities`: current=`cashAndSecuritiesAmount`, noncurrent=`financialAssetsAmount`,
shared aliases включают `aplicacoes financeiras` (есть у ОБОИХ twin'ов).

Трасса маркеров по SERASA-1 assets:

```
[5] ativo circulante        -> HDR_CUR (amt=154.411 — на самом деле SUBTOTAL!)
[6] aplicacoes financeiras  -> (нет маркера) sect=current   <-- БАГ
[7] outros creditos         -> sect=current
[8] realizavel l prazo      -> (нет маркера) sect=current
[9] imobilizado             -> sect=current
[10] ativo nao circulante   -> HDR_NON (amt=235.595)
```

### Корневая причина
Forward-проход (`sbs_labeler.py:556-564`) трактует bare `ativo circulante` (в сете
`header_current`, строки 167) как **открывающий заголовок** → ставит `current` всем
строкам ниже, пока не встретит `ativo nao circulante` (строка [10]). Но в SERASA
`ATIVO CIRCULANTE` — это **закрывающий субтотал** (несёт сумму 154.411 = сумма строк
0-5), а строки [6]-[9] — уже non-current блок (закрывается `ATIVO NAO CIRCULANTE`).
Арифметический проход не может исправить (он gap-fill-only, строка 512: «never override»).

Подтверждение, что bare-маркер у SERASA несёт сумму (= субтотал, не заголовок):
проба показала `ativo circulante` amt=154.411 (SERASA-1), 198.137 (SERASA-2) — реальные
суммы блока. (В ITR эти строки тоже несут суммы, но там twin'ы резолвятся по CODE, а не
позиционно, поэтому ITR не затронут.)

### Правка
Часть **общей секционной правки** (см. пункт 2). Для SERASA/provider-shape бакета
арифметика становится авторитетной:
- `_fill_sections_by_arithmetic` уже корректно классифицирует: строка, чей `AMOUNT_N`
  равен сумме предыдущих contiguous detail-строк, — это субтотал, закрывающий блок;
  блоки нумеруются current→noncurrent (assets: 1-й=current, далее=noncurrent).
- Нужно лишь, чтобы forward/backward маркер-проход **не перебивал** это на SERASA-shape.
  Если идём «бакет-широко + arithmetic-first» (как в пункте 2), bare `ativo circulante`
  не будет ставить `current` строкам ниже — секции возьмутся из арифметики:
  `ATIVO CIRCULANTE` закроет current-блок (0-5), `[6]-[9]` станут noncurrent,
  `ATIVO NAO CIRCULANTE` закроет noncurrent.

Тогда `_stage_twin_section` → `_resolve_twin` для `APLICACOES FINANCEIRAS`:
`positional_section == "noncurrent"` → **`financialAssetsAmount`** ✓.

> Это ровно «реклассификация cash→financial assets в non-current», о которой говорил
> юзер («тут гадать не надо — надо делать»): после правки секций она происходит
> детерминированно из позиции, без угадывания.

### Регрессионный риск
- Другие документы, где `APLICACOES FINANCEIRAS`/`Títulos e valores mobiliários` стоит
  в **current** блоке (DFP стр. 20: `titulos e valores mobiliarios` current →
  cashAndSecurities) — НЕ должны сломаться. DFP не SERASA-shape (есть `total do …`) →
  постранично, без изменений. **Проверить** DFP assets twin'ы против baseline.
- complicated_sample_2 `APLICACAO FINANCEIRA` (sec=current сейчас, positional) — это
  SERASA-shape? У него нет кодов и капс. **Проверить**, что после правки он остаётся
  current (если он реально в current-блоке — арифметика должна это подтвердить; если
  нет — это может быть осознанным исправлением, сверить с фактической формой).

### Проверка
- SERASA-1 `APLICACOES FINANCEIRAS` → `financialAssetsAmount`, method=positional,
  scope=in_scope; снять `needs_review` (детерминированная позиция).
- DFP `titulos e valores mobiliarios` (current) → остаётся `cashAndSecuritiesAmount`.

---

## Пункт 5 — `RESULT OPERACIONAL` (= pretax) ложно → operatingProfit

### Симптом
Один SBS-филд `operatingProfitAmount` назначен двум строкам income:
- `RESULT ATIVIDADE` (S1: 64.860) — dict-hit, score 100 — **корректно** (это EBIT/операционная).
- `RESULT OPERACIONAL` (S1: 25.488) — fuzzy 92.3 — **ложно**.

### Доказательства
`RESULT OPERACIONAL` по значению **точно равен** `RESULTADO ANTES IR` (pretax):

```
S1: RESULT OPERACIONAL = 25.488 / 9.808 ; RESULTADO ANTES IR = 25.488 / 9.808  (==)
S2: RESULT OPERACIONAL = 180.594 / 137.280 ; RESULTADO ANTES IR = 180.594 / 137.280  (==)
```

То есть в шаблоне SERASA `RESULT OPERACIONAL` — это строка ПОСЛЕ финансового результата
= прибыль до налога, а операционная (до фин. результата) — это `RESULT ATIVIDADE`.

Mapping: `operatingProfitAmount.aliases` содержит и `result atividade`, и
`resultado operacional` / `lucro operacional` / `resultado operacional liquido`.
tiers: operatingProfit=5, pretaxProfit=7. `pretaxProfitAmount` ловит
`RESULTADO ANTES IR` через dict (`resultado antes ir`).

### Корневая причина
Алиас `resultado operacional` у `operatingProfitAmount` семантически неоднозначен
(в бразильском DRE «resultado operacional» исторически = lucro antes do IR = pretax).
Fuzzy ловит `RESULT OPERACIONAL` на этот алиас, давая дубль operating, хотя строка —
pretax. Дедуп (Stage 5) не срабатывает: ключ включает `normalize(label)`, а метки
разные (`RESULT ATIVIDADE` ≠ `RESULT OPERACIONAL`).

### Правка — рекомендуемая (value-collision демоция)
Новый мелкий чек (в `_stage_struct_validate` или отдельной мини-стадии PASS 2). Логика:

> Если строка матчила `operatingProfitAmount` **через fuzzy** (не dict) И её AMOUNT-
> значения совпадают со значениями строки, матчившей `pretaxProfitAmount` (dict) на той
> же странице/в том же income-бакете — это ложный operating: снять label
> (`sbs_label=null`, `sbs_blocked_reason="value_equals_pretax"`), т.к. истинная
> операционная (`RESULT ATIVIDADE`) уже захвачена dict'ом.

Псевдокод:

```python
inc = statements.get("INCOME_STATEMENT_LST", [])
pretax_vals = {_amount_tuple(r) for r in inc
               if r.get("sbs_label") == "pretaxProfitAmount"}
for r in inc:
    if (r.get("sbs_label") == "operatingProfitAmount"
            and r.get("sbs_match_source") == "fuzzy"
            and _amount_tuple(r) in pretax_vals
            and _amount_tuple(r)):              # непустые значения
        r["sbs_label"] = None
        r["sbs_blocked_reason"] = "value_equals_pretax"
        r.pop("sbs_scope", None)
        r["needs_review"] = True
```

Условие `source == "fuzzy"` защищает dict-hit `RESULT ATIVIDADE`. Условие совпадения с
pretax — точное, срабатывает только на реальной коллизии.

### Правка — альтернатива (чистка алиасов, требует проверки)
Убрать `resultado operacional` / `lucro operacional` / `resultado operacional liquido`
из `operatingProfitAmount.aliases` (в `labels_OLD.yaml`/`EXTRA_ALIASES`, затем
`python3.13 build_sbs_mapping.py`). **Риск:** в документах, где `RESULTADO OPERACIONAL`
— единственная операционная строка (нет отдельной pretax с тем же значением), это
снизит recall. **Перед удалением — грепнуть голдены:** опирается ли кто-то на эти
алиасы как на operating. По baseline: `RESULT OPERACIONAL → operatingProfit (fuzzy)`
встречается в serasa1/serasa2; проверить DFP/sample2/ITR.

> Рекомендация: **value-collision демоция** (надёжнее, не трогает recall там, где
> коллизии нет). Чистку алиасов держать как задокументированный запасной вариант.

### Регрессионный риск
- Не должно трогать `RESULT ATIVIDADE` (dict) ни в одном документе.
- Документы без коллизии operating==pretax не затрагиваются.
- **Проверить**, что в DFP/sample2/ITR `operatingProfitAmount` остаётся, где он верен.

### Проверка
- SERASA-1/2: `RESULT OPERACIONAL` → `sbs_label=null`,
  `blocked_reason=value_equals_pretax`; `RESULT ATIVIDADE` → operating (без изменений);
  `RESULTADO ANTES IR` → pretax (без изменений).

---

## Порядок исполнения

1. **Пункт 1** (SERASA-детекция) — изолирован, низкий риск. Сделать и проверить первым.
2. **Пункты 2+3+4** (секционная логика) — одна правка `_compute_sections` +
   twin-фоллбэк. Высокий регрессионный риск (общий код) → делать вместе, под защитой
   `serasa_shape`-гейта, с обязательным diff против baseline.
3. **Пункт 5** (value-collision) — изолирован.
4. Перегенерация serasa-выходов и Excel; финальный diff.

## Протокол регрессии (обязателен после КАЖДОГО блока правок)

```bash
cd /c/Users/4885523/projects_with_claude/br_ocr_json_processing
# перепрогнать ВСЕ голдены в НОВЫЕ файлы или поверх (у нас есть baseline-снимок)
python3.13 structure_ocr.py comprehansive_sample.json -o structured_output.json
python3.13 structure_ocr.py sample_2.json -o structured_output_sample2.json
python3.13 structure_ocr.py itr_samples.json -o structured_output_itr.json
python3.13 structure_ocr.py provider_sample.json -o structured_output_provider.json
python3.13 structure_ocr.py complicated_empty_labels_sample.json -o structured_output_complicated.json
python3.13 structure_ocr.py complicated_sample_2.json -o structured_output_complicated_sample2.json
python3.13 structure_ocr.py serasa_report_1.json -o structured_output_serasa1.json
python3.13 structure_ocr.py serasa_report_2.json -o structured_output_serasa2.json
# diff против baseline (написать _diff_baseline.py: показать строки, где sbs/method/
# scope/blocked/nr изменились, сгруппировать по голдену)
python3.13 _diff_baseline.py            # ожидаем изменения ТОЛЬКО в serasa* (+ provider тип)
# Excel
python3.13 export_excel.py structured_output_serasa1.json -o serasa_report_1.xlsx
python3.13 export_excel.py structured_output_serasa2.json -o serasa_report_2.xlsx
```

## Критерии приёмки

- [ ] `detect_doc_type` → `SERASA` для serasa1/serasa2/provider; metadata содержит
      `ACCOUNT_TYPE=SERASA EXPERIAN`; Excel-фильтры = как UPPERCASE (все колонки, без page_class).
- [ ] SERASA-2 долг: обе `EMPREST E FINANCIAMENTOS` → ST/LT (positional), без `needs_review`.
- [ ] SERASA-1 `APLICACOES FINANCEIRAS` → `financialAssetsAmount` (non-current), без `needs_review`.
- [ ] SERASA-1/2 `RESULT OPERACIONAL` снят с operating (blocked=value_equals_pretax);
      `RESULT ATIVIDADE` остаётся operating.
- [ ] diff против `_baseline.json`: НИ ОДНОЙ регрессии в DFP/ITR/sample2/complicated;
      provider — без изменений по строкам (тип может стать SERASA).
- [ ] Обновить `CLAUDE.md` (таблица тест-доков: добавить serasa1/serasa2; описать тип
      SERASA в таблице Excel-фильтрации; описать bucket-wide секции) и память
      `[[br-sbs-labelling]]`, `[[br-excel-export]]`.

## Артефакты этой сессии (для будущего запуска)

- `_baseline.json` — снимок SBS-меток всех 8 голденов ДО правок (регрессионный эталон).
- `_baseline_snapshot.py` — скрипт, его породивший (можно перезапустить для свежего эталона).
- `structured_output_serasa1.json` / `_serasa2.json` + `_report.json` — текущие выходы (до правок).
- `serasa_report_1.xlsx` / `serasa_report_2.xlsx` — текущие Excel (до правок).

## Ключевые места в коде (точки входа правок)

| Файл | Символ | Строки | Зачем |
|---|---|---|---|
| `structure_ocr.py` | `METADATA_NAMES` | 68 | НЕ трогать; добавить отдельный extract |
| `structure_ocr.py` | `extract_metadata` | 138 | рядом вызвать `extract_account_type` |
| `structure_ocr.py` | `build_account_headers` | 416 | (контекст: почему SERASA EXPERIAN теряется) |
| `export_excel.py` | `detect_doc_type` | 114 | + ветка SERASA (сигнатура +metadata) |
| `export_excel.py` | `_consolidado_only`/`_primary_only` | 141/146 | SERASA НЕ добавлять (= все колонки) |
| `export_excel.py` | `META_ORDER`/`build_metadata_sheet` | 96/275 | уже готовы к ACCOUNT_TYPE |
| `sbs_labeler.py` | `_compute_sections` | 520 | bucket-wide для SERASA-shape (п.2/3/4) |
| `sbs_labeler.py` | `_fill_sections_by_arithmetic` | 480 | арифметика-источник (уже корректна) |
| `sbs_labeler.py` | `_resolve_twin` | 299 | детерминированный финал (п.4) |
| `sbs_labeler.py` | `_stage_twin_section` | 666 | снять needs_review на детерм. кейсе |
| `sbs_labeler.py` | `_stage_struct_validate` | 784 | + value-collision демоция (п.5) |
| `sbs_label_mapping.json` | `ambiguity_groups`, aliases | — | опц. чистка алиасов (п.5, альтернатива) |
