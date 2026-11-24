import sqlite3

import pytest

from pywatts.core.workout import Steady, Workout
from pywatts.storage import db
from pywatts.storage.repo import (
    COACH_CONSENT,
    FTP_W,
    HR_ADDR,
    NAME,
    STRAVA_AUTO,
    TRAINER_ADDR,
    WEIGHT_KG,
    Repo,
)
from pywatts.storage.riders import MAX_NAME, RiderError, Riders, clean_name


@pytest.fixture
def riders(tmp_path):
    return Riders(tmp_path / "t.db")


@pytest.fixture
def conn(riders):
    conn = riders.open(riders.root)
    Repo(conn).set_setting(NAME, "Ann")
    riders.register(conn, riders.root)
    yield conn
    conn.close()


def names(riders):
    return [r.name for r in riders.list()]


def test_before_the_first_start(riders):
    assert riders.list() == []


def test_files_of_older_versions(riders, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(db, "LIBRARY_MIGRATIONS", db.LIBRARY_MIGRATIONS[:1])
        m.setattr(db, "_claim_workouts", lambda _c: None)
        riders.open(riders.root).close()
    (only,) = riders.list()
    assert only.path == riders.root and only.name == "Rider 1"
    riders.open(riders.root).close()


def test_new_rider_shares_library_and_trainer_only(riders, conn):
    ann = Repo(conn)
    w = Workout("W", [Steady(600, 0.8)])
    ann.save_workout(w)
    private = {
        FTP_W: "300",
        WEIGHT_KG: "80",
        HR_ADDR: "s",
        COACH_CONSENT: "1",
        STRAVA_AUTO: "1",
    }
    for key, value in (private | {TRAINER_ADDR: "t"}).items():
        ann.set_setting(key, value)
    rider = riders.add("  Bob \n B ", ann)
    assert names(riders) == ["Ann", "Bob B"] and rider.last_used == ""
    other = riders.open(rider.path)
    bob = Repo(other)
    assert bob.rider_id == rider.id != ann.rider_id
    assert bob.list_workouts() == [w] and bob.list_sessions() == []
    assert bob.get_setting(TRAINER_ADDR) == "t" and bob.get_setting(NAME) == "Bob B"
    assert bob.get_setting(FTP_W) == "200"
    assert all(bob.get_setting(k) == "" for k in private if k != FTP_W)
    other.close()


@pytest.mark.parametrize("name", ["", "  \n", "ANN"])
def test_bad_names(riders, conn, name):
    with pytest.raises(RiderError):
        riders.add(name, Repo(conn))
    assert names(riders) == ["Ann"] and not list(riders.folder.glob("*"))


def test_failed_add_leaves_nothing(riders, conn, monkeypatch):
    def fail(*_a, **_k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(riders, "register", fail)
    with pytest.raises(sqlite3.OperationalError):
        riders.add("Bob", Repo(conn))
    assert names(riders) == ["Ann"] and not list(riders.folder.glob("*"))


def test_register_updates_name_and_last_use(riders, conn):
    first = riders.list()[0].last_used
    Repo(conn).set_setting(NAME, "Anna")
    riders.register(conn, riders.root, used=False)
    (anna,) = riders.list()
    assert (anna.name, anna.last_used) == ("Anna", first)
    riders.register(conn, riders.root)
    assert riders.list()[0].last_used > first


def test_replaced_root_file(riders, conn):
    conn.execute("UPDATE settings SET value = ? WHERE key = ?", ("f" * 32, db.RIDER_ID))
    conn.commit()
    riders.register(conn, riders.root)
    assert [r.id for r in riders.list()] == ["f" * 32]


def test_remove_keeps_everything_in_one_file_and_can_come_back(riders, conn):
    bob = riders.add("Bob/..", Repo(conn))
    other = riders.open(bob.path)
    Repo(other).set_setting(FTP_W, "222")
    other.close()
    kept = riders.remove(bob)
    assert names(riders) == ["Ann"] and kept.parent == riders.folder / "removed"
    assert [p.name for p in riders.folder.iterdir()] == ["removed"]
    back = riders.folder / kept.name
    kept.rename(back)
    (_, again) = riders.list()
    assert (again.id, again.name, again.path) == (bob.id, "Bob/..", back)
    other = riders.open(back)
    riders.register(other, back)
    assert Repo(other).get_setting(FTP_W) == "222"
    other.close()


def test_no_removal_of_the_first_or_one_in_use(riders, conn):
    with pytest.raises(RiderError):
        riders.remove(riders.list()[0])
    bob = riders.add("Bob", Repo(conn))
    other = riders.open(bob.path)
    with pytest.raises((RiderError, sqlite3.Error)):
        riders.remove(bob)
    other.close()
    assert names(riders) == ["Ann", "Bob"]


def test_lost_library_loses_no_rider(riders):
    conn = riders.open(riders.root)
    Repo(conn).set_setting(NAME, "Ann")
    riders.register(conn, riders.root)
    bob = riders.add("Bob", Repo(conn))
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        riders.library.with_name(riders.library.name + suffix).unlink(missing_ok=True)
    assert names(riders) == ["Ann", "Bob"]
    assert riders.list()[1].id == bob.id


def test_missing_and_broken_files_are_left_out(riders, conn):
    riders.add("Bob", Repo(conn)).path.unlink()
    (riders.folder / "junk.db").write_bytes(b"no database" * 100)
    assert names(riders) == ["Ann"]


def test_library_cannot_point_elsewhere(riders, conn):
    bob = riders.add("Bob", Repo(conn))
    conn.execute("UPDATE library.riders SET file = ? WHERE id = ?", ("../../x.db", bob.id))
    conn.commit()
    assert all(r.path.parent in (riders.folder, riders.root.parent) for r in riders.list())


def test_clean_name():
    assert clean_name("\x00 a\tb ") == "a b"
    assert len(clean_name("x" * 99)) == MAX_NAME
