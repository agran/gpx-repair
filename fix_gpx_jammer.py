#!/usr/bin/env python3
"""
fix_gpx_jammer.py
-----------------
Обнаруживает и исправляет артефакты GPS-глушилки в GPX-треках.

Принцип работы:
  1. Находит "телепортации" — переходы между соседними точками, где скорость
     превышает реалистичный порог (по умолчанию 30 м/с = 108 км/ч).
  2. Группирует их в эпизоды глушения: первый скачок «наружу» + кластер
     ложных координат + скачок «обратно».
  3. Удаляет ложные точки.
  4. Заполняет пробел линейно интерполированными точками между последней
     хорошей точкой до эпизода и первой хорошей точкой после него.

Использование:
  python fix_gpx_jammer.py track.gpx
  python fix_gpx_jammer.py track.gpx output.gpx
  python fix_gpx_jammer.py track.gpx --max-speed 20 --interval 2
  python fix_gpx_jammer.py track.gpx --no-interpolate   # просто удалить

Зависимости: только стандартная библиотека Python 3.7+
"""

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# Префиксы, зарезервированные ElementTree: ns0, ns1, ns2, ...
_RESERVED_NS_PREFIX = re.compile(r"^ns\d+$")


# ---------------------------------------------------------------------------
# Геодезические утилиты
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в метрах между двумя точками (формула Хаверсина)."""
    R = 6_371_000
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def parse_time(s: str) -> datetime:
    """Разбирает ISO 8601 строку времени в объект datetime (UTC)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_time(dt: datetime) -> str:
    """Форматирует datetime обратно в ISO 8601 строку для GPX."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Обнаружение эпизодов глушения
# ---------------------------------------------------------------------------

def find_jammer_episodes(
    lats: List[float],
    lons: List[float],
    times: List[datetime],
    max_speed_mps: float,
    min_cluster_dist_m: float,
) -> List[Tuple[int, int]]:
    """
    Возвращает список (first_bad_idx, last_bad_idx) — включительно.

    Алгоритм:
      1. Находим «телепортации» — переходы, где скорость > max_speed_mps.
      2. Для каждой телепортации НАРУЖУ (прыжок в точку, далёкую от
         текущего местоположения на >= min_cluster_dist_m) — ищем возврат:
         первую телепортацию, после которой точка снова находится БЛИЗКО
         к точке ДО начала эпизода (< min_cluster_dist_m).
      3. Все точки между скачком наружу и скачком назад — плохой эпизод.

    Такой подход не путается с «дрейфом» внутри зоны глушилки
    (небольшие перемещения внутри ложного кластера не открывают новый эпизод).
    """
    n = len(lats)

    # Индексы i: скорость перехода i -> i+1 превышает порог
    teleport_at: List[int] = []
    for i in range(n - 1):
        dt = max((times[i + 1] - times[i]).total_seconds(), 1.0)
        d = haversine(lats[i], lons[i], lats[i + 1], lons[i + 1])
        if d / dt > max_speed_mps:
            teleport_at.append(i)

    if not teleport_at:
        return []

    episodes: List[Tuple[int, int]] = []
    k = 0  # текущая позиция в списке телепортаций

    while k < len(teleport_at):
        t_out = teleport_at[k]       # последняя хорошая точка
        first_bad = t_out + 1

        if first_bad >= n:
            break

        # Скачок должен быть ДОСТАТОЧНО БОЛЬШИМ, чтобы быть глушилкой
        d_jump = haversine(lats[t_out], lons[t_out], lats[first_bad], lons[first_bad])
        if d_jump < min_cluster_dist_m:
            k += 1
            continue

        # Опорные координаты нормального трека (точка до глушилки)
        ref_lat, ref_lon = lats[t_out], lons[t_out]

        # Ищем возврат: телепортацию t_back, после которой точка
        # снова оказывается БЛИЗКО к ref (т.е. вернулись на нормальный трек)
        found = False
        for m in range(k + 1, len(teleport_at)):
            t_back = teleport_at[m]
            after_idx = t_back + 1

            if after_idx >= n:
                continue

            dist_after_to_ref = haversine(
                lats[after_idx], lons[after_idx], ref_lat, ref_lon
            )
            if dist_after_to_ref < min_cluster_dist_m:
                # Нашли возврат — точки [first_bad..t_back] — плохой эпизод
                episodes.append((first_bad, t_back))
                k = m + 1   # прыгаем СРАЗУ за конец этого эпизода
                found = True
                break

        if not found:
            # Нет парного возврата — пропускаем этот скачок
            k += 1

    return episodes


# ---------------------------------------------------------------------------
# Интерполяция
# ---------------------------------------------------------------------------

def extend_episode_backward(
    lats: List[float],
    lons: List[float],
    eles: List[float],
    times: List[datetime],
    first_bad: int,
    after_idx: int,
    displaced_m: float,
    max_lookback_s: float = 3600.0,
    max_vert_speed_mps: float = 5.0,
) -> int:
    """
    Сканирует назад от first_bad и ищет точки, которые:
    - смещены от post-jammer-позиции больше чем на displaced_m, ИЛИ
    - имеют аномальную скорость изменения высоты > max_vert_speed_mps
      (глушилка часто портит высоту раньше, чем координаты).
    Возвращает новый (расширенный) first_bad.
    """
    if after_idx >= len(lats):
        return first_bad

    ref_lat, ref_lon = lats[after_idx], lons[after_idx]

    new_first = first_bad
    i = first_bad - 1

    while i >= 0:
        dt = abs((times[i + 1] - times[i]).total_seconds())
        if dt > max_lookback_s:
            break

        # Критерий 1: координатное смещение
        coord_displaced = haversine(lats[i], lons[i], ref_lat, ref_lon) >= displaced_m

        # Критерий 2: аномальная вертикальная скорость (глушилка портит высоту)
        ele_anomaly = False
        if dt > 0:
            dh = abs(eles[i + 1] - eles[i])
            if dh / dt > max_vert_speed_mps:
                ele_anomaly = True

        if not coord_displaced and not ele_anomaly:
            break  # точка уже нормальная — стоп

        new_first = i
        i -= 1

    return new_first


def get_pre_episode_ele(
    eles: List[float],
    times: List[datetime],
    before_idx: int,
    lookback_min_s: float = 30.0,
    lookback_max_s: float = 300.0,
) -> float:
    """
    Возвращает стабильную высоту ДО эпизода глушения, игнорируя последние
    lookback_min_s секунд (где высота могла уже быть испорчена).
    Берёт медиану точек в окне [before_idx_time - lookback_max .. - lookback_min].
    Если окно пусто — возвращает eles[before_idx].
    """
    ref_time = times[before_idx]
    window: List[float] = []
    for i in range(before_idx - 1, max(0, before_idx - 50000), -1):
        dt = (ref_time - times[i]).total_seconds()
        if dt > lookback_max_s:
            break
        if dt >= lookback_min_s:
            window.append(eles[i])
    if window:
        window.sort()
        return window[len(window) // 2]  # медиана
    return eles[before_idx]


def build_interpolated_points(
    lat0: float, lon0: float, ele0: float, t0: datetime,
    lat1: float, lon1: float, ele1: float, t1: datetime,
    interval_s: float,
) -> List[Tuple[float, float, float, datetime]]:
    """
    Генерирует линейно интерполированные точки между (lat0,lon0) и (lat1,lon1).
    Не включает сами граничные точки.
    """
    total_s = (t1 - t0).total_seconds()
    if total_s <= interval_s:
        return []

    result = []
    t = interval_s
    while t < total_s - 1e-6:
        alpha = t / total_s
        result.append((
            lat0 + alpha * (lat1 - lat0),
            lon0 + alpha * (lon1 - lon0),
            ele0 + alpha * (ele1 - ele0),
            t0 + timedelta(seconds=t),
        ))
        t += interval_s

    return result


# ---------------------------------------------------------------------------
# Работа с XML / GPX
# ---------------------------------------------------------------------------

def collect_namespaces(path: str) -> dict:
    """Собирает все ns-префиксы из файла, чтобы потом зарегистрировать их."""
    ns_map = {}
    for event, (prefix, uri) in ET.iterparse(path, events=["start-ns"]):
        ns_map[prefix] = uri
    return ns_map


def make_trkpt(
    ns: str,
    ns3: str,
    lat: float,
    lon: float,
    ele: float,
    ts: datetime,
    speed: float = 0.0,
    cad: float = 0.0,
) -> ET.Element:
    """Создаёт элемент <trkpt> с минимальным набором полей."""
    pt = ET.Element(f"{{{ns}}}trkpt")
    pt.set("lat", f"{lat:.8f}")
    pt.set("lon", f"{lon:.8f}")

    ele_el = ET.SubElement(pt, f"{{{ns}}}ele")
    ele_el.text = f"{ele:.2f}"

    time_el = ET.SubElement(pt, f"{{{ns}}}time")
    time_el.text = fmt_time(ts)

    ext_el = ET.SubElement(pt, f"{{{ns}}}extensions")
    tpe = ET.SubElement(ext_el, f"{{{ns3}}}TrackPointExtension")
    sp_el = ET.SubElement(tpe, f"{{{ns3}}}speed")
    sp_el.text = f"{speed:.1f}"
    cad_el = ET.SubElement(tpe, f"{{{ns3}}}cad")
    cad_el.text = f"{cad:.1f}"

    return pt


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def fix_gpx(
    input_path: str,
    output_path: str,
    max_speed_mps: float = 30.0,
    min_cluster_dist_m: float = 1000.0,
    interpolate: bool = True,
    interval_s: float = 1.0,
    pre_jitter_dist_m: float = 200.0,
    max_vert_speed_mps: float = 5.0,
    verbose: bool = True,
) -> None:
    """Исправляет GPX-трек: удаляет зашумлённые точки и интерполирует пробел.

    pre_jitter_dist_m: если > 0, дополнительно удаляет точки непосредственно
    ДО эпизода, которые смещены от post-jammer-позиции более чем на это расстояние
    (убирает «ранний дрейф» GPS перед включением глушилки).
    """

    # --- 1. Регистрируем пространства имён (чтобы не получить ns0/ns1) ---
    ns_map = collect_namespaces(input_path)
    for prefix, uri in ns_map.items():
        # Пропускаем зарезервированные префиксы ns0/ns1/ns2/… —
        # ElementTree присвоит им автоматические имена при записи.
        if not _RESERVED_NS_PREFIX.match(prefix):
            ET.register_namespace(prefix, uri)

    NS  = ns_map.get("", "http://www.topografix.com/GPX/1/1")
    NS3 = ns_map.get("ns3", "http://www.garmin.com/xmlschemas/TrackPointExtension/v1")

    # --- 2. Парсинг ---
    tree = ET.parse(input_path)
    root = tree.getroot()

    seg = root.find(f".//{{{NS}}}trkseg")
    if seg is None:
        print("ОШИБКА: в файле не найден элемент <trkseg>", file=sys.stderr)
        sys.exit(1)

    pts = list(seg.findall(f"{{{NS}}}trkpt"))
    n   = len(pts)

    if verbose:
        print(f"Загружено точек: {n}")

    lats  = [float(p.get("lat")) for p in pts]
    lons  = [float(p.get("lon")) for p in pts]
    eles  = []
    for p in pts:
        e = p.find(f"{{{NS}}}ele")
        eles.append(float(e.text) if e is not None else 0.0)
    times = [parse_time(p.find(f"{{{NS}}}time").text) for p in pts]

    # --- 3. Поиск эпизодов глушения ---
    episodes = find_jammer_episodes(
        lats, lons, times, max_speed_mps, min_cluster_dist_m
    )

    if not episodes:
        print("Аномальных эпизодов не обнаружено. Файл не изменён.")
        if output_path != input_path:
            import shutil
            shutil.copy2(input_path, output_path)
        return

    # --- 4. Отчёт об эпизодах ---
    total_removed   = 0
    total_inserted  = 0
    remove_set      = set()
    # insertion_before[i] = список точек, вставляемых ПЕРЕД pts[i]
    insertion_before: dict = {}

    for ep_start, ep_end in episodes:
        after_idx  = ep_end + 1 if ep_end + 1 < n else None

        # Расширяем эпизод назад: убираем «ранний дрейф» перед глушилкой
        if pre_jitter_dist_m > 0 and after_idx is not None:
            ep_start = extend_episode_backward(
                lats, lons, eles, times,
                first_bad=ep_start,
                after_idx=after_idx,
                displaced_m=pre_jitter_dist_m,
                max_vert_speed_mps=max_vert_speed_mps,
            )

        # Дополнительный проход назад для аномалий ВЫСОТЫ:
        # Обнаруживает "плато неправильной высоты" и предшествующий крэш.
        # (Координаты таких точек корректны, но высота уже испорчена глушилкой.)
        if max_vert_speed_mps > 0:
            before_candidate = ep_start - 1
            i = before_candidate
            new_alt_start = ep_start
            while i >= 0:
                if i == 0:
                    break
                dt_in = abs((times[i] - times[i - 1]).total_seconds())
                if dt_in > 30:
                    break  # временной разрыв — до него GPS-высота надёжна

                dh_in = abs(eles[i] - eles[i - 1])
                in_anomaly = (dt_in > 0 and dh_in / dt_in > max_vert_speed_mps)

                if in_anomaly:
                    # Явная аномалия входящего перехода
                    new_alt_start = i
                    i -= 1
                    continue

                # Переход сам по себе гладкий, но проверяем шаг ДО него:
                # это может быть "плато" после крэша (eles[i-1]→eles[i] = 0,
                # но eles[i-2]→eles[i-1] был резким)
                if i - 1 > 0:
                    dt_prev = abs((times[i - 1] - times[i - 2]).total_seconds())
                    if 0 < dt_prev <= 30:
                        dh_prev = abs(eles[i - 1] - eles[i - 2])
                        if dh_prev / dt_prev > max_vert_speed_mps:
                            # Сразу перед i был крэш → i — это плато
                            new_alt_start = i
                            i -= 1
                            continue
                break  # всё чисто — останавливаемся

            if new_alt_start < ep_start:
                ep_start = new_alt_start

        bad_count = ep_end - ep_start + 1
        total_removed += bad_count

        # Граничные хорошие точки
        before_idx = ep_start - 1

        if verbose:
            dur_s = (
                (times[after_idx] - times[before_idx]).total_seconds()
                if after_idx is not None else 0
            )
            print(
                f"\nЭпизод глушения: точки [{ep_start}..{ep_end}]"
                f"  ({bad_count} точек, {dur_s:.0f} с пробел)"
            )
            print(
                f"  Скачок в  : {times[ep_start]}  "
                f"({lats[ep_start]:.5f}, {lons[ep_start]:.5f})"
                f"  — {haversine(lats[before_idx], lons[before_idx], lats[ep_start], lons[ep_start]):.0f} м"
            )
            if after_idx is not None:
                print(
                    f"  Скачок из : {times[ep_end]}  "
                    f"({lats[ep_end]:.5f}, {lons[ep_end]:.5f})"
                    f"  — {haversine(lats[ep_end], lons[ep_end], lats[after_idx], lons[after_idx]):.0f} м назад"
                )
            print(
                f"  До  : {times[before_idx]}  ({lats[before_idx]:.5f}, {lons[before_idx]:.5f})"
            )
            if after_idx is not None:
                print(
                    f"  После: {times[after_idx]}  ({lats[after_idx]:.5f}, {lons[after_idx]:.5f})"
                )

        for idx in range(ep_start, ep_end + 1):
            remove_set.add(idx)

        # Интерполяция
        if interpolate and after_idx is not None:
            # Используем стабильную высоту из окна 30–300 с ДО эпизода,
            # чтобы не начинать от уже испорченной глушилкой высоты.
            stable_ele0 = get_pre_episode_ele(eles, times, before_idx)
            interp = build_interpolated_points(
                lats[before_idx], lons[before_idx], stable_ele0,        times[before_idx],
                lats[after_idx],  lons[after_idx],  eles[after_idx],    times[after_idx],
                interval_s,
            )
            insertion_before[after_idx] = interp
            total_inserted += len(interp)
            if verbose:
                print(f"  Высота интерп.: {stable_ele0:.1f}м → {eles[after_idx]:.1f}м"
                      f"  (граничная точка до: {eles[before_idx]:.1f}м{"  [скорректирована]" if abs(stable_ele0 - eles[before_idx]) > 5 else ""})")

    # --- 5. Перестроение сегмента ---
    # Удаляем все точки из XML-сегмента
    for pt in pts:
        seg.remove(pt)

    # Добавляем обратно, пропуская плохие и вставляя интерполированные
    for i, pt in enumerate(pts):
        if i in insertion_before:
            for lat, lon, ele, ts in insertion_before[i]:
                seg.append(make_trkpt(NS, NS3, lat, lon, ele, ts))

        if i not in remove_set:
            seg.append(pt)

    # --- 6. Запись ---
    tree.write(output_path, encoding="UTF-8", xml_declaration=True)

    if verbose:
        print(f"\nУдалено плохих точек  : {total_removed}")
        print(f"Вставлено интерполир. : {total_inserted}")
        print(f"Итого точек в треке   : {n - total_removed + total_inserted}")
        print(f"Результат записан в   : {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Удаляет артефакты GPS-глушилки из GPX-трека и интерполирует пробел.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python fix_gpx_jammer.py track.gpx
  python fix_gpx_jammer.py track.gpx fixed.gpx
  python fix_gpx_jammer.py track.gpx --max-speed 15   # пешеходный поход
  python fix_gpx_jammer.py track.gpx --max-speed 80   # автомобиль
  python fix_gpx_jammer.py track.gpx --no-interpolate # только удалить точки
        """,
    )
    parser.add_argument("input",  help="Входной GPX-файл")
    parser.add_argument("output", nargs="?", help="Выходной GPX-файл (по умолчанию: <input>_fixed.gpx)")
    parser.add_argument(
        "--max-speed", type=float, default=30.0,
        metavar="M/S",
        help="Максимальная реалистичная скорость в м/с (по умолчанию: 30 = 108 км/ч). "
             "Для пешего похода рекомендуется 10–15, для велосипеда — 20–30.",
    )
    parser.add_argument(
        "--min-distance", type=float, default=1000.0,
        metavar="M",
        help="Минимальное расстояние от нормального трека до ложного кластера, "
             "чтобы признать его глушилкой (по умолчанию: 1000 м).",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0,
        metavar="SEC",
        help="Шаг интерполяции в секундах (по умолчанию: 1.0). "
             "Подберите под исходную частоту записи трека.",
    )
    parser.add_argument(
        "--max-vert-speed", type=float, default=5.0,
        metavar="M/S",
        help="Максимальная реалистичная вертикальная скорость в м/с (по умолчанию: 5). "
             "Точки с большим изменением высоты удаляются вместе с jammer-эпизодом.",
    )
    parser.add_argument(
        "--pre-jitter-dist", type=float, default=200.0,
        metavar="M",
        help="Убирать точки ПЕРЕД эпизодом, смещённые от post-jammer-позиции "
             "более чем на M метров (ранний дрейф GPS). "
             "0 — не расширять назад (по умолчанию: 200 м).",
    )
    parser.add_argument(
        "--no-interpolate", action="store_true",
        help="Не вставлять интерполированные точки — просто удалить ложные.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Не выводить подробный отчёт.",
    )

    args = parser.parse_args()

    inp = args.input
    if not Path(inp).exists():
        print(f"ОШИБКА: файл не найден: {inp}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        out = args.output
    else:
        p = Path(inp)
        out = str(p.parent / (p.stem + "_fixed" + p.suffix))

    fix_gpx(
        input_path=inp,
        output_path=out,
        max_speed_mps=args.max_speed,
        min_cluster_dist_m=args.min_distance,
        interpolate=not args.no_interpolate,
        interval_s=args.interval,
        pre_jitter_dist_m=args.pre_jitter_dist,
        max_vert_speed_mps=args.max_vert_speed,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
