import pytest
from fixture_sequence import chunks


def test_exact_chunks():
    assert chunks([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_empty_input():
    assert chunks([], 2) == []


def test_short_final_chunk():
    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]
    assert chunks([1], 3) == [[1]]


def test_invalid_size():
    for size in (0, -1):
        with pytest.raises(ValueError):
            chunks([1], size)
