import json
import random
import time
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

from pywatts.ai.tools import MAX_RESULT, ToolError, Tools
from pywatts.core.plan import Plan, PlanUnit
from pywatts.core.workout import Ramp, Repeat, Sprint, Steady, Workout, to_json
from pywatts.storage import repo as r
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import ImportedRide, Repo, Sample

TODAY = date(2026, 3, 18)  # a Wednesday
SECRETS = ("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66", "KICKR-SECRET", "TICKR-SECRET", "C:\\coach")
SEGMENTS = [
    {"type": "ramp", "duration_s": 600, "start": 0.4, "end": 0.7},
    {
        "type": "repeat",
        "count": 4,
        "children": [
            {"type": "steady", "duration_s": 300, "power": 1.0, "cadence": 90},
            {"type": "steady", "duration_s": 180, "power": 0.5},
        ],
    },
    {"type": "sprint", "duration_s": 10},
]


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "a.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    repo = Repo(conn)
    for key, value in {
        r.TRAINER_ADDR: SECRETS[0],
        r.HR_ADDR: SECRETS[1],
        r.TRAINER_NAME: SECRETS[2],
        r.HR_NAME: SECRETS[3],
        r.COACH_PATH: SECRETS[4],
        r.COACH_COMMAND: SECRETS[4],
        r.STRAVA_AUTO: "1",
        r.NAME: "Rider",
        r.BIRTH_DATE: "1990-05-01",
    }.items():
        repo.set_setting(key, value)
    repo.record_metric(r.FTP_W, 250, TODAY - timedelta(days=30))
    repo.record_metric(r.WEIGHT_KG, 70, TODAY - timedelta(days=20))
    repo.record_metric(r.MAX_HR, 190, TODAY - timedelta(days=20))
    return repo


@pytest.fixture
def plans(conn):
    return PlanRepo(conn)


@pytest.fixture
def tools(repo, plans):
    return Tools(repo, plans, today=lambda: TODAY)


@pytest.fixture
def call(tools):
    return lambda tool, /, **args: json.loads(tools.call(tool, args))


@pytest.fixture
def refused(tools, plans):
    """Asserts a ToolError and that nothing was logged; returns the text."""

    def check(name, args):
        before = len(plans.changes(500))
        with pytest.raises(ToolError) as error:
            tools.call(name, args)
        assert len(plans.changes(500)) == before and not tools.changed
        return str(error.value)

    return check


def day(offset=0):
    return (TODAY + timedelta(days=offset)).isoformat()


def ride(repo, offset=0, seconds=600, hour=12, title="R", power=200):
    d = TODAY + timedelta(days=offset)
    start = datetime(d.year, d.month, d.day, hour).astimezone()
    samples = [Sample(float(t), power, 90.0, 140, 0, None) for t in range(seconds)]
    found = ImportedRide(start, start + timedelta(seconds=seconds), title, samples)
    key = str(uuid4())
    return repo.import_session(found, "file", key, key).session_id


def user_workout(repo, name="Mine", segments=None):
    workout = Workout(name, segments or [Steady(600, 0.8)])
    repo.save_workout(workout)
    return workout


def unit(offset=0, **kw):
    return {"day": day(offset), "kind": "indoor", "title": "T"} | kw


def add(call, *units, **kw):
    return call("add_units", units=list(units), **kw)


def test_definitions(tools):
    found = {d["name"]: d for d in tools.definitions()}
    assert len(found) == 22
    json.dumps(tools.definitions())
    for name, definition in found.items():
        assert definition["inputSchema"]["additionalProperties"] is False
        assert definition["inputSchema"]["type"] == "object"
        assert hasattr(tools, f"_{name}")
        reads = name.startswith(("get_", "list_", "check_"))
        assert definition["annotations"]["readOnlyHint"] is reads
    destructive = {n for n, d in found.items() if d["annotations"]["destructiveHint"]}
    assert destructive == {"archive_plan", "delete_unit", "delete_workout", "undo_change"}
    found["add_units"]["inputSchema"]["properties"].clear()  # a copy
    assert tools.definitions()[11]["inputSchema"]["properties"]


# --- reading ---


def test_empty_database(tmp_path):
    conn = open_db(tmp_path / "empty.db")
    tools = Tools(Repo(conn), PlanRepo(conn))
    for name in ("get_profile", "list_rides", "get_progress", "list_workouts", "list_plans"):
        json.loads(tools.call(name, None))
    for name in ("check_progress", "list_changes"):
        json.loads(tools.call(name, {}))
    json.loads(tools.call("get_calendar", {"from": day(), "to": day(30)}))
    conn.close()


def test_profile(call):
    found = call("get_profile")
    rider = found["rider"]
    assert (rider["ftp_w"], rider["weight_kg"], rider["age"]) == (250, 70, 35)
    assert found["today"] == {"day": "2026-03-18", "weekday": "Wed"}
    assert len(rider["power_zones_w"]) == len(rider["hr_zones_bpm"]) == 5
    assert ["2026-02-16", "ftp_w", 250] in found["log"]
    assert "birth" not in json.dumps(found) and "1990" not in json.dumps(found)


def test_rides(call, repo):
    ids = [ride(repo, -i, title=f"R{i}") for i in range(5)]
    active = repo.start_session(Workout("now"), 200)
    found = call("list_rides", limit=2)
    assert [x["id"] for x in found["rides"]] == ids[:2] and found["total"] == 5
    assert found["next_offset"] == 2 and "Truncated" in found["note"]
    assert found["rides"][0]["weekday"] == "Wed" and found["rides"][0]["seconds"] == 600
    found = call("list_rides", **{"from": day(-3), "to": day(-2)})
    assert [x["title"] for x in found["rides"]] == ["R2", "R3"] and "note" not in found
    assert call("list_rides", offset=100)["rides"] == []
    with pytest.raises(ToolError):
        call("get_ride", ride_id=active)


def test_get_ride(call, repo):
    long = ride(repo, seconds=20_000)
    found = call("get_ride", ride_id=long)
    samples = found["samples"]
    assert samples["seconds_per_row"] == 100 and len(samples["rows"]) == 200
    assert samples["rows"][1] == [100, 200, 0, 140, 90]
    assert found["best_power_w"]["1200"] == 200
    assert sum(found["zone_seconds"]["power"]) == 20_000
    assert found["zone_seconds"]["heart_rate"][2] == 20_000
    short = call("get_ride", ride_id=ride(repo, -1))
    assert short["samples"]["seconds_per_row"] == 30 and len(short["samples"]["rows"]) == 20
    assert "samples" not in call("get_ride", ride_id=long, samples=False)


def test_progress(call, repo):
    ride(repo, -80, seconds=1800)
    ride(repo, -1, seconds=1800, power=240)
    found = call("get_progress", weeks=3)
    assert [w["week"] for w in found["weeks"]] == ["2026-03-02", "2026-03-09", "2026-03-16"]
    assert [w["rides"] for w in found["weeks"]] == [0, 0, 1]
    assert found["today"]["fitness"] > 1 and found["today"]["form"] < 0
    assert found["weeks"][-1]["fitness"] == found["today"]["fitness"]
    assert found["best_power_90_days"]["1200"]["w"] == 240
    assert found["estimated_ftp_w_90_days"] == 228
    assert found["history_newest_first"]["ftp_w"] == [["2026-02-16", 250]]
    assert len(call("get_progress", weeks=104)["weeks"]) == 13  # since the first ride


def test_calendar_and_check_progress(call, repo, plans):
    plan = call("create_plan", name="Base", start_day=day(-30), end_day=day(30))["plan_id"]
    ids = [
        u["unit_id"]
        for u in add(
            call,
            unit(-9, kind="outdoor", target_s=600),
            unit(-8, kind="outdoor", target_s=1200),
            unit(-7, kind="outdoor", target_s=600, target_load=20.5),
            unit(-6, kind="rest"),
            unit(-5, kind="outdoor", target_s=600),
            unit(0, kind="indoor", segments=SEGMENTS),
            plan_id=plan,
        )["units"]
    ]
    plans.link(ids[0], ride(repo, -9))
    plans.link(ids[1], ride(repo, -8))
    call("update_unit", unit_id=ids[4], skipped=True)
    extra = ride(repo, -2)
    found = call("get_calendar", **{"from": day(-10), "to": day(5)})
    assert [u["status"] for u in found["units"]] == [
        "done",
        "partial",
        "missed",
        "planned",
        "skipped",
        "planned",
    ]
    assert found["units"][0]["compliance_percent"] == 100
    assert found["units"][1]["ride"]["seconds"] == 600
    assert found["units"][-1]["weekday"] == "Wed" and found["units"][-1]["load"] >= 50
    assert [x["ride_id"] for x in found["unplanned_rides"]] == [extra]
    assert "unplanned_rides" not in call(
        "get_calendar", **{"from": day(-10), "to": day(5), "plan_id": plan}
    )
    linked = call("get_ride", ride_id=found["units"][0]["ride"]["ride_id"])
    assert linked["unit"]["id"] == ids[0] and linked["unit"]["status"] == "done"

    found = call("check_progress")
    assert [w["week"] for w in found["weeks"]][-2:] == ["2026-03-09", "2026-03-16"]
    week = found["weeks"][-2]
    assert week["units"] == {"total": 4, "done": 1, "partial": 1, "missed": 1, "skipped": 1}
    assert week["planned"]["seconds"] == 2400 and week["done"]["seconds"] == 1200
    assert week["compliance_percent_of_due"] == 50
    assert [m["id"] for m in week["missed_units"]] == [ids[2]]
    assert found["weeks"][-1]["unplanned"]["rides"] == 1
    assert len(call("check_progress", plan_id=plan)["weeks"]) == 9
    assert call("list_plans")["plans"][0]["units"] == 6


def test_manual_link_to_another_day(call, repo, plans):
    uid = add(call, unit(-1, kind="outdoor", target_s=600))["units"][0]["unit_id"]
    plans.link(uid, ride(repo, -20))
    found = call("get_calendar", **{"from": day(-2), "to": day()})
    assert found["units"][0]["status"] == "done" and found["unplanned_rides"] == []


def test_date_ranges_are_limited(call, refused):
    assert "371" in refused("get_calendar", {"from": "2026-01-01", "to": "2028-01-01"})
    assert "before" in refused("list_rides", {"from": day(), "to": day(-1)})
    plan = call("create_plan", name="Long", start_day=day(), end_day=day(800))["plan_id"]
    assert "from and to" in refused("check_progress", {"plan_id": plan})
    call("check_progress", plan_id=plan, **{"from": day(), "to": day(7)})


def test_results_are_capped(tools, call, repo, plans):
    text = "ü" * 4000
    units = [PlanUnit(str(i), TODAY, "note", "ä" * 120, notes=text, position=i) for i in range(100)]
    plan = [Plan(str(i), "P" * 120, TODAY, TODAY, text, text) for i in range(60)]
    plans.apply("user", "x" * 5000, [*units, *plan])
    for i in range(120):
        user_workout(repo, f"{i} {text}", [Steady(60, 0.5)] * 3)
        ride(repo, -i, seconds=40, title=text)
    for name, args, key in (
        ("get_calendar", {"from": day(-300), "to": day(), "limit": 100}, "units"),
        ("list_plans", {"limit": 100}, "plans"),
        ("list_workouts", {"limit": 100}, "workouts"),
        ("list_rides", {"limit": 100}, "rides"),
    ):
        seen, offset = set(), 0
        while offset is not None:
            text = tools.call(name, args | {"offset": offset})
            assert len(text.encode()) <= MAX_RESULT
            found = json.loads(text)
            seen |= {x["id"] for x in found[key]}
            offset = found.get("next_offset")
        assert len(seen) == found["total"] >= 60


def test_list_and_get_workout(call, repo):
    mine = user_workout(repo, "Tempo", [Ramp(60, 0.4, 0.6), Repeat(3, [Sprint(10)])])
    made = call("create_workout", name="AI", segments=SEGMENTS)
    found = call("list_workouts")["workouts"]
    assert [(w["name"], w["editable"]) for w in found] == [("AI", True), ("Tempo", False)]
    assert [w["name"] for w in call("list_workouts", search="TEM")["workouts"]] == ["Tempo"]
    full = call("get_workout", workout_id=str(mine.id))
    assert full["segments"][1]["children"] == [{"type": "sprint", "duration_s": 10}]
    assert full["load_without_sprints"] is True
    assert call("get_workout", workout_id=made["workout_id"])["seconds"] == 600 + 1920 + 10


# --- writing ---


def test_plan_lifecycle(tools, call, plans, refused):
    made = call("create_plan", name=" Base\nplan ", start_day=day(), end_day=day(27), goal="FTP")
    assert tools.changed.wrote and not tools.changed.workouts
    plan = plans.get_plan(made["plan_id"])
    assert (plan.name, plan.goal, plan.origin) == ("Base plan", "FTP", "ai")
    call("update_plan", plan_id=plan.id, goal=None, name="New")
    plan = plans.get_plan(plan.id)
    assert (plan.name, plan.goal, plan.end_day) == ("New", "", TODAY + timedelta(days=27))
    assert "end before" in refused("update_plan", {"plan_id": plan.id, "end_day": day(-1)})
    assert "Nothing" in refused("update_plan", {"plan_id": plan.id})
    assert refused("update_plan", {"plan_id": plan.id, "name": None})
    call("archive_plan", plan_id=plan.id)
    assert plans.get_plan(plan.id).archived
    assert call("list_plans")["plans"] == []
    assert call("list_plans", archived=True)["plans"][0]["archived"] is True
    assert "archived" in refused("add_units", {"plan_id": plan.id, "units": [unit()]})
    call("archive_plan", plan_id=plan.id, archived=False)
    assert not plans.get_plan(plan.id).archived
    tools.call("list_plans", {})
    assert not tools.changed


def test_add_units(tools, call, repo, plans):
    mine = user_workout(repo)
    made = add(
        call,
        unit(title="A", workout_id=str(mine.id)),
        unit(title="B", segments=SEGMENTS),
        unit(1, title="B", segments=SEGMENTS),
        unit(1, kind="rest"),
        unit(1, kind="outdoor", target_s=3600.0, target_load=50, notes="easy"),
    )
    assert made["workouts_created"] == 1 and tools.changed.workouts
    units = [plans.get_unit(u["unit_id"]) for u in made["units"]]
    assert [u.position for u in units] == [0, 1, 0, 1, 2]
    assert units[0].workout == mine and units[1].workout == units[2].workout
    assert units[1].workout.name == "B" and units[4].target_s == 3600
    assert all(u.origin == "ai" for u in units)
    assert plans.workout_origin(units[1].workout.id) == "ai"
    call("undo_change", change_id=made["change_id"])
    assert tools.changed.workouts
    assert plans.units_between(TODAY, TODAY + timedelta(days=9)) == []
    assert repo.list_workouts() == [mine]


def test_add_units_is_all_or_nothing(call, repo, plans, refused):
    bad = [unit(), unit(segments=SEGMENTS), unit(kind="rest", segments=SEGMENTS)]
    assert "units[2]" in refused("add_units", {"units": bad})
    assert "units[1]" in refused("add_units", {"units": [unit(), unit(workout_id=str(uuid4()))]})
    assert refused("add_units", {"units": [unit()] * 101})
    assert refused("add_units", {"units": []})
    both = unit(workout_id=str(user_workout(repo).id), segments=SEGMENTS)
    assert "not both" in refused("add_units", {"units": [both]})
    plan = call("create_plan", name="P", start_day=day(), end_day=day(1))["plan_id"]
    outside = {"plan_id": plan, "units": [unit(), unit(2)]}
    assert "units[1]" in refused("add_units", outside)
    assert "no such plan" in refused("add_units", {"plan_id": "nope", "units": [unit()]})
    assert plans.units_between(TODAY, TODAY + timedelta(days=9)) == []
    assert len(repo.list_workouts()) == 1
    assert len(add(call, *[unit(i) for i in range(100)])["units"]) == 100


def test_update_unit(call, repo, plans, refused):
    mine = user_workout(repo)
    plan = call("create_plan", name="P", start_day=day(), end_day=day(7))["plan_id"]
    uid = add(call, unit(notes="n", workout_id=str(mine.id)), plan_id=plan)["units"][0]["unit_id"]
    call("update_unit", unit_id=uid, title="New")
    found = plans.get_unit(uid)
    assert (found.title, found.notes, found.workout, found.plan_id) == ("New", "n", mine, plan)
    call("update_unit", unit_id=uid, notes=None, plan_id=None, workout_id=None, target_s=900)
    found = plans.get_unit(uid)
    assert (found.notes, found.workout, found.plan_id, found.target_s) == ("", None, None, 900)
    made = call("update_unit", unit_id=uid, segments=SEGMENTS)
    assert str(plans.get_unit(uid).workout.id) == made["workout_id"]
    call("update_unit", unit_id=uid, kind="outdoor")
    assert plans.get_unit(uid).workout is None
    assert "indoor" in refused("update_unit", {"unit_id": uid, "workout_id": str(mine.id)})
    assert "Nothing" in refused("update_unit", {"unit_id": uid, "title": "New"})
    assert "no such unit" in refused("update_unit", {"unit_id": "x", "title": "New"})
    assert refused("update_unit", {"unit_id": uid, "kind": None})
    assert refused("update_unit", {"unit_id": uid, "day": day()})
    far = call("create_plan", name="F", start_day=day(50), end_day=day(60))["plan_id"]
    assert "outside" in refused("update_unit", {"unit_id": uid, "plan_id": far})


def test_units_of_the_user_can_be_changed(call, plans):
    plans.apply("user", "x", [PlanUnit("u", TODAY, "note", "Mine")])
    call("update_unit", unit_id="u", title="AI")
    assert plans.get_unit("u").origin == "user"
    call("delete_unit", unit_id="u")
    assert plans.get_unit("u") is None


def test_move_unit(call, plans, refused):
    plan = call("create_plan", name="P", start_day=day(), end_day=day(7))["plan_id"]
    uid = add(call, unit(), plan_id=plan)["units"][0]["unit_id"]
    add(call, unit(1))
    call("move_unit", unit_id=uid, day=day(1))
    assert (plans.get_unit(uid).day.isoformat(), plans.get_unit(uid).position) == (day(1), 1)
    call("move_unit", unit_id=uid, day=day(1), position=0)
    assert plans.get_unit(uid).position == 0
    assert "Nothing" in refused("move_unit", {"unit_id": uid, "day": day(1)})
    assert "update_plan" in refused("move_unit", {"unit_id": uid, "day": day(8)})


def test_ridden_units_are_protected(call, repo, plans, refused):
    made = add(call, unit(-1, kind="outdoor", target_s=600), unit(3))
    uid = made["units"][0]["unit_id"]
    moved = call("move_unit", unit_id=uid, day=day(-2))
    renamed = call("update_unit", unit_id=uid, title="Old")
    plans.link(uid, ride(repo, -2))
    assert "ridden" in refused("move_unit", {"unit_id": uid, "day": day()})
    assert "ridden" in refused("delete_unit", {"unit_id": uid})
    for change in ({"target_s": 60}, {"kind": "note"}, {"skipped": True}, {"segments": SEGMENTS}):
        assert "ridden" in refused("update_unit", {"unit_id": uid} | change)
    call("undo_change", change_id=renamed["change_id"])
    for change in (moved, made):
        assert "ridden" in refused("undo_change", {"change_id": change["change_id"]})
    call("update_unit", unit_id=uid, notes="well done")
    assert plans.get_unit(uid).notes == "well done" and uid in plans.links()


def test_workouts_of_the_user_are_protected(call, repo, plans, refused):
    mine = user_workout(repo)
    wid = str(mine.id)
    assert "copy_workout" in refused("update_workout", {"workout_id": wid, "name": "X"})
    assert "copy_workout" in refused("delete_workout", {"workout_id": wid})
    assert repo.get_workout(mine.id) == mine
    made = call("copy_workout", workout_id=wid)
    assert repo.get_workout(mine.id) == mine and made["workout_id"] != wid
    call("update_workout", workout_id=made["workout_id"], name="X", segments=SEGMENTS)
    names = {w.name for w in repo.list_workouts()}
    assert names == {"Mine", "X"}
    # edited by the rider: theirs from now on
    copy = next(w for w in repo.list_workouts() if w.name == "X")
    repo.save_workout(copy)
    assert refused("delete_workout", {"workout_id": made["workout_id"]})


def test_workout_lifecycle(tools, call, repo, plans, refused):
    made = call("create_workout", name=" W ", segments=SEGMENTS, description="d")
    wid = made["workout_id"]
    assert tools.changed.workouts and made["seconds"] == 2530 and made["load"] > 0
    uid = add(call, unit(workout_id=wid))["units"][0]["unit_id"]
    call("update_workout", workout_id=wid, description=None, name="W2")
    found = plans.get_unit(uid).workout
    assert (found.name, found.description, len(found.segments)) == ("W2", "", 3)
    assert "Nothing" in refused("update_workout", {"workout_id": wid, "name": "W2"})
    copy = call("copy_workout", workout_id=wid, name="C", segments=SEGMENTS[:1])
    assert copy["seconds"] == 600
    deleted = call("delete_workout", workout_id=wid)
    assert "no such workout" in refused("get_workout", {"workout_id": wid})
    row = call("get_calendar", **{"from": day(), "to": day()})["units"][0]
    assert row["workout_deleted"] is True and row["workout_name"] == "W2"
    call("undo_change", change_id=deleted["change_id"])
    assert call("get_workout", workout_id=wid)["editable"]


def test_undo_rules(call, plans, refused):
    plans.apply("user", "x", [PlanUnit("u", TODAY, "note", "Mine")])
    batch = plans.changes()[0].batch
    assert "no such change" in refused("undo_change", {"change_id": batch})
    assert refused("undo_change", {"change_id": "nope"})
    plan = call("create_plan", name="P", start_day=day(), end_day=day(7))
    added = add(call, unit(), plan_id=plan["plan_id"])
    assert "units" in refused("undo_change", {"change_id": plan["change_id"]})
    call("undo_change", change_id=added["change_id"])
    call("undo_change", change_id=plan["change_id"])
    assert "already" in refused("undo_change", {"change_id": plan["change_id"]})
    found = call("list_changes")["changes"]
    assert [(c["actor"], c["undone"]) for c in found] == [
        ("ai", True),
        ("ai", True),
        ("user", False),
    ]
    moved = call("update_unit", unit_id="u", title="AI")
    plans.apply("user", "y", [PlanUnit("u", TODAY, "note", "again")])
    assert "since" in refused("undo_change", {"change_id": moved["change_id"]})


# --- bad input ---


@pytest.mark.parametrize(
    "segments",
    [
        [{"type": "steady", "duration_s": 60}],
        [{"type": "steady", "duration_s": 60, "power": 200}],
        [{"type": "steady", "duration_s": 60, "power": float("nan")}],
        [{"type": "steady", "duration_s": 60, "power": float("inf")}],
        [{"type": "steady", "duration_s": 60, "power": True}],
        [{"type": "steady", "duration_s": 60, "power": "0.5"}],
        [{"type": "steady", "duration_s": 60, "power": -0.1}],
        [{"type": "steady", "duration_s": 0, "power": 0.5}],
        [{"type": "steady", "duration_s": 1.5, "power": 0.5}],
        [{"type": "steady", "duration_s": 10**400, "power": 0.5}],
        [{"type": "steady", "duration_s": 60, "power": 10**400}],
        [{"type": "steady", "duration_s": 60, "power": 0.5, "start": 0.5}],
        [{"type": "steady", "duration_s": 60, "power": 0.5, "x": 1}],
        [{"type": "steady", "duration_s": 60, "power": 0.5, "cadence": 0}],
        [{"type": "ramp", "duration_s": 60, "start": 0.5}],
        [{"type": "ramp", "duration_s": 60, "start": 0.5, "end": 2.6}],
        [{"type": "sprint", "duration_s": 60, "power": 1}],
        [{"type": "nope", "duration_s": 60}],
        [{"type": ["steady"], "duration_s": 60}],
        [{"duration_s": 60}],
        [{"type": "repeat", "count": 2, "children": []}],
        [{"type": "repeat", "count": 2}],
        [{"type": "repeat", "count": 2, "children": [{"type": "repeat", "duration_s": 1}]}],
        [{"type": "repeat", "count": 10**9, "children": [{"type": "sprint", "duration_s": 1}]}],
        [{"type": "repeat", "count": 400, "children": [{"type": "sprint", "duration_s": 1}] * 2}],
        [{"type": "repeat", "count": 2, "children": [{"type": "steady", "duration_s": 9}]}],
        [{"type": "sprint", "duration_s": 20_000}] * 2,
        [{"type": "sprint", "duration_s": 1}] * 201,
        [],
        [None],
        [[]],
        "steady",
        None,
        {},
    ],
)
def test_bad_segments(refused, segments):
    assert "segments" in refused("create_workout", {"name": "W", "segments": segments})
    assert "segments" in refused("add_units", {"units": [unit(segments=segments)]}) or (
        segments is None
    )


@pytest.mark.parametrize("name", ["", "  \n", "x" * 121, "a\x1b[2J", "\ud800", 5, None, [], {}])
def test_bad_names(refused, name):
    assert "name" in refused("create_workout", {"name": name, "segments": SEGMENTS})
    assert "title" in refused("add_units", {"units": [unit(title=name)]})


@pytest.mark.parametrize(
    "value",
    ["2026-02-30", "20260302", "2026-W10-1", "1999-12-31", "9999-12-31", "", "x" * 9000, 5, None]
    + ["２０２６-03-02", "2026-03-02T00:00", {}, [], float("nan")],
)
def test_bad_days(refused, value):
    assert "day" in refused("add_units", {"units": [unit() | {"day": value}]})
    assert "from" in refused("list_rides", {"from": value})


def test_bad_calls(tools, refused):
    for name in ("nope", "", "call", "__init__", "apply", 5, None, [], {}):
        assert "unknown tool" in refused(name, {})
    for arguments in ("x", [], 5, True, [{}]):
        assert "object" in refused("get_profile", arguments)
    assert "allowed: none" in refused("get_profile", {"key": "trainer_addr"})
    assert "required" in refused("get_ride", {})
    for ride_id in (0, -1, 10**30, 1.5, float("inf"), "1", True, None, 999):
        assert refused("get_ride", {"ride_id": ride_id})
    for limit in (0, 101, 10**400, "5", False):
        assert refused("list_rides", {"limit": limit})
    for uid in ("nope", "", "x" * 65, 5, "{}", "'; DROP TABLE workouts; --"):
        for name in ("get_workout", "delete_workout", "copy_workout", "update_workout"):
            assert refused(name, {"workout_id": uid})
        assert refused("delete_unit", {"unit_id": uid})
        assert refused("archive_plan", {"plan_id": uid})
        assert refused("undo_change", {"change_id": uid})
    assert len(refused("get_profile", {"x" * 100_000: 1})) < 200


def _random_value(rng, depth=0):
    choices = [
        lambda: None,
        lambda: rng.choice([True, False]),
        lambda: rng.choice([0, -1, 1, 10**30, 2**63, 60]),
        lambda: rng.choice([0.5, float("nan"), float("inf"), -1e308, 1.0]),
        lambda: rng.choice(["", "x", "\ud800", "\x00", "2026-03-18", "indoor", "u", "x" * 5000]),
        lambda: rng.choice(["steady", "repeat"]),
    ]
    if depth < 3:
        keys = ["type", "day", "kind", "title", "segments", "duration_s", "power", "children", "x"]
        choices += [
            lambda: [_random_value(rng, depth + 1) for _ in range(rng.randrange(3))],
            lambda: {k: _random_value(rng, depth + 1) for k in rng.sample(keys, 3)},
        ]
    return rng.choice(choices)()


def _sample_value(rng, schema, plausible):
    """Mostly what the schema asks for."""
    if rng.random() < 0.1:
        return _random_value(rng)
    kind = schema["type"] if isinstance(schema["type"], str) else rng.choice(schema["type"])
    if kind == "object":
        picked = [k for k in schema["properties"] if k in schema["required"] or rng.random() < 0.3]
        return {
            k: plausible[k]
            if k in plausible and rng.random() < 0.7
            else _sample_value(rng, schema["properties"][k], plausible)
            for k in picked
        }
    if kind == "array":
        items = [_sample_value(rng, schema["items"], plausible) for _ in range(rng.randrange(1, 4))]
        return SEGMENTS if rng.random() < 0.5 and schema["maxItems"] == 200 else items
    if kind in ("integer", "number"):
        return rng.choice([schema["minimum"], schema["maximum"], 1])
    if kind == "boolean":
        return rng.choice([True, False])
    if kind == "null":
        return None
    if "enum" in schema:
        return rng.choice(schema["enum"])
    return day(rng.randrange(-5, 20)) if "format" in schema else rng.choice(["a", "b", "c"])


def test_random_arguments_never_crash(tools, repo, plans):
    mine = user_workout(repo)
    plans.apply("user", "x", [Plan("p", "P", TODAY - timedelta(9), TODAY + timedelta(30))])
    plans.apply("user", "x", [PlanUnit("u", TODAY, "note", "Mine")])
    plans.link("u", ride(repo, seconds=40))
    rng = random.Random(1)
    ok = dict.fromkeys((d["name"] for d in tools.definitions()), 0)
    made = [str(mine.id)]
    for _ in range(2500):
        definition = rng.choice(tools.definitions())
        plausible = {
            "workout_id": rng.choice(made),
            "plan_id": "p",
            "unit_id": "u",
            "ride_id": 1,
            "change_id": rng.choice(plans.changes(20)).batch,
            "kind": rng.choice(["indoor", "rest"]),
        }
        arguments = _sample_value(rng, definition["inputSchema"], plausible)
        try:
            text = tools.call(definition["name"], arguments)
        except ToolError as e:
            assert len(str(e)) < 600 and str(e).strip()
            assert "stored data" not in str(e)
            continue
        ok[definition["name"]] += 1
        found = json.loads(text)
        if "workout_id" in found:
            made.append(found["workout_id"])
        assert len(text.encode()) <= MAX_RESULT
        assert not any(json.dumps(s)[1:-1] in text for s in SECRETS)
    assert repo.get_workout(mine.id) == mine
    assert "u" in plans.links()
    assert all(n for name, n in ok.items() if name not in ("delete_unit", "move_unit")), ok


# --- stored data ---


def test_no_secrets_in_any_result(tools, call, repo):
    ride(repo)
    made = call("create_workout", name="W", segments=SEGMENTS)
    texts = [json.dumps(tools.definitions())]
    for name, args in (
        ("get_profile", {}),
        ("list_rides", {}),
        ("get_ride", {"ride_id": 1}),
        ("get_progress", {}),
        ("list_workouts", {}),
        ("get_workout", {"workout_id": made["workout_id"]}),
        ("list_plans", {}),
        ("get_calendar", {"from": day(), "to": day()}),
        ("check_progress", {}),
        ("list_changes", {}),
    ):
        texts.append(tools.call(name, args))
    for text in texts:
        assert not any(json.dumps(secret)[1:-1] in text for secret in SECRETS)
        assert "strava" not in text.lower() and "trainer_" not in text


def test_corrupt_rows(tools, conn, repo, plans):
    ride(repo)
    huge = Workout("Huge", [Repeat(10**9, [Steady(60, 0.8)])])
    wild = Workout("Wild \x1b \x00", [Steady(60, 1e300)], description="\ud800")
    endless = Workout("Endless", [Steady(10**300, 0.5)])
    for workout in (huge, wild, endless):
        conn.execute(
            "INSERT INTO workouts (id, name, json, updated_at) VALUES (?, 'n', ?, 'x')",
            (str(workout.id), to_json(workout)),
        )
    conn.execute("INSERT INTO workouts (id, name, json, updated_at) VALUES ('x', 'n', '{', 'x')")
    for i, workout in enumerate((huge, wild, endless)):
        conn.execute(
            "INSERT INTO plan_units (id, day, position, kind, title, notes, workout_id, "
            "workout_json, skipped, origin) VALUES (?, ?, ?, 'indoor', 't', '', ?, ?, 0, 'ai')",
            (f"u{i}", day(), i, str(workout.id), to_json(workout)),
        )
    conn.execute(
        "INSERT INTO plan_units (id, day, position, kind, title, notes, skipped, origin) "
        "VALUES ('bad', 'no day', 0, 'rest', 't', '', 0, 'ai')"
    )
    conn.execute(
        "INSERT INTO plans VALUES ('p', 'n', 'x', 'y', '', '', 0, 'ai')",
    )
    conn.execute("INSERT INTO unit_links VALUES ('u2', 1, 'manual')")
    conn.execute(
        "UPDATE sessions SET np = 9e999, load = 9e999, ftp_w = 1, source = ?, kj = 'abc', "
        "workout_snapshot_json = 'x'",
        ("s" * 999,),
    )
    conn.execute("UPDATE samples SET t = 9e999, power = 'x' WHERE rowid < 3")
    conn.commit()
    started = time.perf_counter()
    for name, args in (
        ("list_workouts", {}),
        ("get_workout", {"workout_id": str(wild.id)}),
        ("get_workout", {"workout_id": str(huge.id)}),
        ("get_calendar", {"from": day(), "to": day()}),
        ("list_rides", {}),
        ("get_ride", {"ride_id": 1}),
        ("get_progress", {}),
        ("check_progress", {}),
        ("list_plans", {}),
        ("delete_unit", {"unit_id": "u0"}),
    ):
        text = tools.call(name, args)
        json.loads(text)
        assert "Infinity" not in text and "\x1b" not in text and "\\u001b" not in text
    for workout in (huge, wild, endless):
        with pytest.raises(ToolError):
            tools.call("copy_workout", {"workout_id": str(workout.id)})
    assert time.perf_counter() - started < 5
    conn.execute("UPDATE sessions SET started_at_utc = 'never'")
    with pytest.raises(ToolError, match="stored data"):
        tools.call("list_rides", {})


def test_failure_after_validation_changes_nothing(tools, plans, repo, monkeypatch):
    real = PlanRepo._put

    def put(self, table, row_id, row):
        if table == "plan_units":
            raise ValueError("boom")
        real(self, table, row_id, row)

    monkeypatch.setattr(PlanRepo, "_put", put)
    with pytest.raises(ToolError):
        tools.call("add_units", {"units": [unit(segments=SEGMENTS)]})
    assert repo.list_workouts() == [] and plans.changes() == [] and not tools.changed
