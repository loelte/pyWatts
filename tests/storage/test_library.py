"""Riders with their own files share one workout library."""

import sqlite3
from datetime import date

import pytest

from pywatts.core.plan import PlanUnit
from pywatts.core.workout import Steady, Workout, to_json
from pywatts.storage import db
from pywatts.storage.plan_repo import PlanError, PlanRepo
from pywatts.storage.repo import FTP_W, Repo

DAY = date(2026, 11, 24)
OLD, NEW = "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"


@pytest.fixture
def riders(tmp_path):
    conns = [db.open_db(tmp_path / f"{n}.db", tmp_path / "shared.db") for n in "ab"]
    yield [Repo(c) for c in conns]
    for conn in conns:
        conn.close()


def old_db(path, monkeypatch, *workouts, updated=OLD):
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:5])
        conn = db.open_db(path)
    for w in workouts:
        conn.execute(
            "INSERT INTO main.workouts VALUES (?, ?, ?, ?, 'ai')",
            (str(w.id), w.name, to_json(w), updated),
        )
    conn.execute("INSERT INTO settings VALUES ('ftp_w', '250')")
    conn.commit()
    conn.close()


def names(conn):
    return [w.name for w in Repo(conn).list_workouts()]


def test_workouts_are_shared_the_rest_is_not(riders):
    a, b = riders
    w = Workout("W", [Steady(600, 0.8)])
    a.save_workout(w)
    a.set_setting(FTP_W, "300")
    a.start_session(w, 300)
    assert b.list_workouts() == [w]
    assert b.get_setting(FTP_W) == "200" and b.list_sessions() == []
    w.name = "Edited"
    b.save_workout(w)
    assert a.get_workout(w.id).name == "Edited"


def test_unit_survives_deletion_by_another_rider(riders):
    a, b = riders
    w = Workout("W", [Steady(600, 0.8)])
    a.save_workout(w)
    plans = PlanRepo(b.connection)
    plans.apply("user", "x", [PlanUnit("u", DAY, "indoor", "t", workout=w)])
    a.delete_workout(w.id)
    assert b.list_workouts() == []
    assert plans.get_unit("u").workout == w


def test_undo_does_not_overwrite_the_edit_of_another_rider(riders):
    a, b = riders
    plans = PlanRepo(a.connection)
    w = Workout("W", [Steady(600, 0.8)])
    batch = plans.apply("ai", "new workout", [w])
    w.name = "Edited"
    b.save_workout(w)
    with pytest.raises(PlanError):
        plans.undo(batch)
    assert a.get_workout(w.id).name == "Edited"


def test_ai_must_not_change_what_another_rider_saved(riders):
    a, b = riders
    w = Workout("W", [Steady(600, 0.8)])
    a.save_workout(w)
    with pytest.raises(PlanError):
        PlanRepo(b.connection).apply("ai", "x", [Workout("No", [], id=w.id)])
    assert a.get_workout(w.id).name == "W"


def test_upgrade_moves_workouts_and_keeps_a_backup(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    w = Workout("W", [Steady(600, 0.8)])
    old_db(path, monkeypatch, w)
    for _ in range(2):
        conn = db.open_db(path)
        assert Repo(conn).list_workouts() == [w]
        assert PlanRepo(conn).workout_origin(w.id) == "ai"
        assert PlanRepo(conn).ai_may_change(w.id)
        assert Repo(conn).get_setting(FTP_W) == "250"
        conn.close()
    backup = sqlite3.connect(db.backup_path(path))
    assert backup.execute("SELECT name FROM workouts").fetchall() == [("W",)]
    assert backup.execute("PRAGMA user_version").fetchone() == (5,)
    backup.close()


def test_nothing_to_move_needs_no_backup(tmp_path):
    path = tmp_path / "new.db"
    db.open_db(path).close()
    assert not db.backup_path(path).exists()


def test_crash_after_copying_loses_nothing(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    w = Workout("W", [Steady(600, 0.8)])
    old_db(path, monkeypatch, w)
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:5], ("NOT SQL",)])
        with pytest.raises(sqlite3.Error):
            db.open_db(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT name FROM workouts").fetchall() == [("W",)]
    raw.close()
    conn = db.open_db(path)
    assert names(conn) == ["W"]
    conn.close()


def test_failed_backup_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old_db(path, monkeypatch, Workout("W", []))
    blocked = db.backup_path(path).with_name(db.backup_path(path).name + ".part")
    blocked.mkdir()  # cannot be replaced by a file
    with pytest.raises((OSError, sqlite3.Error)):
        db.open_db(path)
    blocked.rmdir()
    conn = db.open_db(path)
    assert names(conn) == ["W"]
    conn.close()


@pytest.mark.parametrize("first_is_newer", [True, False])
def test_same_workout_in_two_files_keeps_the_newer(tmp_path, monkeypatch, first_is_newer):
    w = Workout("first", [])
    old_db(tmp_path / "a.db", monkeypatch, w, updated=NEW if first_is_newer else OLD)
    w.name = "second"
    old_db(tmp_path / "b.db", monkeypatch, w, updated=OLD if first_is_newer else NEW)
    for n in "ab":
        conn = db.open_db(tmp_path / f"{n}.db", tmp_path / "shared.db")
        found = names(conn)
        conn.close()
    assert found == ["first" if first_is_newer else "second"]


def test_newer_library_rejected_before_any_change(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old_db(path, monkeypatch, Workout("W", []))
    raw = sqlite3.connect(db.default_library(path))
    raw.execute("PRAGMA user_version = 999")
    raw.close()
    with pytest.raises(RuntimeError, match="library"):
        db.open_db(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone() == (5,)
    assert raw.execute("SELECT name FROM workouts").fetchall() == [("W",)]
    raw.close()


def test_rider_ids_differ_and_stay(tmp_path):
    ids = []
    for name in ("a", "b", "a"):
        conn = db.open_db(tmp_path / f"{name}.db", tmp_path / "shared.db")
        ids.append(Repo(conn).rider_id)
        conn.close()
    assert ids[0] == ids[2] != ids[1]


def test_unusable_rider_id_is_replaced(riders):
    a, _ = riders
    a.set_setting(db.RIDER_ID, "../elsewhere")  # it is used as a folder name
    assert len(a.rider_id) == 32 and a.rider_id.isalnum()


def test_ai_changes_only_what_it_made_for_the_same_rider(riders):
    a, b = riders
    ai_a, ai_b = PlanRepo(a.connection), PlanRepo(b.connection)
    w = Workout("W", [Steady(600, 0.8)])
    ai_a.apply("ai", "create", [w])
    assert ai_a.ai_may_change(w.id) and not ai_b.ai_may_change(w.id)
    edited = Workout("No", [], id=w.id)
    for kw in ({"saves": [edited]}, {"deletes": [w]}):
        with pytest.raises(PlanError, match="another rider"):
            ai_b.apply("ai", "x", **kw)
    assert a.get_workout(w.id) == w
    ai_a.apply("ai", "update", [edited])
    assert b.get_workout(w.id).name == "No"
    # Riders themselves may change everything; then it is nobody's AI's.
    b.save_workout(w)
    assert not ai_a.ai_may_change(w.id) and not ai_b.ai_may_change(w.id)


def test_undone_deletion_keeps_the_owner(riders):
    a, b = riders
    plans = PlanRepo(a.connection)
    w = Workout("W", [Steady(600, 0.8)])
    plans.apply("ai", "create", [w])
    plans.undo(plans.apply("ai", "delete", deletes=[w]))
    assert plans.ai_may_change(w.id) and not PlanRepo(b.connection).ai_may_change(w.id)


@pytest.mark.parametrize("delete", [False, True])
def test_undo_of_changes_logged_before_owners(riders, delete):
    plans = PlanRepo(riders[0].connection)
    w = Workout("W", [Steady(600, 0.8)])
    batch = plans.apply("ai", "create", [w])
    if delete:
        batch = plans.apply("ai", "delete", deletes=[w])
    with riders[0].connection as conn:
        for column in ("before_json", "after_json"):
            conn.execute(f"UPDATE changes SET {column} = json_remove({column}, '$.owner')")
    plans.undo(batch)
    assert plans.ai_may_change(w.id) == delete  # restored, or no longer there


def test_library_without_owners_belongs_to_its_rider(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    ai, user = Workout("AI", []), Workout("User", [])
    with monkeypatch.context() as m:
        m.setattr(db, "LIBRARY_MIGRATIONS", db.LIBRARY_MIGRATIONS[:1])
        m.setattr(db, "_claim_workouts", lambda _conn: None)
        conn = db.open_db(path)
    for w, origin in ((ai, "ai"), (user, "user")):
        conn.execute(
            "INSERT INTO workouts VALUES (?, ?, ?, ?, ?)",
            (str(w.id), w.name, to_json(w), OLD, origin),
        )
    conn.commit()
    conn.close()
    conn = db.open_db(path)
    plans = PlanRepo(conn)
    assert plans.ai_may_change(ai.id) and not plans.ai_may_change(user.id)
    conn.close()
