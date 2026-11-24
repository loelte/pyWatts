import gzip
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import Event, EventType, FileType, Manufacturer, Sport

from pywatts import platform
from pywatts.core.workout import Steady, Workout
from pywatts.storage import db, import_files
from pywatts.storage import repo as r
from pywatts.storage.fit_export import export_fit
from pywatts.storage.fit_import import NotCycling, parse_fit
from pywatts.storage.import_files import read_files
from pywatts.storage.repo import ImportedRide, Repo, Sample, Upload

START = datetime(2025, 3, 1, 12, tzinfo=UTC)


def ms(t):
    return int((START.timestamp() + t) * 1000)


def make_fit(
    records, events=(), sport=Sport.CYCLING, maker=Manufacturer.ZWIFT, kind=FileType.ACTIVITY
):
    """records: (t, power) or (t, dict); events: (t, EventType), placed in time order."""
    builder = FitFileBuilder(auto_define=True)
    head = FileIdMessage()
    head.type, head.manufacturer, head.time_created = kind, maker.value, ms(0)
    builder.add(head)
    items = [(t, 1, v) for t, v in records] + [(t, 0, e) for t, e in events]
    for t, is_record, value in sorted(items, key=lambda i: i[:2]):
        m = RecordMessage() if is_record else EventMessage()
        m.timestamp = ms(t)
        if is_record:
            for name, v in (value if isinstance(value, dict) else {"power": value}).items():
                setattr(m, name, v)
        else:
            m.event, m.event_type = Event.TIMER, value
        builder.add(m)
    if sport is not None:
        session = SessionMessage()
        session.timestamp, session.start_time, session.sport = ms(0), ms(0), sport
        builder.add(session)
    return builder.build_bytes()


def powers(ride):
    return [s.power for s in ride.samples]


def segments(ride):
    return [s.segment for s in ride.samples]


# --- FIT ---


def test_export_import_roundtrip(tmp_path):
    workout = Workout("W", [Steady(60, 0.5)])
    row = r.SessionRow(
        1, str(workout.id), workout, START, START, 250, "completed", None, None, None, None, None
    )
    samples = [Sample(float(t), 100 + t, 90.0, 140, 200, 36.0) for t in range(40)]
    export_fit(row, samples, tmp_path / "a.fit")
    ride = parse_fit((tmp_path / "a.fit").read_bytes())
    assert (ride.started_at_utc, ride.ended_at_utc) == (START, START + timedelta(seconds=40))
    assert ride.title == "Imported ride"
    for got, want in zip(ride.samples, samples, strict=True):
        assert (got.t, got.power, got.cadence, got.hr) == (want.t, want.power, 90, 140)
        assert got.speed == pytest.approx(36, abs=0.01)
        assert (got.target_w, got.segment) == (0, 0)


def test_smart_recording_is_held_and_long_gap_is_a_pause():
    ride = parse_fit(make_fit([(0, 100), (4, 200), (5, 210), (60, 300), (61, 310)]))
    assert powers(ride) == [100] * 4 + [200, 210, 300, 310]
    assert segments(ride) == [0] * 6 + [1] * 2
    assert [s.t for s in ride.samples] == list(range(8))
    assert ride.ended_at_utc == START + timedelta(seconds=62)


def test_timer_pause_and_records_inside_it():
    ev = [(0, EventType.START), (2, EventType.STOP_ALL), (6, EventType.START), (9, EventType.STOP)]
    ride = parse_fit(make_fit([(0, 1), (1, 2), (2, 3), (4, 99), (6, 4), (7, 5), (8, 6)], ev))
    assert powers(ride) == [1, 2, 3, 4, 5, 6]
    assert segments(ride) == [0, 0, 0, 1, 1, 1]


def test_records_after_a_stop_without_start_are_kept():
    ride = parse_fit(make_fit([(0, 1), (1, 2), (2, 3), (3, 4)], [(1, EventType.STOP_ALL)]))
    assert powers(ride) == [1, 2, 3, 4] and segments(ride) == [0] * 4


def test_values_are_cleaned():
    rows = [
        (0, {"power": 3000, "heart_rate": 0}),
        (1, {"heart_rate": 150}),
        (2, {"power": 5}),
        (2, {"power": 7}),
    ]
    ride = parse_fit(make_fit(rows))
    assert powers(ride) == [None, None, 7]
    assert [s.hr for s in ride.samples] == [None, 150, None]


def test_rejected_files():
    good = make_fit([(0, 100), (1, 100)])
    with pytest.raises(NotCycling):
        parse_fit(make_fit([(0, 100)], sport=Sport.RUNNING))
    bad = [b"", b"junk" * 50, good[:-20], make_fit([]), make_fit([(0, 1)], kind=FileType.WORKOUT)]
    for data in bad:
        with pytest.raises(ValueError) as e:
            parse_fit(data)
        assert not isinstance(e.value, NotCycling)
    assert parse_fit(make_fit([(0, 1)], sport=None)).samples  # e.g. crashed recorder


# --- files ---


def test_read_files(tmp_path):
    zwift = make_fit([(0, 100)])
    wahoo = make_fit([(0, 100)], maker=Manufacturer.WAHOO_FITNESS)
    sub = tmp_path / "in" / "sub"
    sub.mkdir(parents=True)
    (sub / "b.FIT").write_bytes(zwift)
    (sub / "bad.fit").write_bytes(b"x")
    (sub / "notes.txt").write_text("x")
    with zipfile.ZipFile(tmp_path / "in" / "z.zip", "w") as z:
        z.writestr("activities/1.fit.gz", gzip.compress(wahoo))
        z.writestr("activities/2.gpx", "x")
        z.writestr("activities/3.fit.gz", b"not gzip")
        z.writestr("activities.csv", "x")
        z.writestr("run.fit", make_fit([(0, 1)], sport=Sport.RUNNING))
    (tmp_path / "broken.zip").write_bytes(b"x")
    results = list(read_files([tmp_path / "in", tmp_path / "broken.zip", tmp_path / "none.fit"]))
    by_name = {x.name: x for x in results}
    assert len(results) == 8
    assert by_name["b.FIT"].source == "zwift" and by_name["b.FIT"].ride
    assert by_name["activities/1.fit.gz"].source == "wahoo"
    assert by_name["activities/2.gpx"].skipped and by_name["run.fit"].skipped
    for name in ("bad.fit", "activities/3.fit.gz", "broken.zip", "none.fit"):
        assert by_name[name].error and not by_name[name].skipped and not by_name[name].ride


def test_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(import_files, "MAX_FILE_BYTES", 100)
    (tmp_path / "bomb.fit.gz").write_bytes(gzip.compress(bytes(10_000)))
    (result,) = read_files([tmp_path / "bomb.fit.gz"])
    assert "too large" in result.error


def test_zwift_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.setattr(platform, "user_documents_path", lambda: tmp_path / "Documents")
    assert platform.zwift_activity_dirs() == []
    (tmp_path / "Zwift" / "Activities").mkdir(parents=True)
    assert platform.zwift_activity_dirs() == [tmp_path / "Zwift" / "Activities"]


# --- repo ---


@pytest.fixture
def repo(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


def make_ride(start=START, seconds=60, power=200):
    samples = [Sample(float(t), power, None, None, 0, None) for t in range(seconds)]
    return ImportedRide(start, start + timedelta(seconds=seconds), "Ride", samples)


def add(repo, ride, external_id=None, sha=None):
    return repo.import_session(ride, "wahoo", external_id or str(uuid4()), sha or str(uuid4()))


def test_import_is_analysed(repo):
    outcome = add(repo, make_ride())
    assert outcome.result == "imported"
    row = repo.get_session(outcome.session_id)
    assert (row.source, row.status, row.workout.name) == ("wahoo", "completed", "Ride")
    assert (row.started_at_utc, row.duration_s, row.kj) == (START, 60, 12)
    assert row.np == pytest.approx(200) and row.load == pytest.approx(100 / 60)
    assert repo.best_peaks()[60].watts == 200
    own = repo.start_session(Workout("W"), 200)
    assert repo.get_session(own).source == "pywatts"


def test_ftp_and_weight_of_the_day(repo):
    add(repo, make_ride())
    assert repo.list_sessions()[0].ftp_w == 200 and repo.list_sessions()[0].weight_kg is None
    for day, ftp in ((date(2025, 1, 1), 250), (date(2025, 2, 1), 260), (date(2025, 6, 1), 300)):
        repo.record_metric(r.FTP_W, ftp, day)
    repo.record_metric(r.WEIGHT_KG, 70, date(2025, 1, 1))
    for day, ftp in ((-400, 250), (-40, 250), (1, 260), (91, 260), (93, 300), (400, 300)):
        sid = add(repo, make_ride(START + timedelta(days=day))).session_id
        assert (repo.get_session(sid).ftp_w, repo.get_session(sid).weight_kg) == (ftp, 70)


def test_duplicates(repo):
    first = add(repo, make_ride(), "1", "a")
    later = START + timedelta(days=9)
    assert add(repo, make_ride(later), "1", "b").result == "duplicate"  # same id
    assert add(repo, make_ride(later), "2", "a").result == "duplicate"  # same file
    same = add(repo, make_ride(START + timedelta(seconds=20)), "3", "c")
    assert same == r.ImportOutcome("duplicate", first.session_id)
    assert add(repo, make_ride(START + timedelta(seconds=40)), "4", "d").result == "imported"
    assert repo.known_imports("wahoo") == {"1", "2", "3", "4"}
    assert repo.known_imports("zwift") == set()


def test_deleted_and_skipped_rides_stay_away(repo):
    sid = add(repo, make_ride(), "1", "a").session_id
    repo.delete_session(sid)
    assert add(repo, make_ride(), "1", "a") == r.ImportOutcome("duplicate", None)
    repo.skip_import("wahoo", "run")
    repo.skip_import("wahoo", "run")
    assert repo.known_imports("wahoo") == {"1", "run"} and repo.list_sessions() == []


def test_failed_import_leaves_nothing(repo):
    ride = make_ride()
    broken = replace(ride, samples=[replace(ride.samples[0], target_w=None)])
    with pytest.raises(Exception, match="target_w"):
        add(repo, broken, "1", "a")
    with pytest.raises(ValueError):
        add(repo, replace(ride, samples=[]))
    assert repo.list_sessions() == [] and repo.known_imports("wahoo") == set()
    assert add(repo, ride, "1", "a").result == "imported"


def test_uploads(repo):
    sid = add(repo, make_ride()).session_id
    assert repo.get_upload(sid) is None
    repo.save_upload(Upload(sid, "queued"))
    repo.save_upload(Upload(sid, "done", "5", "6"))
    assert repo.get_upload(sid) == Upload(sid, "done", "5", "6")
    assert repo.list_uploads("queued", "processing") == [] and len(repo.list_uploads()) == 1
    repo.delete_session(sid)
    assert repo.list_uploads() == []


def test_upgrade_from_version_2(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
        conn = db.open_db(path)
        Repo(conn).start_session(Workout("W"), 200)
        conn.close()
    conn = db.open_db(path)
    assert Repo(conn).list_sessions()[0].source == "pywatts"
    conn.close()
