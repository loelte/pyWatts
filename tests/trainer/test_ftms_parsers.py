import pytest

from pywatts.trainer import ftms


def test_bike_data_typical():
    # speed 30.00, cadence 90, power 150, hr 120
    d = ftms.parse_bike_data(bytes.fromhex("4402 b80b b400 9600 78".replace(" ", "")))
    assert (d.speed_kph, d.cadence_rpm, d.power_w, d.hr_bpm) == (30.0, 90.0, 150, 120)
    assert not d.more_data


def test_bike_data_more_data_has_no_speed():
    d = ftms.parse_bike_data(bytes.fromhex("4100f6ff"))
    assert d.more_data and d.speed_kph is None and d.power_w == -10


def test_bike_data_all_fields():
    raw = bytes.fromhex("fe1f")
    raw += bytes.fromhex("e803 e903 b400 b200 010203 fbff 9600 9700 0a00 6400 02 80 55 3c00 7800")
    d = ftms.parse_bike_data(raw)
    assert d == ftms.BikeData(
        False, 10.0, 10.01, 90.0, 89.0, 0x030201, -5, 150, 151, 10, 100, 2, 128, 8.5, 60, 120
    )
    for n in range(len(raw)):
        part = ftms.parse_bike_data(raw[:n])
        assert part.remaining_s is None
        assert part.speed_kph in (None, 10.0)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0078", 120),
        ("017800", 120),
        ("0178", None),
        ("0478", None),
        ("0678", 120),
        ("0000", None),
        ("00", None),
        ("", None),
    ],
)
def test_hr(raw, expected):
    assert ftms.parse_hr_measurement(bytes.fromhex(raw)) == expected


def test_features():
    f = ftms.parse_features(bytes.fromhex("0244000008000000"))
    assert f.cadence and f.heart_rate and f.power_measurement and f.power_target
    assert ftms.parse_features(b"\x02\x40") is None
    assert not ftms.parse_features(bytes(4)).power_target


def test_status():
    assert ftms.parse_status(b"") is None
    assert ftms.parse_status(b"\xff").control_lost
    assert ftms.parse_status(b"\x08\x96\x00").target_power_w == 150
    assert ftms.parse_status(b"\x08\x96").target_power_w is None


def test_control_response():
    assert ftms.parse_control_response(b"\x80\x05\x01") == ftms.ControlResponse(5, 1)
    assert ftms.parse_control_response(b"\x80\x05") is None
    assert ftms.parse_control_response(b"\x00\x05\x01") is None


def test_power_range():
    assert ftms.parse_power_range(bytes.fromhex("0000e8030100")) == (0, 1000)
    assert ftms.parse_power_range(b"\x00\x00\xe8") is None
    assert ftms.parse_power_range(bytes(6)) is None
