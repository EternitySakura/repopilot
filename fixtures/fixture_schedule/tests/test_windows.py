import pytest

from fixture_schedule import merge_windows


def test_adjacent_windows_are_merged():
    assert merge_windows([(3, 5), (1, 3)]) == [(1, 5)]


def test_nested_window_does_not_shorten_coverage():
    assert merge_windows([(1, 8), (2, 4), (5, 7)]) == [(1, 8)]


def test_disjoint_and_empty_windows():
    assert merge_windows([(6, 7), (3, 3), (1, 2)]) == [(1, 2), (6, 7)]
    assert merge_windows([]) == []


def test_reversed_window_is_invalid():
    with pytest.raises(ValueError):
        merge_windows([(3, 1)])
