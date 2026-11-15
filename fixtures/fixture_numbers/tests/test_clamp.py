import pytest
from fixture_numbers import clamp


def test_inside_and_lower_bound():
    assert clamp(5, 0, 10) == 5
    assert clamp(-5, 0, 10) == 0


def test_upper_bound():
    assert clamp(11, 0, 10) == 10
    assert clamp(3, 2, 2) == 2


def test_invalid_range():
    with pytest.raises(ValueError):
        clamp(1, 5, 2)
