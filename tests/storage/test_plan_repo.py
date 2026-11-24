import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from pywatts.core.plan import Plan, PlanUnit
from pywatts.core.workout import Steady, Workout
from pywatts.storage import db, plan_repo
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanError, PlanRepo
from pywatts.storage.repo import ImportedRide, Repo, Sample

DAY = date(2026, 3, 4)


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "a.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return Repo(conn)


@pytest.fixture
def plans(conn):
    return PlanRepo(conn)


def unit(uid="u", **kw):
    return PlanUnit(**{"id": uid, "day": DAY, "kind": "indoor", "title": "T"} | kw)


def ride(repo, seconds=600, hour=12, day=DAY, key="1"):
    start = datetime(day.year, day.month, day.day, hour).astimezone()
    samples = [Sample(float(t), 200, None, None, 0, None) for t in range(seconds)]
    found = ImportedRide(start, start + timedelta(seconds=seconds), "R", samples)
    return repo.import_session(found, "file", key, key).session_id


def test_upgrade_from_version_4(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:4])
        conn = open_db(path)
    conn.execute("INSERT INTO workouts VALUES ('i', 'n', '{}', 'x')")
    conn.commit()
    conn.close()
    conn = open_db(path)
    assert conn.execute("SELECT origin FROM workouts").fetchone() == ("user",)
    conn.close()


def test_roundtrip(plans):
    plan = Plan("p", "Base", DAY, DAY + timedelta(days=27), goal="FTP")
    workout = Workout("W", [Steady(600, 0.8)])
    a = unit("a", plan_id="p", workout=workout, position=1)
    b = unit("b", kind="outdoor", target_s=3600, target_load=60.0, notes="n", origin="ai")
    plans.apply("user", "x", [plan, a, b])
    assert plans.list_plans() == [plan] and plans.get_plan("p") == plan
    assert plans.units_between(DAY, DAY) == [b, a]
    assert plans.units_between(DAY, DAY, "p") == [a]
    assert plans.units_between(DAY + timedelta(days=1), DAY + timedelta(days=9)) == []
    assert plans.get_unit("nope") is None


def test_archived_plan_hides_unridden_units(repo, plans):
    plan = Plan("p", "Base", DAY, DAY, archived=True)
    plans.apply("user", "x", [plan, unit("a", plan_id="p"), unit("b", plan_id="p")])
    assert plans.units_between(DAY, DAY) == [] and plans.list_plans() == []
    plans.link("a", ride(repo))
    assert [u.id for u in plans.units_between(DAY, DAY)] == ["a"]
    assert plans.list_plans(archived=True) == [plan]


def test_live_workout_and_snapshot(repo, plans):
    workout = Workout("W", [Steady(600, 0.8)])
    repo.save_workout(workout)
    plans.apply("user", "x", [unit(workout=workout)])
    repo.save_workout(Workout("W2", [Steady(900, 0.8)], id=workout.id))
    assert plans.get_unit("u").workout.name == "W2"
    plans.link("u", ride(repo))
    repo.save_workout(Workout("W3", [Steady(900, 0.8)], id=workout.id))
    assert plans.get_unit("u").workout.name == "W2"  # as it was when ridden
    plans.unlink("u")
    assert plans.get_unit("u").workout.name == "W3"
    repo.delete_workout(workout.id)
    assert plans.get_unit("u").workout.name == "W3"


def test_undo(plans):
    plans.apply("user", "add", [unit()])
    moved = plans.apply("ai", "move", [unit(day=DAY + timedelta(days=1))])
    assert [c.summary for c in plans.changes()] == ["move", "add"]
    assert [c.summary for c in plans.changes(actor="ai")] == ["move"]
    plans.undo(moved)
    assert plans.get_unit("u").day == DAY
    assert plans.changes()[0].undone
    with pytest.raises(PlanError, match="already"):
        plans.undo(moved)
    with pytest.raises(PlanError):
        plans.undo("nope")


def test_undo_conflict_changes_nothing(plans):
    batch = plans.apply("ai", "add", [unit("a"), unit("b")])
    plans.apply("user", "edit", [unit("a", title="mine")])
    with pytest.raises(PlanError, match="again"):
        plans.undo(batch)
    assert {u.id for u in plans.units_between(DAY, DAY)} == {"a", "b"}
    assert not any(c.undone for c in plans.changes())


def test_undo_restricted_to_actor(plans):
    batch = plans.apply("user", "add", [unit()])
    with pytest.raises(PlanError):
        plans.undo(batch, actor="ai")


def test_undo_delete_restores_link(repo, plans):
    plans.apply("user", "add", [unit()])
    sid = ride(repo)
    plans.link("u", sid)
    batch = plans.apply("ai", "delete", deletes=[unit()])
    assert plans.links() == {} and plans.get_unit("u") is None
    plans.undo(batch)
    assert plans.links()["u"].session_id == sid


def test_update_keeps_link(repo, plans):
    plans.apply("user", "add", [unit()])
    plans.link("u", ride(repo))
    plans.apply("ai", "rename", [unit(title="New")])
    assert "u" in plans.links()


def test_ai_must_not_touch_user_workouts(repo, plans):
    workout = Workout("Mine", [Steady(600, 0.8)])
    repo.save_workout(workout)
    for kw in ({"saves": [replace(workout, name="X")]}, {"deletes": [workout]}):
        with pytest.raises(PlanError, match="rider"):
            plans.apply("ai", "x", **kw)
    assert repo.get_workout(workout.id).name == "Mine" and plans.changes() == []


def test_failed_batch_is_rolled_back(repo, plans):
    workout = Workout("Mine")
    repo.save_workout(workout)
    with pytest.raises(PlanError):
        plans.apply("ai", "x", [unit()], [workout])
    assert plans.get_unit("u") is None


def test_ai_workout_becomes_the_users_by_editing(repo, plans):
    workout = Workout("AI", [Steady(600, 0.8)])
    batch = plans.apply("ai", "create", [workout])
    assert plans.workout_origin(workout.id) == "ai"
    plans.apply("ai", "update", [replace(workout, name="AI 2")])
    repo.save_workout(replace(workout, name="Edited"))
    assert plans.workout_origin(workout.id) == "user"
    with pytest.raises(PlanError):
        plans.apply("ai", "update", [workout])
    with pytest.raises(PlanError):
        plans.undo(batch)


def test_no_change_is_not_logged(plans):
    plans.apply("user", "add", [unit()])
    plans.apply("user", "same", [unit()])
    assert len(plans.changes()) == 1


def test_log_is_trimmed(plans, monkeypatch):
    monkeypatch.setattr(plan_repo, "KEEP_BATCHES", 3)
    for i in range(6):
        plans.apply("user", str(i), [unit(title=str(i))])
    assert [c.summary for c in plans.changes()] == ["5", "4", "3"]


def test_auto_match_and_rejection(repo, plans):
    plans.apply("user", "x", [unit(target_s=600)])
    sid = ride(repo)
    assert plans.auto_match(DAY, DAY) == 1
    assert plans.links()["u"].how == "auto"
    assert plans.auto_match(DAY, DAY) == 0
    plans.unlink("u")
    assert plans.auto_match(DAY, DAY) == 0
    plans.link("u", sid)  # by hand it is still possible
    assert plans.links()["u"].how == "manual"


@pytest.mark.parametrize("hour", [0, 23])
def test_rides_by_local_day(repo, plans, hour):
    ride(repo, seconds=60, hour=hour)
    assert len(plans.rides_between(DAY, DAY)) == 1
    assert plans.rides_between(DAY - timedelta(days=1), DAY - timedelta(days=1)) == []
    assert plans.rides_between(DAY + timedelta(days=1), DAY + timedelta(days=1)) == []


def test_link_replaces_both_sides_and_needs_both(repo, plans):
    plans.apply("user", "x", [unit("a"), unit("b")])
    sid = ride(repo)
    plans.link("a", sid)
    plans.link("b", sid)
    assert list(plans.links()) == ["b"]
    with pytest.raises(PlanError):
        plans.link("b", 999)
    assert list(plans.links()) == ["b"]
    repo.delete_session(sid)
    assert plans.links() == {}


def test_started_ride_is_linked(repo, plans):
    plans.apply("user", "x", [unit()])
    first = repo.start_session(Workout("W"), 200, "u")
    assert plans.links()["u"].session_id == first
    repo.add_samples(first, [Sample(0.0, 1, None, None, 0, None)])
    repo.finish_session(first, "aborted")
    second = repo.start_session(Workout("W"), 200, "u")  # another try replaces the aborted one
    assert plans.links()["u"].session_id == second
    repo.finish_session(second, "completed")
    repo.start_session(Workout("W"), 200, "u")
    assert plans.links()["u"].session_id == second


def test_vanished_unit_does_not_prevent_recording(repo, plans):
    assert repo.start_session(Workout("W"), 200, "gone") > 0
    assert plans.links() == {}


def test_corrupt_rows_are_skipped(conn, plans):
    plans.apply("user", "x", [unit(), Plan("p", "P", DAY, DAY), unit("w", workout=Workout("W"))])
    conn.execute("UPDATE plan_units SET title = '' WHERE id = 'u'")
    conn.execute("UPDATE plan_units SET workout_json = '{' WHERE id = 'w'")
    conn.execute("UPDATE plans SET start_day = 'x'")
    conn.commit()
    assert plans.list_plans() == []
    (left,) = plans.units_between(DAY, DAY)
    assert left.id == "w" and left.workout is None


def test_storage_error_changes_nothing(conn, plans):
    conn.execute("CREATE TRIGGER t BEFORE INSERT ON changes BEGIN SELECT RAISE(ABORT, 'x'); END")
    with pytest.raises(sqlite3.Error):
        plans.apply("user", "x", [unit()])
    assert plans.get_unit("u") is None


def test_change_time_is_utc(plans):
    plans.apply("user", "x", [unit()])
    assert plans.changes()[0].at_utc.tzinfo is UTC


def test_copy_is_brought_up_to_date(repo, plans):
    """By linking, starting and deleting the workout."""
    workout = Workout("Old", [Steady(600, 0.8)])
    new = replace(workout, name="New")
    for i, freeze in enumerate(
        (
            lambda uid: plans.link(uid, ride(repo)),
            lambda uid: repo.start_session(new, 200, uid),
            lambda uid: repo.delete_workout(workout.id),
        )
    ):
        repo.save_workout(workout)
        plans.apply("user", "x", [unit(f"u{i}", workout=workout)])
        repo.save_workout(new)
        freeze(f"u{i}")
        repo.save_workout(replace(workout, name="Newer"))
        repo.delete_workout(workout.id)
        expected = "Newer" if i == 2 else "New"
        assert plans.get_unit(f"u{i}").workout.name == expected


def test_change_rows(plans):
    batch = plans.apply("ai", "x", [unit()])
    (row,) = plans.change_rows(batch)
    assert (row.table, row.row_id, row.before, row.after["title"]) == ("plan_units", "u", None, "T")
    assert plans.change_rows("nope") == []
