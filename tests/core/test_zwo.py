import pytest

from pywatts.core import zwo
from pywatts.core.workout import Ramp, Repeat, Sprint, Steady, Workout, flatten

SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<workout_file>
  <name> Über </name><description>d &amp; e</description>
  <workout>
    <!-- c -->
    <Warmup Duration="600" PowerLow="0.25" PowerHigh="0.75" pace="0"><textevent/></Warmup>
    <FreeRide Duration="60"/>
    <SteadyState Duration="60.0" Power="0.9"/>
    <IntervalsT Repeat="3" OnDuration="30" OffDuration="20" OnPower="1.2" OffPower="0.5"/>
    <Ramp Duration="10" PowerLow="0.9" PowerHigh="0.8"/>
    <Cooldown Duration="300" PowerLow="0.75" PowerHigh="0.25"/>
  </workout>
</workout_file>"""
SEGMENTS = [
    Ramp(600, 0.25, 0.75),
    Sprint(60),
    Steady(60, 0.9),
    Repeat(3, [Steady(30, 1.2), Steady(20, 0.5)]),
    Ramp(10, 0.9, 0.8),
    Ramp(300, 0.75, 0.25),
]


def test_loads():
    w = zwo.loads(SAMPLE)
    assert (w.name, w.description, w.segments) == ("Über", "d & e", SEGMENTS)
    assert zwo.loads(SAMPLE).id != w.id
    assert zwo.loads("﻿\n" + SAMPLE).segments == SEGMENTS


def test_max_effort_is_a_sprint():
    text = '<a><workout><MaxEffort Duration="10"/></workout></a>'
    assert zwo.loads(text).segments == [Sprint(10)]


def test_roundtrip():
    w = Workout("a <&> ü", SEGMENTS, "line1\nline2")
    text = zwo.dumps(w)
    back = zwo.loads(text)
    assert (back.name, back.description, back.segments) == (w.name, w.description, SEGMENTS)
    for tag in ("Warmup", "SteadyState", "IntervalsT", "Ramp", "Cooldown"):
        assert f"<{tag} " in text
    assert '<Cooldown Duration="300" PowerLow="0.75" PowerHigh="0.25"' in text


@pytest.mark.parametrize(
    "children",
    [
        [Sprint(5), Steady(5, 1)],
        [Steady(5, 1)],
        [Steady(5, 1), Ramp(5, 1, 2)],
        [Steady(1, 0), Steady(2, 1), Steady(3, 1)],
    ],
)
def test_other_repeats_expand(children):
    w = Workout("x", [Repeat(2, children)])
    back = zwo.loads(zwo.dumps(w))
    assert back.segments == children * 2
    assert flatten(back) == flatten(w)


def test_precision_and_illegal_chars():
    w = Workout("a\x00\x0b\ud800b", [Steady(7, 1 / 3)])
    back = zwo.loads(zwo.dumps(w))
    assert back.name == "ab"
    assert back.segments == w.segments


def test_legacy_attributes():
    w = zwo.loads(
        '<workout_file><workout><SteadyState Duration="5" PowerLow="0.5" PowerHigh="0.5"/>'
        '<IntervalsT Repeat="1" OnDuration="1" OffDuration="1" PowerOnLow="1" PowerOnHigh="1"'
        ' PowerOffLow="0.5" PowerOffHigh="0.5"/></workout></workout_file>'
    )
    assert w.segments == [Steady(5, 0.5), Repeat(1, [Steady(1, 1), Steady(1, 0.5)])]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nope",
        "<a>",
        "<workout_file/>",
        "<!DOCTYPE workout_file><workout_file><workout/></workout_file>",
        '<!DOCTYPE a [<!ENTITY x "y">]><workout_file><name>&x;</name><workout/></workout_file>',
        '<!DOCTYPE a SYSTEM "file:///x"><workout_file><workout/></workout_file>',
    ],
)
def test_invalid(text):
    with pytest.raises(ValueError):
        zwo.loads(text)


@pytest.mark.parametrize(
    "el",
    [
        "<SteadyState/>",
        '<SteadyState Duration="x" Power="1"/>',
        '<SteadyState Duration="0" Power="1"/>',
        '<SteadyState Duration="1e999" Power="1"/>',
        '<SteadyState Duration="nan" Power="1"/>',
        '<SteadyState Duration="5" Power="inf"/>',
        '<Cooldown Duration="5" PowerLow="1"/>',
        '<IntervalsT Repeat="0" OnDuration="1" OffDuration="1" OnPower="1" OffPower="1"/>',
    ],
)
def test_invalid_segment(el):
    with pytest.raises(ValueError):
        zwo.loads(f"<workout_file><workout>{el}</workout></workout_file>")


def test_files(tmp_path):
    path = tmp_path / "w.zwo"
    zwo.save(Workout("ü", SEGMENTS), path)
    assert zwo.load(path).segments == SEGMENTS
    assert zwo.load(path).name == "ü"
    path.write_bytes(SAMPLE.encode("utf-8-sig"))
    assert zwo.load(path).segments == SEGMENTS
    for encoding in ("utf-16", "iso-8859-1"):
        path.write_bytes(SAMPLE.replace("utf-8", encoding).encode(encoding))
        assert zwo.load(path).name == "Über"
    path.write_bytes(SAMPLE.replace("utf-8", "bogus").encode())
    with pytest.raises(ValueError):
        zwo.load(path)
    path.write_bytes(b"\xff\x80")
    with pytest.raises(ValueError):
        zwo.load(path)


def test_cadence_roundtrip():
    from pywatts.core.workout import Ramp, Repeat, Steady, Workout

    w = Workout(
        "c",
        [
            Steady(60, 0.5, 90),
            Ramp(60, 0.5, 0.8, 85),
            Repeat(2, [Steady(30, 1.0, 100), Steady(30, 0.5)]),
        ],
    )
    assert zwo.loads(zwo.dumps(w)).segments == w.segments
