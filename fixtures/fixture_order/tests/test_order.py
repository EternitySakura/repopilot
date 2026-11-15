import pytest
from fixture_order import total


def test_single_unit():
    assert total({"a": 3}, {"a": 1}) == 3


def test_empty_order():
    assert total({"a": 3}, {}) == 0


def test_quantities_and_zero():
    assert total({"a": 3, "b": 4}, {"a": 2, "b": 3}) == 18
    assert total({"a": 3}, {"a": 0}) == 0


def test_unknown_item():
    with pytest.raises(KeyError):
        total({}, {"missing": 1})
