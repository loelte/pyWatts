"""Rhythms in a daily series: amplitude spectrum over the period in days (an FFT)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

MIN_DAYS = 28
PAD = 4  # zero-padding factor: a smoother curve, not more resolution
PEAK_SHARE = 0.2  # a peak must reach this share of the highest one
MAX_PEAKS = 6
HARMONIC_TOLERANCE = 0.05
NEIGHBOUR = 0.1  # a peak this close in period to a stronger one is its side band, not a rhythm


@dataclass(frozen=True)
class Rhythm:
    period_days: float
    amplitude: float  # unit per day: a series swinging +-25 around its trend gives 25
    harmonic_of: float | None = None  # period of a stronger rhythm this one echoes


@dataclass(frozen=True)
class Spectrum:
    periods_days: list[float]  # shortest (2 days) first
    amplitudes: list[float]
    peaks: list[Rhythm]  # strongest first


def rhythms(values: Sequence[float]) -> Spectrum:
    """Amplitude per period from 2 days (one value per day) up to half the series. A linear
    trend is removed first, so a growing load does not swamp the long periods; a Hann window
    keeps the edges of the history from leaking into every period."""
    n = len(values)
    if n < MIN_DAYS:
        raise ValueError("need at least 4 weeks of rides")
    y = np.asarray(values, dtype=float)
    t = np.arange(n, dtype=float)
    slope, offset = np.polyfit(t, y, 1)
    window = np.hanning(n)
    spectrum = np.fft.rfft((y - (slope * t + offset)) * window, PAD * n)
    amplitudes = 2 * np.abs(spectrum) / window.sum()
    k = np.arange(len(amplitudes))
    keep = (k >= 2 * PAD) & (k <= n * PAD // 2)  # periods N/2 .. 2 days
    periods = (PAD * n / k[keep])[::-1]
    amplitudes = amplitudes[keep][::-1]
    return Spectrum(periods.tolist(), amplitudes.tolist(), _peaks(periods, amplitudes))


def _peaks(periods: np.ndarray, amplitudes: np.ndarray) -> list[Rhythm]:
    highest = float(amplitudes.max(initial=0.0))
    if highest < 1e-9:
        return []
    inner = np.arange(1, len(amplitudes) - 1)
    tops = inner[
        (amplitudes[inner] > amplitudes[inner - 1])
        & (amplitudes[inner] >= amplitudes[inner + 1])
        & (amplitudes[inner] >= PEAK_SHARE * highest)
    ]
    found: list[tuple[float, float]] = []
    for i in sorted(tops, key=lambda i: -amplitudes[i]):
        period = float(periods[i])
        if all(abs(period / q - 1) > NEIGHBOUR for q, _a in found):
            found.append((period, float(amplitudes[i])))
    found = found[:MAX_PEAKS]
    # Spikes on a few days of each week give 3.5 d and 2.3 d too, often stronger than the
    # 7 d they come from. Longest first, so an echo always points at a rhythm of its own:
    # a 4-week block echoes at 2 wk, but the week stays a rhythm.
    bases: list[float] = []
    echo_of: dict[float, float] = {}
    for period, _a in sorted(found, reverse=True):
        base = next(
            (
                q
                for q in bases
                if any(abs(q / (m * period) - 1) <= HARMONIC_TOLERANCE for m in (2, 3))
            ),
            None,
        )
        if base is None:
            bases.append(period)
        else:
            echo_of[period] = base
    return [Rhythm(p, a, echo_of.get(p)) for p, a in found]
