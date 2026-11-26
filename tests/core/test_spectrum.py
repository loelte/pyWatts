import math

import pytest

from pywatts.core.spectrum import MIN_DAYS, rhythms


def test_long_weekend_ride_is_a_weekly_rhythm():
    week = [0, 0, 50, 0, 0, 150, 0]  # Wednesday short, Saturday long
    peaks = rhythms(week * 26).peaks
    bases = [r for r in peaks if r.harmonic_of is None]
    assert [r.period_days for r in bases] == [pytest.approx(7.0, abs=0.1)]
    echoes = [r for r in peaks if r.harmonic_of is not None]
    assert any(r.period_days == pytest.approx(3.5, abs=0.1) for r in echoes)
    assert all(r.harmonic_of == bases[0].period_days for r in echoes)


def test_every_other_day_echo_may_beat_the_week():
    week = [0, 60, 0, 80, 0, 120, 0]  # Tuesday, Thursday, Saturday
    peaks = rhythms(week * 26).peaks
    assert peaks[0].period_days == pytest.approx(7 / 3, abs=0.05)
    weekly = next(r for r in peaks if r.period_days == pytest.approx(7.0, abs=0.1))
    assert weekly.harmonic_of is None
    assert {r.harmonic_of for r in peaks if r is not weekly} == {weekly.period_days}


def test_sine_gives_its_period_and_amplitude():
    days = 168  # six whole cycles
    values = [50 + 20 * math.sin(2 * math.pi * t / 28) for t in range(days)]
    spectrum = rhythms(values)
    assert spectrum.peaks[0].period_days == pytest.approx(28, rel=0.03)
    assert spectrum.peaks[0].amplitude == pytest.approx(20, rel=0.05)


def test_trend_alone_is_no_rhythm():
    spectrum = rhythms([10 + 0.5 * t for t in range(120)])
    assert spectrum.peaks == []


def test_periods_run_from_two_days_to_half_the_history():
    spectrum = rhythms([float(t % 5) for t in range(100)])
    assert spectrum.periods_days[0] == pytest.approx(2)
    assert spectrum.periods_days[-1] == pytest.approx(50)
    assert len(spectrum.periods_days) == len(spectrum.amplitudes)


def test_too_short():
    with pytest.raises(ValueError):
        rhythms([1.0] * (MIN_DAYS - 1))


def test_side_bands_of_a_block_are_no_rhythm():
    week = [0, 40, 0, 60, 0, 150, 0]
    values = [week[d % 7] * (0.5 if (d // 7) % 4 == 3 else 1.3) for d in range(392)]
    periods = sorted(r.period_days for r in rhythms(values).peaks)
    assert all(b / a > 1.1 for a, b in zip(periods, periods[1:], strict=False))
    assert any(p == pytest.approx(28, rel=0.05) for p in periods)


def test_week_inside_a_block_stays_a_rhythm():
    week = [0, 40, 0, 60, 0, 150, 0]
    values = [week[d % 7] * (0.5 if (d // 7) % 4 == 3 else 1.3) for d in range(392)]
    bases = [r.period_days for r in rhythms(values).peaks if r.harmonic_of is None]
    assert any(p == pytest.approx(7, abs=0.1) for p in bases)
    assert any(p == pytest.approx(28, rel=0.05) for p in bases)
