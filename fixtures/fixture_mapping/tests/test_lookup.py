from fixture_mapping import lookup


def test_present_value():
    assert lookup({"x": "value"}, "x", "fallback") == "value"


def test_missing_key():
    assert lookup({}, "missing", "fallback") == "fallback"
    assert lookup({}, "missing") is None


def test_falsey_values_are_preserved():
    for value in (0, False, "", None, []):
        assert lookup({"x": value}, "x", "fallback") == value
