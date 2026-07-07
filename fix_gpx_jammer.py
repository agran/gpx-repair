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
  python fix_gpx_jammer.py track.gpx --profile hiking
  python fix_gpx_jammer.py track.gpx --max-speed 20 --interval 2
  python fix_gpx_jammer.py track.gpx --no-interpolate   # просто удалить
  python fix_gpx_jammer.py track.gpx --gap-fill foot    # заполнить пробел маршрутом по тропам

Зависимости: только стандартная библиотека Python 3.7+
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Префиксы, зарезервированные ElementTree: ns0, ns1, ns2, ...
_RESERVED_NS_PREFIX = re.compile(r"^ns\d+$")


# ---------------------------------------------------------------------------
# Профили активности
# ---------------------------------------------------------------------------
# Профиль — только стартовый набор значений: явно указанные CLI-флаги
# (--max-speed, --min-distance и т.д.) всегда переопределяют его.

ACTIVITY_PROFILES: Dict[str, Dict[str, float]] = {
    "hiking":     {"label": "🥾 Пеший поход",            "max_speed": 10.0, "min_distance": 1000.0,  "pre_jitter_dist": 200.0,  "max_vert_speed": 3.0,  "interval": 1.0},
    "running":    {"label": "🏃 Бег / трейлраннинг",      "max_speed": 12.0, "min_distance": 1500.0,  "pre_jitter_dist": 300.0,  "max_vert_speed": 4.0,  "interval": 1.0},
    "kayak":      {"label": "🛶 Сплав / каяк / SUP",      "max_speed": 8.0,  "min_distance": 1000.0,  "pre_jitter_dist": 200.0,  "max_vert_speed": 2.0,  "interval": 1.0},
    "horse":      {"label": "🐎 Верховая езда",           "max_speed": 15.0, "min_distance": 1500.0,  "pre_jitter_dist": 300.0,  "max_vert_speed": 3.0,  "interval": 1.0},
    "mtb":        {"label": "🚴 Велосипед (МТБ)",         "max_speed": 25.0, "min_distance": 2000.0,  "pre_jitter_dist": 500.0,  "max_vert_speed": 5.0,  "interval": 1.0},
    "road_bike":  {"label": "🚴 Велосипед (шоссе)",       "max_speed": 30.0, "min_distance": 2500.0,  "pre_jitter_dist": 500.0,  "max_vert_speed": 5.0,  "interval": 1.0},
    "ski":        {"label": "⛷ Горные лыжи / сноуборд",  "max_speed": 40.0, "min_distance": 2000.0,  "pre_jitter_dist": 600.0,  "max_vert_speed": 15.0, "interval": 1.0},
    "enduro":     {"label": "🏍 Эндуро / мото-бездорожье", "max_speed": 40.0, "min_distance": 3000.0,  "pre_jitter_dist": 800.0,  "max_vert_speed": 8.0,  "interval": 1.0},
    "boat":       {"label": "🚤 Катер / моторная лодка",  "max_speed": 35.0, "min_distance": 3000.0,  "pre_jitter_dist": 800.0,  "max_vert_speed": 2.0,  "interval": 1.0},
    "car":        {"label": "🚗 Авто / шоссейный мотоцикл", "max_speed": 70.0, "min_distance": 5000.0, "pre_jitter_dist": 1500.0, "max_vert_speed": 6.0,  "interval": 2.0},
    "paraglider": {"label": "🪂 Параплан / дельтаплан",   "max_speed": 25.0, "min_distance": 3000.0,  "pre_jitter_dist": 500.0,  "max_vert_speed": 20.0, "interval": 1.0},
    "train":      {"label": "🚆 Поезд / автобус",         "max_speed": 90.0, "min_distance": 10000.0, "pre_jitter_dist": 0.0,    "max_vert_speed": 4.0,  "interval": 2.0},
}
DEFAULT_PROFILE = "hiking"

# Какой способ заполнения пробелов логично подходит под тип активности.
# Профили без дорожной/тропиночной сети (вода, воздух, снег, рельсы,
# бездорожье) — "line": маршрутизация OSRM по дорогам/тропам там не поможет.
PROFILE_INTERP_MODE: Dict[str, str] = {
    "hiking":     "foot",
    "running":    "foot",
    "horse":      "foot",
    "kayak":      "line",
    "mtb":        "bike",
    "road_bike":  "bike",
    "ski":        "line",
    "enduro":     "line",
    "boat":       "line",
    "car":        "car",
    "paraglider": "line",
    "train":      "line",
}


# ---------------------------------------------------------------------------
# Восстановление по дорогам/тропам (OSRM) + высота рельефа (Open-Meteo)
# ---------------------------------------------------------------------------
# Публичные бесплатные сервисы. Все ошибки сети/API/проверок правдоподобия
# ведут к молчаливому откату на обычную линейную интерполяцию для конкретного
# эпизода — результат должен получаться всегда, независимо от доступности
# внешних сервисов.

OSRM_PRIMARY = "https://routing.openstreetmap.de"
OSRM_FALLBACK = "https://router.project-osrm.org"
ELEVATION_PRIMARY = "https://api.open-meteo.com/v1/elevation"
ELEVATION_FALLBACK = "https://api.opentopodata.org/v1/srtm30m"
NETWORK_TIMEOUT_S = 10.0
NETWORK_PACE_S = 0.25
MAX_ROUTED_EPISODES = 30
MAX_ELEVATION_POINTS_PER_REQUEST = 100
USER_AGENT = "gpx-repair/1.0 (+https://github.com/agran/gpx-repair)"

_last_network_call_at = 0.0


def _pace_network() -> None:
    """Выдерживает паузу между запросами, чтобы не бить бесплатные сервисы
    параллельными/слишком частыми вызовами."""
    global _last_network_call_at
    wait = _last_network_call_at + NETWORK_PACE_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_network_call_at = time.monotonic()


def fetch_json(url: str) -> dict:
    _pace_network()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_S) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(url, resp.status, "HTTP error", resp.headers, None)
        return json.loads(resp.read().decode("utf-8"))


def fetch_route(profile: str, p0: Tuple[float, float], p1: Tuple[float, float]) -> Optional[dict]:
    """profile: "foot" | "bike" | "car". p0/p1: (lat, lon)."""
    lat0, lon0 = p0
    lat1, lon1 = p1
    url = (
        f"{OSRM_PRIMARY}/routed-{profile}/route/v1/{profile}/"
        f"{lon0},{lat0};{lon1},{lat1}?overview=full&geometries=geojson&steps=false"
    )
    try:
        data = fetch_json(url)
        if data.get("code") == "Ok" and data.get("routes"):
            return data
    except Exception:
        pass  # молча идём дальше — либо резерв (для авто), либо fallback на линию

    if profile == "car":
        try:
            fb_url = (
                f"{OSRM_FALLBACK}/route/v1/driving/"
                f"{lon0},{lat0};{lon1},{lat1}?overview=full&geometries=geojson&steps=false"
            )
            data = fetch_json(fb_url)
            if data.get("code") == "Ok" and data.get("routes"):
                return data
        except Exception:
            pass
    return None


def resample_route(
    coords: List[List[float]], total_s: float, interval_s: float
) -> Tuple[List[dict], float]:
    """Ресемплинг ломаной маршрута по времени: модель постоянной скорости
    вдоль пути. coords — [[lon, lat], ...] (порядок OSRM/GeoJSON).
    Возвращает (points, total_length_m)."""
    pts = [(lat, lon) for lon, lat in coords]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + haversine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    total_length = cum[-1] if cum else 0.0

    step = interval_s
    if total_s > 0 and total_s / step > 5000:
        step = total_s / 5000

    out: List[dict] = []
    seg_idx = 0
    t = step
    while t < total_s - 1e-6:
        alpha = t / total_s
        target_dist = alpha * total_length
        while seg_idx < len(cum) - 2 and cum[seg_idx + 1] < target_dist:
            seg_idx += 1
        seg_len = cum[seg_idx + 1] - cum[seg_idx] if seg_idx + 1 < len(cum) else 0.0
        seg_alpha = (target_dist - cum[seg_idx]) / seg_len if seg_len > 0 else 0.0
        a_lat, a_lon = pts[seg_idx]
        b_lat, b_lon = pts[min(seg_idx + 1, len(pts) - 1)]
        out.append({
            "lat": a_lat + seg_alpha * (b_lat - a_lat),
            "lon": a_lon + seg_alpha * (b_lon - a_lon),
            "t": t,
            "alpha": alpha,
        })
        t += step
    return out, total_length


def seam_blend_weight(t: float) -> float:
    """
    Как smoothstep01, но быстрее сходится к 1 (быстрее "встаёт" на
    маршрут/тропу, жертвуя длиной плавного участка) — производная в 0
    по-прежнему нулевая (нет излома в точке шва), но большая часть
    возврата на маршрут происходит в первой половине дистанции примыкания.
    """
    s = smoothstep01(t)
    return 1.0 - (1.0 - s) * (1.0 - s)


def smoothstep01(t: float) -> float:
    """Гладкая S-образная кривая (0 при t<=0, 1 при t>=1, непрерывная
    производная на границах) — используется для плавного примыкания
    маршрута к реальному треку на границах эпизода."""
    c = 0.0 if t < 0 else 1.0 if t > 1 else t
    return c * c * (3 - 2 * c)


def get_heading_ref(
    lats: List[float], lons: List[float], idx: int, direction: int,
    min_dist: float = 20.0, max_steps: int = 80,
) -> Tuple[float, float]:
    """Точка реального трека на расстоянии не менее min_dist метров ДО
    (direction=-1) или ПОСЛЕ (direction=+1) индекса idx — используется, чтобы
    определить курс движения по треку рядом с границей эпизода (устойчиво
    к GPS-дрожанию соседних точек)."""
    n = len(lats)
    i = idx
    dist = 0.0
    steps = 0
    while steps < max_steps:
        nxt = i + direction
        if nxt < 0 or nxt >= n:
            break
        dist += haversine(lats[i], lons[i], lats[nxt], lons[nxt])
        i = nxt
        steps += 1
        if dist >= min_dist:
            break
    return lats[i], lons[i]


def unit_heading(
    lat_a: float, lon_a: float, lat_b: float, lon_b: float,
) -> Optional[Tuple[float, float]]:
    """Единичный вектор направления A→B в локальных метрах восток/север."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians((lat_a + lat_b) / 2))
    dx = (lon_b - lon_a) * m_per_deg_lon
    dy = (lat_b - lat_a) * m_per_deg_lat
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return None
    return dx / length, dy / length


def project_point(
    lat: float, lon: float, direction: Tuple[float, float], dist: float,
) -> Tuple[float, float]:
    """Точка на расстоянии dist метров от (lat, lon) вдоль направления direction."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    dx, dy = direction
    return lat + (dy * dist) / m_per_deg_lat, lon + (dx * dist) / m_per_deg_lon


def make_hermite_curve(
    lat0: float, lon0: float, heading_ref_before: Optional[Tuple[float, float]],
    lat1: float, lon1: float, heading_ref_after: Optional[Tuple[float, float]],
):
    """Кубическая кривая Безье от (lat0,lon0) к (lat1,lon1), которая у старта
    идёт по курсу реального трека ДО пробела (heading_ref_before), а у конца —
    по курсу ПОСЛЕ пробела (heading_ref_after). Если курс не определён, опорные
    точки ложатся на прямую хорду и кривая вырождается в обычную прямую линию.
    Возвращает функцию alpha(0..1) -> (lat, lon)."""
    d = haversine(lat0, lon0, lat1, lon1)
    if d < 1e-6:
        return lambda alpha: (lat0, lon0)

    chord_dir = unit_heading(lat0, lon0, lat1, lon1)
    dir_in = (
        unit_heading(heading_ref_before[0], heading_ref_before[1], lat0, lon0)
        if heading_ref_before else None
    ) or chord_dir
    dir_out = (
        unit_heading(lat1, lon1, heading_ref_after[0], heading_ref_after[1])
        if heading_ref_after else None
    ) or chord_dir

    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians((lat0 + lat1) / 2))
    x3 = (lon1 - lon0) * m_per_deg_lon
    y3 = (lat1 - lat0) * m_per_deg_lat
    k = d / 3
    x1, y1 = dir_in[0] * k, dir_in[1] * k
    x2, y2 = x3 - dir_out[0] * k, y3 - dir_out[1] * k

    def curve_at(alpha: float) -> Tuple[float, float]:
        u = 1 - alpha
        x = 3 * u * u * alpha * x1 + 3 * u * alpha * alpha * x2 + alpha ** 3 * x3
        y = 3 * u * u * alpha * y1 + 3 * u * alpha * alpha * y2 + alpha ** 3 * y3
        return lat0 + y / m_per_deg_lat, lon0 + x / m_per_deg_lon

    return curve_at


_elevation_cache: Dict[str, Optional[float]] = {}


def _ele_cache_key(lat: float, lon: float) -> str:
    return f"{lat:.4f},{lon:.4f}"


def fetch_elevations_raw(coord_pairs: List[Tuple[float, float]]) -> Optional[List[Optional[float]]]:
    lat_str = ",".join(f"{lat:.6f}" for lat, _ in coord_pairs)
    lon_str = ",".join(f"{lon:.6f}" for _, lon in coord_pairs)
    try:
        url = f"{ELEVATION_PRIMARY}?latitude={lat_str}&longitude={lon_str}"
        data = fetch_json(url)
        elevs = data.get("elevation")
        if isinstance(elevs, list) and len(elevs) == len(coord_pairs):
            return [v if isinstance(v, (int, float)) else None for v in elevs]
    except Exception:
        pass
    try:
        loc_str = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in coord_pairs)
        url = f"{ELEVATION_FALLBACK}?locations={loc_str}"
        data = fetch_json(url)
        results = data.get("results")
        if isinstance(results, list) and len(results) == len(coord_pairs):
            return [
                r.get("elevation") if isinstance(r, dict) and isinstance(r.get("elevation"), (int, float)) else None
                for r in results
            ]
    except Exception:
        pass
    return None


def fetch_elevations(points: List[Tuple[float, float]]) -> Dict[str, Optional[float]]:
    """points: [(lat, lon), ...]. Возвращает {cache_key: высота|None}."""
    need = []
    for lat, lon in points:
        key = _ele_cache_key(lat, lon)
        if key not in _elevation_cache:
            need.append((lat, lon, key))
    for i in range(0, len(need), MAX_ELEVATION_POINTS_PER_REQUEST):
        chunk = need[i:i + MAX_ELEVATION_POINTS_PER_REQUEST]
        result = fetch_elevations_raw([(lat, lon) for lat, lon, _ in chunk])
        if result is None:
            for _, _, key in chunk:
                _elevation_cache[key] = None
        else:
            for (_, _, key), val in zip(chunk, result):
                _elevation_cache[key] = val
    return {_ele_cache_key(lat, lon): _elevation_cache.get(_ele_cache_key(lat, lon)) for lat, lon in points}


def calibrate_elevation(dem: float, alpha: float, ele0: float, dem0: float, ele1: float, dem1: float) -> float:
    """Барометрическая/DEM высота имеют смещение друг от друга (10-30м) —
    плавно перетекаем от известного смещения на старте к известному смещению
    на конце, чтобы на швах эпизода не было ступеньки высоты."""
    return dem + (1 - alpha) * (ele0 - dem0) + alpha * (ele1 - dem1)


def _smooth_anchor_values(vals: List[float]) -> List[float]:
    """Опорные точки высоты рельефа берутся редко (до 98 на весь маршрут),
    а само DEM (SRTM/Copernicus) на таком шаге содержит мелкий шум отдельных
    точек (скалы, растительность, ошибки растра). Если тянуть между опорными
    точками прямые отрезки без сглаживания, шум одной точки превращается в
    резкий излом на профиле высоты — выглядит неправдоподобно. Сглаживаем
    сами значения в опорных точках, сохраняя общий тренд рельефа."""
    n = len(vals)
    if n < 3:
        return list(vals)
    out = list(vals)
    for _ in range(2):
        nxt = list(out)
        for i in range(1, n - 1):
            nxt[i] = 0.25 * out[i - 1] + 0.5 * out[i] + 0.25 * out[i + 1]
        out = nxt
    return out


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

    # Локальные микро-выбросы: короткая эрратичная серия точек (GPS-шум/
    # мультипуть, например под лесным пологом), где скачок НЕ дотягивает до
    # min_cluster_dist_m (не «дальняя» глушилка), но скорость перехода ГРУБО
    # (в разы) превышает заявленный максимум — такое не спишешь на обычный
    # шум GPS. В отличие от дальних эпизодов, такая серия обычно не
    # заканчивается чётким «скачком назад», а просто затухает до разумной
    # скорости — поэтому возврат ищем по критерию «скорость от точки ДО
    # эпизода до кандидата снова правдоподобна», а не по фикс. расстоянию.
    MICRO_OVERSPEED_MULT = 3.0
    MICRO_SCAN_SECONDS = 120.0
    MICRO_SCAN_POINTS = 200
    MICRO_BACKWARD_DIST_M = 50.0

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
            dt_jump = max((times[first_bad] - times[t_out]).total_seconds(), 1.0)
            if d_jump / dt_jump >= max_speed_mps * MICRO_OVERSPEED_MULT:
                ref_lat_m, ref_lon_m = lats[t_out], lons[t_out]
                ref_time_m = times[t_out]
                resume_idx = None
                scan_end = min(n, first_bad + 1 + MICRO_SCAN_POINTS)
                for j in range(first_bad + 1, scan_end):
                    dt_after = max((times[j] - ref_time_m).total_seconds(), 1.0)
                    if dt_after > MICRO_SCAN_SECONDS:
                        break
                    d_after = haversine(lats[j], lons[j], ref_lat_m, ref_lon_m)
                    if d_after / dt_after <= max_speed_mps:
                        resume_idx = j
                        break
                if resume_idx is not None:
                    # Расширяем эпизод НАЗАД: точки перед t_out могли уже
                    # уйти в сторону "выброса" со скоростью, ещё не превысившей
                    # порог max_speed_mps (из-за большого dt), хотя по факту
                    # они уже далеко от места возврата (resume_idx) -
                    # то есть являются частью того же самого микро-выброса.
                    ref_lat_r, ref_lon_r = lats[resume_idx], lons[resume_idx]
                    j = t_out
                    steps_back = 0
                    while j >= 0 and steps_back < MICRO_SCAN_POINTS:
                        d_local = haversine(lats[j], lons[j], ref_lat_r, ref_lon_r)
                        if d_local < MICRO_BACKWARD_DIST_M:
                            break
                        first_bad = j
                        j -= 1
                        steps_back += 1
                    episodes.append((first_bad, resume_idx - 1))
                    while k < len(teleport_at) and teleport_at[k] < resume_idx:
                        k += 1
                    continue
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

    # ─── Эпизод глушения без «скачка назад» ────────────────────────────────
    # Трек может обрываться прямо во время глушения (устройство выключили или
    # остановили запись, пока сигнал ещё не восстановился) — тогда среди
    # телепортаций нет пары «туда-обратно», т.к. «обратно» попросту не
    # произошло. Ищем самую ПОЗДНЮЮ телепортацию-«выход» после последнего уже
    # найденного эпизода, от которой трек так и не вернулся близко к исходной
    # точке до самого конца записи, и считаем всё от неё до конца трека одним
    # хвостовым эпизодом (без интерполяции — восстанавливать некуда).
    last_covered = max((e[1] + 1 for e in episodes), default=0)
    for k2 in range(len(teleport_at) - 1, -1, -1):
        t_out = teleport_at[k2]
        if t_out < last_covered:
            break  # не залезаем в уже обработанный участок

        first_bad = t_out + 1
        if first_bad >= n:
            continue

        d_jump = haversine(lats[t_out], lons[t_out], lats[first_bad], lons[first_bad])
        if d_jump < min_cluster_dist_m:
            continue

        dist_end = haversine(lats[n - 1], lons[n - 1], lats[t_out], lons[t_out])
        if dist_end >= min_cluster_dist_m:
            episodes.append((first_bad, n - 1))
            break

    episodes.sort(key=lambda e: e[0])
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
    max_lookback_s: float = 300.0,
    max_vert_speed_mps: float = 5.0,
) -> int:
    """
    Сканирует назад от first_bad и ищет точки, которые:
    - смещены от post-jammer-позиции больше чем на displaced_m, ИЛИ
    - имеют аномальную скорость изменения высоты > max_vert_speed_mps
      (глушилка часто портит высоту раньше, чем координаты).
    Возвращает новый (расширенный) first_bad.

    max_lookback_s ограничивает СУММАРНЫЙ откат назад от исходного начала
    эпизода: «ранний дрейф» по определению короткий (это не поиск похожего
    участка где-то в далёком прошлом трека). Без этого ограничения на
    треках, где GPS реально проходит неподалёку (в пределах displaced_m)
    от точки post-jammer-возврата лишь изредка, можно было ошибочно
    откатиться на десятки минут вглубь нормального трека.
    """
    if after_idx >= len(lats):
        return first_bad

    ref_lat, ref_lon = lats[after_idx], lons[after_idx]
    ref_time = times[first_bad]

    new_first = first_bad
    i = first_bad - 1

    while i >= 0:
        # Суммарный откат назад ограничен — см. docstring.
        if (ref_time - times[i]).total_seconds() > max_lookback_s:
            break

        dt = abs((times[i + 1] - times[i]).total_seconds())
        if dt > 3600:
            break  # реальный разрыв в записи — до него GPS всё ещё надёжен

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
    sensors0: Optional[dict] = None,
    sensors1: Optional[dict] = None,
    heading_ref_before: Optional[Tuple[float, float]] = None,
    heading_ref_after: Optional[Tuple[float, float]] = None,
) -> List[dict]:
    """
    Генерирует интерполированные точки между (lat0,lon0) и (lat1,lon1) вдоль
    плавной кривой (кубический Безье), которая у границ идёт по курсу
    реального трека до/после пробела — как и при маршрутизации по тропам —
    а не по прямой линии со изломом на стыке. Не включает сами граничные точки.

    sensors0/sensors1 — необязательные словари {"hr", "cad", "atemp"} со
    значениями граничных точек. Если у ОБЕИХ границ есть значение конкретного
    поля — оно линейно интерполируется для вставленных точек; если данных
    нет — поле просто не добавляется в результат (никаких нулей-заглушек).
    Скорость вставленных точек — реальная средняя скорость перегона
    (расстояние/время), а не фиктивный 0.0.
    """
    total_s = (t1 - t0).total_seconds()
    if total_s <= interval_s:
        return []

    sensors0 = sensors0 or {}
    sensors1 = sensors1 or {}

    leg_dist = haversine(lat0, lon0, lat1, lon1)
    speed = leg_dist / total_s if total_s > 0 else 0.0

    curve_at = make_hermite_curve(
        lat0, lon0, heading_ref_before, lat1, lon1, heading_ref_after,
    )

    def _both_finite(a: Optional[float], b: Optional[float]) -> bool:
        return a is not None and b is not None and math.isfinite(a) and math.isfinite(b)

    hr0, hr1 = sensors0.get("hr"), sensors1.get("hr")
    cad0, cad1 = sensors0.get("cad"), sensors1.get("cad")
    atemp0, atemp1 = sensors0.get("atemp"), sensors1.get("atemp")

    hr_ok = _both_finite(hr0, hr1)
    cad_ok = _both_finite(cad0, cad1)
    atemp_ok = _both_finite(atemp0, atemp1)

    result: List[dict] = []
    t = interval_s
    while t < total_s - 1e-6:
        alpha = t / total_s
        lat, lon = curve_at(alpha)
        pt = {
            "lat": lat,
            "lon": lon,
            "ele": ele0 + alpha * (ele1 - ele0),
            "time": t0 + timedelta(seconds=t),
            "speed": speed,
        }
        if hr_ok:
            pt["hr"] = round(hr0 + alpha * (hr1 - hr0))
        if cad_ok:
            pt["cad"] = round(cad0 + alpha * (cad1 - cad0))
        if atemp_ok:
            pt["atemp"] = atemp0 + alpha * (atemp1 - atemp0)
        result.append(pt)
        t += interval_s

    return result


def build_routed_interpolated(
    lat0: float, lon0: float, ele0: float, t0: datetime,
    lat1: float, lon1: float, ele1: float, t1: datetime,
    interval_s: float,
    sensors0: dict,
    sensors1: dict,
    profile: str,
    max_speed_mps: float,
    routed_budget: dict,
    heading_ref_before: Optional[Tuple[float, float]] = None,
    heading_ref_after: Optional[Tuple[float, float]] = None,
) -> dict:
    """
    Пытается заполнить пробел маршрутом по дорогам/тропам (OSRM), с высотой
    рельефа из Open-Meteo (резерв Opentopodata). При любой ошибке сети/API
    или неправдоподобном результате молча откатывается на обычную линейную
    интерполяцию — результат должен получаться всегда, вне зависимости от
    доступности внешних сервисов.

    profile: "foot" | "bike" | "car".
    routed_budget: {"used": int, "max": int} — общий на весь трек лимит
    запросов маршрутизации (публичные сервисы бесплатны, не будем их спамить).

    Возвращает {"points": [...], "method": "routed"|"linear", "reason"?: str,
    "dem_ok"?: bool, "distance_km"?: float}.
    """

    def fallback(reason: Optional[str] = None) -> dict:
        return {
            "points": build_interpolated_points(
                lat0, lon0, ele0, t0, lat1, lon1, ele1, t1,
                interval_s, sensors0, sensors1,
                heading_ref_before, heading_ref_after,
            ),
            "method": "linear",
            "reason": reason,
        }

    total_s = (t1 - t0).total_seconds()
    d = haversine(lat0, lon0, lat1, lon1)

    if total_s <= interval_s or d < 30:
        return fallback()

    if routed_budget["used"] >= routed_budget["max"]:
        return fallback(
            f"достигнут лимит маршрутизации ({routed_budget['max']} эпизодов) — простое соединение (офлайн)"
        )
    routed_budget["used"] += 1

    try:
        route = fetch_route(profile, (lat0, lon0), (lat1, lon1))
    except Exception:
        route = None
    if not route:
        return fallback("маршрут не найден")

    r = route["routes"][0]
    wp = route.get("waypoints")
    if not wp or wp[0].get("distance", 0) > 300 or wp[1].get("distance", 0) > 300:
        return fallback("граничные точки слишком далеко от дороги/тропы")

    route_dist = r["distance"]
    if route_dist / max(d, 1) > 4:
        return fallback("маршрут слишком длинный (большой объезд)")
    if route_dist / total_s > max_speed_mps:
        return fallback("маршрут требует нереальной скорости")

    sampled, route_len = resample_route(r["geometry"]["coordinates"], total_s, interval_s)

    # ─── Плавное примыкание маршрута к реальному треку ──────────────────
    # OSRM привязывает граничные точки к БЛИЖАЙШЕЙ дороге/тропе (до 300 м
    # в сторону), из-за чего трек мог влетать в маршрут и вылетать из него
    # под острым/прямым углом. Вместо стягивания к прямой хорде (которая не
    # учитывает, куда реально двигался трек) — стягиваем к линии,
    # продолженной по КУРСУ реального трека до/после эпизода: рядом с
    # границей путь сначала идёт в том же направлении, в котором трек уже
    # двигался, и лишь затем поворачивает на маршрут. Дистанция примыкания
    # намеренно небольшая — быстрее вернуться на тропу важнее, чем долго
    # и плавно к ней подходить.
    seam_blend_dist_m = min(40.0, route_len * 0.15)
    if seam_blend_dist_m > 0 and sampled:
        chord_dir = unit_heading(lat0, lon0, lat1, lon1)
        dir_in = (
            unit_heading(heading_ref_before[0], heading_ref_before[1], lat0, lon0)
            if heading_ref_before else None
        ) or chord_dir
        dir_out = (
            unit_heading(lat1, lon1, heading_ref_after[0], heading_ref_after[1])
            if heading_ref_after else None
        ) or chord_dir

        for s in sampled:
            dist_from_start = s["alpha"] * route_len
            dist_from_end = route_len - dist_from_start
            w_start = (
                1.0 if dist_from_start >= seam_blend_dist_m
                else seam_blend_weight(dist_from_start / seam_blend_dist_m)
            )
            w_end = (
                1.0 if dist_from_end >= seam_blend_dist_m
                else seam_blend_weight(dist_from_end / seam_blend_dist_m)
            )

            if w_start < 1 and dir_in:
                p_lat, p_lon = project_point(lat0, lon0, dir_in, dist_from_start)
                s["lat"] = p_lat * (1 - w_start) + s["lat"] * w_start
                s["lon"] = p_lon * (1 - w_start) + s["lon"] * w_start
            if w_end < 1 and dir_out:
                p_lat, p_lon = project_point(lat1, lon1, dir_out, -dist_from_end)
                s["lat"] = p_lat * (1 - w_end) + s["lat"] * w_end
                s["lon"] = p_lon * (1 - w_end) + s["lon"] * w_end

    # ─── Высота: ≤98 опорных точек маршрута + 2 граничные ───
    max_anchors = 98
    anchor_step = max(1, math.ceil(len(sampled) / max_anchors)) if sampled else 1
    anchor_idxs = list(range(0, len(sampled), anchor_step))
    if sampled and anchor_idxs[-1] != len(sampled) - 1:
        anchor_idxs.append(len(sampled) - 1)

    dem_map: Optional[Dict[str, Optional[float]]] = None
    try:
        anchor_coords = [(sampled[i]["lat"], sampled[i]["lon"]) for i in anchor_idxs]
        dem_map = fetch_elevations([(lat0, lon0), (lat1, lon1)] + anchor_coords)
    except Exception:
        dem_map = None

    dem0 = dem_map.get(_ele_cache_key(lat0, lon0)) if dem_map else None
    dem1 = dem_map.get(_ele_cache_key(lat1, lon1)) if dem_map else None
    dem_ok = (
        dem_map is not None and dem0 is not None and dem1 is not None
        and all(
            dem_map.get(_ele_cache_key(sampled[i]["lat"], sampled[i]["lon"])) is not None
            for i in anchor_idxs
        )
    )

    dem_at_index: Optional[List[float]] = None
    if dem_ok and sampled:
        anchor_vals = _smooth_anchor_values(
            [dem_map.get(_ele_cache_key(sampled[i]["lat"], sampled[i]["lon"])) for i in anchor_idxs]
        )
        dem_at_index = [0.0] * len(sampled)
        for i, val in zip(anchor_idxs, anchor_vals):
            dem_at_index[i] = val
        prev_idx, prev_val = 0, dem_at_index[0]
        for idx in anchor_idxs[1:]:
            val = dem_at_index[idx]
            for j in range(prev_idx + 1, idx):
                a = (j - prev_idx) / (idx - prev_idx)
                dem_at_index[j] = prev_val + a * (val - prev_val)
            prev_idx, prev_val = idx, val
        for j in range(prev_idx + 1, len(sampled)):
            dem_at_index[j] = prev_val

    def _both_finite(a: Optional[float], b: Optional[float]) -> bool:
        return a is not None and b is not None and math.isfinite(a) and math.isfinite(b)

    hr0, hr1 = sensors0.get("hr"), sensors1.get("hr")
    cad0, cad1 = sensors0.get("cad"), sensors1.get("cad")
    atemp0, atemp1 = sensors0.get("atemp"), sensors1.get("atemp")
    hr_ok = _both_finite(hr0, hr1)
    cad_ok = _both_finite(cad0, cad1)
    atemp_ok = _both_finite(atemp0, atemp1)

    pts: List[dict] = []
    prev_lat, prev_lon, prev_t = lat0, lon0, 0.0
    for k, s in enumerate(sampled):
        if dem_ok:
            ele = calibrate_elevation(dem_at_index[k], s["alpha"], ele0, dem0, ele1, dem1)
        else:
            ele = ele0 + s["alpha"] * (ele1 - ele0)
        dt = s["t"] - prev_t
        leg_dist = haversine(prev_lat, prev_lon, s["lat"], s["lon"])
        speed = leg_dist / dt if dt > 0 else 0.0
        pt = {
            "lat": s["lat"], "lon": s["lon"], "ele": ele,
            "time": t0 + timedelta(seconds=s["t"]), "speed": speed,
        }
        if hr_ok:
            pt["hr"] = round(hr0 + s["alpha"] * (hr1 - hr0))
        if cad_ok:
            pt["cad"] = round(cad0 + s["alpha"] * (cad1 - cad0))
        if atemp_ok:
            pt["atemp"] = atemp0 + s["alpha"] * (atemp1 - atemp0)
        pts.append(pt)
        prev_lat, prev_lon, prev_t = s["lat"], s["lon"], s["t"]

    return {
        "points": pts,
        "method": "routed",
        "dem_ok": dem_ok,
        "distance_km": route_dist / 1000,
    }


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
    pt: dict,
) -> ET.Element:
    """Создаёт элемент <trkpt> с минимальным набором полей.

    pt: {"lat", "lon", "ele", "time", "speed"?, "hr"?, "cad"?, "atemp"?} —
    сенсорные поля необязательны: пишем только то, что реально удалось
    интерполировать между граничными точками, никаких нулей-заглушек
    (Strava/Garmin Connect учитывают их в среднюю скорость/каденс за
    активность, занижая статистику).
    """
    el = ET.Element(f"{{{ns}}}trkpt")
    el.set("lat", f"{pt['lat']:.8f}")
    el.set("lon", f"{pt['lon']:.8f}")

    ele_el = ET.SubElement(el, f"{{{ns}}}ele")
    ele_el.text = f"{pt['ele']:.2f}"

    time_el = ET.SubElement(el, f"{{{ns}}}time")
    time_el.text = fmt_time(pt["time"])

    fields = []
    if "atemp" in pt:
        fields.append(("atemp", f"{pt['atemp']:.1f}"))
    if "hr" in pt:
        fields.append(("hr", str(pt["hr"])))
    if "cad" in pt:
        fields.append(("cad", str(pt["cad"])))
    if "speed" in pt:
        fields.append(("speed", f"{pt['speed']:.2f}"))

    if fields:
        ext_el = ET.SubElement(el, f"{{{ns}}}extensions")
        tpe = ET.SubElement(ext_el, f"{{{ns3}}}TrackPointExtension")
        for tag, text in fields:
            sub_el = ET.SubElement(tpe, f"{{{ns3}}}{tag}")
            sub_el.text = text

    return el


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
    gap_fill_mode: str = "line",
    verbose: bool = True,
) -> None:
    """Исправляет GPX-трек: удаляет зашумлённые точки и интерполирует пробел.

    pre_jitter_dist_m: если > 0, дополнительно удаляет точки непосредственно
    ДО эпизода, которые смещены от post-jammer-позиции более чем на это расстояние
    (убирает «ранний дрейф» GPS перед включением глушилки).

    gap_fill_mode: "line" — простое соединение (офлайн), "foot"/"bike"/"car" —
    маршрутизация по дорогам/тропам через OSRM с высотой рельефа из
    Open-Meteo. При ошибке сети/API или неправдоподобном маршруте молча
    откатывается на прямую линию для конкретного эпизода.
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

    # Сенсорные данные (пульс/каденс/температура) из TrackPointExtension —
    # нужны, чтобы интерполировать их для вставленных точек, а не писать нули.
    def _find_ext_value(p: ET.Element, tag: str) -> float:
        el = p.find(f".//{{{NS3}}}{tag}")
        return float(el.text) if el is not None and el.text is not None else float("nan")

    hrs    = [_find_ext_value(p, "hr") for p in pts]
    cads   = [_find_ext_value(p, "cad") for p in pts]
    atemps = [_find_ext_value(p, "atemp") for p in pts]

    length_before = sum(
        haversine(lats[i - 1], lons[i - 1], lats[i], lons[i]) for i in range(1, n)
    )
    if verbose:
        print(f"Длина трека до исправления: {length_before / 1000:.2f} км")

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
    routed_budget = {"used": 0, "max": MAX_ROUTED_EPISODES}

    if gap_fill_mode in ("foot", "bike", "car"):
        routed_requested = sum(
            1 for ep_start, ep_end in episodes if ep_end + 1 < n
        )
        if routed_requested > MAX_ROUTED_EPISODES:
            print(
                f"Внимание: запрошено {routed_requested} эпизодов с маршрутизацией, "
                f"но лимит — {MAX_ROUTED_EPISODES} за один запуск "
                "(остальные будут заполнены прямой линией)."
            )

    for ep_start, ep_end in episodes:
        after_idx  = ep_end + 1 if ep_end + 1 < n else None

        # Проход назад для аномалий ВЫСОТЫ (запускаем ПЕРВЫМ):
        # Обнаруживает "плато неправильной высоты" и предшествующий крэш.
        # (Координаты таких точек корректны, но высота уже испорчена глушилкой.)
        # Идёт первым, потому что у него нет ограничения на суммарное время
        # отката (аномалия сама себя ограничивает) — а координатный проход
        # ниже использует УЖЕ расширенную границу как точку отсчёта для
        # своего ограниченного по времени отката, чтобы поймать раннее
        # смещение координат сразу ПЕРЕД началом высотного крэша, а не
        # отсчитывать время от далёкого сырого начала эпизода.
        if max_vert_speed_mps > 0:
            before_candidate = ep_start - 1
            i = before_candidate
            new_alt_start = ep_start
            frozen_back_s = 0.0
            FROZEN_EPS_M = 0.005
            MAX_FROZEN_BACK_S = 60.0
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
                    frozen_back_s = 0.0  # настоящая аномалия сбрасывает лимит
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

                # "Замороженное" показание высоты (глушилка иногда сначала
                # застывает на одном значении, и лишь потом начинается сам
                # крэш) — считаем частью глитча, но не дальше MAX_FROZEN_BACK_S
                # назад, чтобы не съесть настоящий длительный привал.
                if dh_in < FROZEN_EPS_M and frozen_back_s < MAX_FROZEN_BACK_S:
                    frozen_back_s += dt_in
                    new_alt_start = i
                    i -= 1
                    continue

                break  # всё чисто — останавливаемся

            if new_alt_start < ep_start:
                ep_start = new_alt_start

        # Расширяем эпизод назад по координатам: убираем «ранний дрейф»
        # перед глушилкой (запускается ВТОРЫМ, от уже расширенной по
        # высоте границы — см. комментарий выше).
        if pre_jitter_dist_m > 0 and after_idx is not None:
            ep_start = extend_episode_backward(
                lats, lons, eles, times,
                first_bad=ep_start,
                after_idx=after_idx,
                displaced_m=pre_jitter_dist_m,
                max_vert_speed_mps=max_vert_speed_mps,
            )

        bad_count = ep_end - ep_start + 1
        total_removed += bad_count

        # Граничные хорошие точки
        before_idx = ep_start - 1

        if verbose:
            dur_s = (
                (times[after_idx] - times[before_idx]).total_seconds()
                if after_idx is not None else 0
            )
            tail_marker = "  [обрыв в конце трека, без возврата]" if after_idx is None else ""
            print(
                f"\nЭпизод глушения: точки [{ep_start}..{ep_end}]"
                f"  ({bad_count} точек, {dur_s:.0f} с пробел){tail_marker}"
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
            else:
                print(
                    "  После: — (запись обрывается во время глушения, точки удаляются до конца трека)"
                )

        for idx in range(ep_start, ep_end + 1):
            remove_set.add(idx)

        # Интерполяция
        if interpolate and after_idx is not None:
            # Используем стабильную высоту из окна 30–300 с ДО эпизода,
            # чтобы не начинать от уже испорченной глушилкой высоты.
            stable_ele0 = get_pre_episode_ele(eles, times, before_idx)
            sensors0 = {
                "hr": hrs[before_idx],
                "cad": cads[before_idx],
                "atemp": atemps[before_idx],
            }
            sensors1 = {
                "hr": hrs[after_idx],
                "cad": cads[after_idx],
                "atemp": atemps[after_idx],
            }

            heading_ref_before = (
                get_heading_ref(lats, lons, before_idx, -1) if before_idx > 0 else None
            )
            heading_ref_after = (
                get_heading_ref(lats, lons, after_idx, 1) if after_idx < len(lats) - 1 else None
            )

            if gap_fill_mode in ("foot", "bike", "car"):
                result = build_routed_interpolated(
                    lats[before_idx], lons[before_idx], stable_ele0,     times[before_idx],
                    lats[after_idx],  lons[after_idx],  eles[after_idx], times[after_idx],
                    interval_s, sensors0, sensors1,
                    gap_fill_mode, max_speed_mps, routed_budget,
                    heading_ref_before, heading_ref_after,
                )
                interp = result["points"]
                if verbose:
                    if result["method"] == "routed":
                        dem_note = "" if result.get("dem_ok") else ", высота: линейно (DEM недоступен)"
                        print(
                            f"  Заполнение    : маршрут ({gap_fill_mode}), "
                            f"{result['distance_km']:.2f} км{dem_note}"
                        )
                    else:
                        reason = result.get("reason")
                        suffix = f" — {reason}" if reason else ""
                        print(f"  Заполнение    : простое соединение (офлайн){suffix}")
            else:
                interp = build_interpolated_points(
                    lats[before_idx], lons[before_idx], stable_ele0,     times[before_idx],
                    lats[after_idx],  lons[after_idx],  eles[after_idx], times[after_idx],
                    interval_s,
                    sensors0=sensors0,
                    sensors1=sensors1,
                    heading_ref_before=heading_ref_before,
                    heading_ref_after=heading_ref_after,
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
    length_after = 0.0
    prev_lat: Optional[float] = None
    prev_lon: Optional[float] = None

    def add_leg(lat: float, lon: float) -> None:
        nonlocal length_after, prev_lat, prev_lon
        if prev_lat is not None:
            length_after += haversine(prev_lat, prev_lon, lat, lon)
        prev_lat, prev_lon = lat, lon

    for i, pt in enumerate(pts):
        if i in insertion_before:
            for ipt in insertion_before[i]:
                seg.append(make_trkpt(NS, NS3, ipt))
                add_leg(ipt["lat"], ipt["lon"])

        if i not in remove_set:
            seg.append(pt)
            add_leg(lats[i], lons[i])

    # --- 6. Запись ---
    tree.write(output_path, encoding="UTF-8", xml_declaration=True)

    if verbose:
        print(f"\nУдалено плохих точек  : {total_removed}")
        print(f"Вставлено интерполир. : {total_inserted}")
        print(f"Итого точек в треке   : {n - total_removed + total_inserted}")
        print(
            f"Длина трека после исправления: {length_after / 1000:.2f} км"
            f" (было {length_before / 1000:.2f} км,"
            f" разница {(length_after - length_before) / 1000:+.2f} км)"
        )
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
  python fix_gpx_jammer.py track.gpx --profile hiking
  python fix_gpx_jammer.py track.gpx --profile car
  python fix_gpx_jammer.py track.gpx --profile mtb --max-speed 28  # профиль + ручная подстройка
  python fix_gpx_jammer.py track.gpx --no-interpolate # только удалить точки
  python fix_gpx_jammer.py track.gpx --gap-fill foot  # маршрут по тропам вместо прямой линии
  python fix_gpx_jammer.py track.gpx --gap-fill car   # маршрут по дорогам (для авто/мото)

Доступные профили (--profile), по умолчанию — """
        + DEFAULT_PROFILE
        + """:
"""
        + "\n".join(
            f"  {key:<12} {p['label']}" for key, p in ACTIVITY_PROFILES.items()
        )
        + "\n        ",
    )
    parser.add_argument("input",  help="Входной GPX-файл")
    parser.add_argument("output", nargs="?", help="Выходной GPX-файл (по умолчанию: <input>_fixed.gpx)")
    parser.add_argument(
        "--profile", choices=sorted(ACTIVITY_PROFILES), default=None,
        metavar="NAME",
        help=f"Профиль активности, задаёт значения ниже по умолчанию (по умолчанию: {DEFAULT_PROFILE}). "
             "См. список профилей ниже. Явно указанные флаги --max-speed и т.п. всегда переопределяют значение профиля.",
    )
    parser.add_argument(
        "--max-speed", type=float, default=None,
        metavar="M/S",
        help="Максимальная реалистичная скорость в м/с (по умолчанию — из профиля, для hiking это 10). "
             "Переопределяет значение --profile.",
    )
    parser.add_argument(
        "--min-distance", type=float, default=None,
        metavar="M",
        help="Минимальное расстояние от нормального трека до ложного кластера, "
             "чтобы признать его глушилкой (по умолчанию — из профиля). Переопределяет --profile.",
    )
    parser.add_argument(
        "--interval", type=float, default=None,
        metavar="SEC",
        help="Шаг интерполяции в секундах (по умолчанию — из профиля). "
             "Подберите под исходную частоту записи трека. Переопределяет --profile.",
    )
    parser.add_argument(
        "--max-vert-speed", type=float, default=None,
        metavar="M/S",
        help="Максимальная реалистичная вертикальная скорость в м/с (по умолчанию — из профиля). "
             "Точки с большим изменением высоты удаляются вместе с jammer-эпизодом. Переопределяет --profile.",
    )
    parser.add_argument(
        "--pre-jitter-dist", type=float, default=None,
        metavar="M",
        help="Убирать точки ПЕРЕД эпизодом, смещённые от post-jammer-позиции "
             "более чем на M метров (ранний дрейф GPS). 0 — не расширять назад "
             "(по умолчанию — из профиля). Переопределяет --profile.",
    )
    parser.add_argument(
        "--no-interpolate", action="store_true",
        help="Не вставлять интерполированные точки — просто удалить ложные.",
    )
    parser.add_argument(
        "--gap-fill", choices=["line", "foot", "bike", "car"], default=None,
        metavar="MODE",
        help="Способ заполнения пробела: line — простое соединение (офлайн); "
             "foot/bike/car — маршрутизация по дорогам и тропам через OSRM "
             "(routing.openstreetmap.de) с высотой рельефа из Open-Meteo "
             "(требует интернет; при ошибке сети/API молча откатывается на line). "
             "По умолчанию — режим, подходящий выбранному --profile "
             "(hiking/running/horse → foot, mtb/road_bike → bike, car → car, остальные → line).",
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

    profile = ACTIVITY_PROFILES[args.profile or DEFAULT_PROFILE]
    profile_key = args.profile or DEFAULT_PROFILE
    gap_fill_mode = args.gap_fill or PROFILE_INTERP_MODE.get(profile_key, "line")

    if not args.quiet:
        print(f"Профиль: {profile['label']} ({profile_key})")
        if gap_fill_mode != "line":
            print(f"Заполнение пробелов: маршрут ({gap_fill_mode}) через OSRM/Open-Meteo")

    fix_gpx(
        input_path=inp,
        output_path=out,
        max_speed_mps=args.max_speed if args.max_speed is not None else profile["max_speed"],
        min_cluster_dist_m=args.min_distance if args.min_distance is not None else profile["min_distance"],
        interpolate=not args.no_interpolate,
        interval_s=args.interval if args.interval is not None else profile["interval"],
        pre_jitter_dist_m=args.pre_jitter_dist if args.pre_jitter_dist is not None else profile["pre_jitter_dist"],
        max_vert_speed_mps=args.max_vert_speed if args.max_vert_speed is not None else profile["max_vert_speed"],
        gap_fill_mode=gap_fill_mode,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
