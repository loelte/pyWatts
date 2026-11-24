from datetime import date, timedelta

import pytest

from pywatts.core.plan import (
    Plan,
    PlanUnit,
    Ride,
    Status,
    compliance,
    daily_loads,
    match,
    planned_load,
    status,
    week_start,
)
from pywatts.core.workout import Ramp, Repeat, Sprint, Steady, Workout

DAY = date(2026, 3, 4)
HOUR = Workout("W", [Steady(3600, 1.0)])


def unit(uid="u", day=DAY, kind="indoor", **kw):
    return PlanUnit(uid, day, kind, "T", **kw)


def test_planned_load():
    assert planned_load(HOUR) == pytest.approx(100)
    assert planned_load(Workout("e")) == 0
    assert planned_load(Workout("s", [Sprint(60)])) == 0
    mixed = Workout("m", [Ramp(600, 0.5, 1.0), Repeat(4, [Steady(60, 1.2), Steady(60, 0.5)])])
    assert 0 < planned_load(mixed) < 100
    assert planned_load(Workout("long", [Steady(10**7, 1.0)])) == pytest.approx(10**7 / 36)


@pytest.mark.parametrize(
    "kw",
    [
        {"kind": "swim"},
        {"target_s": 0},
        {"target_s": True},
        {"target_load": float("nan")},
        {"target_load": 1e9},
        {"kind": "outdoor", "workout": HOUR},
        {"notes": "x" * 5000},
    ],
)
def test_invalid_unit(kw):
    with pytest.raises(ValueError):
        unit(**kw)


def test_invalid_plan():
    with pytest.raises(ValueError):
        Plan("p", "n", DAY, DAY - timedelta(days=1))
    with pytest.raises(ValueError):
        Plan("p", " ", DAY, DAY)


def test_status():
    later = DAY + timedelta(days=1)
    u = unit(target_s=3600)
    assert status(u, None, DAY) is Status.PLANNED  # not missed before the day is over
    assert status(u, None, later) is Status.MISSED
    assert status(u, Ride(1, DAY, 3600, None), later) is Status.DONE
    assert status(u, Ride(1, DAY, 600, None), DAY) is Status.PARTIAL
    assert status(unit(), Ride(1, DAY, 600, None), DAY) is Status.DONE  # no target
    skipped = unit(skipped=True)
    assert status(skipped, None, later) is Status.SKIPPED
    assert status(skipped, Ride(1, DAY, 600, None), later) is Status.DONE
    for kind in ("rest", "note"):
        assert status(unit(kind=kind), None, later) is Status.PLANNED


def test_compliance_falls_back_to_load():
    assert compliance(unit(target_load=50), Ride(1, DAY, 600, 25)) == 0.5
    assert compliance(unit(), Ride(1, DAY, 600, 25)) is None


def test_match():
    a, b = unit("a", target_s=3600), unit("b", target_s=1200)
    long, short = Ride(1, DAY, 3500, None), Ride(2, DAY, 1300, None)
    assert sorted(match([a, b], [long, short])) == [("a", 1), ("b", 2)]
    assert match([a], [long], [("a", 1)]) == []
    assert match([a], [Ride(1, DAY, 1800, None)]) == []
    assert match([a], [Ride(1, DAY + timedelta(days=1), 3600, None)]) == []
    assert match([unit("r", kind="rest", target_s=3600)], [long]) == []
    assert match([unit("s", skipped=True, target_s=3600)], [long]) == []
    assert match([unit("n")], [long]) == []


def test_match_ties_link_nothing():
    a, b = unit("a", target_s=3600), unit("b", target_s=3600)
    assert match([a, b], [Ride(1, DAY, 3600, None)]) == []
    assert match([a], [Ride(1, DAY, 3600, None), Ride(2, DAY, 3600, None)]) == []


def test_match_by_load():
    assert match([unit("a", kind="outdoor", target_load=50)], [Ride(1, DAY, 9, 55)]) == [("a", 1)]


def test_days():
    assert week_start(date(2026, 3, 8)) == date(2026, 3, 2)
    rides = [Ride(1, DAY, 1, 10), Ride(2, DAY, 1, 5), Ride(3, DAY, 1, None)]
    assert daily_loads(rides, DAY - timedelta(days=1), DAY + timedelta(days=1)) == [0, 15, 0]
