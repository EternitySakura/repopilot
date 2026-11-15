import pytest

from fixture_paging import paginate


def test_full_page_keeps_next_offset():
    assert paginate(["a", "b", "c"], 0, 2) == (["a", "b"], 2)


def test_exact_final_page_has_no_continuation():
    assert paginate(["a", "b", "c"], 1, 2) == (["b", "c"], None)


def test_empty_and_past_end():
    assert paginate([], 0, 2) == ([], None)
    assert paginate(["a"], 4, 2) == ([], None)


def test_invalid_paging_arguments():
    with pytest.raises(ValueError):
        paginate(["a"], -1, 1)
    with pytest.raises(ValueError):
        paginate(["a"], 0, 0)
