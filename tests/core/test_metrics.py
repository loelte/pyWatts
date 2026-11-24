import pytest

from pywatts.core.metrics import average, kilojoules, normalized_power


def test_empty():
    assert average([]) == normalized_power([]) == kilojoules([]) == 0.0


def test_basics():
    assert average([100, 200]) == 150
    assert kilojoules([200] * 3600) == pytest.approx(720)
    assert kilojoules([200, 200], 0.5) == pytest.approx(0.2)


def test_normalized_power():
    assert normalized_power([100, 200]) == 150
    assert normalized_power([200] * 100) == pytest.approx(200)
    values = [100] * 60 + [300] * 60
    rolling = [sum(values[i : i + 30]) / 30 for i in range(len(values) - 29)]
    expected = (sum(v**4 for v in rolling) / len(rolling)) ** 0.25
    assert normalized_power(values) == pytest.approx(expected)
