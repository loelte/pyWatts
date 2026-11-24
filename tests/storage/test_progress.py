import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from pywatts.core.workout import Steady, Workout, to_json
from pywatts.core.zones import Zones, power_zones
from pywatts.storage import db
from pywatts.storage import repo as r
from pywatts.storage.profile import Profile, load_profile, save_profile
from pywatts.storage.repo import Repo, Sample

W = Workout("W", [Steady(60, 0.5)])
D1, D2 = date(2026, 1, 1), date(2026, 2, 1)


@pytest.fixture
def repo(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


def ride(repo, powers, segment=lambda t: 0):
    sid = repo.start_session(W, 200)
    repo.add_samples(
        sid,
        [Sample(float(t), p, None, None, 0, None, segment(t)) for t, p in enumerate(powers)],
    )
    repo.finish_session(sid, "completed")
    return sid


def test_session_analysis(repo):
    repo.record_metric(r.WEIGHT_KG, 80.0, D1)
    sid = ride(repo, [200] * 60)
    row = repo.get_session(sid)
    assert row.np == pytest.approx(200) and row.weight_kg == 80
    assert row.load == pytest.approx(100 / 60)
    assert repo.session_peaks(sid) == {d: 200 for d in (3, 5, 10, 20, 30, 60)}
    assert repo.get_samples(sid)[0].segment == 0
    repo.delete_session(sid)
    assert repo.best_peaks() == {}


def test_pause_and_missing_power(repo):
    sid = ride(repo, [200] * 8, segment=lambda t: t // 4)
    assert repo.session_peaks(sid) == {3: 200}
    sid = ride(repo, [300, None, 300, 300])
    assert repo.session_peaks(sid)[3] == 200
    sid = ride(repo, [None] * 5)
    assert repo.session_peaks(sid) == {} and repo.get_session(sid).np is None


def test_best_peaks(repo):
    ride(repo, [100] * 10)
    ride(repo, [300] * 5)
    ride(repo, [300] * 5)
    best = repo.best_peaks()
    assert {d: p.watts for d, p in best.items()} == {3: 300, 5: 300, 10: 100}
    sessions = repo.list_sessions()
    assert best[3].started_at_utc == sessions[1].started_at_utc  # the earlier of the ties
    assert repo.best_peaks(datetime.now(UTC) + timedelta(days=1)) == {}


def test_metric_log_drives_current_value(repo):
    repo.record_metric(r.WEIGHT_KG, 80, D2)
    repo.record_metric(r.WEIGHT_KG, 85, D1)  # older entry does not change the current value
    assert load_profile(repo).weight_kg == 80
    repo.record_metric(r.WEIGHT_KG, 79.5, D2)  # same day replaces
    assert [(m.day, m.value) for m in repo.list_metrics()] == [(D2, 79.5), (D1, 85)]
    repo.delete_metric(repo.list_metrics()[0].id)
    assert load_profile(repo).weight_kg == 85
    repo.delete_metric(12345)
    for metric, value in ((r.WEIGHT_KG, 0), (r.NAME, 5), (r.WEIGHT_KG, float("nan"))):
        with pytest.raises(ValueError):
            repo.record_metric(metric, value, D1)


def test_profile_roundtrip_logs_only_changes(repo):
    p = Profile(
        250,
        "A",
        "other",
        date(1990, 6, 15),
        180.5,
        72.25,
        48,
        190,
        170,
        Zones((1, 2, 3, 4)),
        "custom",
        Zones((100, 120, 140, 160)),
    )
    save_profile(repo, p, D1)
    assert load_profile(repo) == p
    assert {m.metric for m in repo.list_metrics()} == set(r.TRACKED)
    save_profile(repo, p, D2)
    assert all(m.day == D1 for m in repo.list_metrics())
    cleared = Profile(250)
    save_profile(repo, cleared, D2)
    assert load_profile(repo) == cleared and len(repo.list_metrics()) == 4


def test_profile_derived_values(repo):
    assert load_profile(repo) == Profile(200)
    p = Profile(200, birth_date=date(1990, 6, 15))
    assert (p.age(date(2026, 6, 14)), p.age(date(2026, 6, 15))) == (35, 36)
    assert p.power_zones == power_zones(200) and p.hr_zones is None
    assert Profile(200, max_hr=200).hr_zones.limits == (120, 140, 160, 180)
    assert Profile(200, max_hr=190, resting_hr=50, hr_method="reserve").hr_zones.limits[0] == 134
    assert Profile(200, max_hr=190, hr_method="reserve").hr_zones is None
    assert Profile(200, max_hr=3).hr_zones is None  # no distinct limits possible


def test_junk_settings_count_as_not_set(repo):
    for key in (r.FTP_W, r.WEIGHT_KG, r.BIRTH_DATE, r.POWER_ZONES, r.HR_METHOD, r.MAX_HR):
        repo.set_setting(key, "junk")
    assert load_profile(repo) == Profile(200)
    for value in ("nan", "inf", "-5", "99999999"):
        repo.set_setting(r.FTP_W, value)
        assert load_profile(repo).ftp_w == 200


def test_upgrade_from_version_1(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
        db.open_db(path).close()
    old = sqlite3.connect(path)
    old.execute(
        "INSERT INTO sessions (id, workout_id, workout_snapshot_json, started_at_utc, "
        "ended_at_utc, ftp_w, status, avg_power) VALUES (1, ?, ?, ?, ?, 200, 'completed', 210)",
        (str(W.id), to_json(W), "2026-01-01T10:00:00+00:00", "2026-01-01T10:01:00+00:00"),
    )
    old.executemany(
        "INSERT INTO samples (session_id, t, power, target_w) VALUES (1, ?, 210, 210)",
        [(t,) for t in range(60)],
    )
    old.commit()
    old.close()
    conn = db.open_db(path)
    repo = Repo(conn)
    repo.backfill_analysis()
    repo.backfill_analysis()
    row = repo.get_session(1)
    assert row.np == pytest.approx(210) and row.weight_kg is None
    assert repo.session_peaks(1)[60] == 210
    conn.close()
