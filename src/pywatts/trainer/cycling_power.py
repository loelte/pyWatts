"""Cycling Power Service (0x1818): pure parsers. Field layout: Bluetooth SIG, GATT supplement."""

from __future__ import annotations

from dataclasses import dataclass

CP_SERVICE = "00001818-0000-1000-8000-00805f9b34fb"
CP_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"

# (flag bit, size) of what precedes the crank data: balance, accumulated torque, wheel data
_BEFORE_CRANK = ((0, 1), (2, 2), (4, 6))
_CRANK_BIT = 5

STOPPED_AFTER_S = 3.0  # no crank event for longer: not pedalling
MAX_RPM = 250.0


@dataclass(frozen=True)
class PowerMeasurement:
    """None = absent (or cut off)."""

    power_w: int | None = None
    crank_revs: int | None = None  # u16, wraps
    crank_time: int | None = None  # u16, 1/1024 s, wraps


def parse_power_measurement(data: bytes) -> PowerMeasurement:
    """Parse 0x2A63. Truncated packets yield the complete fields before the cut."""
    if len(data) < 4:
        return PowerMeasurement()
    flags = int.from_bytes(data[:2], "little")
    power = int.from_bytes(data[2:4], "little", signed=True)
    pos = 4 + sum(size for bit, size in _BEFORE_CRANK if flags & (1 << bit))
    if not flags & (1 << _CRANK_BIT) or pos + 4 > len(data):
        return PowerMeasurement(power)
    revs = int.from_bytes(data[pos : pos + 2], "little")
    time = int.from_bytes(data[pos + 2 : pos + 4], "little")
    return PowerMeasurement(power, revs, time)


class CrankCadence:
    """Cadence from the crank counters of successive measurements."""

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None  # revs, time
        self._changed = 0.0  # when the counters last changed
        self._rpm: float | None = None

    def update(self, m: PowerMeasurement, now: float) -> float | None:
        """None = not known (yet)."""
        if m.crank_revs is None or m.crank_time is None:
            return None
        last, self._last = self._last, (m.crank_revs, m.crank_time)
        idle = now - self._changed
        if last is None:
            self._changed = now
            return None
        revs = (m.crank_revs - last[0]) & 0xFFFF
        ticks = (m.crank_time - last[1]) & 0xFFFF
        if not revs:
            self._last = last  # some meters move the time on without a revolution
            if idle > STOPPED_AFTER_S:
                self._rpm = 0.0
            return self._rpm
        self._changed = now
        # After a pause the time is that of the pause (and wraps after 64 s).
        if idle > STOPPED_AFTER_S or not ticks:
            return self._rpm
        rpm = revs * 60 * 1024 / ticks
        if rpm <= MAX_RPM:  # else the counters were reset
            self._rpm = round(rpm, 1)
        return self._rpm
