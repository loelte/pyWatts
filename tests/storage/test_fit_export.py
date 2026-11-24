from datetime import UTC, datetime, timedelta

import pytest
from fit_tool.data_message import DataMessage
from fit_tool.fit_file import FitFile
from fit_tool.profile.profile_type import Event, EventType, FileType, Sport, SubSport

from pywatts.core.workout import Steady, Workout
from pywatts.storage.fit_export import export_fit
from pywatts.storage.repo import Sample, SessionRow

START = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)


def make_session():
    workout = Workout("W", [Steady(60, 0.5)])
    return SessionRow(
        id=7,
        workout_id=str(workout.id),
        workout=workout,
        started_at_utc=START,
        ended_at_utc=START + timedelta(seconds=90),
        ftp_w=250,
        status="completed",
        avg_power=200,
        max_power=220,
        avg_hr=140,
        duration_s=60,
        kj=12,
    )


def decode(path):
    fit = FitFile.from_file(str(path))  # checks CRC
    return fit, [r.message for r in fit.records if isinstance(r.message, DataMessage)]


def ms(seconds):
    return int((START + timedelta(seconds=seconds)).timestamp() * 1000)


def test_roundtrip(tmp_path):
    path = tmp_path / "a.fit"
    samples = [Sample(float(t), 180 + t, 90.6, 140, 200, 36.0) for t in range(60)]
    export_fit(make_session(), samples, path)
    fit, messages = decode(path)
    names = [m.name for m in messages]
    assert names == ["file_id", "event", *["record"] * 60, "event", "lap", "session", "activity"]

    assert messages[0].type == FileType.ACTIVITY.value
    assert messages[0].time_created == ms(0)
    assert messages[1].event == Event.TIMER.value
    assert messages[1].event_type == EventType.START.value
    assert messages[1].timestamp == ms(0)

    records = messages[2:62]
    assert [r.timestamp for r in records] == [ms(t) for t in range(60)]
    # Independent of fit-tool's conversion: raw value is seconds since the FIT epoch.
    raw = records[5].get_field_by_name("timestamp").encoded_values[0]
    assert raw == (START - datetime(1989, 12, 31, tzinfo=UTC)).total_seconds() + 5
    assert [r.power for r in records] == [180 + t for t in range(60)]
    assert records[0].cadence == 91
    assert records[0].heart_rate == 140
    assert records[0].speed == pytest.approx(10.0)  # m/s
    assert records[0].enhanced_speed == pytest.approx(10.0)

    stop, lap, session, activity = messages[62:]
    assert (stop.event, stop.event_type) == (Event.TIMER.value, EventType.STOP_ALL.value)
    assert stop.timestamp == ms(60)
    for m in (lap, session):
        assert m.timestamp == ms(60)
        assert m.start_time == ms(0)
        assert m.total_timer_time == pytest.approx(60)
        assert m.total_elapsed_time == pytest.approx(60)
        assert (m.sport, m.sub_sport) == (Sport.CYCLING.value, SubSport.INDOOR_CYCLING.value)
        assert m.avg_power == round(sum(s.power for s in samples) / 60)
        assert m.max_power == 239
        assert m.total_work == sum(s.power for s in samples)
        assert m.avg_heart_rate == 140
        assert m.avg_speed == pytest.approx(10.0)
        assert m.total_distance == pytest.approx(600.0)
    assert records[-1].distance == pytest.approx(600.0)
    assert session.threshold_power == 250
    assert session.num_laps == 1
    assert activity.timestamp == ms(60)
    assert activity.num_sessions == 1

    fit.validate(raise_on_error=True)


def test_none_values_omitted(tmp_path):
    path = tmp_path / "a.fit"
    samples = [
        Sample(0.0, 100, None, None, 100, None),
        Sample(1.0, None, None, 130, 100, 18.0),
        Sample(2.0, None, None, None, 100, None),
    ]
    export_fit(make_session(), samples, path)
    _, messages = decode(path)
    records = [m for m in messages if m.name == "record"]
    present = [{f.name for f in r.fields if f.is_valid()} for r in records]
    assert present == [
        {"timestamp", "power", "distance"},
        {"timestamp", "heart_rate", "speed", "enhanced_speed", "distance"},
        {"timestamp", "distance"},
    ]
    assert [r.distance for r in records] == pytest.approx([0, 5, 5])
    session = next(m for m in messages if m.name == "session")
    assert session.avg_cadence is None
    assert session.avg_heart_rate == 130
    assert session.avg_speed == pytest.approx(5.0)


def test_extremes_and_all_none(tmp_path):
    path = tmp_path / "a.fit"
    samples = [
        Sample(0.0, -5, 400.0, 255, 0, 900.0),
        Sample(0.4, None, None, None, 0, None),
    ]
    export_fit(make_session(), samples, path)
    _, messages = decode(path)
    record = next(m for m in messages if m.name == "record")
    assert (record.power, record.cadence, record.heart_rate) == (0, 254, 254)
    export_fit(make_session(), samples[1:], path)
    _, messages = decode(path)
    session = next(m for m in messages if m.name == "session")
    assert session.avg_power is None


def test_no_samples(tmp_path):
    path = tmp_path / "a.fit"
    with pytest.raises(ValueError):
        export_fit(make_session(), [], path)
    assert not path.exists()
