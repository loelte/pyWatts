from pywatts.ui.ride_keys import default_keys, parse_keys, serialize_keys


def test_defaults_and_roundtrip():
    assert parse_keys("") == default_keys()
    keys = default_keys() | {"power_up": "W", "skip": ""}
    text = serialize_keys(keys)
    assert parse_keys(text) == keys
    assert serialize_keys(default_keys()) == ""


def test_unknown_actions_dropped():
    assert parse_keys("bogus=X;power_up=Q")["power_up"] == "Q"
    assert "bogus" not in parse_keys("bogus=X")
