import logging
import sqlite3
import subprocess
import sys
from datetime import UTC, timedelta
from uuid import uuid4

import pytest

from pywatts.core.workout import Ramp, Repeat, Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.repo import Repo, Sample, SessionRecorder


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return Repo(conn)


def make_workout(name="W"):
    return Workout(name, [Steady(60, 0.5), Ramp(30, 0.5, 1.0), Repeat(2, [Steady(10, 1.2)])], "d")


def make_sample(t, power: int | None = 200, hr: int | None = 140):
    return Sample(float(t), power, 90.5, hr, 200, 30.0)


def test_workout_crud_sorted(repo):
    for name in ("beta", "Alpha", "Gamma"):
        repo.save_workout(make_workout(name))
    workouts = repo.list_workouts()
    assert [w.name for w in workouts] == ["Alpha", "beta", "Gamma"]
    first = workouts[0]
    assert repo.get_workout(first.id) == first
    first.name = "zeta"
    repo.save_workout(first)
    assert [w.name for w in repo.list_workouts()] == ["beta", "Gamma", "zeta"]
    repo.delete_workout(first.id)
    assert repo.get_workout(first.id) is None
    assert len(repo.list_workouts()) == 2


@pytest.mark.parametrize("bad", ["{nope", "[]", "{}", b"\xff\xfe{", "null"])
def test_corrupt_workout_skipped_and_logged(repo, conn, caplog, bad):
    repo.save_workout(make_workout())
    bad_id = str(uuid4())
    with conn:
        conn.execute(
            "INSERT INTO workouts (id, name, json, updated_at) "
            "VALUES (?, 'x', CAST(? AS TEXT), '')",
            (bad_id, bad),
        )
    with caplog.at_level(logging.WARNING):
        assert [w.name for w in repo.list_workouts()] == ["W"]
    assert bad_id in caplog.text
    assert repo.get_workout(bad_id) is None


def test_corrupt_snapshot_gets_placeholder(repo, conn, caplog):
    workout = make_workout()
    good = repo.start_session(workout, 200)
    bad = repo.start_session(workout, 200)
    with conn:
        conn.execute("UPDATE sessions SET workout_snapshot_json='{' WHERE id=?", (bad,))
    with caplog.at_level(logging.WARNING):
        rows = repo.list_sessions()
    assert [r.id for r in rows] == [bad, good]
    assert rows[0].workout.id == workout.id
    assert rows[0].workout.segments == []
    assert rows[1].workout == workout
    assert caplog.records


def test_snapshot_survives_edit_and_delete(repo):
    workout = make_workout()
    repo.save_workout(workout)
    sid = repo.start_session(workout, 250)
    original = repo.get_session(sid).workout
    workout.name = "edited"
    workout.segments = [Steady(1, 0.1)]
    repo.save_workout(workout)
    assert repo.get_session(sid).workout == original
    repo.delete_workout(workout.id)
    row = repo.get_session(sid)
    assert row.workout == original
    assert row.workout.name == "W"
    assert row.workout_id == str(workout.id)
    assert row.ftp_w == 250


def test_start_session_state(repo):
    row = repo.get_session(repo.start_session(make_workout(), 200))
    assert row.status == "crashed"
    assert row.ended_at_utc is None
    assert row.started_at_utc.tzinfo is UTC


def test_finish_summary_ignores_none(repo):
    sid = repo.start_session(make_workout(), 200)
    samples = [make_sample(0, 100, None), make_sample(1, None, 120), make_sample(2, 300, 140)]
    repo.add_samples(sid, samples)
    repo.finish_session(sid, "completed")
    row = repo.get_session(sid)
    assert row.status == "completed"
    assert row.ended_at_utc >= row.started_at_utc
    assert (row.avg_power, row.max_power, row.avg_hr) == (200, 300, 130)
    assert row.duration_s == 3
    assert row.kj == pytest.approx(0.4)
    assert repo.get_samples(sid) == samples


def test_finish_without_samples_and_bad_status(repo):
    sid = repo.start_session(make_workout(), 200)
    with pytest.raises(ValueError):
        repo.finish_session(sid, "bogus")
    repo.finish_session(sid, "aborted")
    row = repo.get_session(sid)
    assert (row.status, row.avg_power, row.avg_hr, row.duration_s) == ("aborted", None, None, 0)


def test_delete_session_cascades(repo, conn):
    sid = repo.start_session(make_workout(), 200)
    repo.add_samples(sid, [make_sample(0)])
    repo.delete_session(sid)
    assert repo.get_session(sid) is None
    assert conn.execute("SELECT count(*) FROM samples").fetchone()[0] == 0


def test_settings(repo):
    assert repo.get_setting("ftp_w") == "200"
    assert repo.get_setting("unknown") == ""
    repo.set_setting("ftp_w", "250")
    repo.set_setting("ftp_w", "260")
    assert repo.get_setting("ftp_w") == "260"


def test_add_samples_is_atomic(repo):
    sid = repo.start_session(make_workout(), 200)
    bad = Sample(None, 1, None, None, 0, None)  # violates NOT NULL
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_samples(sid, [make_sample(0), bad])
    assert repo.get_samples(sid) == []


def test_recorder_flushes_every_10s(repo):
    rec = SessionRecorder(repo, make_workout(), 200)

    def stored():
        return len(repo.get_samples(rec.session_id))

    for t in range(10):
        rec.add(make_sample(t))
    assert stored() == 0
    rec.add(make_sample(10))
    assert stored() == 11
    for t in range(11, 20):
        rec.add(make_sample(t))
    assert stored() == 11
    rec.add(make_sample(20))
    assert stored() == 21
    rec.add(make_sample(21))
    rec.flush()
    rec.flush()
    assert stored() == 22


def test_recorder_retries_failed_flush(repo, monkeypatch):
    rec = SessionRecorder(repo, make_workout(), 200)

    def fail(*_):
        raise sqlite3.OperationalError("disk I/O error")

    samples = [make_sample(t) for t in range(13)]
    with monkeypatch.context() as m:
        m.setattr(repo, "add_samples", fail)
        for s in samples[:12]:
            rec.add(s)
        with pytest.raises(sqlite3.Error):
            rec.flush()
        with pytest.raises(sqlite3.Error):
            rec.finish("completed")
    rec.add(samples[12])
    assert repo.get_samples(rec.session_id) == samples
    rec.finish("completed")
    assert repo.get_session(rec.session_id).status == "completed"


def test_recorder_retries_after_real_lock(repo, tmp_path):
    rec = SessionRecorder(repo, make_workout(), 200)
    other = sqlite3.connect(tmp_path / "t.db")
    other.execute("BEGIN IMMEDIATE")
    for t in range(11):
        rec.add(make_sample(t))
    other.rollback()
    other.close()
    assert repo.get_samples(rec.session_id) == []
    rec.flush()
    assert len(repo.get_samples(rec.session_id)) == 11


def test_recorder_finish_idempotent(repo):
    rec = SessionRecorder(repo, make_workout(), 200)
    rec.add(make_sample(0))
    rec.add(make_sample(1))
    rec.finish("completed")
    row = repo.get_session(rec.session_id)
    rec.finish("aborted")
    rec.add(make_sample(2))
    rec.flush()
    assert repo.get_session(rec.session_id) == row
    assert len(repo.get_samples(rec.session_id)) == 2


CRASH_SCRIPT = """
import os, sys
from pywatts.core.workout import Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.repo import Repo, Sample, SessionRecorder
repo = Repo(open_db(sys.argv[1]))
SessionRecorder(repo, Workout("empty", [Steady(60, 0.5)]), 200)
rec = SessionRecorder(repo, Workout("crash", [Steady(60, 0.5)]), 200)
for t in range(25):
    rec.add(Sample(float(t), 100 + t, 90.0, 140, 100, 30.0))
os._exit(9)
"""


def test_crash_recovery(tmp_path):
    path = tmp_path / "crash.db"
    proc = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(path)], check=False)
    assert proc.returncode == 9
    conn = open_db(path)
    repo = Repo(conn)
    assert len(repo.list_sessions()) == 2
    assert repo.recover_crashed() == 1
    (row,) = repo.list_sessions()
    assert row.workout.name == "crash"
    assert row.status == "crashed"
    assert len(repo.get_samples(row.id)) == 21
    assert row.duration_s == 21
    assert row.ended_at_utc == row.started_at_utc + timedelta(seconds=20)
    assert (row.avg_power, row.max_power, row.avg_hr) == (110, 120, 140)
    assert row.kj == pytest.approx(2.31)
    assert repo.recover_crashed() == 0
    conn.close()


def test_recover_leaves_finished_alone(repo):
    rec = SessionRecorder(repo, make_workout(), 200)
    rec.finish("completed")
    assert repo.recover_crashed() == 0
    assert repo.get_session(rec.session_id).status == "completed"
