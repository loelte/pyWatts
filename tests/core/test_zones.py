import pytest

from pywatts.core.metrics import PEAK_DURATIONS, peak_powers, training_load
from pywatts.core.workout import Block
from pywatts.core.zones import (
    Zones,
    format_limits,
    hr_zones,
    parse_limits,
    power_zones,
    split_by_zone,
)


def test_power_zone_limits():
    zones = power_zones(200)
    assert zones.limits == (110, 150, 180, 210)
    cases = {0: 1, 110: 1, 110.4: 1, 110.6: 2, 150: 2, 151: 3, 180: 3, 181: 4, 210: 4, 211: 5}
    assert {w: zones.zone_of(w) for w in cases} == cases
    assert zones.ranges() == [(0, 110), (111, 150), (151, 180), (181, 210), (211, None)]


def test_hr_zones():
    assert hr_zones(200).limits == (120, 140, 160, 180)
    assert hr_zones(190, 50).limits == (134, 148, 162, 176)


@pytest.mark.parametrize("limits", [(1, 2, 3), (1, 2, 2, 3), (0, 1, 2, 3), (4, 3, 2, 1)])
def test_invalid(limits):
    with pytest.raises(ValueError):
        Zones(limits)


def test_text_roundtrip():
    assert parse_limits(format_limits(power_zones(250))) == power_zones(250)
    for junk in ("", "a,b,c,d", "1,2,3", "4,3,2,1", "1.5,2,3,4"):
        assert parse_limits(junk) is None


@pytest.mark.parametrize("start,end", [(0.4, 1.2), (1.2, 0.4)])
def test_ramp_is_split_at_every_limit(start, end):
    zones = power_zones(200)
    pieces = split_by_zone(Block(10, 110, start, end), 200, zones)
    order = [z for _, z in pieces]
    assert order == ([1, 2, 3, 4, 5] if start < end else [5, 4, 3, 2, 1])
    blocks = [b for b, _ in pieces]
    assert blocks[0].t0 == 10 and blocks[-1].t1 == pytest.approx(110)
    for a, b in zip(blocks, blocks[1:], strict=False):
        assert a.t1 == b.t0 and a.p_end == b.p_start


def test_steady_and_small_ramp_stay_whole():
    zones = power_zones(200)
    for block in (Block(0, 5, 1.1, 1.1), Block(0, 5, 0.6, 0.7), Block(0, 5, 0, 0)):
        assert [b for b, _ in split_by_zone(block, 200, zones)] == [block]


def test_peaks():
    power = [100.0] * 60
    power[20:25] = [500.0] * 5
    peaks = peak_powers([power])
    assert peaks[3] == peaks[5] == 500 and peaks[10] == 300
    assert peaks[60] == pytest.approx(100 + 400 * 5 / 60)
    assert max(peaks) == 60 and peak_powers([]) == {}
    assert set(peak_powers([[1.0] * 3600])) == set(PEAK_DURATIONS)


def test_peaks_never_span_a_pause():
    peaks = peak_powers([[200.0] * 4, [300.0] * 4])
    assert peaks == {3: 300}


def test_training_load():
    assert training_load(200, 3600, 200) == 100
    assert training_load(200, 3600, 0) == 0
