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


def training_load(np_w: float, duration_s: float, ftp_w: int) -> float:
    """100 = one hour at FTP."""
    if ftp_w <= 0:
        return 0.0
    return duration_s / 3600 * (np_w / ftp_w) ** 2 * 100
