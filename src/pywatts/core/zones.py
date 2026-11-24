"""Five training zones for power and heart rate."""

from __future__ import annotations

from dataclasses import dataclass

from pywatts.core.workout import Block

ZONE_COUNT = 5
POWER_LIMITS = (0.55, 0.75, 0.90, 1.05)  # upper limits of Z1..Z4, fraction of FTP
HR_LIMITS = (0.60, 0.70, 0.80, 0.90)  # fraction of max HR or of the heart rate reserve


@dataclass(frozen=True)
class Zones:
    """Inclusive upper limits of Z1..Z4 (W or bpm); above the last one is Z5."""

    limits: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.limits) != ZONE_COUNT - 1 or self.limits[0] <= 0:
            raise ValueError("Four zone limits above 0 are needed")
        if any(a >= b for a, b in zip(self.limits, self.limits[1:], strict=False)):
            raise ValueError("Each zone limit must be higher than the one before")

    def zone_of(self, value: float) -> int:
        """1..5; compares the rounded value, as it is displayed."""
        return 1 + sum(round(value) > limit for limit in self.limits)

    def ranges(self) -> list[tuple[int, int | None]]:
        """(low, high) per zone; the last high is None."""
        lows = [0, *(limit + 1 for limit in self.limits)]
        return list(zip(lows, [*self.limits, None], strict=True))


def power_zones(ftp_w: int) -> Zones:
    return Zones(tuple(round(ftp_w * f) for f in POWER_LIMITS))  # type: ignore[arg-type]


def hr_zones(max_hr: int, resting_hr: int = 0) -> Zones:
    """Percent of max HR; with resting_hr percent of the heart rate reserve (Karvonen)."""
    reserve = max_hr - resting_hr
    return Zones(tuple(round(resting_hr + reserve * f) for f in HR_LIMITS))  # type: ignore[arg-type]


def format_limits(zones: Zones) -> str:
    return ",".join(str(v) for v in zones.limits)


def parse_limits(text: str) -> Zones | None:
    """Inverse of format_limits; None if empty or invalid."""
    try:
        return Zones(tuple(int(v) for v in text.split(",")))  # type: ignore[arg-type]
    except ValueError:
        return None


def split_by_zone(block: Block, ftp_w: int, zones: Zones) -> list[tuple[Block, int]]:
    """Cut a ramp where it crosses zone limits. Steady blocks give one piece."""
    w0, w1 = block.p_start * ftp_w, block.p_end * ftp_w
    if zones.zone_of(w0) == zones.zone_of(w1):
        return [(block, zones.zone_of(w0))]
    # +0.5: zones compare rounded watts
    cuts = sorted((lim + 0.5 - w0) / (w1 - w0) for lim in zones.limits)
    edges = [0.0, *(c for c in cuts if 0 < c < 1), 1.0]
    pieces = []
    for a, b in zip(edges, edges[1:], strict=False):

        def at(f: float, lo: float, hi: float) -> float:
            return lo + (hi - lo) * f

        piece = Block(
            at(a, block.t0, block.t1),
            at(b, block.t0, block.t1),
            at(a, block.p_start, block.p_end),
            at(b, block.p_start, block.p_end),
        )
        pieces.append((piece, zones.zone_of(at((a + b) / 2, w0, w1))))
    return pieces
