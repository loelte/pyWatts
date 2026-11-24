import json
import math

import pytest

from pywatts.core.workout import (
    Block,
    Ramp,
    Repeat,
    Sprint,
    Steady,
    Workout,
    flatten,
    from_json,
    power_at,
    to_json,
    total_duration,
)

W = Workout(
    "Tést",
    [Ramp(60, 0.4, 0.8), Repeat(2, [Steady(30, 1.2), Ramp(10, 0.5, 0.6)]), Steady(20, 0)],
    "desc",
)


@pytest.mark.parametrize("duration", [0, -1, 1.5, True, "5", None, 10**400], ids=str)
def test_bad_duration(duration):
    with pytest.raises(ValueError):
        Steady(duration, 1.0)
    with pytest.raises(ValueError):
        Ramp(duration, 1.0, 1.0)


@pytest.mark.parametrize("power", [-0.1, math.nan, math.inf, "1", None, True, 10**400], ids=str)
def test_bad_power(power):
    with pytest.raises(ValueError):
        Steady(1, power)
    with pytest.raises(ValueError):
        Ramp(1, power, 1.0)
    with pytest.raises(ValueError):
        Ramp(1, 1.0, power)


@pytest.mark.parametrize(
    "count,children",
    [(0, [Steady(1, 1)]), (1, []), (1, None), (1, [1]), (1, [Repeat(1, [Steady(1, 1)])])],
)
def test_bad_repeat(count, children):
    with pytest.raises(ValueError):
        Repeat(count, children)


def test_flatten():
    blocks = flatten(W)
    assert blocks == [
        Block(0, 60, 0.4, 0.8),
        Block(60, 90, 1.2, 1.2),
        Block(90, 100, 0.5, 0.6),
        Block(100, 130, 1.2, 1.2),
        Block(130, 140, 0.5, 0.6),
        Block(140, 160, 0, 0),
    ]
    assert total_duration(W) == 160
    assert flatten(Workout("x")) == []
    assert total_duration(Workout("x")) == 0


@pytest.mark.parametrize(
    "t,p",
    [(-5, 0.4), (0, 0.4), (30, 0.6), (60, 1.2), (95, 0.55), (139.999, 0.6), (140, 0), (999, 0)],
)
def test_power_at(t, p):
    assert power_at(flatten(W), t) == pytest.approx(p, abs=1e-4)


def test_power_at_ends():
    assert power_at([], 3) == 0.0
    assert power_at([Block(0, 10, 0.5, 1.0)], 10) == 1.0


def test_roundtrip():
    assert from_json(to_json(W)) == W
    assert from_json(to_json(Workout(""))).segments == []
    assert [s["type"] for s in json.loads(to_json(W))["segments"]] == ["ramp", "repeat", "steady"]


@pytest.mark.parametrize(
    "text",
    ["", "{", "null", "[]", "1", '"x"', "{}", "[" * 200_000, b"\xff", None],
    ids=lambda t: repr(t)[:10],
)
def test_malformed_text(text):
    with pytest.raises(ValueError):
        from_json(text)


VALUES = [None, True, 0, -1, 1.5, "x", [], {}, [[]], [None], [{}], [{"type": []}], math.nan]


@pytest.mark.parametrize("value", VALUES, ids=repr)
@pytest.mark.parametrize(
    "path",
    [
        ("version",),
        ("id",),
        ("name",),
        ("description",),
        ("segments",),
        ("segments", 0),
        ("segments", 0, "type"),
        ("segments", 0, "duration_s"),
        ("segments", 0, "start"),
        ("segments", 1, "count"),
        ("segments", 1, "children"),
        ("segments", 1, "children", 0),
        ("segments", 2, "power"),
    ],
    ids=str,
)
def test_malformed_field(path, value):
    data = json.loads(to_json(W))
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    text = json.dumps(data)
    valid = (
        (path[-1] in ("name", "description") and value == "x")
        or (path == ("segments",) and value == [])
        or (path[-1] in ("start", "power") and value in (0, 1.5))
    )
    if valid:
        from_json(text)
    else:
        with pytest.raises(ValueError):
            from_json(text)
    if isinstance(node, dict):
        del node[path[-1]]
        with pytest.raises(ValueError):
            from_json(json.dumps(data))


def test_nested_repeat_rejected():
    data = json.loads(to_json(W))
    data["segments"][1]["children"] = [dict(data["segments"][1])]
    with pytest.raises(ValueError):
        from_json(json.dumps(data))


def test_cadence_target_roundtrip_and_validation():
    import pytest as _pytest

    w = Workout("c", [Steady(60, 0.5, 90), Ramp(60, 0.5, 0.8), Repeat(2, [Steady(30, 1.0, 100)])])
    assert from_json(to_json(w)).segments == w.segments
    assert [b.cadence for b in flatten(w)] == [90, None, 100, 100]
    for bad in (0, 301, 90.5, True):
        with _pytest.raises(ValueError):
            Steady(60, 0.5, bad)


def test_sprint():
    w = Workout("s", [Sprint(15), Repeat(2, [Sprint(5), Steady(10, 0.5, 90)])])
    assert from_json(to_json(w)) == w
    blocks = flatten(w)
    assert [b.open for b in blocks] == [True, True, False, True, False]
    assert blocks[0] == Block(0, 15, 0, 0, None, True)
    assert total_duration(w) == 45
    with pytest.raises(ValueError):
        Sprint(0)
