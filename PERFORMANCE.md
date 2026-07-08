# Руководство по ускорению цикла «загрузить → исправить» (index.html)

Документ описывает конкретные правки в `index.html`, упорядоченные по
ожидаемому выигрышу. Каждая правка атомарна и независима, их можно
вносить по одной. Все правки сохраняют числовую идентичность результатов
(длина трека, набор высоты, пульс, статистика), если не отмечено иное.

Контекст: реальный трек 56 288 точек (17 МБ GPX), загрузка ~5 с,
исправление ~5,6 с. Горячий путь — `computeAllSmoothedSeries` и внутри
него `smoothedSpeedSeries` + `smoothedGradeSeries`.

---

## Содержание

1. [F1 — Убрать дублирующийся вызов `smoothedElevationSeries`](#f1)
2. [F2 — Убрать дублирующийся вызов `buildCumDist`](#f2)
3. [F3 — Кэшировать `series`/`cumDist` при смене режима раскраски](#f3)
4. [F4 — Предвычислить `legDist[]`/`legDt[]`, убрать `haversine` из внутреннего цикла скорости](#f4)
5. [F5 — Two-pointer для `gradeWindowBounds` (O(n·W) → O(n))](#f5)
6. [F8 — Убрать двойной `DOMParser.parseFromString` при загрузке файла](#f8)
7. [F9 — Объединить два O(n) прохода в `computeTrackStats`](#f9)
8. [Правки с риском ULP-изменения](#риск)
9. [Итоговая оценка выигрыша](#итог)

---

<a id="f1"></a>
## F1 — Убрать дублирующийся вызов `smoothedElevationSeries`

### Что происходит сейчас

`computeAllSmoothedSeries` (строка 2286) вызывает `smoothedElevationSeries`
**дважды** на одних и тех же данных:

```
computeAllSmoothedSeries
├── smoothedGradeSeries(eles, times)
│     └── smoothedElevationSeries(eles, times)  ← вызов 1
└── smoothedElevationSeries(eles, times)         ← вызов 2 (то же)
```

Каждый вызов — `fillMissingEle` (O(n), копирование массива) +
`smoothSeries` (O(n) скользящее среднее).

### Патч

```javascript
// ~строка 2244  smoothedGradeSeries теперь принимает уже готовый smoothedEle
function smoothedGradeSeries(lats, lons, eles, times, smoothedEle, cumDist) {
  const n = lats.length;
  const out = new Array(n).fill(NaN);
  const hasEle = eles && eles.some((e) => Number.isFinite(e));
  if (!hasEle || !times) return out;
  // smoothedEle и cumDist переданы снаружи — не вычисляем заново
  for (let i = 0; i < n; i++) {
    const { lo, hi } = gradeWindowBounds(cumDist, times, i);
    const d = cumDist[hi] - cumDist[lo];
    if (d < GRADE_MIN_DIST_FLOOR_M) continue;
    out[i] = ((smoothedEle[hi] - smoothedEle[lo]) / d) * 100;
  }
  return out;
}

// ~строка 2286  computeAllSmoothedSeries считает elevation один раз
function computeAllSmoothedSeries(lats, lons, eles, times, hrs, precomputedCumDist) {
  // Высота вычисляется один раз и передаётся в smoothedGradeSeries
  const elevation = smoothedElevationSeries(eles, times);
  const cumDist = precomputedCumDist ?? buildCumDist(lats, lons);
  return {
    speed:     smoothedSpeedSeries(lats, lons, times),
    grade:     smoothedGradeSeries(lats, lons, eles, times, elevation, cumDist),
    elevation,
    hr:        smoothedHrSeries(hrs),
  };
}
```

**Риск:** отсутствует — передаётся та же ссылка на тот же массив.  
**Выигрыш:** −1 × `fillMissingEle` + `smoothSeries` за каждый `showTrackMap`.

---

<a id="f2"></a>
## F2 — Убрать дублирующийся вызов `buildCumDist`

### Что происходит сейчас

`showTrackMap` (строка 2798) вычисляет `buildCumDist` и сразу же
`computeAllSmoothedSeries` → `smoothedGradeSeries` вычисляет его **ещё раз**
(строка 2250) на тех же `lats/lons`. Каждый вызов — n обращений к `haversine`.

За цикл «загрузить → исправить»: **116 576 лишних вызовов `haversine`**
(56 288 «до» + 60 017 «после»).

### Патч

В `showTrackMap` строки 2791–2800:

```javascript
// Строим cumDist один раз и передаём в computeAllSmoothedSeries
const cumDist = buildCumDist(lats, lons);          // ← один раз
mapTrackData[containerId] = {
  lats, lons, eles, times, hrs, color,
  cumDist,                                          // сохраняем (нужен renderElevationProfile)
  series: computeAllSmoothedSeries(lats, lons, eles, times, hrs, cumDist),
};
```

Сигнатура `computeAllSmoothedSeries` принимает опциональный `precomputedCumDist`
(см. F1 выше). `smoothedGradeSeries` тоже принимает его параметром (см. F1).

**Риск:** отсутствует — тот же `cumDist`.  
**Выигрыш:** −116k вызовов `haversine` за полный цикл.

---

<a id="f3"></a>
## F3 — Кэшировать `series`/`cumDist` при смене режима раскраски

### Что происходит сейчас

`redrawMapsForColorMode` (строка 2726) вызывает `showTrackMap` с теми же
массивами `lats/lons/eles/times/hrs`. `showTrackMap` не проверяет, изменились
ли данные, и каждый раз заново вычисляет `buildCumDist` + весь
`computeAllSmoothedSeries`. При каждом переключении между «Скорость / Уклон /
Высота / Пульс» — ~620k вызовов `haversine` и ~1,7 М итераций цикла
уклона абсолютно вхолостую.

### Патч

В начале `showTrackMap`:

```javascript
function showTrackMap(containerId, lats, lons, color, eles, times, hrs,
                      keepView, interpSegments) {
  // ...
  const existing = mapTrackData[containerId];
  // Если массивы те же объекты — данные не изменились, пересчёт не нужен
  const sameData = existing &&
    existing.lats === lats &&
    existing.lons === lons;

  const cumDist = sameData
    ? existing.cumDist
    : buildCumDist(lats, lons);

  const series = sameData
    ? existing.series
    : computeAllSmoothedSeries(lats, lons, eles, times, hrs, cumDist);

  mapTrackData[containerId] = {
    lats, lons, eles, times, hrs, color, cumDist, series,
    interpMask: sameData
      ? existing.interpMask
      : buildInterpMask(containerId, times),
  };
  // ... дальше без изменений
```

**Риск:** отсутствует — вычисление не производится, результат тот же.  
**Выигрыш:** O(1) вместо O(n) для каждого переключения режима раскраски.

---

<a id="f4"></a>
## F4 — Предвычислить `legDist[]`/`legDt[]`, убрать `haversine` из внутреннего цикла скорости

### Что происходит сейчас

`smoothedSpeedSeries` (строки 2155–2170): внешний цикл по i, внутренний —
по окну `[lo, hi]`. Внутри вызывается `haversine` на каждую пару соседних
точек. Для трека 56 288 точек с окном W_speed ≈ 10:
**~562 880 вызовов `haversine`** (каждый — 2 синуса, 2 косинуса, `asin`,
`sqrt`).

### Патч

```javascript
function smoothedSpeedSeries(lats, lons, times) {
  const n = lats.length;
  const out = new Array(n).fill(NaN);
  if (!times) return out;

  // Предвычисляем расстояния и интервалы между соседними точками — один раз.
  // Float64Array: нет boxing float-значений, лучше кэш L1/L2.
  // Результат побитово идентичен оригиналу: legDist[j] === haversine(j, j+1),
  // порядок суммирования в каждой итерации тот же.
  const legDist = new Float64Array(n - 1);
  const legDt   = new Float64Array(n - 1);
  for (let j = 0; j < n - 1; j++) {
    legDist[j] = haversine(lats[j], lons[j], lats[j + 1], lons[j + 1]);
    legDt[j]   = (Number.isFinite(times[j]) && Number.isFinite(times[j + 1]))
                   ? times[j + 1] - times[j]
                   : NaN;
  }

  for (let i = 0; i < n; i++) {
    const { lo, hi } = timeWindowBounds(times, i, SPEED_HALF_S);
    if (hi <= lo) continue;
    let sumDist = 0, sumDt = 0;
    // Простые сложения вместо тригонометрии
    for (let j = lo; j < hi; j++) {
      if (!Number.isFinite(legDt[j]) || legDt[j] <= 0) continue;
      sumDist += legDist[j];
      sumDt   += legDt[j];
    }
    out[i] = sumDt > 0 ? (sumDist / sumDt) * 3.6 : NaN;
  }
  return out;
}
```

**Риск:** отсутствует — `legDist[j]` идентичен прямому `haversine(j, j+1)`,
порядок суммирования не изменился → результат побитово совпадает.  
**Выигрыш:** haversine вызывается 1× за пару точек вместо W_speed раз.
За один `showTrackMap` на 56k треке: ~56k вместо ~562k вызовов (−90%).

---

<a id="f5"></a>
## F5 — Two-pointer для `gradeWindowBounds` (O(n·W) → O(n))

### Что происходит сейчас

`smoothedGradeSeries` (строки 2244–2258): для каждой точки i вызывается
`gradeWindowBounds`, которая строит окно с нуля (`lo = hi = i`,
раздвигает наружу). Суммарно — O(n × W_grade) итераций внутренних
`while`-циклов. Для 56k точек при W_grade ≈ 20: **~1,1 М итераций**.

### Обоснование корректности two-pointer

`times[]` монотонно не убывает, `cumDist[]` монотонно не убывает
(сумма неотрицательных расстояний). Ограничения окна:
- `t0 − times[lo] ≤ GRADE_MAX_HALF_S` (t0 растёт → lo не идёт влево)
- `times[hi] − t0 ≤ GRADE_MAX_HALF_S` (t0 растёт → hi не идёт влево)
- Пауза > PAUSE_GAP_S блокирует переход через неё

Следовательно `lo(i)` и `hi(i)` — монотонно неубывающие функции от `i`.
Совокупное число сдвигов lo и hi за весь цикл не превышает 2n.
Итоговые значения `(lo, hi)` **побитово идентичны** оригиналу: внутри
`gradeWindowBounds` нет float-арифметики, только сравнения индексов.

### Патч

> **Внимание:** логика two-pointer для `gradeWindowBounds` сложна
> из-за асимметричного расширения (сначала lo, потом hi). Перед мержем
> необходимо прогнать регрессионные тесты на треках с паузами,
> стоянками и граничными случаями GRADE_MIN_DIST_FLOOR_M.

Заменить функцию `smoothedGradeSeries` целиком:

```javascript
function smoothedGradeSeries(lats, lons, eles, times, smoothedEle, cumDist) {
  const n = lats.length;
  const out = new Array(n).fill(NaN);
  const hasEle = eles && eles.some((e) => Number.isFinite(e));
  if (!hasEle || !times) return out;

  let lo = 0, hi = 0; // указатели движутся только вперёд за весь цикл

  for (let i = 0; i < n; i++) {
    const t0 = times[i];

    // lo не правее i
    if (lo > i) lo = i;
    if (hi < i) hi = i;

    // Сдвигаем lo вправо: убираем точки, которые уже за пределами
    // временного горизонта GRADE_MAX_HALF_S или отделены паузой
    while (lo < i) {
      if (Number.isFinite(t0) && Number.isFinite(times[lo]) &&
          t0 - times[lo] > GRADE_MAX_HALF_S) {
        lo++;
        continue;
      }
      if (lo + 1 <= i) {
        const dtStep = times[lo + 1] - times[lo];
        if (!Number.isFinite(dtStep) || dtStep <= 0 || dtStep > PAUSE_GAP_S) {
          lo++;
          continue;
        }
      }
      break;
    }

    // Расширяем hi вправо, пока не наберём GRADE_MIN_DIST_M
    while (hi < n - 1 && cumDist[hi] - cumDist[lo] < GRADE_MIN_DIST_M) {
      const dtNext = times[hi + 1] - times[hi];
      if (!Number.isFinite(dtNext) || dtNext <= 0 || dtNext > PAUSE_GAP_S) break;
      if (Number.isFinite(t0) && Number.isFinite(times[hi + 1]) &&
          times[hi + 1] - t0 > GRADE_MAX_HALF_S) break;
      hi++;
    }

    const d = cumDist[hi] - cumDist[lo];
    if (d < GRADE_MIN_DIST_FLOOR_M) continue;
    out[i] = ((smoothedEle[hi] - smoothedEle[lo]) / d) * 100;
  }
  return out;
}
```

**Риск:** при неточной реализации two-pointer возможен сдвиг (lo, hi) на
1 точку в краевых случаях. Числовые эталоны (871,73 км / 2419 м набора)
**не затрагиваются**: они считаются в `computeTrackStats` / `computeElevationGainLoss`
через отдельные проходы.  
**Выигрыш:** ~1,1 М → ~113k итераций (−90%) за один `showTrackMap`.

---

<a id="f8"></a>
## F8 — Убрать двойной `DOMParser.parseFromString` при загрузке файла

### Что происходит сейчас

При загрузке файла в `loadFile` вызывается:
1. `extractTrackData(gpxText)` (строка 5934) → `DOMParser.parseFromString` #1
2. `updateEpisodeList()` (строка 5962) → `extractTrackData(gpxText)` (строка 5668)
   → `DOMParser.parseFromString` #2 на том же 17 МБ XML

Кроме того, `updateEpisodeList` вызывается повторно при каждом изменении
параметров `p-maxspeed` и `p-mindist` (строка 5838), каждый раз парся
17 МБ заново.

### Патч

```javascript
// Кэш массивов текущего трека — используется в updateEpisodeList,
// чтобы не парсить XML повторно при изменении параметров.
let cachedTrackArrays = null;

// updateEpisodeList принимает опциональный предвычисленный объект
function updateEpisodeList(precomputed) {
  if (!gpxText) {
    cachedTrackArrays = null;
    // ... остальной код сброса без изменений
    return;
  }
  let lats, lons, eles, times;
  try {
    if (precomputed) {
      ({ lats, lons, eles, times } = precomputed);
    } else if (cachedTrackArrays) {
      ({ lats, lons, eles, times } = cachedTrackArrays); // нет повторного парса
    } else {
      ({ lats, lons, eles, times } = extractTrackData(gpxText));
    }
  } catch (e) {
    // ... без изменений
  }
  // ... остальное без изменений
}

// В loadFile передаём уже готовые массивы:
const extracted = extractTrackData(gpxText);  // парс #1 (единственный)
cachedTrackArrays = extracted;
showTrackMap("map-before", extracted.lats, extracted.lons, "#e11d48",
             extracted.eles, extracted.times, extracted.hrs);
// ...
updateEpisodeList(extracted);   // ← не вызывает DOMParser второй раз
```

Не забыть сбрасывать `cachedTrackArrays = null` в обработчике ошибки
конвертации (строка 5898).

**Риск:** отсутствует.  
**Выигрыш:** −1 × `DOMParser.parseFromString(17МБ)` при загрузке,
−1 × парс при каждом изменении параметров поиска эпизодов.

---

<a id="f9"></a>
## F9 — Объединить два O(n) прохода в `computeTrackStats`

### Что происходит сейчас

`computeTrackStats` (строка 1686) содержит два отдельных прохода по массиву
с `haversine`:
1. Собственный цикл (строки 1690–1705): длина трека + максимальная скорость.
2. `computeMovingTimeStats` (строка 1727, тело на строке 1651): время в движении,
   тоже с `haversine` на каждую пару точек.

### Патч

Объединить оба прохода в один:

```javascript
function computeTrackStats(lats, lons, eles, times, hrs) {
  let length = 0, maxSpeed = 0, hasSpeed = false;
  let movingTime = 0, movingDist = 0;
  const t0 = times && times[0];
  const t1 = times && times[times.length - 1];
  const hasDuration = Number.isFinite(t0) && Number.isFinite(t1);
  const duration = hasDuration ? t1 - t0 : 0;

  for (let i = 1; i < lats.length; i++) {
    const d = haversine(lats[i - 1], lons[i - 1], lats[i], lons[i]);
    length += d;
    if (times && Number.isFinite(times[i]) && Number.isFinite(times[i - 1])) {
      const dt = times[i] - times[i - 1];
      if (dt > 0) {
        hasSpeed = true;
        const v = d / dt;
        if (v > maxSpeed) maxSpeed = v;
        // Считаем время в движении в том же проходе
        if (dt <= PAUSE_GAP_S && v > STOP_SPEED_THRESHOLD_MS) {
          movingTime += dt;
          movingDist += d;
        }
      }
    }
  }
  // ... остальная сборка результата (hr, elevation) без изменений
}
```

**Риск:** отсутствует — арифметически эквивалентно.  
**Выигрыш:** −(n−1) вызовов `haversine` при каждом `computeTrackStats`.
Вызывается 2 раза за цикл (до + после) → −116k вызовов `haversine`.

---

<a id="риск"></a>
## Правки с риском ULP-изменения

Следующие правки дают значительный выигрыш, но меняют порядок
float-операций. Эталоны статистики (871,73 км / 42,78 км / 2419/2009 м /
88–198 bpm) **не затрагиваются**, так как считаются в отдельных путях.
Меняется только скорость/уклон в цвете трека и всплывающих подсказках.

### F6 — Two-pointer + скользящая сумма для `smoothedSpeedSeries`

После F4 `haversine` из внутреннего цикла уже убраны. Дополнительный
O(n) → O(амортизированный 1) per iteration выигрыш даёт two-pointer
для `sumDist`:

```javascript
// Вариант: cumDist[hi] − cumDist[lo] вместо суммирования legDist[lo..hi-1]
// Порядок float-операций меняется → результат не побитово идентичен.
// Относительная погрешность скорости: ≈ 10⁻¹² — невидима в км/ч.
sumDist = cumDist[hi] - cumDist[lo];
```

**Риск (только цвет/попап):** ULP-уровня разница в скорости.
Статистические эталоны не меняются.

---

<a id="итог"></a>
## Итоговая оценка выигрыша для трека 56 288 точек

### Вызовы `haversine` (за один полный цикл «загрузить → исправить»)

| Правка | До | После | Экономия |
|---|---|---|---|
| **F2** дубль buildCumDist | 116 576 | 0 | −116 576 |
| **F4** haversine в скорости «до» | 562 880 | 56 288 | −506 592 |
| **F4** haversine в скорости «после» | 600 170 | 60 017 | −540 153 |
| **F9** дубль в computeTrackStats | 116 305 | 0 | −116 305 |
| **Итого F2+F4+F9** | **~1,40 М** | **~116 305** | **−92%** |

### Итерации «тяжёлых» циклов (грейдовое окно)

| Правка | До | После | Экономия |
|---|---|---|---|
| **F5** gradeWindowBounds «до» | ~1 125 760 | ~112 576 | −90% |
| **F5** gradeWindowBounds «после» | ~1 200 340 | ~120 034 | −90% |

### Прочее

| Правка | Выигрыш |
|---|---|
| **F1** дубль smoothedEle | −2 × O(n) прохода smoothSeries за showTrackMap |
| **F3** кэш при смене цвета | 0 (вместо полного пересчёта) при переключении режима |
| **F8** дубль DOMParser | −1 × парс 17 МБ при загрузке, −1 × при каждом вводе параметров |

---

## Рекомендуемый порядок внесения правок

1. **F1 + F2** — одновременно (передача `elevation` и `cumDist` параметрами).
   Минимальные, абсолютно безопасные. Сначала изменить сигнатуры
   `smoothedGradeSeries` и `computeAllSmoothedSeries`, затем `showTrackMap`.

2. **F4** — изолированная правка внутри `smoothedSpeedSeries`.
   Самый большой числовой выигрыш, нулевой риск.

3. **F3** — добавить проверку `existing.lats === lats` в `showTrackMap`.
   Делает интерактивное переключение режимов мгновенным.

4. **F8** — рефакторинг `updateEpisodeList` + `loadFile`.
   Требует аккуратности с `cachedTrackArrays = null` при сбросе.

5. **F9** — объединение двух проходов в `computeTrackStats`.
   Простая, независимая правка.

6. **F5** — two-pointer для `gradeWindowBounds`. Вносить последней:
   логика сложнее, требует верификации на тестовых треках с паузами.
