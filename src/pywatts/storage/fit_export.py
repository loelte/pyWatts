"""FIT activity export using fit-tool."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    LapTrigger,
    Manufacturer,
    SessionTrigger,
    Sport,
    SubSport,
)

from pywatts.core.metrics import average, kilojoules, normalized_power
from pywatts.storage.repo import Sample, SessionRow

# Largest valid values; all-ones is the FIT "invalid" sentinel.
_U8_MAX = 0xFE
_U16_MAX = 0xFFFE


def export_fit(session: SessionRow, samples: list[Sample], path: Path) -> None:
    # Encode completely before touching the target file.
    data = fit_bytes(session, samples)
    path.write_bytes(data)


def fit_bytes(session: SessionRow, samples: list[Sample]) -> bytes:
    """Indoor-cycling activity; record timestamp = started_at + t. No samples -> ValueError."""
    if not samples:
        raise ValueError("session has no samples")
    samples = sorted(samples, key=lambda s: s.t)

    def timestamp(t: float) -> int:
        # fit-tool date_time values are Unix milliseconds (stored as whole FIT seconds).
        return round((session.started_at_utc + timedelta(seconds=t)).timestamp() * 1000)

    start = timestamp(0)
    duration = samples[-1].t + 1.0  # each sample covers 1 s
    end = timestamp(duration)

    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = session.id
    file_id.time_created = start
    builder.add(file_id)

    builder.add(_timer_event(EventType.START, start))
    for sample, distance in zip(samples, _distances(samples), strict=True):
        builder.add(_record(sample, timestamp(sample.t), distance))
    builder.add(_timer_event(EventType.STOP_ALL, end))

    lap = LapMessage()
    _fill_summary(lap, samples, start, end, duration)
    lap.event = Event.LAP
    lap.event_type = EventType.STOP
    lap.lap_trigger = LapTrigger.SESSION_END
    builder.add(lap)

    fit_session = SessionMessage()
    _fill_summary(fit_session, samples, start, end, duration)
    fit_session.event = Event.SESSION
    fit_session.event_type = EventType.STOP
    fit_session.trigger = SessionTrigger.ACTIVITY_END
    fit_session.first_lap_index = 0
    fit_session.num_laps = 1
    fit_session.threshold_power = _clamp(session.ftp_w, _U16_MAX)
    builder.add(fit_session)

    activity = ActivityMessage()
    activity.timestamp = end
    activity.total_timer_time = duration
    activity.num_sessions = 1
    activity.type = Activity.MANUAL
    activity.event = Event.ACTIVITY
    activity.event_type = EventType.STOP
    builder.add(activity)

    return builder.build_bytes()


def _clamp(value: float, maximum: int) -> int:
    return min(max(round(value), 0), maximum)


def _m_per_s(kmh: float) -> float:
    return min(max(kmh / 3.6, 0.0), _U16_MAX / 1000)


def _timer_event(event_type: EventType, timestamp: int) -> EventMessage:
    message = EventMessage()
    message.timestamp = timestamp
    message.event = Event.TIMER
    message.event_type = event_type
    message.event_group = 0
    return message


def _distances(samples: list[Sample]) -> list[float | None]:
    """Metres covered up to the end of each sample (1 s each); None without any speed."""
    if all(s.speed is None for s in samples):
        return [None] * len(samples)
    total = 0.0
    result: list[float | None] = []
    for s in samples:
        total += _m_per_s(s.speed) if s.speed is not None else 0.0
        result.append(total)
    return result


def _record(sample: Sample, timestamp: int, distance: float | None) -> RecordMessage:
    message = RecordMessage()
    message.timestamp = timestamp
    if distance is not None:
        message.distance = distance
    if sample.power is not None:
        message.power = _clamp(sample.power, _U16_MAX)
    if sample.cadence is not None:
        message.cadence = _clamp(sample.cadence, _U8_MAX)
    if sample.hr is not None:
        message.heart_rate = _clamp(sample.hr, _U8_MAX)
    if sample.speed is not None:
        message.speed = message.enhanced_speed = _m_per_s(sample.speed)
    return message


def _fill_summary(
    message: LapMessage | SessionMessage,
    samples: list[Sample],
    start: int,
    end: int,
    duration: float,
) -> None:
    message.message_index = 0
    message.timestamp = end
    message.start_time = start
    message.total_elapsed_time = duration
    message.total_timer_time = duration
    message.sport = Sport.CYCLING
    message.sub_sport = SubSport.INDOOR_CYCLING

    powers = [s.power for s in samples if s.power is not None]
    if powers:
        message.avg_power = _clamp(average(powers), _U16_MAX)
        message.max_power = _clamp(max(powers), _U16_MAX)
        message.normalized_power = _clamp(normalized_power(powers), _U16_MAX)
        message.total_work = max(round(kilojoules(powers) * 1000), 0)
    cadences = [s.cadence for s in samples if s.cadence is not None]
    if cadences:
        message.avg_cadence = _clamp(average(cadences), _U8_MAX)
        message.max_cadence = _clamp(max(cadences), _U8_MAX)
    hrs = [s.hr for s in samples if s.hr is not None]
    if hrs:
        message.avg_heart_rate = _clamp(average(hrs), _U8_MAX)
        message.max_heart_rate = _clamp(max(hrs), _U8_MAX)
    speeds = [_m_per_s(s.speed) for s in samples if s.speed is not None]
    if speeds:
        message.avg_speed = message.enhanced_avg_speed = average(speeds)
        message.max_speed = message.enhanced_max_speed = max(speeds)
        message.total_distance = sum(speeds)  # 1 s per sample
