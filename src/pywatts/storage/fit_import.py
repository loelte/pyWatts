"""FIT activity -> ImportedRide. Pure: bytes in, data out."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import Event, EventType, FileType, Manufacturer, Sport

from pywatts.storage.repo import ImportedRide, Sample

ZWIFT = Manufacturer.ZWIFT.value
WAHOO = Manufacturer.WAHOO_FITNESS.value

HOLD_S = 10  # longer gaps between records are pauses
MAX_POWER_W = 2500
_MAX_SAMPLES = 200_000
_OLDEST = datetime(2000, 1, 1, tzinfo=UTC)  # earlier: the device clock was not set
_SPORTS = {Sport.CYCLING.value, Sport.E_BIKING.value}
_STOPS = {
    EventType.STOP.value,
    EventType.STOP_ALL.value,
    EventType.STOP_DISABLE.value,
    EventType.STOP_DISABLE_ALL.value,
}
_TITLES = {ZWIFT: "Zwift ride", WAHOO: "Wahoo ride"}


class NotCycling(ValueError):
    """A readable activity of another sport."""


def parse_fit(data: bytes) -> ImportedRide:
    """Raises NotCycling, or ValueError for anything that is not a readable activity."""
    try:
        return _parse(data)
    except ValueError:
        raise
    except Exception as e:  # fit-tool on damaged input
        raise ValueError(f"unreadable FIT file: {type(e).__name__}: {e}") from e


def _parse(data: bytes) -> ImportedRide:
    messages = [r.message for r in FitFile.from_bytes(data).records]
    manufacturer = None
    for m in messages:
        if isinstance(m, FileIdMessage):
            if m.type is not None and m.type != FileType.ACTIVITY.value:
                raise ValueError("not an activity file")
            manufacturer = m.manufacturer
    if any(isinstance(m, SessionMessage) and m.sport not in _SPORTS for m in messages):
        raise NotCycling("not a cycling activity")

    records: dict[int, Sample] = {}  # by Unix second; the last one wins
    events: list[tuple[int, int]] = []
    for m in messages:
        if isinstance(m, RecordMessage) and m.timestamp is not None:
            records[round(m.timestamp / 1000)] = _sample(m)
        elif isinstance(m, EventMessage) and m.timestamp is not None:
            if m.event == Event.TIMER.value and m.event_type is not None:
                events.append((round(m.timestamp / 1000), m.event_type))
    if not records:
        raise ValueError("the file contains no records")
    samples = _resample(records, events)
    start = datetime.fromtimestamp(min(records), UTC)
    if start < _OLDEST:
        raise ValueError("the file has no valid date")
    return ImportedRide(
        started_at_utc=start,
        ended_at_utc=datetime.fromtimestamp(max(records), UTC) + timedelta(seconds=1),
        title=_TITLES.get(manufacturer, "Imported ride"),
        samples=samples,
        manufacturer=manufacturer,
    )


def _resample(records: dict[int, Sample], events: list[tuple[int, int]]) -> list[Sample]:
    """1 Hz ride time; a timer stop or a long gap starts a new segment and is not counted."""
    stops: list[int] = []
    starts: list[int] = []
    running = True
    for at, kind in sorted(events, key=lambda e: e[0]):
        if running and kind in _STOPS:
            stops.append(at)
            running = False
        elif not running and kind == EventType.START.value:
            starts.append(at)
            running = True
    times = [t for t in sorted(records) if not _paused(t, stops, starts)] or sorted(records)

    samples: list[Sample] = []
    segment = 0
    for i, at in enumerate(times):
        seconds, pause = 1, False
        if i + 1 < len(times):
            following = times[i + 1]
            stop = bisect_left(stops, at)
            # A stop without a later start ends the file; records after it are kept.
            pause = stop < len(starts) and stops[stop] < following
            end = stops[stop] if pause else following
            if end - at <= HOLD_S:
                seconds = max(1, end - at)
            else:
                pause = True
        source = records[at]
        for _ in range(seconds):
            samples.append(
                Sample(
                    t=float(len(samples)),
                    power=source.power,
                    cadence=source.cadence,
                    hr=source.hr,
                    target_w=0,
                    speed=source.speed,
                    segment=segment,
                    altitude=source.altitude,
                )
            )
        if len(samples) > _MAX_SAMPLES:
            raise ValueError("the ride is too long")
        segment += pause
    return samples


def _paused(t: int, stops: list[int], starts: list[int]) -> bool:
    """Strictly inside a stop/start pair."""
    i = bisect_right(starts, t)  # first start after t
    return i < len(starts) and stops[i] < t


def _sample(m: RecordMessage) -> Sample:
    power = _number(m.power)
    hr = _number(m.heart_rate)
    speed = _number(m.enhanced_speed)
    if speed is None:
        speed = _number(m.speed)
    altitude = m.enhanced_altitude if m.enhanced_altitude is not None else m.altitude
    return Sample(
        t=0.0,
        power=round(power) if power is not None and power <= MAX_POWER_W else None,
        cadence=_number(m.cadence),
        hr=round(hr) if hr else None,
        target_w=0,
        speed=None if speed is None else speed * 3.6,
        # unlike the others, below sea level is valid
        altitude=float(altitude) if altitude is not None and math.isfinite(altitude) else None,
    )


def _number(value: float | None) -> float | None:
    return float(value) if value is not None and math.isfinite(value) and value >= 0 else None
