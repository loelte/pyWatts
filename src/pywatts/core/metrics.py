"""Ride metrics. Input: 1 Hz samples. Empty input -> 0.0."""

from __future__ import annotations

from collections.abc import Sequence

_WINDOW = 30


def average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if len(values) else 0.0


def normalized_power(power_w: Sequence[float]) -> float:
    """30 s rolling mean, 4th-power mean, 4th root. < 30 samples -> average."""
    n = len(power_w)
    if n < _WINDOW:
        return average(power_w)
    window = sum(power_w[:_WINDOW])
    total = (window / _WINDOW) ** 4
    for i in range(_WINDOW, n):
        window += power_w[i] - power_w[i - _WINDOW]
        total += (window / _WINDOW) ** 4
    return (total / (n - _WINDOW + 1)) ** 0.25


def kilojoules(power_w: Sequence[float], dt_s: float = 1.0) -> float:
    return sum(power_w) * dt_s / 1000.0


PEAK_DURATIONS = (3, 5, 10, 20, 30, 60, 120, 180, 300, 600, 1200, 1800, 3600)


def peak_powers(
    segments: Sequence[Sequence[float]], durations: Sequence[int] = PEAK_DURATIONS
) -> dict[int, float]:
    """Best mean power per duration (s). Each segment is uninterrupted 1 Hz power; an effort
    never spans two segments (a pause). Durations longer than every segment are left out."""
    best: dict[int, float] = {}
    for power in segments:
        total = [0.0]
        for p in power:
            total.append(total[-1] + p)
        for d in durations:
            if d <= len(power):
                peak = max(total[i + d] - total[i] for i in range(len(power) - d + 1)) / d
                best[d] = max(best.get(d, 0.0), peak)
    return best


FITNESS_DAYS = 42
FATIGUE_DAYS = 7
FTP_FROM_20_MIN = 0.95


def estimated_ftp(best_20_min_w: float) -> float:
    return best_20_min_w * FTP_FROM_20_MIN


def fitness_form(
    daily_load: Sequence[float],
    fitness_days: int = FITNESS_DAYS,
    fatigue_days: int = FATIGUE_DAYS,
) -> list[tuple[float, float, float]]:
    """Per day (fitness, fatigue, form) from the daily training load, oldest first.
    Fitness and fatigue are exponential averages of the load over long and short time constants;
    form is their difference: positive = fresh, negative = carrying fatigue."""
    fitness = fatigue = 0.0
    out = []
    for load in daily_load:
        fitness += (load - fitness) / fitness_days
        fatigue += (load - fatigue) / fatigue_days
        out.append((fitness, fatigue, fitness - fatigue))
    return out


def smooth(values: Sequence[float], radius: int = 3) -> list[float]:
    """Centered moving average over 2*radius+1 points; the window shrinks at the ends."""
    n = len(values)
    total = [0.0]
    for v in values:
        total.append(total[-1] + v)
    out = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out.append((total[hi] - total[lo]) / (hi - lo))
    return out


_G = 9.81
_RHO = 1.2  # air density, kg/m3
_CDA = 0.4  # m2, upright-ish rider
_CRR = 0.005
_BIKE_KG = 9.0
_DRIVETRAIN = 0.975
_MAX_GRADE = 0.2
_SMOOTH_S = 15  # radius of the altitude smoothing: raw altitude is noisy
_FALLBACK_KG = 75.0


def estimated_power(
    speed_kmh: Sequence[float | None],
    altitude_m: Sequence[float | None],
    weight_kg: float | None,
) -> list[float]:
    """Rough 1 Hz power from speed and the slope of the altitude: rolling, air and climbing
    resistance. Gaps in speed count as standing still; coasting downhill counts as 0 W."""
    mass = (weight_kg or _FALLBACK_KG) + _BIKE_KG
    known = [a for a in altitude_m if a is not None]
    alt: list[float] | None = None
    if len(known) == len(altitude_m) and known:
        alt = smooth(known, _SMOOTH_S)
    out = []
    for i, kmh in enumerate(speed_kmh):
        v = (kmh or 0.0) / 3.6
        if v <= 0:
            out.append(0.0)
            continue
        grade = 0.0
        if alt is not None and len(alt) > 1:
            lo, hi = max(0, i - _SMOOTH_S), min(len(alt) - 1, i + _SMOOTH_S)
            if hi > lo:
                run = sum((speed_kmh[j] or 0.0) / 3.6 for j in range(lo, hi))
                grade = (alt[hi] - alt[lo]) / run if run > 1 else 0.0
                grade = max(-_MAX_GRADE, min(_MAX_GRADE, grade))
        force = mass * _G * (_CRR + grade) + 0.5 * _RHO * _CDA * v * v
        out.append(max(0.0, force * v / _DRIVETRAIN))
    return out


def estimated_load(
    speed_kmh: Sequence[float | None],
    altitude_m: Sequence[float | None],
    weight_kg: float | None,
    ftp_w: int,
) -> float | None:
    """Training load guessed from speed and altitude, or None without any speed."""
    if not any(speed_kmh):
        return None
    power = estimated_power(speed_kmh, altitude_m, weight_kg)
    return training_load(normalized_power(power), len(power), ftp_w)


def training_load(np_w: float, duration_s: float, ftp_w: int) -> float:
    """100 = one hour at FTP."""
    if ftp_w <= 0:
        return 0.0
    return duration_s / 3600 * (np_w / ftp_w) ** 2 * 100
